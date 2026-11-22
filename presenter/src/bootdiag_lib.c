/* rb-bootdiag: pure helpers (bootdiag.h). No I/O here. */
#include "bootdiag.h"

#include <ctype.h>
#include <dirent.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* ------------------------------------------------------------------------ */
/* kernel log                                                               */

int bd_kmsg_parse(const char *rec, size_t n, struct bd_kmsg *out)
{
    memset(out, 0, sizeof *out);
    const char *semi = memchr(rec, ';', n);
    if (!semi)
        return -1;
    /* header: prefix,seq,ts_us,flags[,caller=...] */
    char head[128];
    size_t hn = (size_t)(semi - rec);
    if (hn >= sizeof head)
        return -1;
    memcpy(head, rec, hn);
    head[hn] = 0;
    char *save = NULL, *f = strtok_r(head, ",", &save);
    if (!f)
        return -1;
    char *end;
    long pri = strtol(f, &end, 10);
    if (end == f)
        return -1;
    out->prio = (int)(pri & 7);
    if ((f = strtok_r(NULL, ",", &save)))
        out->seq = strtoull(f, NULL, 10);
    if ((f = strtok_r(NULL, ",", &save)))
        out->ts_us = strtoull(f, NULL, 10);
    /* text: up to the first newline (continuation lines are " KEY=VALUE") */
    const char *t = semi + 1, *te = rec + n;
    const char *nl = memchr(t, '\n', (size_t)(te - t));
    if (nl)
        te = nl;
    size_t o = 0;
    for (const char *p = t; p < te && o < sizeof out->text - 1; p++) {
        unsigned char c = (unsigned char)*p;
        if (c == '\\' && p + 3 < te && p[1] == 'x' && isxdigit((unsigned char)p[2]) && isxdigit((unsigned char)p[3])) {
            unsigned v = (unsigned)strtoul((char[]){p[2], p[3], 0}, NULL, 16);
            out->text[o++] = v >= 0x20 && v < 0x7f ? (char)v : (v < 0x20 ? ' ' : '?');
            p += 3;
            continue;
        }
        out->text[o++] = c < 0x20 ? ' ' : (char)c;
    }
    while (o > 0 && out->text[o - 1] == ' ')
        o--;
    out->text[o] = 0;
    return 0;
}

static bool has(const char *s, const char *needle) { return strstr(s, needle) != NULL; }

