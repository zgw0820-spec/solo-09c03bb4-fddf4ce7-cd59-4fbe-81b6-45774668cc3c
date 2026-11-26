#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集插件 CI · 批编译 jobserver 演练台（Flask）。

页面选择 -j2 / -j4、可勾选注入编译失败；服务端为每次演练启动一个全新的
顶层 ``make -C sample -jN all``（清掉可能从父环境泄漏的 MAKEFLAGS，确保
make 自建管道 jobserver），并用 /proc 采样器记录全机构 GCC 并发峰值。

每次演练在 runs/<id>/ 下落三样东西：
  meta.json     结构化结果（峰值、令牌核算、各批 summary、时序）
  build.log     完整构建日志（网页可看，也可直接 GET /runs/<id>/log）
  events.jsonl  batchcc 写出的逐事件 JSONL
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from procsampler import GccSampler  # noqa: E402

SAMPLE_DIR = ROOT / "sample"
RUNS_DIR = Path(os.environ.get("DRILL_RUNS_DIR", ROOT / "runs"))
RUNS_DIR.mkdir(parents=True, exist_ok=True)

INTERNAL_RE = re.compile(
    r"INTERNAL:.*jobserver tokens|jobserver unavailable|should be \d+", re.IGNORECASE
)

app = Flask(__name__)

_active_lock = threading.Lock()
_active_id: str | None = None
_active_jobs: int | None = None


# --------------------------------------------------------------------------- #
# 元数据读写
# --------------------------------------------------------------------------- #
def meta_path(run_id: str) -> Path:
    return RUNS_DIR / run_id / "meta.json"


def load_meta(run_id: str) -> dict | None:
    mp = meta_path(run_id)
    if not mp.is_file():
        return None
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_meta(meta: dict) -> None:
    mp = meta_path(meta["id"])
    mp.parent.mkdir(parents=True, exist_ok=True)
    tmp = mp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, mp)


def list_runs(limit: int = 20) -> list[dict]:
    runs = []
    for child in RUNS_DIR.iterdir():
        if not child.is_dir():
            continue
        meta = load_meta(child.name)
        if meta:
            runs.append(meta)
    runs.sort(key=lambda m: m.get("started_ts", 0), reverse=True)
    return runs[:limit]


def parse_event_summaries(event_log: Path) -> list[dict]:
    summaries = []
    if not event_log.is_file():
        return summaries
    for line in event_log.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") == "summary":
            summaries.append(rec)
    return summaries


def ensure_sample_sources() -> None:
    inc = SAMPLE_DIR / "src" / "normal" / "workload.inc"
    if not inc.is_file():
        subprocess.run(
            [sys.executable, str(ROOT / "tools" / "gen_sample.py"), str(SAMPLE_DIR / "src")],
            check=True,
            stdout=subprocess.DEVNULL,
        )


