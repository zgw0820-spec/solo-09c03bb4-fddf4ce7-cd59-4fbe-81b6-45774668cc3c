#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batchcc —— 接入 GNU Make 4.3 管道 jobserver 的批编译助手。

工作模型（与 GNU make 手册 "POSIX Jobserver Interaction" 一致）
------------------------------------------------------------------
1. Make 4.3 创建管道 jobserver，并把 ``--jobserver-auth=R,W`` 放进
   ``MAKEFLAGS``。但它给管道 fd 设置了 ``O_CLOEXEC``，且只对被判定为
   “递归”的配方行（``+`` 前缀，或命令行含 ``$(MAKE)``）调用
   ``fd_inherit()``。因此调用本助手的 make 配方**必须**写成（见
   sample/Makefile）::

       batch-xxx:
       	+python3 tools/batchcc.py --group xxx -- src1.c src2.c ...

   否则本进程能在 MAKEFLAGS 里看到 fd 号，却会拿到 EBADF。

2. Make 为每条启动的命令预留了 **1 个隐式名额（implicit token）**。
   所以即使管道已空 / jobserver 不可用，本助手也始终可以直接运行
   1 个 GCC，而无需从管道读令牌 —— 这就是“令牌用完时，每批仍能用
   make 已分给它的名额开工”。

3. 想让批内更多文件并行编译时，每多开 1 个 GCC，就从读端非阻塞地
   读取 1 个字节作为“借用令牌”。

4. 某个 GCC 退出并被 ``waitpid`` 回收后，才把**当初读到的同一个字节**
   原样写回写端；编译失败、被信号中断、助手自身异常时也保证归还。
   隐式名额对应的令牌**绝不**由本助手写回（它属于 make）。

5. 任意时刻：运行中的 GCC 数 = 隐式 GCC(≤1) + 已借未还令牌数，而全
   机构的名额总数就是 ``-j``，故整个构建的 GCC 并发数不会超过 ``-j``。

本文件既可作为 CLI 被 Makefile/Flask 调用，也可被测试直接 import。
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import re
import select
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# GNU make 4.3 在管道里放的令牌字节通常是 '+'，但协议要求原样借还，
# 不能假设所有令牌都相同（手册明确警告 different characters）。
_AUTH_RE = re.compile(r"--jobserver-auth=(\d+),(\d+)")
# 兼容更老的 --jobserver-fds=R,W 写法。
_FDS_RE = re.compile(r"--jobserver-fds=(\d+),(\d+)")


class JobserverUnavailable(Exception):
    """MAKEFLAGS 声称有 jobserver，但 fd 不可用（典型：配方漏了 '+'）。"""


