/*
 * assets.c - Built-in Platinum controls and optional guest assets.
 *
 * rbui loads the image's Platinum controls alongside its own fonts and icons.
 * A bundle from the user's Mac OS disk can supply these assets at runtime.
 * Formats: platinum/rbplatinum/rbfont.py (.rbf),
 * bundle.py rbi() (.rbi) and manifest.json (sprite slices, palette).
 */
#include "rbui_internal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "cJSON.h"

#ifndef RBUI_ASSETS_DEFAULT
#define RBUI_ASSETS_DEFAULT "/var/lib/retroboot/assets/platinum"
#endif
#ifndef RBUI_BUILTIN_ASSETS
#define RBUI_BUILTIN_ASSETS "/usr/lib/retroboot/platinum-theme"
#endif

/* rbui font cname -> bundle font file */
static const struct { const char *name, *file; } kFonts[] = {
    {"system12", "fonts/charcoal-12.rbf"},
    {"regular12", "fonts/geneva-12.rbf"},
    {"small10", "fonts/geneva-10.rbf"},
    {"small10b", "fonts/geneva-10-bold.rbf"},   /* About This Computer's labels ("facts") */
    {"small9", "fonts/geneva-9.rbf"},           /* Monitors' bevel button labels */
    {"large24", "fonts/charcoal-24.rbf"},   /* the large style (pairing codes), else system12 at 2x */
};
#define NFONTS (int)(sizeof(kFonts) / sizeof(kFonts[0]))

/* rbui icon name -> bundle icon name (icons without a counterpart keep the original art) */
static const struct { const char *name, *bundle; } kIcons[] = {
    {"mac", "overview"}, {"disk", "disk"}, {"cd", "cd"}, {"folder", "folder"}, {"network", "network"},
    {"web", "network"}, {"wrench", "general"}, {"stop", "stop"}, {"caution", "caution"}, {"note", "note"},
    {"display", "display"}, {"sound", "sound"}, {"power", "recovery"},
    {"lock", "lock"},   /* 16 px only: the Keychain control strip's padlock (rbplatinum 3) */
    /* The RetroBoot window's panes: the matching control panels (update, datetime: rbplatinum 4) */
    {"startup", "machine"}, {"profiler", "diagnostics"}, {"update", "update"}, {"clock", "datetime"},
    {"printer", "printer"},
    {"chip", "memory"},   /* the computer's processors (About This Computer's meters): Memory's chip */
    /* Display & Sound (rbplatinum 6): Monitors' bevel buttons (40 px) and its display (64 px,
     * as "display"), the Keyboard control panel, Sound's volume slider ends. */
    {"monitor_page", "monitor-page"}, {"sound_page", "sound-page"}, {"keyboard", "keyboard"},
    {"speaker_low", "speaker-low"}, {"speaker_high", "speaker-high"},
};
#define NICONS (int)(sizeof(kIcons) / sizeof(kIcons[0]))

/* Control sprites platinum.c draws (bundle chrome names). */
static const char *const kSprites[] = {
    "movable", "alert", "button-normal", "button-pressed", "button-disabled", "button-default",
    "button-default-pressed", "checkbox-off", "checkbox-on", "checkbox-off-pressed", "checkbox-on-pressed",
    "checkbox-off-disabled", "checkbox-on-disabled", "radio-off", "radio-on", "radio-off-pressed",
    "radio-on-pressed", "radio-off-disabled", "radio-on-disabled", "popup", "popup-disabled", "field",
    "field-focus", "group", "progress", "progress-fill", "scroll-track-v", "scroll-thumb-v", "scroll-grip-v",
    "scroll-up", "scroll-down", "menu",
};
#define NSPRITES (int)(sizeof(kSprites) / sizeof(kSprites[0]))

static rb_sprite g_sprites[NSPRITES];
static int g_sprite_ok[NSPRITES];
static uint32_t g_ramp[8], g_highlight;
static int g_ramp_ok;

