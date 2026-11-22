#include "log.h"

#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static const char *log_tag = "rb-presenter";
static int kmsg_fd = -1;

static uint64_t clock_us(clockid_t id)
{
    struct timespec ts;
    clock_gettime(id, &ts);
    return (uint64_t)ts.tv_sec * 1000000u + (uint64_t)ts.tv_nsec / 1000u;
}

uint64_t rb_mono_us(void) { return clock_us(CLOCK_MONOTONIC); }
uint64_t rb_mono_ms(void) { return clock_us(CLOCK_MONOTONIC) / 1000u; }
uint64_t rb_boot_ms(void) { return clock_us(CLOCK_BOOTTIME) / 1000u; }

void rb_log_init(const char *tag, int use_kmsg)
{
    if (tag)
        log_tag = tag;
    if (use_kmsg && kmsg_fd < 0)
        kmsg_fd = open("/dev/kmsg", O_WRONLY | O_CLOEXEC | O_NOCTTY);
}

void rb_log(const char *fmt, ...)
{
    char msg[1024];
    char line[1200];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof msg, fmt, ap);
    va_end(ap);
    uint64_t mono = clock_us(CLOCK_MONOTONIC), boot = clock_us(CLOCK_BOOTTIME);
    int n;
    if (kmsg_fd >= 0) {
        /* Priority 7 (debug): never printed on the console at sane loglevels,
         * so the splash cannot trigger an fbcon takeover by logging. */
        n = snprintf(line, sizeof line, "<7>%s: [mono %llu.%03llu boot %llu.%03llu] %s\n", log_tag,
                     (unsigned long long)(mono / 1000000u), (unsigned long long)(mono / 1000u % 1000u),
                     (unsigned long long)(boot / 1000000u), (unsigned long long)(boot / 1000u % 1000u), msg);
        if (n > 0 && write(kmsg_fd, line, (size_t)n < sizeof line ? (size_t)n : sizeof line - 1) >= 0)
            return;
    }
    n = snprintf(line, sizeof line, "%s: [mono %llu.%03llu boot %llu.%03llu] %s\n", log_tag,
                 (unsigned long long)(mono / 1000000u), (unsigned long long)(mono / 1000u % 1000u),
                 (unsigned long long)(boot / 1000000u), (unsigned long long)(boot / 1000u % 1000u), msg);
    if (n > 0) {
        ssize_t r = write(STDERR_FILENO, line, (size_t)n < sizeof line ? (size_t)n : sizeof line - 1);
        (void)r;
    }
}