int bd_problem_score(int prio, const char *t)
{
    if (has(t, "retroboot: panic") || has(t, "Kernel panic"))
        return 100;
    if (!strncmp(t, "rb-", 3)) /* RetroBoot's own instrumentation (rb-splash, rb-presenter, rb-bootdiag) */
        return 0;
    /* The display stopped taking new frames (amdgpu on a panel stuck in self
     * refresh: the screen keeps the last frame, often the checkerboard). */
    if (has(t, "flip_done timed out") || has(t, "commit wait timed out") || has(t, "hw_done timed out"))
        return 95;
    if ((has(t, "ring ") && has(t, "timeout")) || has(t, "GPU reset begin") || has(t, "GPU hang"))
        return 92;
    if ((has(t, "dmub") || has(t, "DMUB") || has(t, "PSR") || has(t, "psr") || has(t, "Replay")) &&
        (prio <= 4 || has(t, "rror") || has(t, "imeout") || has(t, "fail")))
        return 88;
    if (has(t, "Direct firmware load for") && has(t, "failed"))
        return 90;
    if (has(t, "Fatal error during GPU init") || has(t, "GPU init failed"))
        return 90;
    if ((has(t, "probe of ") && has(t, "failed with error")) || (has(t, "probe with driver") && has(t, "failed")))
        return 85;
    if (has(t, "failed to load firmware") || has(t, "Failed to load firmware") || has(t, "firmware: failed"))
        return 85;
    if (has(t, "*ERROR*"))
        return 80;
    if (prio <= 3 && (has(t, "amdgpu") || has(t, "i915") || has(t, "xe ") || has(t, "[drm]") || has(t, "nouveau") ||
                      has(t, "radeon") || has(t, "vc4") || has(t, "v3d")))
        return 80;
    /* Raspberry Pi: the supply sags below 4.63 V (raspberrypi-hwmon) */
    if (has(t, "Undervoltage detected") || has(t, "Under-voltage detected"))
        return 72;
    if (has(t, "SQUASHFS error") || (has(t, "overlayfs:") && prio <= 3))
        return 75;
    if (has(t, "segfault at") || has(t, "traps:")) /* a program crashed (not the kernel) */
        return 45;
    if (has(t, "BUG:") || has(t, "Oops") || has(t, "general protection fault") || has(t, "kernel BUG"))
        return 75;
    if (has(t, "WARNING: CPU:"))
        return 70;
    if (has(t, "Call Trace")) /* the line before it says what happened */
        return 20;
    if (has(t, "I/O error") || has(t, "EXT4-fs error") || (has(t, "FAT-fs") && prio <= 3))
        return 60;
    if (prio <= 3) {
        /* firmware tables a laptop boots fine with */
        if (has(t, "ACPI BIOS Error") || has(t, "ACPI Error") || has(t, "AE_NOT_FOUND") || has(t, "AE_ALREADY") ||
            has(t, "ACPI: ") || has(t, "tpm") || has(t, "TPM") || has(t, "ucsi") || has(t, "thermal") ||
            has(t, "Bluetooth: hci"))
            return 5;
        return 40;
    }
    return 0;
}

bool bd_marker(const char *text, char *id, size_t idn, char *detail, size_t detn)
{
    const char *p;
    if (!strncmp(text, "retroboot: stage ", 17)) {
        p = text + 17;
    } else if (!strncmp(text, "retroboot: panic", 16)) {
        snprintf(id, idn, "panic");
        p = text + 16;
        while (*p == ' ' || *p == ':')
            p++;
        snprintf(detail, detn, "%s", p);
        return true;
    } else {
        return false;
    }
    size_t n = strcspn(p, " ");
    if (n == 0 || n >= idn)
        return false;
    memcpy(id, p, n);
    id[n] = 0;
    p += n;
    while (*p == ' ')
        p++;
    snprintf(detail, detn, "%s", p);
    return true;
}

/* ------------------------------------------------------------------------ */
/* stages                                                                   */

static const char *const stage_ids[BD_NSTAGES] = {
    "kernel", "medium-search", "medium-found", "squashfs-mounted", "root-mounted",
    "switch-root", "display-driver", "presenter", "hostd", "ready",
};
static const int stage_pm[BD_NSTAGES] = {50, 100, 250, -1, 350, 450, 550, 650, 800, 1000};

const char *bd_stage_id(int s) { return s >= 0 && s < BD_NSTAGES ? stage_ids[s] : "?"; }
int bd_stage_permille(int s) { return s >= 0 && s < BD_NSTAGES ? stage_pm[s] : -1; }

int bd_stage_from_id(const char *id)
{
    for (int i = 0; i < BD_NSTAGES; i++)
        if (!strcmp(stage_ids[i], id))
            return i;
    return -1;
}

static bool implied(const struct bd_stages *st, int i)
{
    for (int j = i + 1; j < BD_NSTAGES; j++) {
        if (!st->done[j])
            continue;
        if (j == BD_DISPLAY)
            continue; /* a display driver can bind before the medium is found */
        if (i == BD_DISPLAY && j != BD_READY)
            continue; /* the presenter runs without a display (it waits for one) */
        return true;
    }
    return false;
}

