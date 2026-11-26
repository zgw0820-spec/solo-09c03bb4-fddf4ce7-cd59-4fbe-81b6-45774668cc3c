#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fakecc —— 单元测试用的“慢编译器”。

batchcc 会以 `[python3, fakecc.py, <cflags...>, -c, src, -o, obj]` 调用它。
行为由环境变量控制：
  FAKECC_LOG         JSONL 事件文件（start/end，带单调时间戳与 pid）
  FAKECC_SLEEP       每个作业睡眠秒数（默认 0.3）
  FAKECC_FAIL_MATCH  源文件名含该子串则以 rc=1 失败（默认 zz_fail）
"""

import json
import os
import sys
import time


def _emit(log_path, record):
    if not log_path:
        return
    record["wall"] = time.time()
    line = json.dumps(record, separators=(",", ":")) + "\n"
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


def main(argv):
    log_path = os.environ.get("FAKECC_LOG", "")
    sleep_s = float(os.environ.get("FAKECC_SLEEP", "0.3"))
    fail_match = os.environ.get("FAKECC_FAIL_MATCH", "zz_fail")

    src = ""
    obj = ""
    for i, a in enumerate(argv):
        if a == "-c" and i + 1 < len(argv):
            src = argv[i + 1]
        if a == "-o" and i + 1 < len(argv):
            obj = argv[i + 1]
    name = os.path.basename(src)
    will_fail = bool(fail_match and fail_match in name)

    _emit(log_path, {"phase": "start", "pid": os.getpid(), "name": name})
    # 失败作业快速退出，保证调度器最先观察到失败（测试“见失败即不再开新工”）。
    this_sleep = 0.05 if will_fail else sleep_s
    # 可被中断信号打断；batchcc 的取消清理会 SIGTERM 整个进程组。
    time.sleep(this_sleep)

    if obj:
        try:
            with open(obj, "w") as fh:
                fh.write("FAKE-OBJ\n")
        except OSError:
            pass

    failed = will_fail
    _emit(
        log_path,
        {"phase": "end", "pid": os.getpid(), "name": name, "rc": 1 if failed else 0},
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
