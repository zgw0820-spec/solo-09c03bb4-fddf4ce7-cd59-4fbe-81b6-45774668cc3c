# 采集插件 CI 批编译 jobserver 演练台
# 固定 bookworm：其 make 包正是题目要求的 GNU Make 4.3-4.1。
# （更新的发行版可能带 Make 4.4，jobserver 实现/默认风格有变化。）
FROM python:3.11-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        make \
        gcc \
        libc6-dev \
    && make --version | head -1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 预生成内置 C 样例的重复性工作负载（网页首次构建时 Makefile 也会兜底生成）。
RUN python3 tools/gen_sample.py sample/src \
    && chmod +x tools/batchcc.py tests/fakecc.py || true

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

EXPOSE 8000
# 两种入口等价：python3 app.py 或 python3 web/server.py
CMD ["python3", "app.py"]
