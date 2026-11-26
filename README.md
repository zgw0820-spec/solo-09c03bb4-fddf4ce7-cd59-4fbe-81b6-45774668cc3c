# 采集插件 CI · 批编译助手（GNU Make 4.3 管道 jobserver 演练）

给采集插件的 CI 构建加一个用 Python 写的**批编译助手 `batchcc`**。它接入
GNU Make 4.3 的**管道 jobserver**：每个批目标先用 make 已分给该配方的
**1 个隐式名额**开工；批内还有文件时，再从 jobserver 管道**借空闲令牌**
并行开更多 GCC。某个 GCC 退出并被回收后，才把**借到的同一个令牌字节原样
写回**；编译失败 / 被信号中断也保证归还。因此整个构建同时存活的 GCC 进程数
恒不超过网页选择的 `-j2` / `-j4`。

仓库里还带一个 Flask 演练页：选 `-j2` 或 `-j4`、可勾选**注入真实编译失败**、
在线看 GCC 并发曲线 / 各批令牌账 / 完整构建日志；并用 Docker Compose 一键启动。

---

## 1. 快速开始

### 方式 A：Docker Compose（推荐演练）

```bash
docker compose up --build
# 打开 http://127.0.0.1:8000
```

页面上选择 `-j2` / `-j4`，点“开始构建”；勾选“注入样例编译失败”可触发
`#error`。详情页每秒轮询，展示 GCC 峰值、并发曲线、各批借还令牌数和日志，
也可在 `/runs/<id>/log` 查看原始日志。`./runs` 以 bind mount 挂到容器，
构建日志 / `meta.json` / `events.jsonl` 都落在宿主机 `runs/`。

> 镜像固定 `python:3.11-slim-bookworm`，其中 apt 的 `make` 正是 GNU Make
> **4.3**（更新的发行版可能带 4.4，jobserver 默认风格有变化）。

### 方式 B：本机直接跑

环境需要 `make`（4.3）、`gcc`、`python3`、Flask：

```bash
pip install -r requirements.txt          # 或系统已装 Flask 可跳过
python3 web/server.py                    # 默认 0.0.0.0:8000，可用 PORT 改端口
```

不用网页，也可以直接命令行构建样例：

```bash
python3 tools/gen_sample.py              # 生成内置 C 样例（Makefile 也会兜底）
make -C sample -j4 all                   # 成功构建
make -C sample -j2 FAIL=1 all            # 注入失败，观察令牌仍如数归还
```

### CI 自检（单元测试 + 真 make 四场演练）

```bash
./ci.sh
# 或：
python3 tests/test_batchcc.py             # 令牌协议确定性单测（假编译器）
python3 tests/drill.py                   # 真 make -j2/-j4 × 成功/失败 + /proc 采样
```

---

## 2. 内置 C 样例（两个批目标 + 一个普通 GCC 目标）

```
sample/
├── Makefile
└── src/
    ├── alpha/modbus_channel_01..06.c   # 批目标 batch-modbus（6 个编译单元）
    ├── beta/mqtt_parser_01..06.c       # 批目标 batch-mqtt（6 个编译单元）
    ├── normal/loader.c + workload.inc  # 普通 GCC 目标 loader.o（一条普通规则）
    └── plugin_sdk.h
```

- `batch-modbus`、`batch-mqtt`：配方调用 `tools/batchcc.py` 批量并行编译。
- `$(LOADER_OBJ)`：**普通 GCC 目标**，一条没有 `+` 的最普通 `$(CC) -c` 规则，
  只占 make 给它的一个 `-j` 名额，不直接碰 jobserver。
- 最后 `$(CC) -shared ...` 链出 `sample/build/collector-plugin.so`。
- 每个单元含数百个数值函数，让真实 GCC 存活约 0.3–1.3s，便于 `/proc`
  采样稳定看到并行重叠（上限正确性不依赖编译耗时）。
- `modbus_channel_03.c` 内有
  `#ifdef DRILL_INJECT_FAILURE / #error "DRILL FAILURE ..."`；
  网页勾选后 Makefile 传 `-DDRILL_INJECT_FAILURE=1`，产生**真实的 GCC 失败**。

---

## 3. 工作原理与 Make 4.3 的关键“机关”

GNU Make 手册 “POSIX Jobserver Interaction” 的要求这里全部落实。但在
**Make 4.3** 上有一个容易踩的版本细节（读 `make-4.3/src/posixos.c` 与
`commands.c` 并实测确认）：

1. make 创建管道，把 `--jobserver-auth=3,4` 放进 `MAKEFLAGS`；
2. 但它给两个 fd 设置了 `O_CLOEXEC`，且默认**不**让配方子进程继承，
   只在 `jobserver_pre_child(recursive=1)` 时临时 `fd_inherit()`；