int bd_progress(const struct bd_stages *st, int *stage)
{
    int best = -1;
    for (int i = 0; i < BD_NSTAGES; i++) {
        if (stage_pm[i] < 0)
            continue;
        if (!(st->done[i] || st->skipped[i] || implied(st, i)))
            break;
        best = i;
    }
    int pm = best >= 0 ? stage_pm[best] : 0;
    if (pm < st->published) {
        /* keep the stage that was published */
        pm = st->published;
        for (int i = 0; i < BD_NSTAGES; i++)
            if (stage_pm[i] == pm)
                best = i;
    }
    if (stage)
        *stage = best;
    return pm;
}

int bd_progress_line(int permille, int stage, const char *text, char *out, size_t cap)
{
    char t[81];
    size_t n = strlen(text ? text : "");
    if (n > 80) {
        n = 80;
        while (n > 0 && ((unsigned char)text[n] & 0xc0) == 0x80) /* never split a UTF-8 sequence */
            n--;
    }
    memcpy(t, text ? text : "", n);
    t[n] = 0;
    for (char *p = t; *p; p++)
        if (*p == '\n' || *p == '\r')
            *p = ' ';
    if (permille < 0)
        permille = 0;
    if (permille > 1000)
        permille = 1000;
    return snprintf(out, cap, "%d %s %s\n", permille, stage >= 0 ? bd_stage_id(stage) : "kernel", t);
}

/* ------------------------------------------------------------------------ */
/* firmware framebuffer                                                     */

static unsigned u16le(const uint8_t *b, size_t o) { return (unsigned)b[o] | (unsigned)b[o + 1] << 8; }
static uint32_t u32le(const uint8_t *b, size_t o)
{
    return (uint32_t)b[o] | (uint32_t)b[o + 1] << 8 | (uint32_t)b[o + 2] << 16 | (uint32_t)b[o + 3] << 24;
}

int bd_screen_info(const uint8_t *b, size_t n, struct bd_fbinfo *fi)
{
    memset(fi, 0, sizeof *fi);
    if (!b || n < 0x2e)
        return -1;
    fi->type = b[0x0f];
    if (fi->type != 0x23 && fi->type != 0x70) /* VIDEO_TYPE_VLFB, VIDEO_TYPE_EFI */
        return -1;
    fi->w = (int)u16le(b, 0x12);
    fi->h = (int)u16le(b, 0x14);
    fi->depth = (int)u16le(b, 0x16);
    fi->base = u32le(b, 0x18);
    fi->pitch = (int)u16le(b, 0x24);
    fi->rsize = b[0x26];
    fi->rpos = b[0x27];
    fi->gsize = b[0x28];
    fi->gpos = b[0x29];
    fi->bsize = b[0x2a];
    fi->bpos = b[0x2b];
    if (n >= 0x3e && (u32le(b, 0x36) & 2)) /* VIDEO_CAPABILITY_64BIT_BASE */
        fi->base |= (uint64_t)u32le(b, 0x3a) << 32;
    if (fi->w < 320 || fi->h < 200 || !fi->base)
        return -1;
    if (fi->depth != 16 && fi->depth != 24 && fi->depth != 32)
        return -1;
    if (fi->pitch < fi->w * (fi->depth / 8))
        return -1;
    if (!fi->rsize && !fi->gsize && !fi->bsize) { /* no colour layout: the usual one */
        if (fi->depth == 16) {
            fi->rsize = 5, fi->rpos = 11, fi->gsize = 6, fi->gpos = 5, fi->bsize = 5, fi->bpos = 0;
        } else {
            fi->rsize = fi->gsize = fi->bsize = 8;
            fi->rpos = 16, fi->gpos = 8, fi->bpos = 0;
        }
    }
    if (fi->rsize > 8 || fi->gsize > 8 || fi->bsize > 8 || fi->rpos + fi->rsize > fi->depth ||
        fi->gpos + fi->gsize > fi->depth || fi->bpos + fi->bsize > fi->depth)
        return -1;
    return 0;
}

