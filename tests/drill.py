#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实 GNU Make 4.3 演练：试跑 -j2 / -j4，校验全局 GCC 上限与失败还令牌。

用法：
    python3 tests/drill.py

硬校验（不满足则退出码 1）：
  * 成功构建 make rc=0 且产出 collector-plugin.so；
  * 日志中没有 make 收尾的 "INTERNAL: ... jobserver tokens ..." 令牌核算错误；
  * /proc 采样到的 GCC 进程峰值绝不超过 -j；
  * 失败注入构建 make rc=2、日志含 DRILL FAILURE，且同样没有令牌核算错误、
    GCC 峰值不超过 -j；
  * 每个批 borrowed_tokens == returned_tokens。

“峰值是否恰好等于 -j”只报告、不硬失败（调度时序可能让采样错过满额瞬间），
但内置样例的编译时长足以稳定观察到满额。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from procsampler import GccSampler  # noqa: E402

SAMPLE = ROOT / "sample"
RUN_DIR = ROOT / "runs" / "cli-drill"
INTERNAL_RE = re.compile(r"INTERNAL:.*jobserver tokens|jobserver unavailable|should be \d+", re.I)


def ensure_sources() -> None:
    inc = SAMPLE / "src" / "normal" / "workload.inc"
    if not inc.exists():
        subprocess.run([sys.executable, str(ROOT / "tools" / "gen_sample.py")], check=True)


def parse_summaries(events: Path) -> list[dict]:
    out = []
    if not events.exists():
        return out
    for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") == "summary":
            out.append(rec)
    return out


def run_one(jobs: int, fail: bool) -> dict:
    tag = f"j{jobs}-{'fail' if fail else 'ok'}"
    case_dir = RUN_DIR / tag
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True)
    log_path = case_dir / "build.log"
    events_path = case_dir / "events.jsonl"

    shutil.rmtree(SAMPLE / "build", ignore_errors=True)

    env = os.environ.copy()
    env["BATCHCC_EVENT_LOG"] = str(events_path)
    env["PYTHONUNBUFFERED"] = "1"
    if fail:
        env["FAIL"] = "1"

    cmd = ["make", "-C", str(SAMPLE), f"-j{jobs}", "all"]
    t0 = time.monotonic()
    with open(log_path, "wb") as logf:
        logf.write(("$ " + " ".join(cmd) + (" FAIL=1" if fail else "") + "\n").encode())
        logf.flush()
        proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, env=env)
        sampler = GccSampler(interval=0.02, root_pid=proc.pid)
        sampler.start()
        rc = proc.wait()
        sampler.stop()
    wall = time.monotonic() - t0

    text = log_path.read_text(encoding="utf-8", errors="replace")
    summaries = parse_summaries(events_path)
    token_errors = [ln for ln in text.splitlines() if INTERNAL_RE.search(ln)]
    cap_violated = sampler.peak > jobs
    borrow_ok = all(s.get("borrowed_tokens") == s.get("returned_tokens") for s in summaries)

    return {
        "jobs": jobs,
        "fail": fail,
        "rc": rc,
        "wall_s": round(wall, 2),
        "peak_gcc": sampler.peak,
        "peak_at_ms": sampler.peak_at_ms,
        "peak_detail": sampler.peak_detail,
        "cap_violated": cap_violated,
        "token_errors": token_errors,
        "borrow_ok": borrow_ok,
        "summaries": summaries,
        "log_path": log_path,
        "events_path": events_path,
        "failure_seen": "DRILL FAILURE" in text,
        "so_exists": (SAMPLE / "build" / "collector-plugin.so").exists(),
        "series_len": len(sampler.series),
    }


def report(r: dict) -> bool:
    label = f"-j{r['jobs']} {'FAIL' if r['fail'] else 'OK'}"
    print(f"\n===== {label} =====")
    print(f"make rc={r['rc']}  wall={r['wall_s']}s  peak_gcc={r['peak_gcc']} "
          f"@ {r['peak_at_ms']}ms (samples={r['series_len']})  cap<={r['jobs']}: "
          f"{'VIOLATED' if r['cap_violated'] else 'ok'}")
    for s in r["summaries"]:
        print(f"  batch {s['group']:8s} peak={s['peak_concurrency']} "
              f"ok={s['succeeded']} failed={s['failed']} abandoned={s['abandoned']} "
              f"borrowed={s['borrowed_tokens']} returned={s['returned_tokens']}")
    print(f"  borrow==return: {r['borrow_ok']}; make token sanity errors: {len(r['token_errors'])}")
    for ln in r["token_errors"]:
        print("   TOKEN-ERROR>", ln)
    if r["fail"]:
        print(f"  injected #error observed in log: {r['failure_seen']}")
    else:
        print(f"  collector-plugin.so exists: {r['so_exists']}")

    ok = True
    if r["cap_violated"]:
        ok = False
        print("  !! HARD FAIL: GCC peak exceeded -j; peak snapshot:")
        for d in r.get("peak_detail", []):
            print(f"       pid={d['pid']} ppid={d['ppid']} state={d['state']} exe={d['exe']}")
    if r["token_errors"]:
        ok = False
        print("  !! HARD FAIL: make reported jobserver token accounting error")
    if not r["borrow_ok"]:
        ok = False
        print("  !! HARD FAIL: borrowed/returned token count mismatch")
    if r["fail"]:
        if r["rc"] == 0 or not r["failure_seen"]:
            ok = False
            print("  !! HARD FAIL: failure injection did not produce a failing build")
    else:
        if r["rc"] != 0 or not r["so_exists"]:
            ok = False
            print("  !! HARD FAIL: success build did not complete/link")
    if ok and r["peak_gcc"] < r["jobs"]:
        print(f"  note: peak {r['peak_gcc']} < -j{r['jobs']} (cap holds, but full occupancy not sampled)")
    return ok


def main() -> int:
    ensure_sources()
    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR)
    RUN_DIR.mkdir(parents=True)
    all_ok = True
    results = []
    for jobs in (2, 4):
        r = run_one(jobs, fail=False)
        results.append(r)
        all_ok &= report(r)
    for jobs in (2, 4):
        r = run_one(jobs, fail=True)
        results.append(r)
        all_ok &= report(r)

    print("\n===== summary =====")
    for r in results:
        print(f"j={r['jobs']} fail={int(r['fail'])} rc={r['rc']} "
              f"peak_gcc={r['peak_gcc']}/{r['jobs']} "
              f"token_sanity={'ok' if not r['token_errors'] else 'BAD'}")
    print("DRILL", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