3. `recursive` 仅当配方行带 **`+` 前缀**（或命令行含 `$(MAKE)` 字样）。

所以两个批目标的配方**必须**以 `+` 开头：

```make
batch-modbus:
	+$(PYTHON) $(TOOLS)/batchcc.py --group modbus \
	    --cc "$(CC)" --cflags "$(PIC_CFLAGS)" \
	    --output-dir $(BUILD)/modbus -- $(ALPHA_SRCS)
```

若漏写 `+`，batchcc 仍能从 `MAKEFLAGS` 看到 `3,4`，但 `fcntl(F_GETFD)`
会拿到 `EBADF`。此时它按手册提示打印 WARNING，并**退化为只用隐式名额**
（批内串行），不会卡死：

```
[batchcc:noplus] WARNING: MAKEFLAGS 给出 --jobserver-auth=3,4，但 fd 已关闭；
GNU Make 4.3 默认对 jobserver fd 设置 O_CLOEXEC，请在调用 batchcc 的配方行加 '+' 前缀
[batchcc:noplus] SUMMARY ... peak_gcc=1 borrowed=0 returned=0 rc=0
```

### 并发 / 令牌不变量

`-jN` 时 make 预放 `N-1` 个令牌，自己保留 1 个自由名额。对任意时刻：

- 每个运行中的批助手有 **≤1 个隐式 GCC**（对应 make 为该配方保留的槽）；
- 批内每个额外 GCC 都严格对应一次成功的管道 `read(...,1)`；
- 每个这样的令牌在对应 GCC 被 `waitpid` 回收后，用**当初读到的同一字节**
  `write` 回 fd4（不假设令牌都是 `+`，单测用 `X/Y/Z` 不同字节验证）；
- 隐式名额**不**写回。

因此全部 N 个名额恰好覆盖：make 配方槽（含普通 GCC）+ 各批隐式 GCC +
各批借来的 GCC，**全机构 GCC 数 ≤ N**。make 收尾还会执行
`1 + jobserver_acquire_all() == master_job_slots` 自检：泄漏（少还）或
多还都会打出 `INTERNAL: Exiting with ... jobserver tokens ... should be N!`。

读端在 Linux/Make4.3 下实际是 `O_NONBLOCK`；助手用 `select(0 超时)` +
捕获 `EAGAIN` 非阻塞借令牌，且**不**改 `O_NONBLOCK`（该文件状态与父 make
共享），避免影响 make 自身的 pselect 循环。调度为单线程事件循环：先填满
隐式槽，再尽量借空闲令牌，随后阻塞 `waitpid` 等任一 GCC 退出并回收、还令牌，
再用 `WNOHANG` 排空僵尸进程。

---

## 4. 实测结果

环境：Debian 12（容器/本机同一用户态）、GNU Make **4.3**、gcc 12.2、
Python 3.11。采样器每 20ms 扫一次 `/proc/*/exe`，按 `gcc` / `gcc-NN`
匹配 GCC **驱动进程**（不含 `cc1/as/ld`），只统计 ppid 链能回溯到本次
`make` 根进程的子树、跳过 `Z` 僵尸态，并排除“父进程也是 gcc”的 fork→exec
残影（高频狩猎证实：极偶尔会抓到 gcc 刚 fork、尚未 exec 成 `cc1` 的子进程，
其 cmdline 往往已经是 cc1；它不是额外编译槽）。因此采样数与编译槽 1:1。
开发期用“全机 + 不过滤嵌套”的粗采样曾偶发读到一次 `5/4`，分别定位为宿主
外部 gcc 与上述 fork 残影；加了子树归属和顶层过滤后，连续多轮每场都稳定
等于或小于 `-j`。

### `python3 tests/test_batchcc.py`：5/5 通过

```
test_no_jobserver_runs_serial_on_implicit_slot  ... ok
test_empty_pipe_still_compiles_one_at_a_time    ... ok   # 管道空：隐式名额串行
test_two_borrowed_tokens_..._to_three           ... ok   # 1 隐式 + 2 令牌 => 峰值 3，原字节 XY 如数归还
test_failure_still_returns_exact_tokens         ... ok   # 失败后 A/B/C 原字节全部回管
test_closed_auth_warns_and_falls_back_...       ... ok   # 漏 '+' 的 EBADF 场景
Ran 5 tests ... OK
```

### `python3 tests/drill.py`：DRILL PASS（最后一轮）

