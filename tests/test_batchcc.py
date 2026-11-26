#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batchcc 令牌协议的确定性测试（不依赖真实 gcc / make）。

运行：python3 tests/test_batchcc.py -v
或：  python3 -m unittest tests.test_batchcc -v
"""

from __future__ import annotations

import fcntl
import json
import os
import select
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BATCHCC = ROOT / "tools" / "batchcc.py"
FAKECC = Path(__file__).resolve().parent / "fakecc.py"


def make_sources(srcdir: Path, names: list[str]) -> list[Path]:
    srcdir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in names:
        p = srcdir / f"{name}.c"
        p.write_text("/* fake compiler input */\n", encoding="utf-8")
        paths.append(p)
    return paths


def clear_cloexec(fd: int) -> None:
    fl = fcntl.fcntl(fd, fcntl.F_GETFD)
    fcntl.fcntl(fd, fcntl.F_SETFD, fl & ~fcntl.FD_CLOEXEC)


def read_pipe_tokens(rfd: int, timeout: float = 1.0) -> bytes:
    ready, _, _ = select.select([rfd], [], [], timeout)
    if not ready:
        return b""
    chunks = []
    while True:
        try:
            chunk = os.read(rfd, 4096)
        except BlockingIOError:
            break
        if not chunk:
            break
        chunks.append(chunk)
        if len(chunk) < 4096:
            break
    return b"".join(chunks)


def overlap_peak(fake_log: Path) -> tuple[int, int]:
    """根据 fakecc 的 start/end wall 时间计算最大同时存活进程数。"""
    if not fake_log.exists():
        return 0, 0
    open_spans: dict[int, float] = {}
    spans: list[tuple[float, float]] = []
    for line in fake_log.read_text().splitlines():
        rec = json.loads(line)
        if rec["phase"] == "start":
            open_spans[rec["pid"]] = rec["wall"]
        else:
            start = open_spans.pop(rec["pid"], None)
            if start is not None:
                spans.append((start, rec["wall"]))
    events: list[tuple[float, int]] = []
    for start, end in spans:
        events.append((start, +1))
        events.append((end, -1))
    # 同一时刻先算结束(-1)再算开始(+1)，避免把首尾相接误判成重叠。
    events.sort(key=lambda e: (e[0], e[1]))
    cur = peak = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)
    return peak, len(spans)


class BatchccTokenTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="batchcc-test-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def run_batchcc(
        self,
        names: list[str],
        tokens: bytes | None,
        sleep: str = "0.3",
        makeflags: str | None = "PIPE",
        extra_env: dict | None = None,
        timeout: float = 60,
    ):
        sources = make_sources(self.tmp / "src", names)
        events = self.tmp / "events.jsonl"
        fake_log = self.tmp / "fakecc.jsonl"
        outdir = self.tmp / "obj"

        r = w = -1
        pass_fds: tuple[int, ...] = ()
        env = os.environ.copy()
        env.pop("MAKEFLAGS", None)
        env["FAKECC_LOG"] = str(fake_log)
        env["FAKECC_SLEEP"] = sleep
        if extra_env:
            env.update(extra_env)

        if makeflags == "PIPE":
            r, w = os.pipe()
            clear_cloexec(r)
            clear_cloexec(w)
            if tokens:
                os.write(w, tokens)
            env["MAKEFLAGS"] = f" -j{len(tokens or b'') + 1} --jobserver-auth={r},{w}"
            pass_fds = (r, w)
        elif makeflags is not None:
            env["MAKEFLAGS"] = makeflags

        cmd = [
            sys.executable,
            str(BATCHCC),
            "--group", "unit",
            "--cc", f"{sys.executable} {FAKECC}",
            "--output-dir", str(outdir),
            "--event-log", str(events),
            "--",
            *[str(p) for p in sources],
        ]
        t0 = time.monotonic()
        proc = subprocess.run(
            cmd, env=env, pass_fds=pass_fds,
            capture_output=True, text=True, timeout=timeout,
        )
        wall = time.monotonic() - t0

        remaining = b""
        if r >= 0:
            remaining = read_pipe_tokens(r)
            os.close(r)
            os.close(w)

        summaries = []
        if events.exists():
            for line in events.read_text().splitlines():
                rec = json.loads(line)
                if rec.get("type") == "summary":
                    summaries.append(rec)
        peak, spans = overlap_peak(fake_log)
        started_names = []
        if fake_log.exists():
            for line in fake_log.read_text().splitlines():
                rec = json.loads(line)
                if rec["phase"] == "start":
                    started_names.append(rec["name"])
        return proc, remaining, summaries[-1] if summaries else None, peak, spans, wall, started_names

    def test_no_jobserver_runs_serial_on_implicit_slot(self):
        proc, remaining, summary, peak, spans, wall, _ = self.run_batchcc(
            ["a", "b", "c", "d"], tokens=None, makeflags=None, sleep="0.2"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(spans, 4)
        self.assertEqual(peak, 1, "没有 jobserver 时只能用隐式名额，必须串行")
        self.assertIsNotNone(summary)
        self.assertEqual(summary["peak_concurrency"], 1)
        self.assertGreaterEqual(wall, 0.75)

    def test_empty_pipe_still_compiles_one_at_a_time(self):
        # -j2 且唯一的管道令牌已被父 make 拿走（管道为空）：
        # 每个批仍可靠隐式名额开工，但不能额外并行。
        proc, remaining, summary, peak, spans, wall, _ = self.run_batchcc(
            ["a", "b", "c", "d"], tokens=b"", sleep="0.22"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(peak, 1)
        self.assertEqual(remaining, b"", "空管道跑完后仍应为空（隐式令牌不能写回）")
        self.assertEqual(summary["borrowed_tokens"], 0)
        self.assertEqual(summary["returned_tokens"], 0)
        self.assertGreaterEqual(wall, 0.8)

    def test_two_borrowed_tokens_raise_batch_parallelism_to_three(self):
        # 1 个隐式 + 2 个借来的不同令牌 => 峰值 3，且原字节如数归还。
        proc, remaining, summary, peak, spans, wall, _ = self.run_batchcc(
            [f"f{i}" for i in range(6)], tokens=b"XY", sleep="0.35"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertEqual(spans, 6)
        self.assertEqual(peak, 3, f"应观察到 3 并发，实际 peak={peak}\n{proc.stdout}")
        self.assertEqual(summary["peak_concurrency"], 3)
        self.assertEqual(summary["borrowed_tokens"], summary["returned_tokens"])
        self.assertEqual(sorted(remaining), sorted(b"XY"), "借的两个原字节必须原样归还")
        self.assertLess(wall, 6 * 0.35 - 0.2, "3 并发应明显快于串行")

    def test_failure_still_returns_exact_tokens(self):
        names = ["ok_00", "zz_fail_01", "ok_02", "ok_03", "ok_04_late"]
        proc, remaining, summary, peak, spans, wall, started = self.run_batchcc(
            names, tokens=b"ABC", sleep="0.35"
        )
        self.assertNotEqual(proc.returncode, 0, "包含失败编译，batchcc 应非零退出")
        # 前 4 个（隐式 + 3 令牌）立刻开工；第 5 个在首次失败后不再启动。
        self.assertNotIn("ok_04_late.c", started, "失败后不应再启动新文件")
        self.assertEqual(len(started), 4)
        self.assertEqual(spans, 4, "所有已启动的 GCC 都必须被回收（start/end 配平）")
        self.assertLessEqual(peak, 4)
        self.assertIsNotNone(summary)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["abandoned"], 1)
        self.assertEqual(summary["borrowed_tokens"], 3)
        self.assertEqual(summary["returned_tokens"], 3, "失败路径下 3 个令牌都要归还")
        self.assertEqual(sorted(remaining), sorted(b"ABC"), "失败后借到的 A/B/C 必须原字节回到管道")

    def test_closed_auth_warns_and_falls_back_to_implicit(self):
        # 模拟 Make 4.3 配方漏写 '+'：MAKEFLAGS 有 auth=40,41，但 fd 已 CLOEXEC。
        proc, remaining, summary, peak, spans, wall, _ = self.run_batchcc(
            ["a", "b", "c"],
            tokens=None,
            makeflags=" -j4 --jobserver-auth=40,41",
            sleep="0.15",
        )
        combined = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, combined)
        self.assertIn("O_CLOEXEC", combined)
        self.assertEqual(peak, 1)
        self.assertEqual(spans, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