void bd_fb_convert_row(const uint32_t *src, uint8_t *dst, int w, const struct bd_fbinfo *fi)
{
    int bpp = fi->depth / 8;
    if (bpp == 4 && fi->rpos == 16 && fi->gpos == 8 && fi->bpos == 0 && fi->rsize == 8 && fi->gsize == 8 &&
        fi->bsize == 8) {
        memcpy(dst, src, (size_t)w * 4);
        return;
    }
    for (int x = 0; x < w; x++) {
        uint32_t p = src[x];
        uint32_t r = (p >> 16) & 0xff, g = (p >> 8) & 0xff, b = p & 0xff;
        uint32_t v = (r >> (8 - fi->rsize)) << fi->rpos | (g >> (8 - fi->gsize)) << fi->gpos |
                     (b >> (8 - fi->bsize)) << fi->bpos;
        uint8_t *d = dst + (size_t)x * bpp;
        for (int k = 0; k < bpp; k++)
            d[k] = (uint8_t)(v >> (8 * k));
    }
}

void bd_fb_decode_row(const uint8_t *src, uint32_t *dst, int w, const struct bd_fbinfo *fi)
{
    int bpp = fi->depth / 8;
    for (int x = 0; x < w; x++) {
        const uint8_t *s = src + (size_t)x * bpp;
        uint32_t v = 0;
        for (int k = 0; k < bpp; k++)
            v |= (uint32_t)s[k] << (8 * k);
        uint32_t r = (v >> fi->rpos) & ((1u << fi->rsize) - 1), g = (v >> fi->gpos) & ((1u << fi->gsize) - 1),
                 b = (v >> fi->bpos) & ((1u << fi->bsize) - 1);
        /* widen to 8 bits by repeating the top bits */
        r = fi->rsize ? (r << (8 - fi->rsize)) | (r >> (2 * fi->rsize > 8 ? 2 * fi->rsize - 8 : 0)) : 0;
        g = fi->gsize ? (g << (8 - fi->gsize)) | (g >> (2 * fi->gsize > 8 ? 2 * fi->gsize - 8 : 0)) : 0;
        b = fi->bsize ? (b << (8 - fi->bsize)) | (b >> (2 * fi->bsize > 8 ? 2 * fi->bsize - 8 : 0)) : 0;
        dst[x] = (r & 0xff) << 16 | (g & 0xff) << 8 | (b & 0xff);
    }
}

/* ------------------------------------------------------------------------ */
/* reports                                                                  */

void bd_report_name(time_t t, const char *boot_id, char *out, size_t cap)
{
    struct tm tm;
    gmtime_r(&t, &tm);
    char id[9];
    int k = 0;
    for (const char *p = boot_id ? boot_id : ""; *p && k < 8; p++)
        if (isxdigit((unsigned char)*p))
            id[k++] = (char)tolower((unsigned char)*p);
    while (k < 8)
        id[k++] = '0';
    id[8] = 0;
    snprintf(out, cap, "%04d%02d%02d-%02d%02d%02d-%s.txt", tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday, tm.tm_hour,
             tm.tm_min, tm.tm_sec, id);
}

bool bd_report_name_ok(const char *s)
{
    /* 8 digits, '-', 6 digits, '-', 8 hex, ".txt" */
    if (strlen(s) != 28 || strcmp(s + 24, ".txt"))
        return false;
    for (int i = 0; i < 24; i++) {
        char c = s[i];
        if (i == 8 || i == 15) {
            if (c != '-')
                return false;
        } else if (i < 15 ? !isdigit((unsigned char)c) : !isxdigit((unsigned char)c)) {
            return false;
        }
    }
    return true;
}

static int cmp_name(const void *a, const void *b, void *names)
{
    char (*nm)[64] = names;
    return strcmp(nm[*(const int *)a], nm[*(const int *)b]);
}

