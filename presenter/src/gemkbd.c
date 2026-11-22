#include "gemkbd.h"

#include <linux/input-event-codes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

/* How an output key is pressed. */
enum {
    GK_PASS = 0,      /* the key itself, Shift as held */
    GK_KEEP = 1,      /* `code`, Shift as held */
    GK_SHIFT_ON = 2,  /* Shift+`code` */
    GK_SHIFT_OFF = 4, /* `code` with Shift released around it */
    GK_OPTION = 8,    /* Option+`code` (with SHIFT_ON or SHIFT_OFF) */
    GK_DROP = 16,     /* nothing: a legend Mac OS has no key for */
};

struct gk_out {
    uint16_t code;
    uint8_t how;
};

struct gk_key {
    uint16_t key;
    struct gk_out lv[4]; /* base, Shift, Fn, Fn+Shift */
};

#define P {0, GK_PASS}
#define K(c) {(c), GK_KEEP}
#define U(c) {(c), GK_SHIFT_OFF}
#define S(c) {(c), GK_SHIFT_ON}
#define O(c) {(c), GK_OPTION | GK_SHIFT_OFF}
#define OS(c) {(c), GK_OPTION | GK_SHIFT_ON}
#define X {0, GK_DROP}

/* The gemini-nixos "gemini" XKB symbols, variant "us" (US keycaps), translated
 * to Mac OS's U.S. keyboard layout. Level 3 is Fn, level 4 Shift+Fn. Symbols
 * Mac OS types with Option: £ Option-3, € Shift-Option-2, ß Option-S, ÷
 * Option-/, « Option-\, » Shift-Option-\, Ω Option-Z, ™ Option-2, ® Option-R,
 * ¥ Option-Y, ± Shift-Option-=, Ø Shift-Option-O, Æ Shift-Option-', §
 * Option-6, ª Option-9, ° Shift-Option-8, © Option-G, ‘ Option-], ’
 * Shift-Option-], ¡ Option-1, nonbreaking space Option-space; the dead
 * accents ¨ ˆ ´ ` ˜ Option-U I E ` N. Media and XF86 legends without a host
 * key (play, previous, next, print, task pane, top menu, UWB, XF86Option, ☺),
 * the arrows and letters Mac Roman lacks (← ↓ → ↑ ŋ Ŋ Ł Þ Ð ˇ × and the dot
 * accents) give nothing. */