| 档位 | 注入失败 | make rc | /proc GCC 峰值/上限 | 令牌 sanity | 各批 peak / 借=还 |
|---|---|---:|---:|---|---|
| `-j2` | 否 | 0（链出 .so） | **2 / 2** | 无 INTERNAL | modbus 1、mqtt 1；borrowed 全 0 |
| `-j4` | 否 | 0（链出 .so） | **4 / 4** | 无 INTERNAL | 两批 peak 2~3；借/还 2=2、4=4 |
| `-j2` | 是 | 2 | **2 / 2** | 无 INTERNAL | modbus 1（1 失败 3 未开工）；mqtt 借 2 还 2 |
| `-j4` | 是 | 2 | **4 / 4** | 无 INTERNAL | mqtt peak 3、借 3 还 3；失败批借 0 还 0 |

要点：

- `-j2` 成功时两个批各占隐式名额，管道里没有空闲令牌，所以两批都串行，
  全局峰值正好 2；
- `-j4` 成功时除初始空闲令牌外，先完成的普通目标 / 批会让 make 把名额
  放回管道，仍在编译的批随即“借空闲名额”把批内并发提到 2–3，全局峰值 4；
- 失败场 make rc=2、日志含 `DRILL FAILURE`，失败批停止开新文件，但在跑的
  GCC 全部回收、令牌全部归还；存活的另一批还能借到 make 释放的名额；
- 四场日志均无 `INTERNAL ... jobserver tokens` / `jobserver unavailable`，
  即 make 主进程收尾令牌总账正确。

Flask 链路也用 test client 与真实 HTTP（`POST /build`、轮询
`/api/runs/<id>`、`/runs/<id>/log`）各跑过 `-j2/-j4` 成功与失败，
判定分别为 `success` / `injected_failure_ok`，峰值与令牌字段同上。

---

## 5. 目录说明

```
tools/batchcc.py       核心：jobserver 客户端 + 批调度器 + CLI + JSONL 事件
tools/procsampler.py   /proc GCC 并发采样器（Web 与 drill 共用的独立旁证）
tools/gen_sample.py    内置 C 样例工作负载生成器
sample/Makefile        两个 '+' 批目标 + 一个普通 GCC 目标 + 链接
web/server.py          Flask：构建管理、meta/日志、API、采样
web/templates/         演练台页面（档位选择 / 并发曲线 / 令牌账 / 日志）
tests/test_batchcc.py  令牌协议确定性单测（自控管道 + 不同字节 + 假慢编译器）
tests/fakecc.py        可设睡眠 / 失败的假编译器
tests/drill.py         真 make 四场演练 + 硬校验
Dockerfile             python:3.11-slim-bookworm + make4.3 + gcc
docker-compose.yml     http://127.0.0.1:8000，挂载 ./runs
ci.sh                  CI 入口：单测 + drill
```

每次网页构建产物：`runs/<时间-id>/{meta.json,build.log,events.jsonl}`。

---

## 6. 已知限制 / 未完成项（如实说明）

1. **本环境没有 docker / docker compose 可执行文件或守护进程**，因此
   `docker compose up --build` 没能在交付环境里实际 build/up 验证；
   `docker-compose.yml` 已通过 PyYAML 解析核对，Dockerfile 用的是与本机
   一致的 bookworm + Make 4.3 + gcc12，且应用本身已用等价的本机
   `python3 web/server.py` 完整跑通。容器实跑需在有 Docker 的机器上确认。
2. 没有浏览器 / 截图环境，页面只验证到模板渲染、HTTP 200、接口数据正确；
   前端 JS（轮询、SVG 曲线）未做真实浏览器点击验证。
3. `/proc` 20ms 采样是**外部旁证**（已按 make 进程子树过滤、排除僵尸），
   可能漏掉极短命的越界；并发上限的严格保证来自第 3 节的令牌协议，而非采样。
   样例负载下多轮四场都稳定采到峰值恰为 `-j`，但不保证任意机器 / 调度时序
   都能采样到满额（只保证永不超 `-j`）。
4. SIGINT/SIGTERM 的整组清理与还令牌路径已实现（`start_new_session` +
   killpg + 回收），但没有专门的自动化信号测试；正常编译失败路径有完整测试。
5. 目前只支持 Make 4.3 的**管道**风格 `--jobserver-auth=R,W`；检测到 4.4 的
   `fifo:PATH` 会提示并退化为隐式串行，未实现 FIFO 客户端。
6. 按约定**未创建 git commit**；所有改动都在工作区，可用 `git status` /
   `git diff --no-index /dev/null <file>` 核对。`sample/build/`、
   `runs/*`（保留 `.gitkeep`）、`__pycache__` 已加入 `.gitignore`。