int bd_prune(char names[][64], int n, int keep, const char *own, int *del)
{
    if (n <= keep)
        return 0;
    int *idx = malloc(sizeof(int) * (size_t)n);
    if (!idx)
        return 0;
    for (int i = 0; i < n; i++)
        idx[i] = i;
    qsort_r(idx, (size_t)n, sizeof(int), cmp_name, names);
    int remove = n - keep, k = 0;
    for (int i = 0; i < n && k < remove; i++) {
        if (own && !strcmp(names[idx[i]], own))
            continue;
        del[k++] = idx[i];
    }
    free(idx);
    return k;
}

int bd_fat_label(const uint8_t *bs, size_t n, char *label, size_t cap)
{
    if (!bs || n < 512 || cap < 12 || bs[510] != 0x55 || bs[511] != 0xaa)
        return -1;
    size_t off;
    if (!memcmp(bs + 0x52, "FAT32", 5) && bs[0x42] == 0x29)
        off = 0x47;
    else if (!memcmp(bs + 0x36, "FAT", 3) && bs[0x26] == 0x29)
        off = 0x2b;
    else
        return -1;
    memcpy(label, bs + off, 11);
    label[11] = 0;
    for (int i = 10; i >= 0 && label[i] == ' '; i--)
        label[i] = 0;
    return 0;
}

/* ------------------------------------------------------------------------ */
/* panels                                                                   */

int bd_edid_ident(const uint8_t *ed, size_t n, char *out, size_t cap)
{
    static const uint8_t magic[8] = {0, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0};
    if (!ed || n < 128 || memcmp(ed, magic, 8) || cap == 0)
        return -1;
    unsigned v = (unsigned)ed[8] << 8 | ed[9];
    char maker[4] = {(char)('@' + ((v >> 10) & 31)), (char)('@' + ((v >> 5) & 31)), (char)('@' + (v & 31)), 0};
    unsigned prod = (unsigned)ed[10] | (unsigned)ed[11] << 8;
    char names[80] = "";
    for (int d = 54; d + 18 <= 126; d += 18) {
        if (ed[d] || ed[d + 1] || ed[d + 2] || (ed[d + 3] != 0xfc && ed[d + 3] != 0xfe))
            continue;
        char t[14];
        int k = 0;
        for (int i = 5; i < 18 && ed[d + i] != 0x0a && ed[d + i]; i++)
            t[k++] = ed[d + i] >= 0x20 && ed[d + i] < 0x7f ? (char)ed[d + i] : '?';
        while (k > 0 && t[k - 1] == ' ')
            k--;
        t[k] = 0;
        if (t[0]) {
            size_t nl = strlen(names);
            snprintf(names + nl, sizeof names - nl, "%s\"%s\"", nl ? " " : "", t);
        }
    }
    snprintf(out, cap, "%s %04x%s%s, %dx%d cm", maker, prod, names[0] ? " " : "", names, ed[21], ed[22]);
    return 0;
}

void bd_dcdebugmask_text(unsigned long m, char *out, size_t cap)
{
    static const struct {
        unsigned long bit;
        const char *name;
    } bits[] = {{0x10, "PSR off"},  {0x200, "PSR-SU off"}, {0x400, "Panel Replay off"},
                {0x800, "IPS off"}, {0x40, "MPO off"},     {0x2, "stutter off"}};
    size_t l = 0;
    if (cap)
        out[0] = 0;
    for (size_t i = 0; i < sizeof bits / sizeof bits[0] && l < cap; i++)
        if (m & bits[i].bit) {
            int k = snprintf(out + l, cap - l, "%s%s", l ? ", " : "", bits[i].name);
            l = k > 0 && l + (size_t)k < cap ? l + (size_t)k : cap - 1;
        }
    if (cap && !out[0])
        snprintf(out, cap, "defaults");
}

/* ------------------------------------------------------------------------ */
/* device-tree boards                                                       */

bool bd_firmware_fbdev(const char *id)
{
    return id && (!strncmp(id, "simple", 6) || !strncmp(id, "BCM2708 FB", 10));
}