static const struct gk_key GK_US[] = {
    {KEY_1, {P, P, S(KEY_GRAVE), U(KEY_F1)}},         /* ~ */
    {KEY_2, {P, P, U(KEY_GRAVE), U(KEY_F2)}},         /* ` */
    {KEY_3, {P, P, O(KEY_3), U(KEY_F3)}},             /* £ */
    {KEY_4, {P, P, OS(KEY_2), U(KEY_F4)}},            /* € */
    {KEY_5, {P, P, S(KEY_COMMA), U(KEY_F5)}},         /* < */
    {KEY_6, {P, P, S(KEY_DOT), U(KEY_F6)}},           /* > */
    {KEY_7, {P, P, U(KEY_LEFTBRACE), U(KEY_F7)}},     /* [ */
    {KEY_8, {P, P, U(KEY_RIGHTBRACE), U(KEY_F8)}},    /* ] */
    {KEY_9, {P, P, S(KEY_LEFTBRACE), U(KEY_F9)}},     /* { */
    {KEY_0, {P, P, S(KEY_RIGHTBRACE), U(KEY_F10)}},   /* } */
    {KEY_Q, {P, P, X, O(KEY_Z)}},                     /* play; Ω */
    {KEY_W, {P, P, X, X}},                            /* previous; Ł */
    {KEY_E, {P, P, X, O(KEY_2)}},                     /* next; ™ */
    {KEY_R, {P, P, X, O(KEY_R)}},                     /* print; ® */
    {KEY_T, {P, P, U(KEY_MUTE), O(KEY_Y)}},           /* mute (host); ¥ */
    {KEY_Y, {P, P, X, X}},                            /* ← → */
    {KEY_U, {P, P, X, X}},                            /* ↓ ↑ */
    {KEY_I, {P, P, S(KEY_EQUAL), OS(KEY_EQUAL)}},     /* + ± */
    {KEY_O, {P, P, U(KEY_MINUS), OS(KEY_O)}},         /* - Ø */
    {KEY_P, {P, P, U(KEY_EQUAL), X}},                 /* = Þ */
    {KEY_A, {P, P, X, OS(KEY_APOSTROPHE)}},           /* task pane; Æ */
    {KEY_S, {P, P, O(KEY_S), O(KEY_6)}},              /* ß § */
    {KEY_D, {P, P, X, X}},                            /* top menu; Ð */
    {KEY_F, {P, P, X, O(KEY_9)}},                     /* UWB; ª */
    {KEY_G, {P, P, X, X}},                            /* ŋ Ŋ */
    {KEY_H, {P, P, O(KEY_SLASH), O(KEY_U)}},          /* ÷ dead ¨ */
    {KEY_J, {P, P, S(KEY_MINUS), O(KEY_I)}},          /* _ dead ˆ */
    {KEY_K, {P, P, U(KEY_SEMICOLON), OS(KEY_8)}},     /* ; ° */
    {KEY_L, {P, P, S(KEY_APOSTROPHE), X}},            /* " dead ˇ */
    {KEY_APOSTROPHE, {K(KEY_BACKSLASH), K(KEY_BACKSLASH), S(KEY_SEMICOLON), O(KEY_E)}}, /* \ | : dead ´ */
    {KEY_Z, {P, P, O(KEY_BACKSLASH), X}},             /* « */
    {KEY_X, {P, P, OS(KEY_BACKSLASH), X}},            /* » */
    {KEY_C, {P, P, U(KEY_VOLUMEDOWN), O(KEY_G)}},     /* volume down (host); © */
    {KEY_V, {P, P, U(KEY_VOLUMEUP), O(KEY_RIGHTBRACE)}},          /* volume up (host); ‘ */
    {KEY_B, {P, P, U(KEY_BRIGHTNESSDOWN), OS(KEY_RIGHTBRACE)}},   /* brightness down (host); ’ */
    {KEY_N, {P, P, U(KEY_BRIGHTNESSUP), O(KEY_1)}},   /* brightness up (host); ¡ */
    {KEY_M, {P, P, U(KEY_APOSTROPHE), O(KEY_GRAVE)}}, /* ' dead ` */
    {KEY_COMMA, {P, U(KEY_SLASH), X, X}},             /* , / */
    {KEY_DOT, {P, K(KEY_SLASH), X, O(KEY_N)}},        /* . ? ☺ dead ˜ */
    {KEY_BACKSPACE, {P, P, U(KEY_DELETE), K(KEY_DELETE)}}, /* Shift+Backspace stays Shift+Delete */
    {KEY_TAB, {P, P, U(KEY_CAPSLOCK), U(KEY_CAPSLOCK)}},
    {KEY_SPACE, {P, P, U(KEY_SPACE), O(KEY_SPACE)}},  /* space; nonbreaking space */
    {KEY_UP, {P, P, U(KEY_PAGEUP), K(KEY_PAGEUP)}},
    {KEY_DOWN, {P, P, U(KEY_PAGEDOWN), K(KEY_PAGEDOWN)}},
    {KEY_LEFT, {P, P, U(KEY_HOME), K(KEY_HOME)}},
    {KEY_RIGHT, {P, P, U(KEY_END), K(KEY_END)}},
};

/* The default "gemini" symbols (UK keycaps) where they differ from US. */
static const struct gk_key GK_UK[] = {
    {KEY_1, {P, P, S(KEY_BACKSLASH), U(KEY_F1)}},                 /* | */
    {KEY_2, {P, K(KEY_APOSTROPHE), S(KEY_3), U(KEY_F2)}},         /* " # */
    {KEY_3, {P, O(KEY_3), U(KEY_BACKSLASH), U(KEY_F3)}},          /* £ \ */
    {KEY_K, {P, P, S(KEY_2), OS(KEY_8)}},                         /* @ ° */
    {KEY_L, {P, P, U(KEY_SEMICOLON), X}},                         /* ; */
    {KEY_APOSTROPHE, {P, K(KEY_GRAVE), S(KEY_SEMICOLON), O(KEY_E)}}, /* ' ~ : */
    {KEY_M, {P, P, U(KEY_GRAVE), O(KEY_GRAVE)}},                  /* ` */
};

#undef P
#undef K
#undef U
#undef S
#undef O
#undef OS
#undef X

static const struct gk_key *find(const struct gk_key *t, size_t n, unsigned key)
{
    for (size_t i = 0; i < n; i++)
        if (t[i].key == key)
            return &t[i];
    return NULL;
}

