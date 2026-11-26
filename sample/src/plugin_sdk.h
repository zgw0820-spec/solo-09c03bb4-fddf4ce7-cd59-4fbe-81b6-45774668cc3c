#ifndef COLLECTOR_PLUGIN_SDK_H
#define COLLECTOR_PLUGIN_SDK_H

/* 采集插件演练样例的最小 SDK 声明。 */

const char *collector_plugin_name(void);
int collector_plugin_init(void);

/* 由 normal/loader.c 连同 workload.inc 提供。 */
long loader_work_units(void);
double loader_run_all(double seed);

#endif /* COLLECTOR_PLUGIN_SDK_H */
