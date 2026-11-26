#!/usr/bin/env bash
# 采集插件 CI 自检：先跑令牌协议单元测试，再跑真实 make -j2/-j4 + 失败演练。
set -euo pipefail
cd "$(dirname "$0")"

echo "== [1/2] batchcc token protocol unit tests =="
python3 tests/test_batchcc.py

echo
echo "== [2/2] real GNU Make drill (-j2/-j4 x ok/fail) =="
python3 tests/drill.py
