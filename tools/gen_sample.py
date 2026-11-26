#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成“采集插件”内置 C 演练样例的重复性工作负载。

产出：
  <target>/alpha/modbus_channel_01..06.c   —— 批编译目标 A（6 个源文件）
  <target>/beta/mqtt_parser_01..06.c        —— 批编译目标 B（6 个源文件）
  <target>/normal/workload.inc              —— 普通 GCC 目标 loader.c 包含它

每个编译单元故意包含数百个带运算的函数，让真实 GCC 进程存活几百毫秒，
这样从 /proc 采样才能稳定观察到并行重叠（并发上限的正确性并不依赖
编译耗时，耗时只为“看得见”）。

其中 modbus_channel_03.c 内置 DRILL_INJECT_FAILURE 开关，网页勾选
“注入编译失败”后 Makefile 会传 -DDRILL_INJECT_FAILURE=1，触发 #error。
"""

from __future__ import annotations

import sys
from pathlib import Path

ALPHA_FILES = [f"modbus_channel_{i:02d}" for i in range(1, 7)]
BETA_FILES = [f"mqtt_parser_{i:02d}" for i in range(1, 7)]

# 本机实测 220 函数 * 10 项表达式在 gcc -O0 下约 0.3~0.5s。
FUNCS_PER_UNIT = 220
EXPR_TERMS = 10
NORMAL_FUNCS = 260

FAILURE_FILE = "modbus_channel_03"


def _unit_source(symbol: str, index: int, n_funcs: int) -> str:
    lines = [
        "/* Auto-generated drill workload. Do not edit; see tools/gen_sample.py. */",
        "#include <math.h>",
        "#include <stddef.h>",
        "",
    ]
    if symbol == FAILURE_FILE:
        lines += [
            "#ifdef DRILL_INJECT_FAILURE",
            '#error "DRILL FAILURE: injected #error in modbus_channel_03 (requested from web page)"',
            "#endif",
            "",
        ]
    for i in range(n_funcs):
        terms = "+".join(f"x*{j + 1}.{(i + index) % 17}" for j in range(EXPR_TERMS))
        lines.append(
            f"double {symbol}_eval_{i:04d}(double x) {{"
            f" double base = sin(x*{index % 5 + 1})*0.37 + sqrt(fabs(x))*0.11;"
            f" return base + ({terms}) * {1 + (i % 7)}; }}"
        )
    lines += [
        "",
        f"long {symbol}_checksum(double x) {{"
        "    long acc = 0;",
        f"    for (int i = 0; i < {n_funcs}; ++i) {{",
        "        acc += (long)(x * (i + 1)) & 0xff;",
        "    }",
        "    return acc;",
        "}",
        "",
        f"int {symbol}_ready(void) {{ return {n_funcs} > 0 ? 1 : 0; }}",
        "",
    ]
    return "\n".join(lines)


def _normal_inc(n_funcs: int) -> str:
    lines = [
        "/* Auto-generated workload included by the *ordinary* GCC target loader.c. */",
        "#ifndef DRILL_NORMAL_WORKLOAD_INC",
        "#define DRILL_NORMAL_WORKLOAD_INC 1",
        "#include <math.h>",
        "",
    ]
    for i in range(n_funcs):
        terms = "+".join(f"v[{j % 8}]*{j + 1}" for j in range(EXPR_TERMS))
        lines.append(
            f"double host_transform_{i:04d}(const double *v) {{"
            f" return cos(v[0])*0.5 + ({terms}) + {i}.25; }}"
        )
    lines += [
        "static long host_work_units(void) { return " + str(n_funcs) + "; }",
        "static double host_run_all(double seed) {",
        "    double v[8] = {1,2,3,4,5,6,7,8}; double total = 0;",
        f"    for (int i = 0; i < {n_funcs}; ++i) {{",
        "        v[i & 7] = seed + i;",
        "    }",
        # 直接顺序引用若干生成函数，保证它们参与编译且不被整体跳过。
        "    total += host_transform_0000(v);",
        f"    total += host_transform_{n_funcs - 1:04d}(v);",
        "    return total;",
        "}",
        "long loader_work_units(void) { return host_work_units(); }",
        "double loader_run_all(double s) { return host_run_all(s); }",
        "#endif",
        "",
    ]
    return "\n".join(lines)


def generate(target_root: Path) -> list[Path]:
    written: list[Path] = []
    alpha = target_root / "alpha"
    beta = target_root / "beta"
    normal = target_root / "normal"
    for d in (alpha, beta, normal):
        d.mkdir(parents=True, exist_ok=True)

    for idx, name in enumerate(ALPHA_FILES):
        p = alpha / f"{name}.c"
        p.write_text(_unit_source(name, idx + 1, FUNCS_PER_UNIT), encoding="utf-8")
        written.append(p)

    for idx, name in enumerate(BETA_FILES):
        p = beta / f"{name}.c"
        p.write_text(_unit_source(name, idx + 11, FUNCS_PER_UNIT), encoding="utf-8")
        written.append(p)

    inc = normal / "workload.inc"
    inc.write_text(_normal_inc(NORMAL_FUNCS), encoding="utf-8")
    written.append(inc)
    return written


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / "sample" / "src"
    files = generate(target)
    print(f"gen_sample: wrote {len(files)} workload files under {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