# --------------------------------------------------------------------------- #
# 构建执行
# --------------------------------------------------------------------------- #
def run_build(run_id: str, jobs: int, fail: bool) -> None:
    global _active_id, _active_jobs
    rdir = RUNS_DIR / run_id
    log_path = rdir / "build.log"
    event_log = rdir / "events.jsonl"

    meta = load_meta(run_id) or {"id": run_id}
    meta.update(
        {
            "jobs": jobs,
            "fail": fail,
            "status": "running",
            "started_ts": time.time(),
            "started_iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    save_meta(meta)

    try:
        ensure_sample_sources()
        shutil.rmtree(SAMPLE_DIR / "build", ignore_errors=True)

        env = os.environ.copy()
        # 关键：不要让 Web 服务自身的 MAKEFLAGS 泄漏给演练 make，
        # 每次演练都由这个顶层 make 自建全新的管道 jobserver。
        env.pop("MAKEFLAGS", None)
        env.pop("MFLAGS", None)
        env["BATCHCC_EVENT_LOG"] = str(event_log)
        env["PYTHONUNBUFFERED"] = "1"
        if fail:
            env["FAIL"] = "1"

        cmd = ["make", "-C", str(SAMPLE_DIR), f"-j{jobs}", "all"]
        t0 = time.monotonic()
        with open(log_path, "wb") as logf:
            logf.write(("$ " + " ".join(cmd) + (" FAIL=1" if fail else "") + "\n").encode())
            logf.flush()
            # 先启动 make，再以它的 pid 为根采样“本次构建子树”内的 GCC，
            # 避免把共享宿主机上无关的 gcc 误计进来。
            proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, env=env)
            sampler = GccSampler(interval=0.02, root_pid=proc.pid)
            sampler.start()
            rc = proc.wait()
            sampler.stop()
        duration = round(time.monotonic() - t0, 2)

        text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
        token_errors = [ln.strip() for ln in text.splitlines() if INTERNAL_RE.search(ln)]
        summaries = parse_event_summaries(event_log)
        borrow_ok = all(
            s.get("borrowed_tokens") == s.get("returned_tokens") for s in summaries
        )
        failure_seen = "DRILL FAILURE" in text
        so_exists = (SAMPLE_DIR / "build" / "collector-plugin.so").is_file()
        cap_violated = sampler.peak > jobs

        if fail:
            if rc != 0 and failure_seen and not token_errors and not cap_violated:
                verdict = "injected_failure_ok"
            elif cap_violated:
                verdict = "cap_violation"
            elif token_errors:
                verdict = "token_error"
            else:
                verdict = "unexpected_failure"
        else:
            if rc == 0 and so_exists and not token_errors and not cap_violated:
                verdict = "success"
            elif cap_violated:
                verdict = "cap_violation"
            elif token_errors:
                verdict = "token_error"
            else:
                verdict = "unexpected_failure"

        meta.update(
            {
                "status": "done",
                "exit_code": rc,
                "duration_s": duration,
                "peak_gcc": sampler.peak,
                "peak_at_ms": sampler.peak_at_ms,
                "peak_detail": sampler.peak_detail,
                "series": sampler.series,
                "cap_violated": cap_violated,
                "token_sanity_ok": not token_errors,
                "token_errors": token_errors,
                "borrow_return_ok": borrow_ok,
                "failure_seen": failure_seen,
                "so_exists": so_exists,
                "summaries": summaries,
                "verdict": verdict,
            }
        )
    except Exception as exc:  # noqa: BLE001 - 页面要能看到启动失败
        meta.update(
            {
                "status": "done",
                "exit_code": -1,
                "verdict": "server_error",
                "error": repr(exc),
            }
        )
    finally:
        meta["finished_ts"] = time.time()
        save_meta(meta)
        with _active_lock:
            _active_id = None
            _active_jobs = None


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@app.route("/healthz")
def healthz():
    return {"ok": True}


@app.route("/")
def index():
    with _active_lock:
        active = {"id": _active_id, "jobs": _active_jobs} if _active_id else None
    return render_template("index.html", runs=list_runs(), active=active)


@app.route("/build", methods=["POST"])
def start_build():
    jobs_raw = request.form.get("jobs", "2")
    try:
        jobs = int(jobs_raw)
    except ValueError:
        jobs = 2
    if jobs not in (2, 4):
        jobs = 2

    fail = request.form.get("fail") in ("1", "on", "true", "yes")

    global _active_id, _active_jobs
    with _active_lock:
        if _active_id is not None:
            return redirect(url_for("run_detail", run_id=_active_id))
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        (RUNS_DIR / run_id).mkdir(parents=True, exist_ok=True)
        _active_id = run_id
        _active_jobs = jobs

    meta = {"id": run_id, "status": "queued", "jobs": jobs, "fail": fail}
    save_meta(meta)
    t = threading.Thread(target=run_build, args=(run_id, jobs, fail), daemon=True)
    t.start()
    return redirect(url_for("run_detail", run_id=run_id))


@app.route("/runs/<run_id>")
def run_detail(run_id: str):
    meta = load_meta(run_id)
    if meta is None:
        abort(404)
    with _active_lock:
        is_active = _active_id == run_id
    return render_template("run.html", run=meta, is_active=is_active)


@app.route("/api/runs/<run_id>")
def api_run(run_id: str):
    meta = load_meta(run_id)
    if meta is None:
        return jsonify({"error": "not found"}), 404
    with _active_lock:
        meta["is_active"] = _active_id == run_id
    return jsonify(meta)


@app.route("/runs/<run_id>/log")
def run_log(run_id: str):
    if load_meta(run_id) is None:
        abort(404)
    lp = RUNS_DIR / run_id / "build.log"
    if not lp.is_file():
        return "(log not yet created)\n", 200, {"Content-Type": "text/plain; charset=utf-8"}
    return lp.read_text(encoding="utf-8", errors="replace"), 200, {
        "Content-Type": "text/plain; charset=utf-8"
    }


@app.route("/api/runs")
def api_runs():
    return jsonify(list_runs())


def _mark_interrupted_on_boot() -> None:
    """服务重启后，把上次残留的 running/queued 标记为 interrupted。"""
    for meta in list_runs(limit=100):
        if meta.get("status") in ("running", "queued"):
            meta["status"] = "done"
            meta["verdict"] = "interrupted_by_server_restart"
            save_meta(meta)


_mark_interrupted_on_boot()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