static rbf_font g_fonts[NFONTS];
static int g_font_ok[NFONTS];
static const int kIconSizes[] = {16, 32, 40, 64};
#define NSIZES (int)(sizeof(kIconSizes) / sizeof(kIconSizes[0]))
static rbi_icon g_icons[NICONS * NSIZES];
static int g_icon_n;
static int g_tried;
static int g_use_builtin;
/* The loaded bundle's folder and its manifest's identity, to notice hostd's rebuild (a newer
 * rbplatinum swaps a new bundle in while the panel runs): assets_refresh. */
static char g_dir[512];
static long long g_stamp;

static long long manifest_stamp(const char *dir)
{
    char path[600];
    struct stat st;
    if (snprintf(path, sizeof(path), "%s/manifest.json", dir) >= (int)sizeof(path) || stat(path, &st) != 0) return 0;
    return (long long)st.st_mtime * 1000003LL + (long long)st.st_size * 31LL + (long long)st.st_ino;
}

/* A slot is reused when the bundle is loaded again (tests, or a changed bundle): free what
 * the previous load allocated first. The slots themselves stay valid for live rbui objects. */
static void drop_font(rbf_font *f)
{
    free((void *)f->glyphs);
    free((void *)f->bits);
    memset(f, 0, sizeof(*f));
}

static void drop_icon(rbi_icon *ic)
{
    free((void *)ic->argb);
    memset(ic, 0, sizeof(*ic));
}

static void drop_sprite(rb_sprite *sp)
{
    free((void *)sp->argb);
    memset(sp, 0, sizeof(*sp));
}

static unsigned char *slurp(const char *dir, const char *rel, size_t *len)
{
    char path[1024];
    if (snprintf(path, sizeof(path), "%s/%s", dir, rel) >= (int)sizeof(path)) return NULL;
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    unsigned char *buf = NULL;
    if (fseek(f, 0, SEEK_END) == 0) {
        long n = ftell(f);
        if (n > 0 && n < (8L << 20) && fseek(f, 0, SEEK_SET) == 0) {
            buf = malloc((size_t)n);
            if (buf && fread(buf, 1, (size_t)n, f) != (size_t)n) {
                free(buf);
                buf = NULL;
            }
            if (buf) *len = (size_t)n;
        }
    }
    fclose(f);
    return buf;
}

