# sample/Makefile —— 采集插件 CI 构建演练样例
#
# 三个编译目标：
#   batch-modbus : 批编译目标 A（6 个 .c，由 tools/batchcc.py 批量并行编译）
#   batch-mqtt   : 批编译目标 B（6 个 .c，同上）
#   loader.o     : 普通 GCC 目标（一条普通 $(CC) 规则，不接触 jobserver）
# 最后链接出 build/collector-plugin.so。
#
# 关键机关：GNU Make 4.3 的 jobserver 管道 fd(3,4) 默认带 O_CLOEXEC，
# make 只对被标记为 COMMANDS_RECURSE 的配方行（'+' 前缀，或命令行里
# 含 $(MAKE)/${MAKE} 字样）在 fork 前临时清除 CLOEXEC。因此两个批目标
# 的配方行必须以 '+' 开头，batchcc.py 才能通过 MAKEFLAGS 里的
# --jobserver-auth=3,4 真正读写管道。漏了 '+' 时 batchcc 会发现 fd 已
# EBADF，打印 WARNING 并退化为只用隐式名额（批内串行）。

ROOT := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
REPO := $(abspath $(ROOT)/..)

PYTHON ?= python3
ifeq ($(origin CC),default)
CC := gcc
endif

TOOLS      := $(REPO)/tools
BUILD      := $(ROOT)/build

DRILL_CFLAGS ?= -O0 -g -Wall
PIC_CFLAGS   := $(DRILL_CFLAGS) -fPIC -I$(ROOT)/src

# 在解析期确保生成的 C 负载已存在，因为下面的 $(wildcard) 解析时就要展开。
GEN_STAMP := $(ROOT)/src/normal/workload.inc
$(shell test -f $(GEN_STAMP) || $(PYTHON) $(TOOLS)/gen_sample.py $(ROOT)/src >/dev/null)

# 网页勾选“注入编译失败”时，Flask 以 FAIL=1 调 make。
ifeq ($(FAIL),1)
PIC_CFLAGS += -DDRILL_INJECT_FAILURE=1
endif

ALPHA_SRCS := $(wildcard $(ROOT)/src/alpha/*.c)
BETA_SRCS  := $(wildcard $(ROOT)/src/beta/*.c)
ALPHA_OBJS := $(patsubst $(ROOT)/src/alpha/%.c,$(BUILD)/modbus/%.o,$(ALPHA_SRCS))
BETA_OBJS  := $(patsubst $(ROOT)/src/beta/%.c,$(BUILD)/mqtt/%.o,$(BETA_SRCS))
LOADER_OBJ := $(BUILD)/normal/loader.o
PLUGIN_SO  := $(BUILD)/collector-plugin.so

.PHONY: all help batch-modbus batch-mqtt normal clean

all: $(PLUGIN_SO)

help:
	@echo "usage: make -j2|-j4 [FAIL=1] all"

# ---- 批编译目标 A：注意行首的 '+'（Make 4.3 继承 jobserver fd 所必需）----
batch-modbus:
	+$(PYTHON) $(TOOLS)/batchcc.py --group modbus --cc "$(CC)" --cflags "$(PIC_CFLAGS)" --output-dir $(BUILD)/modbus -- $(ALPHA_SRCS)

# ---- 批编译目标 B：同样必须有 '+' ----
batch-mqtt:
	+$(PYTHON) $(TOOLS)/batchcc.py --group mqtt --cc "$(CC)" --cflags "$(PIC_CFLAGS)" --output-dir $(BUILD)/mqtt -- $(BETA_SRCS)

normal: $(LOADER_OBJ)

# ---- 普通 GCC 目标：没有 '+'，就是一条最普通的单文件编译规则 ----
$(LOADER_OBJ): $(ROOT)/src/normal/loader.c $(GEN_STAMP) $(ROOT)/src/plugin_sdk.h
	@mkdir -p $(dir $@)
	$(CC) $(PIC_CFLAGS) -c $< -o $@

# 链接：先构建两个批目标（伪目标）和普通目标；批目标退出时其 .o 已齐全。
$(PLUGIN_SO): batch-modbus batch-mqtt $(LOADER_OBJ)
	$(CC) -shared -fPIC $(ALPHA_OBJS) $(BETA_OBJS) $(LOADER_OBJ) -o $@
	@echo "==> linked $@"

clean:
	rm -rf $(BUILD)
