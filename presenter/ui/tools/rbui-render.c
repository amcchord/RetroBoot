/*
 * rbui-render - render a UI document to a PNG for review and tests
 *
 *   rbui-render DOC.json|builtin:NAME OUT.png [options]
 *
 * Options are applied in the order given, so input can be scripted:
 *   --size WxH            logical canvas (default 960x540)
 *   --scale N             integer output magnification (default 1)
 *   --background B        checkerboard (default) | black | dimmed
 *   --focus ID            move keyboard focus to a widget
 *   --pointer X,Y[,down]  move the pointer (and press or release the button)
 *   --keys K1,K2,...      keys: tab shift+tab return enter escape backspace
 *                         delete left right up down home end pageup pagedown
 *                         space, modifiers shift+ cmd+ ctrl+ opt+, a single
 *                         character, or text=STRING (rest of the token)
 *   --tick MS             advance the clock by MS milliseconds
 *   --doc FILE            replace the document (state preserved by id)
 *   --crop X,Y,W,H        write only this logical rectangle (before --scale)
 *   --cursor / --no-cursor  draw the arrow cursor (default: only after --pointer)
 *   --events              print emitted events to stdout
 *   --describe            print rbui_describe() JSON to stdout at the end
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "rbui.h"

static uint64_t now_ms = 1000;
static int print_events;

static void on_event(void *ctx, const char *json)
{
    (void)ctx;
    if (print_events) printf("%s\n", json);
}

static char *read_file(const char *path, size_t *len)
{
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    size_t cap = 4096, n = 0;
    char *buf = malloc(cap);
    while (buf) {
        size_t got = fread(buf + n, 1, cap - n, f);
        n += got;
        if (got == 0) break;
        if (n == cap) {
            char *nb = realloc(buf, cap * 2);
            if (!nb) {
                free(buf);
                buf = NULL;
                break;
            }
            buf = nb;
            cap *= 2;
        }
    }
    fclose(f);
    if (buf) *len = n;
    return buf;
}

static char *load_doc(const char *spec, size_t *len)
{
    if (strncmp(spec, "builtin:", 8) == 0) {
        const char *d = rbui_builtin_doc(spec + 8);
        if (!d) return NULL;
        *len = strlen(d);
        return strdup(d);
    }
    return read_file(spec, len);
}

static int set_doc(rbui *ui, const char *spec)
{
    size_t len = 0;
    char *json = load_doc(spec, &len);
    if (!json) {
        fprintf(stderr, "rbui-render: cannot read %s\n", spec);
        return -1;
    }
    char err[256];
    int rc = rbui_set_doc(ui, json, len, err, sizeof(err));
    free(json);
    if (rc) fprintf(stderr, "rbui-render: %s: %s\n", spec, err);
    return rc;
}

static int send_key(rbui *ui, const char *tok)
{
    unsigned mods = 0;
    for (;;) {
        if (strncmp(tok, "shift+", 6) == 0) { mods |= RBUI_MOD_SHIFT; tok += 6; }
        else if (strncmp(tok, "cmd+", 4) == 0) { mods |= RBUI_MOD_COMMAND; tok += 4; }
        else if (strncmp(tok, "ctrl+", 5) == 0) { mods |= RBUI_MOD_CTRL; tok += 5; }
        else if (strncmp(tok, "opt+", 4) == 0) { mods |= RBUI_MOD_OPTION; tok += 4; }
        else break;
    }
    static const struct { const char *name; int key; } keys[] = {
        {"tab", RBUI_KEY_TAB}, {"return", RBUI_KEY_RETURN}, {"enter", RBUI_KEY_KP_ENTER},
        {"escape", RBUI_KEY_ESCAPE}, {"esc", RBUI_KEY_ESCAPE}, {"backspace", RBUI_KEY_BACKSPACE},
        {"delete", RBUI_KEY_DELETE}, {"left", RBUI_KEY_LEFT}, {"right", RBUI_KEY_RIGHT},
        {"up", RBUI_KEY_UP}, {"down", RBUI_KEY_DOWN}, {"home", RBUI_KEY_HOME}, {"end", RBUI_KEY_END},
        {"pageup", RBUI_KEY_PAGEUP}, {"pagedown", RBUI_KEY_PAGEDOWN}, {"space", RBUI_KEY_SPACE},
    };
    if (strncmp(tok, "text=", 5) == 0) {
        /* One rbui_key per code point, as a keyboard would deliver it. */
        const char *s = tok + 5;
        while (*s) {
            char buf[8] = {0};
            int n = 1;
            unsigned char c = (unsigned char)*s;
            if (c >= 0xF0) n = 4;
            else if (c >= 0xE0) n = 3;
            else if (c >= 0xC0) n = 2;
            for (int i = 0; i < n && s[i]; ++i) buf[i] = s[i];
            rbui_key(ui, 0, buf, 1, mods);
            rbui_key(ui, 0, buf, 0, mods);
            s += strlen(buf);
        }
        return 0;
    }
    for (size_t i = 0; i < sizeof(keys) / sizeof(keys[0]); ++i)
        if (strcmp(tok, keys[i].name) == 0) {
            const char *text = keys[i].key == RBUI_KEY_SPACE ? " " : NULL;
            rbui_key(ui, keys[i].key, text, 1, mods);
            rbui_key(ui, keys[i].key, text, 0, mods);
            return 0;
        }
    if (tok[0] && !tok[1]) {
        rbui_key(ui, 0, tok, 1, mods);
        rbui_key(ui, 0, tok, 0, mods);
        return 0;
    }
    fprintf(stderr, "rbui-render: unknown key \"%s\"\n", tok);
    return -1;
}

