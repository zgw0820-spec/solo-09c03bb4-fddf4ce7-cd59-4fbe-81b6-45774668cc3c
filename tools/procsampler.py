#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻量 /proc 采样器：统计“本次构建进程子树内”同时存活的 GCC 驱动进程数。

batchcc 自己的事件日志能精确给出“每个批”的并发；本模块从外部扫描
/proc/<pid>/exe，按可执行文件名匹配 gcc / gcc-NN（Debian 上 /usr/bin/gcc
实际是 gcc-12），提供跨两个批目标 + 普通 GCC 目标 + 链接 gcc 的独立旁证。

两个防误报要点：
  * root_pid 给定时，只保留 ppid 链能回溯到该 make 根进程的 gcc，避免把
    共享宿主机上“别人的 gcc”算进本次构建；
  * 跳过 stat 状态为 Z 的僵尸进程（已退出、仅待 waitpid 回收），它们不算
    “正在运行的 GCC”。

注意只统计 GCC *驱动*进程（每个编译槽对应一个，编译期间它一直在 wait
cc1），不把 cc1/as/ld 等工具链子进程算作 GCC。
"""

from __future__ import annotations

import os
import re
import threading
import time

# /usr/bin/gcc、/usr/bin/gcc-12、/usr/bin/x86_64-linux-gnu-gcc-12 等。
_GCC_EXE_RE = re.compile(r"(?:^|/)(?:[^/]{0,80}-)?gcc(?:-\d+(?:\.\d+)*)?$")
# 某些环境 /usr/bin/cc 经 alternatives 解析后 exe 仍是 gcc-12；
# readlink 失败时退化为 comm 匹配。
_GCC_COMM_RE = re.compile(r"^(?:gcc|cc)(?:-\d+)?$")


def _proc_stat(pid: str) -> tuple[str, int] | None:
    """返回 (state, ppid)；解析 /proc/<pid>/stat 中可能含空格/括号的 comm。"""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="ascii", errors="ignore") as fh:
            data = fh.read()
    except OSError:
        return None
    rp = data.rfind(")")
    if rp < 0:
        return None
    rest = data[rp + 2:].split()
    if len(rest) < 2:
        return None
    try:
        return rest[0], int(rest[1])
    except ValueError:
        return None


def _ppid_of(pid: int) -> int | None:
    st = _proc_stat(str(pid))
    return st[1] if st else None


def is_descendant(pid: int, root_pid: int, max_depth: int = 64) -> bool:
    """pid 的 ppid 链能否到达 root_pid。"""
    if pid == root_pid:
        return True
    cur = pid
    for _ in range(max_depth):
        ppid = _ppid_of(cur)
        if ppid is None or ppid <= 1 or ppid == cur:
            return False
        if ppid == root_pid:
            return True
        cur = ppid
    return False


def _gcc_identity(pid: str) -> tuple[bool, str]:
    """返回 (是否 GCC 驱动, exe 路径)。"""
    proc = f"/proc/{pid}"
    try:
        exe = os.readlink(f"{proc}/exe")
    except OSError:
        exe = ""
    if exe:
        base = os.path.basename(exe)
        if _GCC_EXE_RE.search(base):
            return True, exe
    try:
        with open(f"{proc}/comm", "r", encoding="ascii", errors="ignore") as fh:
            comm = fh.read().strip()
        if _GCC_COMM_RE.match(comm):
            if comm == "cc" and exe and "gcc" not in os.path.basename(exe):
                return False, exe
            return True, exe or f"comm:{comm}"
    except OSError:
        pass
    return False, exe


def count_gcc_processes(
    root_pid: int | None = None, details: list[dict] | None = None
) -> int:
    """统计本次构建子树内的“顶层 GCC 驱动”进程数。

    GCC 驱动编译时会 fork 一个子进程再 exec 成 cc1/as；在 fork→exec 的极短
    窗口里，/proc/<pid>/exe 仍可能读到 gcc 镜像（实测高频采样能抓到这种
    “gcc 的 gcc 子进程”残影，其 cmdline 常常已经变成 cc1）。它不是额外的
    编译槽、也不参与 jobserver 借令牌，因此这里先收集所有 gcc 镜像进程，
    再排除“父进程本身也是 gcc”的嵌套残影，只保留与编译槽 1:1 的顶层驱动。
    """
    try:
        pids = os.listdir("/proc")
    except OSError:
        return 0

    candidates: list[dict] = []
    for pid_s in pids:
        if not pid_s.isdigit():
            continue
        pid = int(pid_s)
        st = _proc_stat(pid_s)
        if st is None:
            continue
        state, ppid = st
        if state == "Z":
            continue  # 僵尸已不在运行
        is_gcc, exe = _gcc_identity(pid_s)
        if not is_gcc:
            continue
        if root_pid is not None and not is_descendant(pid, root_pid):
            continue
        cmdline = ""
        if details is not None:
            try:
                raw = open(f"/proc/{pid_s}/cmdline", "rb").read()
                cmdline = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
            except OSError:
                pass
        candidates.append(
            {"pid": pid, "ppid": ppid, "state": state, "exe": exe, "cmdline": cmdline}
        )

    gcc_pids = {c["pid"] for c in candidates}
    top_level = [c for c in candidates if c["ppid"] not in gcc_pids]
    if details is not None:
        details.extend(top_level)
    return len(top_level)


class GccSampler(threading.Thread):
    """每 interval 秒采样一次 GCC 数量，记录峰值、时间序列与峰值现场。"""

    def __init__(self, interval: float = 0.02, root_pid: int | None = None):
        super().__init__(name="gcc-sampler", daemon=True)
        self.interval = interval
        self.root_pid = root_pid
        self._stop_evt = threading.Event()
        self._t0 = 0.0
        self.peak = 0
        self.peak_at_ms = 0
        self.peak_detail: list[dict] = []
        self.series: list[list[float | int]] = []

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_evt.set()
        self.join(timeout=timeout)

    def run(self) -> None:
        self._t0 = time.monotonic()
        while not self._stop_evt.is_set():
            detail: list[dict] = []
            n = count_gcc_processes(self.root_pid, detail)
            ms = int((time.monotonic() - self._t0) * 1000)
            self.series.append([ms, n])
            if n > self.peak:
                self.peak = n
                self.peak_at_ms = ms
                self.peak_detail = detail
            self._stop_evt.wait(self.interval)