int bd_fbdev_info(const struct bd_fbdev *v, struct bd_fbinfo *fi)
{
    memset(fi, 0, sizeof *fi);
    if (!v || v->w < 320 || v->h < 200 || (v->bpp != 16 && v->bpp != 24 && v->bpp != 32) ||
        v->pitch < v->w * (v->bpp / 8) || !v->rlen || !v->glen || !v->blen || v->rlen > 8 || v->glen > 8 ||
        v->blen > 8 || v->roff + v->rlen > v->bpp || v->goff + v->glen > v->bpp || v->boff + v->blen > v->bpp)
        return -1;
    fi->type = BD_FB_FBDEV;
    fi->w = v->w;
    fi->h = v->h;
    fi->depth = v->bpp;
    fi->pitch = v->pitch;
    fi->base = v->base;
    fi->rpos = v->roff;
    fi->rsize = v->rlen;
    fi->gpos = v->goff;
    fi->gsize = v->glen;
    fi->bpos = v->boff;
    fi->bsize = v->blen;
    return 0;
}

int bd_pi_revision(uint32_t code, char *out, size_t cap)
{
    static const struct {
        unsigned type;
        const char *name;
    } types[] = {{0x11, "Raspberry Pi 4 Model B"}, {0x13, "Raspberry Pi 400"},   {0x14, "Compute Module 4"},
                 {0x15, "Compute Module 4S"},      {0x17, "Raspberry Pi 5"},     {0x18, "Compute Module 5"},
                 {0x19, "Raspberry Pi 500"},       {0x1a, "Compute Module 5 Lite"}, {0x0d, "Raspberry Pi 3 Model B+"},
                 {0x08, "Raspberry Pi 3 Model B"}};
    static const char *const socs[] = {"BCM2835", "BCM2836", "BCM2837", "BCM2711", "BCM2712"};
    static const int mem[] = {256, 512, 1024, 2048, 4096, 8192, 16384};
    if (!(code & (1u << 23))) {
        snprintf(out, cap, "revision %06x (old style)", code);
        return -1;
    }
    unsigned type = (code >> 4) & 0xff, soc = (code >> 12) & 0xf, m = (code >> 20) & 0x7, rev = code & 0xf;
    const char *name = NULL;
    for (size_t i = 0; i < sizeof types / sizeof types[0]; i++)
        if (types[i].type == type)
            name = types[i].name;
    int mb = m < sizeof mem / sizeof mem[0] ? mem[m] : -1;
    char model[48], ram[16];
    if (name)
        snprintf(model, sizeof model, "%s rev 1.%u", name, rev);
    else
        snprintf(model, sizeof model, "board type 0x%02x rev 1.%u", type, rev);
    if (mb >= 1024)
        snprintf(ram, sizeof ram, "%d GB", mb / 1024);
    else if (mb > 0)
        snprintf(ram, sizeof ram, "%d MB", mb);
    else
        snprintf(ram, sizeof ram, "? GB");
    snprintf(out, cap, "%s, %s, %s (revision %06x)", model, soc < sizeof socs / sizeof socs[0] ? socs[soc] : "?",
             ram, code);
    return mb;
}

const char *bd_pi_boot_mode(unsigned mode)
{
    switch (mode) {
    case 0x0: return "SD card (detect)";
    case 0x1: return "SD card";
    case 0x2: return "network";
    case 0x3: return "USB device boot (rpiboot)";
    case 0x4: return "USB mass storage";
    case 0x5: return "USB mass storage (BCM2711 USB 2.0 port)";
    case 0x6: return "NVMe";
    case 0x7: return "HTTP";
    default: return NULL;
    }
}