class Jobserver:
    """对 GNU make 管道 jobserver 的极简客户端封装。

    读端在 Make 4.3(Linux/HAVE_PSELECT) 下实际是 O_NONBLOCK 的；
    为兼容手册描述的 blocking 版本，读取前先 select(0 超时)，并额外
    捕获 EAGAIN。我们**不**修改 fd 的 O_NONBLOCK —— 该状态属于内核
    “打开文件描述”，与父 make 共享，乱改会影响 make 自身的事件循环。
    """

    def __init__(self, rfd: int, wfd: int, source: str):
        self.r = rfd
        self.w = wfd
        self.source = source
        self.broken = False

    # ---------- 构造 / 解析 ----------
    @staticmethod
    def _fd_ok(fd: int) -> bool:
        try:
            fcntl.fcntl(fd, fcntl.F_GETFD)
            return True
        except OSError:
            return False

    @classmethod
    def from_makeflags(
        cls, makeflags: Optional[str] = None, explicit: Optional[str] = None
    ) -> tuple[Optional["Jobserver"], Optional[str]]:
        """返回 (jobserver, warning)。

        找不到 auth 时返回 (None, None)：视为“不在 make jobserver 下”，
        退化为只用隐式名额（串行），这本身仍是合法且安全的。
        找到 auth 但 fd 已关闭时返回 (None, warning)：对应手册说的
        “调用方没用 '+' 标记递归配方”，调用者应把警告展示给用户。
        """
        if explicit:
            m = re.match(r"^\s*(\d+)\s*,\s*(\d+)\s*$", explicit)
            if not m:
                return None, f"无法解析 --jobserver-auth={explicit!r}（应为 R,W）"
            rfd, wfd = int(m.group(1)), int(m.group(2))
            source = "cli"
        else:
            flags = makeflags if makeflags is not None else os.environ.get("MAKEFLAGS", "")
            if not flags:
                return None, None
            m = _AUTH_RE.search(flags) or _FDS_RE.search(flags)
            if not m:
                # 可能是 4.4 风格的 fifo:PATH / Windows 命名信号量，本演练不支持。
                if "--jobserver-auth=fifo:" in flags:
                    return None, "检测到 fifo 风格 jobserver；本助手按题目要求只支持 Make 4.3 管道风格"
                return None, None
            rfd, wfd = int(m.group(1)), int(m.group(2))
            source = "MAKEFLAGS"

        if not cls._fd_ok(rfd) or not cls._fd_ok(wfd):
            return None, (
                f"{source} 给出 --jobserver-auth={rfd},{wfd}，但 fd 已关闭；"
                "GNU Make 4.3 默认对 jobserver fd 设置 O_CLOEXEC，"
                "请在调用 batchcc 的配方行加 '+' 前缀"
            )

        js = cls(rfd, wfd, source)
        # 我们自己 spawn GCC 时绝不能把管道 fd 带过去（subprocess 默认
        # close_fds=True 已会关闭；这里再给本进程持有的副本加 CLOEXEC，
        # 双保险）。FD_CLOEXEC 是本进程 fd 表项属性，不影响父 make。
        for fd in (rfd, wfd):
            try:
                fl = fcntl.fcntl(fd, fcntl.F_GETFD)
                fcntl.fcntl(fd, fcntl.F_SETFD, fl | fcntl.FD_CLOEXEC)
            except OSError:
                pass
        return js, None

    # ---------- 令牌借 / 还 ----------
    def try_acquire(self) -> Optional[bytes]:
        """非阻塞借 1 个令牌；管道空闲（暂无空闲名额）时返回 None。"""
        if self.broken:
            return None
        try:
            ready, _, _ = select.select([self.r], [], [], 0)
        except (OSError, ValueError):
            self.broken = True
            return None
        if not ready:
            return None
        try:
            token = os.read(self.r, 1)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                # select 可读后被父 make 抢走了令牌 —— 正常竞争。
                return None
            self.broken = True
            return None
        if token == b"":
            # 写端全部关闭，理论上构建结束时才会发生。
            self.broken = True
            return None
        return token

    def release(self, token: bytes) -> bool:
        """把借到的*同一个*令牌字节写回管道。返回是否成功。"""
        if self.w < 0:
            return False
        while True:
            try:
                written = os.write(self.w, token)
                return written == len(token)
            except InterruptedError:
                continue
            except OSError:
                return False


def makeflags_dry_run(makeflags: Optional[str] = None) -> bool:
    """按手册建议识别 MAKEFLAGS 首段短选项束里的 n（make -n）。"""
    flags = makeflags if makeflags is not None else os.environ.get("MAKEFLAGS", "")
    if not flags:
        return False
    stripped = flags.lstrip()
    first_word = stripped.split()[0] if stripped.split() else ""
    # 首词若不含 '-' 前缀，就是短选项束（如 "wn"）。
    if first_word and not first_word.startswith("-") and "n" in first_word:
        return True
    return bool(re.search(r"(?:^|\s)(?:-[A-Za-z]*n|--dry-run|--just-print)\b", flags))