static struct gk_out level_out(int layout, unsigned key, int level)
{
    const struct gk_key *e = NULL;
    if (layout == RB_GEMKBD_UK)
        e = find(GK_UK, sizeof GK_UK / sizeof GK_UK[0], key);
    if (!e)
        e = find(GK_US, sizeof GK_US / sizeof GK_US[0], key);
    struct gk_out o = e ? e->lv[level & 3] : (struct gk_out){0, GK_PASS};
    if (o.how == GK_PASS)
        o.code = (uint16_t)key;
    return o;
}

struct rb_gemkbd_map rb_gemkbd_lookup(int layout, unsigned key, int level)
{
    struct gk_out o = level_out(layout, key, level);
    struct rb_gemkbd_map m = {0, -1, false};
    if (o.how & GK_DROP)
        return m;
    m.code = o.code;
    m.shift = (o.how & GK_SHIFT_ON) ? 1 : (o.how & GK_SHIFT_OFF) ? 0 : -1;
    m.option = (o.how & GK_OPTION) != 0;
    return m;
}

void rb_gemkbd_init(struct rb_gemkbd *k, int layout, bool swap)
{
    memset(k, 0, sizeof *k);
    rb_gemkbd_configure(k, layout, swap);
}

void rb_gemkbd_configure(struct rb_gemkbd *k, int layout, bool swap)
{
    k->layout = layout == RB_GEMKBD_UK ? RB_GEMKBD_UK : RB_GEMKBD_US;
    k->swap = swap;
}

struct evbuf {
    struct rb_gemkbd_ev *e;
    int n;
};

static void push(struct evbuf *b, unsigned code, bool down)
{
    if (b->n < RB_GEMKBD_MAX_EV)
        b->e[b->n++] = (struct rb_gemkbd_ev){(uint16_t)code, down};
}

static bool is_shift(unsigned c) { return c == KEY_LEFTSHIFT || c == KEY_RIGHTSHIFT; }

/* Reference-counted presses; a Shift is not delivered while a key holds it off. */
static void out_down(struct rb_gemkbd *k, struct evbuf *b, unsigned code)
{
    if (code >= RB_GEMKBD_CODES)
        return;
    if (k->refs[code]++ == 0 && !(is_shift(code) && k->lift > 0))
        push(b, code, true);
}

static void out_up(struct rb_gemkbd *k, struct evbuf *b, unsigned code)
{
    if (code >= RB_GEMKBD_CODES || !k->refs[code])
        return;
    if (--k->refs[code] == 0 && !(is_shift(code) && k->lift > 0))
        push(b, code, false);
}

static void lift_shift(struct rb_gemkbd *k, struct evbuf *b, bool on)
{
    static const unsigned shifts[] = {KEY_LEFTSHIFT, KEY_RIGHTSHIFT};
    if (on) {
        if (k->lift++ == 0)
            for (int i = 0; i < 2; i++)
                if (k->refs[shifts[i]])
                    push(b, shifts[i], false);
    } else if (k->lift > 0 && --k->lift == 0) {
        for (int i = 0; i < 2; i++)
            if (k->refs[shifts[i]])
                push(b, shifts[i], true);
    }
}

/* Fn is Option while it is held with Alt or Ctrl, or for a touch that began
 * with it (KEY_LEFTALT: the presenter's swap makes it Command when swapped). */
static void update_fnopt(struct rb_gemkbd *k, struct evbuf *b)
{
    bool want = (k->fn && (k->alt || k->ctrl)) || k->touch_opt != 0;
    if (want == k->fnopt)
        return;
    k->fnopt = want;
    if (want)
        out_down(k, b, KEY_LEFTALT);
    else
        out_up(k, b, KEY_LEFTALT);
}

static void release_key(struct rb_gemkbd *k, struct evbuf *b, unsigned code)
{
    uint16_t o = k->held[code].out, optc = k->held[code].optc;
    uint8_t how = k->held[code].mods;
    k->held[code].out = k->held[code].optc = 0;
    k->held[code].mods = 0;
    if (!o)
        return;
    out_up(k, b, o);
    if (optc)
        out_up(k, b, optc);
    if (how & GK_SHIFT_ON)
        out_up(k, b, KEY_LEFTSHIFT);
    if (how & GK_SHIFT_OFF)
        lift_shift(k, b, false);
}