const char *bd_arm_core(unsigned implementer, unsigned part)
{
    if (implementer != 0x41)
        return NULL;
    switch (part) {
    case 0xd03: return "Cortex-A53";
    case 0xd04: return "Cortex-A35";
    case 0xd05: return "Cortex-A55";
    case 0xd07: return "Cortex-A57";
    case 0xd08: return "Cortex-A72";
    case 0xd09: return "Cortex-A73";
    case 0xd0a: return "Cortex-A75";
    case 0xd0b: return "Cortex-A76";
    case 0xd0d: return "Cortex-A77";
    default: return NULL;
    }
}

static bool read_small(const char *path, char *buf, size_t cap)
{
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return false;
    ssize_t n = read(fd, buf, cap - 1);
    close(fd);
    if (n <= 0)
        return false;
    buf[n] = 0;
    while (n > 0 && (buf[n - 1] == '\n' || buf[n - 1] == ' '))
        buf[--n] = 0;
    return true;
}

int bd_usb_storage_unbound(const char *usb_root, char out[][128], int max)
{
    DIR *dir = opendir(usb_root);
    if (!dir)
        return 0;
    int n = 0;
    struct dirent *e;
    while ((e = readdir(dir)) && n < max) {
        const char *colon = strchr(e->d_name, ':');
        if (e->d_name[0] == '.' || !colon)
            continue;
        char path[512], v[128];
        snprintf(path, sizeof path, "%s/%s/bInterfaceClass", usb_root, e->d_name);
        if (!read_small(path, v, sizeof v) || strcmp(v, "08"))
            continue;
        snprintf(path, sizeof path, "%s/%s/driver", usb_root, e->d_name);
        if (access(path, F_OK) == 0)
            continue;
        char dev[64], vid[16] = "?", pid[16] = "?", maker[48] = "", product[64] = "";
        size_t dl = (size_t)(colon - e->d_name) < sizeof dev - 1 ? (size_t)(colon - e->d_name) : sizeof dev - 1;
        memcpy(dev, e->d_name, dl);
        dev[dl] = 0;
        snprintf(path, sizeof path, "%s/%s/idVendor", usb_root, dev);
        read_small(path, vid, sizeof vid);
        snprintf(path, sizeof path, "%s/%s/idProduct", usb_root, dev);
        read_small(path, pid, sizeof pid);
        snprintf(path, sizeof path, "%s/%s/manufacturer", usb_root, dev);
        read_small(path, maker, sizeof maker);
        snprintf(path, sizeof path, "%s/%s/product", usb_root, dev);
        read_small(path, product, sizeof product);
        bool dup = false;
        for (int i = 0; i < n; i++)
            if (!strncmp(out[i], dev, dl) && out[i][dl] == ' ')
                dup = true; /* one line per device (a device may have two storage interfaces) */
        if (dup)
            continue;
        snprintf(out[n++], 128, "%s %s:%s%s%s%s%s", dev, vid, pid, maker[0] ? " " : "", maker, product[0] ? " " : "",
                 product);
    }
    closedir(dir);
    return n;
}

/* ------------------------------------------------------------------------ */
/* text                                                                     */

void bd_fmt_secs(uint64_t ms, char *out, size_t cap)
{
    if (ms >= 600000)
        snprintf(out, cap, "%llu s", (unsigned long long)(ms / 1000));
    else
        snprintf(out, cap, "%llu.%llu s", (unsigned long long)(ms / 1000), (unsigned long long)(ms % 1000 / 100));
}

const char *bd_pci_vendor(unsigned v)
{
    switch (v) {
    case 0x1002:
    case 0x1022: return "AMD";
    case 0x8086: return "Intel";
    case 0x10de: return "NVIDIA";
    case 0x1234: return "QEMU standard VGA";
    case 0x1af4: return "Virtio";
    case 0x1b36: return "QEMU";
    case 0x15ad: return "VMware";
    case 0x1414: return "Microsoft Hyper-V";
    case 0x80ee: return "VirtualBox";
    case 0x1a03: return "ASPEED";
    case 0x102b: return "Matrox";
    case 0x5333: return "S3";
    default: return NULL;
    }
}
