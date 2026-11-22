/* Logging and clocks. Every line carries CLOCK_MONOTONIC and CLOCK_BOOTTIME
 * timestamps so presenter instrumentation can be correlated with the kernel,
 * the journal and outer-display captures. */
#pragma once
#include <stdint.h>

uint64_t rb_mono_us(void);
uint64_t rb_mono_ms(void);
uint64_t rb_boot_ms(void);

/* tag: prefix such as "rb-presenter". use_kmsg: write to /dev/kmsg at debug
 * priority (initramfs splash, where stderr is the console). */
void rb_log_init(const char *tag, int use_kmsg);
void rb_log(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