int rb_gemkbd_key(struct rb_gemkbd *k, unsigned code, bool down, struct rb_gemkbd_ev *out)
{
    struct evbuf b = {out, 0};
    if (code >= RB_GEMKBD_CODES) { /* nothing on this keyboard; pass it on */
        push(&b, code, down);
        return b.n;
    }
    switch (code) {
    case KEY_RIGHTALT: /* Fn: a layer, never a key of its own */
        k->fn = down;
        update_fnopt(k, &b);
        return b.n;
    case KEY_LEFTALT:  /* Command (the Windows key's code) */
    case KEY_LEFTCTRL: /* Control */
        if (down == (code == KEY_LEFTALT ? k->alt : k->ctrl))
            return 0;
        if (code == KEY_LEFTALT)
            k->alt = down;
        else
            k->ctrl = down;
        if (down) {
            k->held[code].out = code == KEY_LEFTALT ? KEY_LEFTMETA : KEY_LEFTCTRL;
            out_down(k, &b, k->held[code].out);
        } else {
            release_key(k, &b, code);
        }
        update_fnopt(k, &b);
        return b.n;
    default:
        break;
    }
    if (!down) {
        if (code == KEY_LEFTSHIFT)
            k->lshift = false;
        if (code == KEY_RIGHTSHIFT)
            k->rshift = false;
        release_key(k, &b, code);
        return b.n;
    }
    if (k->held[code].out) /* a repeated press without a release: ignore */
        return 0;
    if (is_shift(code)) {
        if (code == KEY_LEFTSHIFT)
            k->lshift = true;
        else
            k->rshift = true;
        k->held[code].out = (uint16_t)code;
        out_down(k, &b, code);
        return b.n;
    }
    bool shift = k->lshift || k->rshift;
    /* Fn with Alt or Ctrl is Option: the key types itself under it */
    int level = (k->fn && !k->fnopt ? 2 : 0) + (shift ? 1 : 0);
    struct gk_out m = level_out(k->layout, code, level);
    if (!m.code || (m.how & GK_DROP))
        return 0;
    uint16_t optc = (m.how & GK_OPTION) ? (uint16_t)(k->swap ? KEY_LEFTMETA : KEY_LEFTALT) : 0;
    k->held[code].out = m.code;
    k->held[code].mods = m.how & (GK_SHIFT_ON | GK_SHIFT_OFF);
    k->held[code].optc = optc;
    if (m.how & GK_SHIFT_OFF)
        lift_shift(k, &b, true);
    if (m.how & GK_SHIFT_ON)
        out_down(k, &b, KEY_LEFTSHIFT);
    if (optc)
        out_down(k, &b, optc);
    out_down(k, &b, m.code);
    return b.n;
}

int rb_gemkbd_pointer(struct rb_gemkbd *k, int button, bool down, struct rb_gemkbd_ev *out)
{
    struct evbuf b = {out, 0};
    if (button < 0 || button > 7)
        return 0;
    int bit = 1 << button;
    if (down && k->fn && !(k->touch_opt & bit))
        k->touch_opt |= bit;
    else if (!down)
        k->touch_opt &= ~bit;
    else
        return 0;
    update_fnopt(k, &b);
    return b.n;
}

int rb_gemkbd_layout_parse(const char *name)
{
    if (!name)
        return -1;
    if (!strcasecmp(name, "us"))
        return RB_GEMKBD_US;
    if (!strcasecmp(name, "uk") || !strcasecmp(name, "gb"))
        return RB_GEMKBD_UK;
    return -1;
}

const char *rb_gemkbd_layout_name(int layout) { return layout == RB_GEMKBD_UK ? "uk" : "us"; }

bool rb_gemini_board(const char *path)
{
    FILE *f = fopen(path ? path : "/proc/device-tree/compatible", "re");
    if (!f)
        return false;
    char buf[1024];
    size_t n = fread(buf, 1, sizeof buf - 1, f);
    fclose(f);
    buf[n] = 0;
    for (size_t i = 0; i < n; i += strlen(buf + i) + 1)
        if (!strcmp(buf + i, "planet,gemini-pda"))
            return true;
    return false;
}

bool rb_gemini_keyboard_device(const char *name, unsigned bus)
{
    if (!name)
        return false;
    const char *want = getenv("RB_GEMINI_KEYBOARD_NAME");
    if (want && *want)
        return !strcmp(name, want);
    /* gpio-matrix-keypad names its input device after the DT node ("keyboard") */
    return bus == 0x19 /* BUS_HOST */ && (!strcmp(name, "keyboard") || !strcmp(name, "matrix-keypad"));
}