class Reporter:
    """向构建日志打印人类可读行，并向 JSONL 事件文件追加结构化事件。"""

    def __init__(self, group: str, event_log: Optional[Path] = None):
        self.group = group
        self.event_log = Path(event_log) if event_log else None
        if self.event_log:
            self.event_log.parent.mkdir(parents=True, exist_ok=True)

    def log(self, message: str) -> None:
        print(f"[batchcc:{self.group}] {message}", flush=True)

    def event(self, etype: str, **fields) -> None:
        record = {"ts": round(time.time(), 6), "type": etype, "group": self.group}
        record.update(fields)
        if self.event_log:
            try:
                line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
                # O_APPEND：两个批目标是不同进程，各自单行小写入，追加不会互相覆盖。
                fd = os.open(str(self.event_log), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
                try:
                    os.write(fd, line.encode("utf-8"))
                finally:
                    os.close(fd)
            except OSError:
                # 事件记录失败不能影响编译 / 令牌归还主流程。
                pass


class BuildInterrupted(Exception):
    def __init__(self, signum: int):
        self.signum = signum


def compile_batch(
    group: str,
    sources: list[str | Path],
    output_dir: str | Path,
    cc: str = "gcc",
    cflags: str = "",
    event_log: Optional[str | Path] = None,
    js: Optional[Jobserver] = None,
    jobserver_warning: Optional[str] = None,
    max_parallel: int = 0,
) -> int:
    """编译一批源文件。返回进程退出码（0 成功，非 0 有编译失败）。

    并发模型：1 个始终保留的隐式槽 + 每个借来的管道令牌 1 个槽。
    """
    rep = Reporter(group, event_log)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    if jobserver_warning:
        rep.log("WARNING: " + jobserver_warning)
        rep.event("warning", message=jobserver_warning)

    if js is not None:
        rep.log(
            f"已接入 GNU make 管道 jobserver ({js.source} r={js.r},w={js.w})；"
            "本批保留 1 个隐式名额，额外并发逐个从管道借令牌"
        )
        rep.event("jobserver", connected=True, r=js.r, w=js.w, source=js.source)
    else:
        rep.log("未发现可用管道 jobserver；只使用 make 分给本配方的 1 个隐式名额（批内串行）")
        rep.event("jobserver", connected=False)

    if makeflags_dry_run():
        rep.log("MAKEFLAGS 含 -n/dry-run 标记：按 jobserver 工具约定只预演不编译")
        for src in sources:
            obj = out / (Path(src).stem + ".o")
            rep.log(f"(dry-run) {cc} {cflags} -c {src} -o {obj}")
        return 0

    cc_argv = shlex.split(cc)
    flag_argv = shlex.split(cflags) if cflags else []

    total = len(sources)
    pos = 0
    # pid -> dict(proc, src, obj, token(bytes|None), start, pgid)
    running: dict[int, dict] = {}
    implicit_busy = False
    peak = 0
    started = 0
    succeeded = 0
    failed = 0
    borrowed = 0          # 借令牌启动的 GCC 数
    returned = 0          # 已原样归还的令牌数
    failure_messages: list[str] = []
    cancel_signum = 0

    def _raise_on_signal(signum, _frame):  # noqa: ANN001
        raise BuildInterrupted(signum)

    # 仅在主线程安装；被测试以库形式调用且非主线程时跳过。
    old_handlers = {}
    if threading_current_is_main():
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                old_handlers[sig] = signal.signal(sig, _raise_on_signal)
            except (ValueError, OSError):
                pass

    def spawn_one(src: str | Path, token: Optional[bytes]) -> bool:
        nonlocal implicit_busy, started, borrowed
        obj = out / (Path(src).stem + ".o")
        argv = cc_argv + flag_argv + ["-c", str(src), "-o", str(obj)]
        slot = "implicit" if token is None else "token"
        token_hex = token.hex() if token is not None else None
        token_char = token.decode("latin-1") if token is not None else None
        try:
            proc = subprocess.Popen(  # noqa: S603 - 参数为列表，无 shell 注入
                argv,
                stdin=subprocess.DEVNULL,
                stdout=None,
                stderr=None,
                close_fds=True,          # GCC 不继承 jobserver 管道 fd
                start_new_session=True,  # 便于收到取消信号时整组清理 cc1 等子进程
            )
        except OSError as exc:
            rep.log(f"无法启动编译器 {argv[0]!r}: {exc}")
            failure_messages.append(f"spawn failed: {exc}")
            failed_add()
            # 启动失败：若令牌已借到手，立刻归还（没有 GCC 在跑，槽没被占用）。
            if token is not None and js is not None:
                js.release(token)
            return False

        if token is None:
            implicit_busy = True
        else:
            borrowed += 1
        started += 1
        running[proc.pid] = {
            "proc": proc,
            "src": str(src),
            "obj": str(obj),
            "token": token,
            "start": time.monotonic(),
        }
        rep.log(
            f"START pid={proc.pid} {Path(src).name} slot={slot}"
            + (f" token={token_char!r}(hex {token_hex})" if token is not None else "（make 已分配的隐式名额）")
            + f" running={len(running)}"
        )
        rep.event(
            "start",
            pid=proc.pid,
            src=str(src),
            name=Path(src).name,
            slot=slot,
            token=token_char,
            token_hex=token_hex,
            running=len(running),
        )
        return True

    def failed_add() -> None:
        nonlocal failed
        failed += 1

    def reap_one(pid: int, status: int) -> None:
        nonlocal implicit_busy, succeeded, failed, returned
        handle = running.pop(pid, None)
        if handle is None:
            return
        proc = handle["proc"]
        if os.WIFEXITED(status):
            rc = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            rc = 128 + os.WTERMSIG(status)
        else:
            rc = -1
        try:
            proc.returncode = rc  # 防止 Popen 析构时重复 waitpid / 告警
        except Exception:  # noqa: BLE001
            pass

        elapsed_ms = int((time.monotonic() - handle["start"]) * 1000)
        token: Optional[bytes] = handle["token"]

        # —— 核心契约：GCC 退出并回收后，才原样归还它对应的令牌 ——
        if token is not None:
            ok = js.release(token) if js is not None else False
            if ok:
                returned += 1
            token_char = token.decode("latin-1")
            rep.log(
                f"FINISH pid={pid} {Path(handle['src']).name} rc={rc} {elapsed_ms}ms"
                f" -> 原样归还令牌 {token_char!r}" + ("" if ok else "（写入失败！）")
            )
            rep.event(
                "finish",
                pid=pid,
                src=handle["src"],
                name=Path(handle["src"]).name,
                rc=rc,
                ms=elapsed_ms,
                slot="token",
                token=token_char,
                token_hex=token.hex(),
                token_returned=ok,
            )
        else:
            implicit_busy = False
            rep.log(
                f"FINISH pid={pid} {Path(handle['src']).name} rc={rc} {elapsed_ms}ms"
                " -> 释放隐式名额（不向管道写令牌）"
            )
            rep.event(
                "finish",
                pid=pid,
                src=handle["src"],
                name=Path(handle["src"]).name,
                rc=rc,
                ms=elapsed_ms,
                slot="implicit",
                token_returned=False,
            )

        if rc == 0:
            succeeded += 1
        else:
            failed += 1
            failure_messages.append(f"{handle['src']} exited {rc}")
            rep.log(f"编译失败（rc={rc}）：{handle['src']}；仍会回收在跑进程并还清全部令牌")

    def drain_zombies() -> None:
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return
            reap_one(pid, status)

    def terminate_all() -> None:
        """信号取消 / 意外错误时：杀掉整组 GCC 并确保回收、还令牌。"""
        for pid, handle in list(running.items()):
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    handle["proc"].terminate()
                except Exception:  # noqa: BLE001
                    pass
        deadline = time.monotonic() + 2.0
        while running and time.monotonic() < deadline:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid:
                reap_one(pid, status)
            else:
                time.sleep(0.03)
        for pid, handle in list(running.items()):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        while running:
            try:
                pid, status = os.waitpid(-1, 0)
            except ChildProcessError:
                break
            reap_one(pid, status)

    exit_code = 0
    try:
        while pos < total or running:
            # ---- 尽量开满当前可用槽位 ----
            while pos < total and failed == 0 and cancel_signum == 0:
                if max_parallel and len(running) >= max_parallel:
                    break
                src = sources[pos]
                token: Optional[bytes] = None
                if implicit_busy:
                    # 隐式槽正忙：想再并行，只能借管道里的空闲令牌。
                    if js is None:
                        break
                    token = js.try_acquire()
                    if token is None:
                        break  # 管道暂无空闲名额，等某个 GCC 退出后再说
                # implicit 空闲 -> token 保持 None，使用 make 分配的隐式名额
                pos += 1
                if not spawn_one(src, token):
                    # spawn 失败会令 failed>0；对应令牌已在 spawn_one 内归还
                    break

            peak = max(peak, len(running))

            if not running:
                if pos >= total:
                    break
                # 没有在跑的 GCC 却还有任务（通常是 spawn 持续失败），避免空转。
                if failed:
                    break
                continue

            # ---- 阻塞等任意一个 GCC 退出（不占轮询 CPU）----
            try:
                pid, status = os.waitpid(-1, 0)
            except InterruptedError:
                continue
            reap_one(pid, status)
            drain_zombies()

    except BuildInterrupted as bi:
        cancel_signum = bi.signum
        exit_code = 128 + bi.signum
        rep.log(f"收到信号 {bi.signum}：终止所有在跑 GCC，并归还全部已借令牌")
        terminate_all()
    except Exception as exc:  # noqa: BLE001 - 任何意外也要先还令牌再退出
        rep.log(f"助手内部异常：{exc!r}；清理在跑 GCC 并归还令牌")
        exit_code = 1
        terminate_all()
    finally:
        # 最终保险：只要还有没回收的子进程，回收并还令牌。
        if running:
            terminate_all()
        for sig, old in old_handlers.items():
            try:
                signal.signal(sig, old)
            except (ValueError, OSError):
                pass

    abandoned = total - started
    if failed and exit_code == 0:
        exit_code = 1

    summary = {
        "total": total,
        "started": started,
        "succeeded": succeeded,
        "failed": failed,
        "abandoned": abandoned,
        "peak_concurrency": peak,
        "borrowed_tokens": borrowed,
        "returned_tokens": returned,
        "implicit_slots": 1,
        "exit_code": exit_code,
        "failure_messages": failure_messages[:5],
    }
    rep.event("summary", **summary)
    rep.log(
        "SUMMARY "
        f"total={total} ok={succeeded} failed={failed} abandoned={abandoned} "
        f"peak_gcc={peak} borrowed={borrowed} returned={returned} rc={exit_code}"
    )
    if js is not None and borrowed != returned:
        rep.log(
            f"WARNING: 借令牌数 {borrowed} 与归还数 {returned} 不一致，"
            "请检查归还路径！"
        )
    return exit_code


def threading_current_is_main() -> bool:
    import threading

    return threading.current_thread() is threading.main_thread()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="batchcc",
        description="GNU Make 4.3 pipe-jobserver aware batch C compiler helper",
    )
    p.add_argument("--group", default="batch", help="批/组名，用于日志与事件分组")
    p.add_argument("--output-dir", required=True, help=".o 输出目录")
    p.add_argument("--cc", default=os.environ.get("CC", "gcc"), help="编译器（可含参数，按 shell 分词）")
    p.add_argument("--cflags", default=os.environ.get("CFLAGS", ""), help="编译参数串（shlex 分词）")
    p.add_argument("--event-log", default=os.environ.get("BATCHCC_EVENT_LOG"), help="JSONL 事件文件")
    p.add_argument("--jobserver-auth", default=None, help="显式指定 R,W（通常留空，从 MAKEFLAGS 解析）")
    p.add_argument("--max-parallel", type=int, default=0, help=">0 时额外限制本批总并发（默认只受令牌约束）")
    p.add_argument("sources", nargs="+", help="待编译的 C 源文件")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    js, warning = Jobserver.from_makeflags(explicit=args.jobserver_auth)
    return compile_batch(
        group=args.group,
        sources=args.sources,
        output_dir=args.output_dir,
        cc=args.cc,
        cflags=args.cflags,
        event_log=args.event_log,
        js=js,
        jobserver_warning=warning,
        max_parallel=args.max_parallel,
    )


if __name__ == "__main__":
    sys.exit(main())
