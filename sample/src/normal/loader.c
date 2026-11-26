/*
 * loader.c —— 采集插件宿主装载器。
 *
 * 这是样例里的“普通 GCC 目标”：sample/Makefile 用一条普通模式/显式
 * 规则直接 $(CC) -c 编译它，不经过 batchcc，也不主动接触 jobserver。
 * 它只占用 make 分给该配方的那一个 -j 名额。
 *
 * 为了让演练时 /proc 采样能观察到与两个批目标的时间重叠，它 include
 * 了 gen_sample.py 生成的 workload.inc（数百个数值函数）。
 */

#include <stdio.h>

#include "workload.inc"
#include "plugin_sdk.h"

const char *collector_plugin_name(void) {
    return "sensor-collector-drill";
}

int collector_plugin_init(void) {
    double score = loader_run_all(3.14159);
    long units = loader_work_units();
    if (units <= 0) {
        return 1;
    }
    printf("[loader] %s initialized, workload units=%ld score=%.3f\n",
           collector_plugin_name(), units, score);
    return 0;
}

int main(void) {
    return collector_plugin_init() == 0 ? 0 : 2;
}