static void fake_guest(uint32_t *px, int w, int h)
{
    /* A stand-in for a frozen guest frame: periwinkle desktop, menu bar and
     * a white window, so --background dimmed has something to dim. */
    for (int y = 0; y < h; ++y)
        for (int x = 0; x < w; ++x) {
            uint32_t c = ((x / 4 + y / 4) & 7) == 0 ? 0x8C8CD6u : 0x9C9CE6u;
            if (y < 20) c = y == 19 ? 0x000000u : 0xEEEEEEu;
            if (x > w / 8 && x < w / 2 && y > h / 5 && y < h * 3 / 5) c = 0xFFFFFFu;
            px[(size_t)y * (size_t)w + (size_t)x] = c;
        }
}

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "usage: rbui-render DOC.json|builtin:NAME OUT.png [--size WxH] [--scale N]\n"
                        "       [--background checkerboard|black|dimmed] [--focus ID] [--pointer X,Y[,down]]\n"
                        "       [--keys K,...] [--tick MS] [--doc FILE] [--cursor|--no-cursor] [--events] [--describe]\n");
        return 2;
    }
    const char *doc = argv[1], *out = argv[2];
    int w = 960, h = 540, scale = 1, bg = RBUI_BG_CHECKERBOARD, cursor = 0, describe = 0;
    int cx = 0, cy = 0, cw = 0, ch = 0;
    /* First pass: canvas options that must precede the document. */
    for (int i = 3; i < argc; ++i) {
        if (strcmp(argv[i], "--size") == 0 && i + 1 < argc) {
            if (sscanf(argv[++i], "%dx%d", &w, &h) != 2 || w < 16 || h < 16 || w > 8192 || h > 8192) {
                fprintf(stderr, "rbui-render: bad --size\n");
                return 2;
            }
        } else if (strcmp(argv[i], "--events") == 0) {
            print_events = 1;
        }
    }
    rbui *ui = rbui_new();
    if (!ui) return 1;
    rbui_set_emitter(ui, on_event, NULL);
    rbui_resize(ui, w, h);
    rbui_tick(ui, now_ms);
    if (set_doc(ui, doc)) return 1;
    for (int i = 3; i < argc; ++i) {
        const char *a = argv[i];
        const char *v = i + 1 < argc ? argv[i + 1] : NULL;
        if (strcmp(a, "--size") == 0) {
            ++i;
        } else if (strcmp(a, "--events") == 0) {
        } else if (strcmp(a, "--scale") == 0 && v) {
            scale = atoi(v);
            if (scale < 1 || scale > 8) scale = 1;
            ++i;
        } else if (strcmp(a, "--background") == 0 && v) {
            bg = strcmp(v, "black") == 0 ? RBUI_BG_BLACK : strcmp(v, "dimmed") == 0 ? RBUI_BG_IMAGE_DIMMED : RBUI_BG_CHECKERBOARD;
            ++i;
        } else if (strcmp(a, "--focus") == 0 && v) {
            if (rbui_focus(ui, v)) fprintf(stderr, "rbui-render: no focusable widget \"%s\"\n", v);
            ++i;
        } else if (strcmp(a, "--pointer") == 0 && v) {
            int x = 0, y = 0;
            char state[16] = "";
            if (sscanf(v, "%d,%d,%15s", &x, &y, state) < 2) {
                fprintf(stderr, "rbui-render: bad --pointer\n");
                return 2;
            }
            rbui_pointer(ui, x, y, strcmp(state, "down") == 0 ? 1u : 0u);
            cursor = 1;
            ++i;
        } else if (strcmp(a, "--keys") == 0 && v) {
            char *copy = strdup(v), *save = NULL;
            for (char *t = strtok_r(copy, ",", &save); t; t = strtok_r(NULL, ",", &save))
                if (send_key(ui, t)) return 2;
            free(copy);
            ++i;
        } else if (strcmp(a, "--tick") == 0 && v) {
            long ms = atol(v);
            for (long t = 0; t < ms; t += 10) rbui_tick(ui, now_ms += 10);
            ++i;
        } else if (strcmp(a, "--doc") == 0 && v) {
            if (set_doc(ui, v)) return 1;
            ++i;
        } else if (strcmp(a, "--crop") == 0 && v) {
            if (sscanf(v, "%d,%d,%d,%d", &cx, &cy, &cw, &ch) != 4) {
                fprintf(stderr, "rbui-render: bad --crop\n");
                return 2;
            }
            ++i;
        } else if (strcmp(a, "--cursor") == 0) {
            cursor = 1;
        } else if (strcmp(a, "--no-cursor") == 0) {
            cursor = 0;
        } else if (strcmp(a, "--describe") == 0) {
            describe = 1;
        } else {
            fprintf(stderr, "rbui-render: unknown option %s\n", a);
            return 2;
        }
    }
    uint32_t *frame = malloc((size_t)w * (size_t)h * 4), *guest = NULL;
    if (!frame) return 1;
    if (bg == RBUI_BG_IMAGE_DIMMED) {
        guest = malloc((size_t)w * (size_t)h * 4);
        if (guest) fake_guest(guest, w, h);
    }
    rbui_render(ui, frame, w, bg, guest, w, cursor);
    int rc;
    if (cw <= 0 || ch <= 0 || cx < 0 || cy < 0 || cx + cw > w || cy + ch > h) {
        cx = cy = 0;
        cw = w;
        ch = h;
    }
    {
        uint32_t *big = malloc((size_t)cw * scale * (size_t)ch * scale * 4);
        if (!big) return 1;
        for (int y = 0; y < ch * scale; ++y)
            for (int x = 0; x < cw * scale; ++x)
                big[(size_t)y * (size_t)(cw * scale) + (size_t)x] =
                    frame[(size_t)(cy + y / scale) * (size_t)w + (size_t)(cx + x / scale)];
        rc = rbui_write_png(out, big, cw * scale, ch * scale, cw * scale);
        free(big);
    }
    if (rc) fprintf(stderr, "rbui-render: cannot write %s\n", out);
    if (describe) {
        char *d = rbui_describe(ui);
        if (d) printf("%s\n", d);
        free(d);
    }
    free(frame);
    free(guest);
    rbui_free(ui);
    return rc ? 1 : 0;
}