static unsigned u16le(const unsigned char *p) { return (unsigned)p[0] | (unsigned)p[1] << 8; }
static uint32_t u32le(const unsigned char *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

/* Parse an .rbf strike; the buffer is kept as the font's bits. */
static int load_font(rbf_font *out, const char *name, unsigned char *b, size_t len)
{
    if (len < 14 || memcmp(b, "RBF1", 4) != 0) return -1;
    int ascent = b[5], descent = b[6], count = (int)u16le(b + 12);
    if (count <= 0 || count > 4096) return -1;
    rbf_glyph *gl = calloc((size_t)count, sizeof(*gl));
    if (!gl) return -1;
    size_t pos = 14;
    int cap = 0, xh = 0;
    for (int i = 0; i < count; ++i) {
        if (pos + 9 > len) goto bad;
        const unsigned char *p = b + pos;
        gl[i].cp = u32le(p);
        gl[i].adv = (uint8_t)(int8_t)p[4];
        gl[i].xoff = (int8_t)p[5];
        gl[i].top = (int8_t)(-(int8_t)p[6]);
        gl[i].w = p[7];
        gl[i].h = p[8];
        gl[i].off = (uint32_t)(pos + 9);
        pos += 9 + (size_t)gl[i].w * gl[i].h;
        if (pos > len || (i && gl[i].cp <= gl[i - 1].cp)) goto bad;
        if (gl[i].cp == 'H') cap = -gl[i].top;
        if (gl[i].cp == 'x') xh = -gl[i].top;
    }
    out->name = name;
    out->ascent = ascent;
    out->descent = descent;
    out->cap = cap ? cap : ascent * 3 / 4;
    out->xheight = xh ? xh : out->cap * 2 / 3;
    out->count = count;
    out->glyphs = gl;
    out->bits = b;
    out->levels = 1;
    return 0;
bad:
    free(gl);
    return -1;
}

static int load_icon(rbi_icon *out, const char *name, unsigned char *b, size_t len)
{
    if (len < 8 || memcmp(b, "RBI1", 4) != 0) return -1;
    int w = (int)u16le(b + 4), h = (int)u16le(b + 6);
    if (w != h || (w != 16 && w != 32 && w != 40 && w != 64) || len < 8 + (size_t)w * h * 4) return -1;
    uint32_t *argb = malloc((size_t)w * h * sizeof(uint32_t));
    if (!argb) return -1;
    for (int i = 0; i < w * h; ++i) {
        const unsigned char *p = b + 8 + i * 4;
        argb[i] = (uint32_t)p[3] << 24 | (uint32_t)p[0] << 16 | (uint32_t)p[1] << 8 | p[2];
    }
    memset(out, 0, sizeof(*out));
    out->name = name;
    out->size = w;
    out->argb = argb;
    return 0;
}

/* An .rbi of any size (control sprites). */
static int load_sprite(rb_sprite *out, const char *name, const unsigned char *b, size_t len)
{
    if (len < 8 || memcmp(b, "RBI1", 4) != 0) return -1;
    int w = (int)u16le(b + 4), h = (int)u16le(b + 6);
    if (w < 1 || h < 1 || w > 512 || h > 512 || len < 8 + (size_t)w * h * 4) return -1;
    uint32_t *argb = malloc((size_t)w * h * sizeof(uint32_t));
    if (!argb) return -1;
    for (int i = 0; i < w * h; ++i) {
        const unsigned char *p = b + 8 + i * 4;
        argb[i] = (uint32_t)p[3] << 24 | (uint32_t)p[0] << 16 | (uint32_t)p[1] << 8 | p[2];
    }
    out->name = name;
    out->w = w;
    out->h = h;
    out->argb = argb;
    return 0;
}

static int parse_hex(const char *s, uint32_t *out)
{
    if (!s || s[0] != '#' || strlen(s) != 7) return -1;
    char *end = NULL;
    unsigned long v = strtoul(s + 1, &end, 16);
    if (!end || *end) return -1;
    *out = (uint32_t)v;
    return 0;
}

/* Sprites need manifest.json: their slice widths and the variation colours. */
static int load_chrome(const char *dir)
{
    size_t len = 0;
    unsigned char *mb = slurp(dir, "manifest.json", &len);
    if (!mb) return 0;
    cJSON *man = cJSON_ParseWithLength((const char *)mb, len);
    free(mb);
    if (!man) return 0;
    int n = 0;
    const cJSON *pal = cJSON_GetObjectItemCaseSensitive(man, "palette");
    const cJSON *ramp = pal ? cJSON_GetObjectItemCaseSensitive(pal, "ramp") : NULL;
    const cJSON *hi = pal ? cJSON_GetObjectItemCaseSensitive(pal, "highlight") : NULL;
    uint32_t colors[8], highlight;
    if (cJSON_IsArray(ramp) && cJSON_GetArraySize(ramp) == 8 && cJSON_IsString(hi) &&
        parse_hex(hi->valuestring, &highlight) == 0) {
        int valid = 1;
        for (int i = 0; i < 8; ++i) {
            const cJSON *c = cJSON_GetArrayItem(ramp, i);
            if (!cJSON_IsString(c) || parse_hex(c->valuestring, &colors[i]) != 0) {
                valid = 0;
                break;
            }
        }
        if (valid) {
            memcpy(g_ramp, colors, sizeof(g_ramp));
            g_highlight = highlight;
            g_ramp_ok = 1;
        }
    }
    const cJSON *chrome = cJSON_GetObjectItemCaseSensitive(man, "chrome");
    for (int i = 0; chrome && i < NSPRITES; ++i) {
        const cJSON *e = cJSON_GetObjectItemCaseSensitive(chrome, kSprites[i]);
        const cJSON *file = e ? cJSON_GetObjectItemCaseSensitive(e, "rbi") : NULL;
        if (!cJSON_IsString(file) || strstr(file->valuestring, "..")) continue;
        size_t blen = 0;
        unsigned char *b = slurp(dir, file->valuestring, &blen);
        if (!b) continue;
        rb_sprite sp;
        memset(&sp, 0, sizeof(sp));
        if (load_sprite(&sp, kSprites[i], b, blen) == 0) {
            const cJSON *sl = cJSON_GetObjectItemCaseSensitive(e, "slice");
            if (cJSON_IsArray(sl) && cJSON_GetArraySize(sl) == 4)
                for (int k = 0; k < 4; ++k) {
                    const cJSON *v = cJSON_GetArrayItem(sl, k);
                    int x = cJSON_IsNumber(v) ? v->valueint : 0;
                    sp.slice[k] = x < 0 ? 0 : x;
                }
            drop_sprite(&g_sprites[i]);
            g_sprites[i] = sp;
            g_sprite_ok[i] = 1;
            ++n;
        }
        free(b);
    }
    cJSON_Delete(man);
    return n;
}

static int load_assets(const char *dir)
{
    int n = 0;
    g_tried = 1;
    if (g_use_builtin) load_chrome(RBUI_BUILTIN_ASSETS);
    if (!dir || !*dir) return 0;
    if (dir != g_dir) snprintf(g_dir, sizeof(g_dir), "%s", dir);
    g_stamp = manifest_stamp(dir);
    for (int i = 0; i < NFONTS; ++i) {
        size_t len = 0;
        unsigned char *b = slurp(dir, kFonts[i].file, &len);
        if (!b) continue;
        rbf_font f;
        memset(&f, 0, sizeof(f));
        if (load_font(&f, kFonts[i].name, b, len) == 0) {
            drop_font(&g_fonts[i]);
            g_fonts[i] = f;
            g_font_ok[i] = 1;
            ++n;
        } else {
            free(b);
        }
    }
    for (int i = 0; i < NICONS; ++i)
        for (int k = 0; k < NSIZES; ++k) {
            int s = kIconSizes[k];
            char rel[128];
            snprintf(rel, sizeof(rel), "icons/%s-%d.rbi", kIcons[i].bundle, s);
            size_t len = 0;
            unsigned char *b = slurp(dir, rel, &len);
            if (!b) continue;
            rbi_icon ic;
            memset(&ic, 0, sizeof(ic));
            if (g_icon_n < NICONS * NSIZES && load_icon(&ic, kIcons[i].name, b, len) == 0) {
                drop_icon(&g_icons[g_icon_n]);
                g_icons[g_icon_n] = ic;
                ++g_icon_n;
                ++n;
            }
            free(b);
        }
    n += load_chrome(dir);
    pt_theme_update();
    return n;
}

int rbui_load_assets(const char *dir)
{
    g_use_builtin = 0;
    return load_assets(dir);
}

/* Tests: forget loaded assets. The memory stays with its slot until the next load reuses
 * it (an rbui object may still point at a slot). */
void assets_reset(void)
{
    memset(g_font_ok, 0, sizeof(g_font_ok));
    memset(g_sprite_ok, 0, sizeof(g_sprite_ok));
    g_ramp_ok = 0;
    g_icon_n = 0;
    g_tried = 0;
    pt_theme_update();
}

int assets_refresh(void)
{
    if (!g_tried || !g_dir[0]) return 0;
    long long now = manifest_stamp(g_dir);
    if (now == g_stamp) return 0;
    assets_reset();
    load_assets(g_dir);
    return 1;
}

void assets_auto(void)
{
    if (g_tried) return;
    const char *env = getenv("RBUI_ASSETS");
    g_use_builtin = env == NULL;
    load_assets(env ? env : RBUI_ASSETS_DEFAULT);
}

const rbf_font *assets_font(const char *name)
{
    for (int i = 0; i < NFONTS; ++i)
        if (g_font_ok[i] && strcmp(kFonts[i].name, name) == 0) return &g_fonts[i];
    return NULL;
}

const rbi_icon *assets_icon(const char *name, int size)
{
    for (int i = 0; i < g_icon_n; ++i)
        if (g_icons[i].size == size && strcmp(g_icons[i].name, name) == 0) return &g_icons[i];
    return NULL;
}

const rb_sprite *assets_sprite(const char *name)
{
    for (int i = 0; i < NSPRITES; ++i)
        if (g_sprite_ok[i] && strcmp(kSprites[i], name) == 0) return &g_sprites[i];
    return NULL;
}

int assets_ramp(uint32_t ramp[8], uint32_t *highlight)
{
    if (!g_ramp_ok) return 0;
    memcpy(ramp, g_ramp, sizeof(g_ramp));
    *highlight = g_highlight;
    return 1;
}
