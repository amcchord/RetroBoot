#include "rotation.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int rb_rotation_norm(int degrees)
{
    if (degrees % 90)
        return -1;
    int r = degrees % 360;
    return r < 0 ? r + 360 : r;
}

int rb_rotation_add(int a, int b)
{
    int r = rb_rotation_norm(a + b);
    return r < 0 ? 0 : r;
}

int rb_cmdline_rotation(const char *cmdline)
{
    static const char key[] = "retroboot.rotation=";
    const size_t kn = sizeof key - 1;
    int found = 0;
    const char *p = cmdline;
    while (p && *p) {
        while (*p == ' ' || *p == '\t' || *p == '\n')
            p++;
        const char *end = p;
        while (*end && *end != ' ' && *end != '\t' && *end != '\n')
            end++;
        size_t n = (size_t)(end - p);
        if (n > kn && n <= kn + 3 && !strncmp(p, key, kn)) {
            char v[4] = {0};
            memcpy(v, p + kn, n - kn);
            found = 0; /* the last occurrence wins, as for other kernel parameters */
            if (!strcmp(v, "90") || !strcmp(v, "180") || !strcmp(v, "270"))
                found = atoi(v);
        }
        p = end;
    }
    return found;
}

int rb_boot_rotation(const char *path)
{
    char buf[4096];
    FILE *f = fopen(path ? path : "/proc/cmdline", "re");
    if (!f)
        return 0;
    size_t n = fread(buf, 1, sizeof buf - 1, f);
    fclose(f);
    buf[n] = 0;
    return rb_cmdline_rotation(buf);
}

int rb_hyperpixel_panel_rotation(const char *root)
{
    char path[1024], compatible[128];
    snprintf(path, sizeof path, "%s/spi/display@0/compatible", root ? root : "/proc/device-tree");
    FILE *f = fopen(path, "re");
    if (!f)
        return -1;
    size_t n = fread(compatible, 1, sizeof compatible, f);
    fclose(f);
    const char known[] = "pimoroni,hyperpixel4";
    bool found = false;
    /* Compatible is a bounded NUL-separated list, not a substring match. */
    for (size_t i = 0; i < n;) {
        const char *end = memchr(compatible + i, 0, n - i);
        if (!end)
            break;
        size_t len = (size_t)(end - (compatible + i));
        if (len == sizeof known - 1 && !memcmp(compatible + i, known, len))
            found = true;
        i += len + 1;
    }
    if (!found)
        return -1;
    snprintf(path, sizeof path, "%s/spi/display@0/rotation", root ? root : "/proc/device-tree");
    f = fopen(path, "re");
    if (!f)
        return -1;
    unsigned char bytes[5];
    n = fread(bytes, 1, sizeof bytes, f);
    fclose(f);
    if (n != 4 || bytes[0] || bytes[1])
        return -1;
    unsigned value = (unsigned)bytes[2] * 256u + bytes[3];
    return value == 0 || value == 90 || value == 180 || value == 270 ? (int)value : -1;
}
