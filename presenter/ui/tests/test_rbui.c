/*
 * test_rbui.c - unit, golden and fuzz tests for rbui
 *
 *   test_rbui EXAMPLES_DIR [--update-golden] [--only NAME] [--fuzz N]
 *
 * Golden hashes (FNV-1a over rendered XRGB frames) live in
 * tests/golden.txt next to EXAMPLES_DIR/../tests; --update-golden rewrites
 * them after an intentional visual change (review the PNGs first).
 */
#include <dirent.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "rbui.h"
#include "rbui_internal.h"   /* assets test: fonts, icons, text drawing */

static int failures, checks;
static const char *examples_dir;
static int update_golden;
static long fuzz_iters = 4000;

#define CHECK(cond, ...)                                                      \
    do {                                                                      \
        ++checks;                                                             \
        if (!(cond)) {                                                        \
            ++failures;                                                       \
            fprintf(stderr, "FAIL %s:%d: %s: ", __FILE__, __LINE__, #cond);   \
            fprintf(stderr, __VA_ARGS__);                                     \
            fprintf(stderr, "\n");                                            \
        }                                                                     \
    } while (0)

/* ---- Helpers ---------------------------------------------------------------- */

typedef struct {
    char *ev[64];
    int n;
} events;

static void on_event(void *ctx, const char *json)
{
    events *e = ctx;
    if (e->n < 64) e->ev[e->n++] = strdup(json);
}

static void ev_clear(events *e)
{
    for (int i = 0; i < e->n; ++i) free(e->ev[i]);
    e->n = 0;
}

static const char *ev_last(events *e) { return e->n ? e->ev[e->n - 1] : ""; }

static char *read_file(const char *path, size_t *len)
{
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *buf = malloc((size_t)n + 1);
    if (buf && fread(buf, 1, (size_t)n, f) != (size_t)n) {
        free(buf);
        buf = NULL;
    }
    fclose(f);
    if (buf) {
        buf[n] = 0;
        *len = (size_t)n;
    }
    return buf;
}

static uint64_t fnv(const uint32_t *px, size_t n)
{
    uint64_t h = 1469598103934665603ull;
    for (size_t i = 0; i < n; ++i) {
        uint32_t v = px[i] & 0xFFFFFFu;
        for (int b = 0; b < 3; ++b) {
            h ^= (v >> (8 * b)) & 255u;
            h *= 1099511628211ull;
        }
    }
    return h;
}

static rbui *new_ui(int w, int h, events *e)
{
    rbui *ui = rbui_new();
    rbui_resize(ui, w, h);
    rbui_tick(ui, 1000);
    if (e) rbui_set_emitter(ui, on_event, e);
    return ui;
}

static int set(rbui *ui, const char *json)
{
    char err[256];
    int rc = rbui_set_doc(ui, json, strlen(json), err, sizeof(err));
    if (rc) fprintf(stderr, "  set_doc: %s\n", err);
    return rc;
}

static void key(rbui *ui, int k, unsigned mods) { rbui_key(ui, k, k == RBUI_KEY_SPACE ? " " : NULL, 1, mods); rbui_key(ui, k, NULL, 0, mods); }
static void text(rbui *ui, const char *s) { rbui_key(ui, 0, s, 1, 0); }

static const char *focus(rbui *ui)
{
    const char *f = rbui_focus_id(ui);
    return f ? f : "(none)";
}

/* Find "id":"X" widget rect via rbui_describe (crude but dependency free). */
static int widget_rect(rbui *ui, const char *id, int r[4])
{
    char *d = rbui_describe(ui);
    char pat[128];
    snprintf(pat, sizeof(pat), "\"id\":\"%s\",\"rect\":[", id);
    char *p = d ? strstr(d, pat) : NULL;
    int ok = p && sscanf(p + strlen(pat), "%d,%d,%d,%d", &r[0], &r[1], &r[2], &r[3]) == 4;
    free(d);
    return ok;
}

static void click(rbui *ui, int x, int y)
{
    rbui_pointer(ui, x, y, 0);
    rbui_pointer(ui, x, y, 1);
    rbui_pointer(ui, x, y, 0);
}

/* ---- Tests ------------------------------------------------------------------- */

static void test_checkerboard(void)
{
    uint32_t px[8 * 6];
    memset(px, 0x55, sizeof(px));
    rbui_draw_checkerboard(px, 8, 1, 1, 5, 4);
    for (int y = 0; y < 6; ++y)
        for (int x = 0; x < 8; ++x) {
            uint32_t v = px[y * 8 + x];
            if (x >= 1 && x < 6 && y >= 1 && y < 5)
                CHECK(v == (((x + y) & 1) ? 0x000000u : 0xFFFFFFu), "pixel %d,%d = %06x", x, y, v);
            else
                CHECK(v == 0x55555555u, "outside pixel %d,%d touched", x, y);
        }
}

static const char *kForm =
    "{\"id\":\"form\",\"title\":\"Form\",\"width\":460,\"items\":["
    "{\"type\":\"text\",\"text\":\"Hello\"},"
    "{\"type\":\"field\",\"id\":\"name\",\"label\":\"Name:\",\"value\":\"Mac\"},"
    "{\"type\":\"field\",\"id\":\"pw\",\"label\":\"Password:\",\"secure\":true,\"max_length\":8},"
    "{\"type\":\"checkbox\",\"id\":\"share\",\"label\":\"Share\",\"value\":false},"
    "{\"type\":\"checkbox\",\"id\":\"off\",\"label\":\"Disabled\",\"disabled\":true},"
    "{\"type\":\"radio\",\"id\":\"res\",\"label\":\"Size:\",\"value\":\"a\",\"options\":["
    "{\"id\":\"a\",\"label\":\"A\"},{\"id\":\"b\",\"label\":\"B\",\"disabled\":true},{\"id\":\"c\",\"label\":\"C\"}]},"
    "{\"type\":\"popup\",\"id\":\"scale\",\"label\":\"Scale:\",\"value\":\"x1\",\"options\":["
    "{\"id\":\"x1\",\"label\":\"One\"},{\"id\":\"x2\",\"label\":\"Two\"},{\"id\":\"x3\",\"label\":\"Three\"}]},"
    "{\"type\":\"list\",\"id\":\"disk\",\"rows\":3,\"value\":\"d1\",\"items\":["
    "{\"id\":\"d1\",\"label\":\"Alpha\"},{\"id\":\"d2\",\"label\":\"Beta\"},{\"id\":\"d3\",\"label\":\"Gamma\",\"disabled\":true},"
    "{\"id\":\"d4\",\"label\":\"Delta\"},{\"id\":\"d5\",\"label\":\"Epsilon\"}]},"
    "{\"type\":\"buttons\",\"items\":[{\"id\":\"cancel\",\"label\":\"Cancel\"},{\"id\":\"nope\",\"label\":\"Nope\",\"disabled\":true},"
    "{\"id\":\"ok\",\"label\":\"OK\",\"default\":true}]}]}";

static void test_keyboard(void)
{
    events e = {0};
    rbui *ui = new_ui(960, 540, &e);
    CHECK(set(ui, kForm) == 0, "form doc");
    CHECK(strcmp(focus(ui), "name") == 0, "initial focus on first field, got %s", focus(ui));
    const char *order[] = {"pw", "share", "res", "scale", "disk", "cancel", "ok", "name"};
    for (int i = 0; i < 8; ++i) {
        key(ui, RBUI_KEY_TAB, 0);
        CHECK(strcmp(focus(ui), order[i]) == 0, "tab %d: want %s got %s", i, order[i], focus(ui));
    }
    key(ui, RBUI_KEY_TAB, RBUI_MOD_SHIFT);
    CHECK(strcmp(focus(ui), "ok") == 0, "shift-tab wraps to ok, got %s", focus(ui));
    key(ui, RBUI_KEY_TAB, RBUI_MOD_SHIFT);
    key(ui, RBUI_KEY_TAB, RBUI_MOD_SHIFT);
    CHECK(strcmp(focus(ui), "disk") == 0, "shift-tab to disk, got %s", focus(ui));

    ev_clear(&e);
    key(ui, RBUI_KEY_DOWN, 0);
    CHECK(e.n == 1 && strcmp(ev_last(&e), "{\"event\":\"ui_change\",\"doc\":\"form\",\"id\":\"disk\",\"value\":\"d2\"}") == 0,
          "list down: %s", ev_last(&e));
    key(ui, RBUI_KEY_DOWN, 0);   /* skips disabled d3 */
    CHECK(strstr(ev_last(&e), "\"d4\""), "list skips disabled: %s", ev_last(&e));
    key(ui, RBUI_KEY_END, 0);
    CHECK(strstr(ev_last(&e), "\"d5\""), "list end: %s", ev_last(&e));
    int n = e.n;
    key(ui, RBUI_KEY_DOWN, 0);
    CHECK(e.n == n, "no change event at end of list");
    key(ui, RBUI_KEY_HOME, 0);
    CHECK(strstr(ev_last(&e), "\"d1\""), "list home: %s", ev_last(&e));
    rbui_tick(ui, 5000);
    text(ui, "g");
    CHECK(strstr(ev_last(&e), "\"d1\""), "type-ahead ignores disabled Gamma: %s", ev_last(&e));
    text(ui, "E");
    CHECK(strstr(ev_last(&e), "\"d5\"") == NULL, "type-ahead 'gE' matches nothing new");
    rbui_tick(ui, 7000);
    text(ui, "e");
    CHECK(strstr(ev_last(&e), "\"d5\""), "type-ahead 'e' selects Epsilon: %s", ev_last(&e));

    rbui_focus(ui, "res");
    ev_clear(&e);
    key(ui, RBUI_KEY_DOWN, 0);
    CHECK(strcmp(ev_last(&e), "{\"event\":\"ui_change\",\"doc\":\"form\",\"id\":\"res\",\"value\":\"c\"}") == 0,
          "radio skips disabled option: %s", ev_last(&e));
    key(ui, RBUI_KEY_UP, 0);
    CHECK(strstr(ev_last(&e), "\"a\""), "radio up: %s", ev_last(&e));

    rbui_focus(ui, "scale");
    key(ui, RBUI_KEY_DOWN, 0);
    CHECK(strstr(ev_last(&e), "\"id\":\"scale\",\"value\":\"x2\""), "popup arrow: %s", ev_last(&e));
    key(ui, RBUI_KEY_SPACE, 0);
    char *d = rbui_describe(ui);
    CHECK(d && strstr(d, "\"menu\":["), "space opens popup menu");
    free(d);
    key(ui, RBUI_KEY_DOWN, 0);
    key(ui, RBUI_KEY_RETURN, 0);
    CHECK(strstr(ev_last(&e), "\"id\":\"scale\",\"value\":\"x3\""), "menu choose: %s", ev_last(&e));
    d = rbui_describe(ui);
    CHECK(d && !strstr(d, "\"menu\":["), "menu closed after choose");
    free(d);
    key(ui, RBUI_KEY_SPACE, 0);
    key(ui, RBUI_KEY_ESCAPE, 0);
    n = e.n;
    d = rbui_describe(ui);
    CHECK(d && !strstr(d, "\"menu\":[") && e.n == n, "escape closes menu without cancel");
    free(d);

    rbui_focus(ui, "share");
    key(ui, RBUI_KEY_SPACE, 0);
    CHECK(strcmp(ev_last(&e), "{\"event\":\"ui_change\",\"doc\":\"form\",\"id\":\"share\",\"value\":true}") == 0,
          "space toggles checkbox: %s", ev_last(&e));

    ev_clear(&e);
    key(ui, RBUI_KEY_RETURN, 0);
    CHECK(e.n == 1, "return emits one action (%d)", e.n);
    CHECK(strcmp(ev_last(&e),
                 "{\"event\":\"ui_action\",\"doc\":\"form\",\"id\":\"ok\",\"values\":{\"name\":\"Mac\",\"pw\":\"\","
                 "\"share\":true,\"off\":false,\"res\":\"a\",\"scale\":\"x3\",\"disk\":\"d5\"}}") == 0,
          "action json: %s", ev_last(&e));
    key(ui, RBUI_KEY_ESCAPE, 0);
    CHECK(strstr(ev_last(&e), "\"id\":\"cancel\""), "escape -> cancel: %s", ev_last(&e));
    text(ui, NULL);
    rbui_key(ui, 0, ".", 1, RBUI_MOD_COMMAND);
    CHECK(e.n == 3 && strstr(ev_last(&e), "\"id\":\"cancel\""), "cmd-. -> cancel");
    rbui_focus(ui, "cancel");
    key(ui, RBUI_KEY_RIGHT, 0);
    CHECK(strcmp(focus(ui), "ok") == 0, "right arrow between buttons skips disabled, got %s", focus(ui));
    key(ui, RBUI_KEY_LEFT, 0);
    key(ui, RBUI_KEY_SPACE, 0);
    CHECK(strstr(ev_last(&e), "\"id\":\"cancel\""), "space activates focused button");
    CHECK(rbui_focus(ui, "nope") == -1 && rbui_focus(ui, "off") == -1, "disabled widgets are not focusable");
    CHECK(rbui_tick(ui, 9000) != 0, "button flash needs redraw");
    ev_clear(&e);
    rbui_free(ui);
}

static void test_field(void)
{
    events e = {0};
    rbui *ui = new_ui(960, 540, &e);
    CHECK(set(ui, kForm) == 0, "form doc");
    key(ui, RBUI_KEY_END, 0);
    text(ui, "in");
    text(ui, "tosh \xC3\xA9\xE6\xBC\xA2");   /* é and a CJK char (replacement box on screen) */
    CHECK(strstr(ev_last(&e), "\"value\":\"Macintosh \xC3\xA9\xE6\xBC\xA2\""), "utf8 typing: %s", ev_last(&e));
    key(ui, RBUI_KEY_BACKSPACE, 0);
    CHECK(strstr(ev_last(&e), "\"value\":\"Macintosh \xC3\xA9\"}"), "backspace removes one code point: %s", ev_last(&e));
    key(ui, RBUI_KEY_LEFT, 0);
    key(ui, RBUI_KEY_LEFT, 0);
    key(ui, RBUI_KEY_DELETE, 0);
    CHECK(strstr(ev_last(&e), "\"value\":\"Macintosh\xC3\xA9\"}"), "delete forward: %s", ev_last(&e));
    key(ui, RBUI_KEY_HOME, 0);
    key(ui, RBUI_KEY_RIGHT, RBUI_MOD_SHIFT);
    key(ui, RBUI_KEY_RIGHT, RBUI_MOD_SHIFT);
    key(ui, RBUI_KEY_RIGHT, RBUI_MOD_SHIFT);
    text(ui, "X");
    CHECK(strstr(ev_last(&e), "\"value\":\"Xintosh\xC3\xA9\"}"), "typing replaces selection: %s", ev_last(&e));
    rbui_key(ui, 0, "a", 1, RBUI_MOD_COMMAND);
    text(ui, "z");
    CHECK(strstr(ev_last(&e), "\"value\":\"z\"}"), "cmd-a selects all: %s", ev_last(&e));
    text(ui, "\x01\x1b[A\x7f");
    CHECK(strstr(ev_last(&e), "\"value\":\"z[A\"}"), "control characters filtered: %s", ev_last(&e));
    key(ui, RBUI_KEY_BACKSPACE, RBUI_MOD_COMMAND);
    CHECK(strstr(ev_last(&e), "\"value\":\"\"}"), "cmd-backspace clears to start: %s", ev_last(&e));
    key(ui, RBUI_KEY_BACKSPACE, 0);   /* nothing left: no event, no crash */

    key(ui, RBUI_KEY_TAB, 0);
    CHECK(strcmp(focus(ui), "pw") == 0, "tab to password");
    text(ui, "secret123456");
    CHECK(strstr(ev_last(&e), "\"id\":\"pw\",\"value\":\"secret12\",\"secure\":true"), "max_length and secure flag: %s",
          ev_last(&e));
    ev_clear(&e);
    rbui_free(ui);
}

static void test_pointer(void)
{
    events e = {0};
    rbui *ui = new_ui(960, 540, &e);
    CHECK(set(ui, kForm) == 0, "form doc");
    int r[4];
    CHECK(widget_rect(ui, "ok", r), "ok rect");
    int cx = r[0] + r[2] / 2, cy = r[1] + r[3] / 2;
    rbui_pointer(ui, cx, cy, 1);
    rbui_pointer(ui, cx + 200, cy, 1);
    rbui_pointer(ui, cx + 200, cy, 0);
    CHECK(e.n == 0, "release outside a button does nothing");
    rbui_pointer(ui, cx, cy, 1);
    rbui_pointer(ui, cx + 200, cy, 1);
    rbui_pointer(ui, cx, cy, 1);
    rbui_pointer(ui, cx, cy, 0);
    CHECK(e.n == 1 && strstr(ev_last(&e), "\"ui_action\"") && strstr(ev_last(&e), "\"id\":\"ok\""), "tracked click");

    CHECK(widget_rect(ui, "share", r), "share rect");
    click(ui, r[0] + r[2] - 2, r[1] + r[3] / 2);   /* on the label */
    CHECK(strstr(ev_last(&e), "\"id\":\"share\",\"value\":true"), "label click toggles: %s", ev_last(&e));
    CHECK(widget_rect(ui, "off", r), "off rect");
    int n = e.n;
    click(ui, r[0] + 5, r[1] + 5);
    CHECK(e.n == n, "disabled checkbox ignores clicks");

    CHECK(widget_rect(ui, "disk", r), "disk rect");
    click(ui, r[0] + 20, r[1] + 1 + 18 + 9);
    CHECK(strstr(ev_last(&e), "\"id\":\"disk\",\"value\":\"d2\""), "list click selects row 2: %s", ev_last(&e));
    CHECK(strcmp(focus(ui), "disk") == 0, "list click focuses the list");
    n = e.n;
    rbui_tick(ui, 1100);
    click(ui, r[0] + 20, r[1] + 1 + 18 + 9);
    CHECK(e.n == n + 1 && strstr(ev_last(&e), "\"ui_action\"") && strstr(ev_last(&e), "\"id\":\"ok\""),
          "double-click activates default: %s", ev_last(&e));
    rbui_tick(ui, 3000);
    /* Scroll arrow at the bottom of the list's scroll bar. */
    click(ui, r[0] + r[2] - 8, r[1] + r[3] - 8);
    char *d = rbui_describe(ui);
    CHECK(d && strstr(d, "\"id\":\"disk\"") && strstr(strstr(d, "\"id\":\"disk\""), "\"scroll\":1"), "scroll arrow scrolls");
    free(d);

    CHECK(widget_rect(ui, "scale", r), "popup rect");
    cx = r[0] + 10;
    cy = r[1] + r[3] / 2;
    rbui_pointer(ui, cx, cy, 1);
    d = rbui_describe(ui);
    CHECK(d && strstr(d, "\"menu\":["), "press opens popup menu");
    free(d);
    rbui_pointer(ui, cx, cy + 18, 1);
    rbui_pointer(ui, cx, cy + 18, 0);
    CHECK(strstr(ev_last(&e), "\"id\":\"scale\",\"value\":\"x2\""), "drag-release chooses: %s", ev_last(&e));
    /* Click and release in place: the menu stays open (sticky), next click chooses. */
    rbui_pointer(ui, cx, cy, 1);
    rbui_pointer(ui, cx, cy, 0);
    d = rbui_describe(ui);
    CHECK(d && strstr(d, "\"menu\":["), "click-release leaves the menu open");
    free(d);
    rbui_pointer(ui, cx, cy + 18, 0);
    rbui_pointer(ui, cx, cy + 18, 1);
    rbui_pointer(ui, cx, cy + 18, 0);
    CHECK(strstr(ev_last(&e), "\"id\":\"scale\",\"value\":\"x3\""), "sticky menu click chooses: %s", ev_last(&e));

    CHECK(widget_rect(ui, "name", r), "name rect");
    click(ui, r[0] + 6, r[1] + 10);
    CHECK(strcmp(focus(ui), "name") == 0, "field click focuses");
    text(ui, "A");
    CHECK(strstr(ev_last(&e), "\"value\":\"AMac\""), "click puts the caret at the start: %s", ev_last(&e));
    int x, y;
    rbui_cursor_pos(ui, &x, &y);
    CHECK(x == r[0] + 6 && y == r[1] + 10, "cursor position");
    ev_clear(&e);
    rbui_free(ui);
}

static void test_state(void)
{
    events e = {0};
    rbui *ui = new_ui(960, 540, &e);
    CHECK(set(ui, kForm) == 0, "form doc");
    key(ui, RBUI_KEY_END, 0);   /* the focused field starts fully selected */
    text(ui, "!");
    rbui_focus(ui, "disk");
    key(ui, RBUI_KEY_DOWN, 0);
    rbui_focus(ui, "share");
    key(ui, RBUI_KEY_SPACE, 0);
    CHECK(set(ui, kForm) == 0, "same doc again");
    ev_clear(&e);
    rbui_focus(ui, "ok");
    key(ui, RBUI_KEY_SPACE, 0);
    CHECK(strstr(ev_last(&e), "\"name\":\"Mac!\"") && strstr(ev_last(&e), "\"disk\":\"d2\"") &&
              strstr(ev_last(&e), "\"share\":true"),
          "state preserved: %s", ev_last(&e));

    /* hostd changes a value: its new value wins; others keep user state. */
    char *changed = strdup(kForm);
    char *p = strstr(changed, "\"value\":\"d1\"");
    p[10] = '4';
    CHECK(set(ui, changed) == 0, "changed doc");
    key(ui, RBUI_KEY_SPACE, 0);
    CHECK(strstr(ev_last(&e), "\"disk\":\"d4\"") && strstr(ev_last(&e), "\"name\":\"Mac!\""), "doc change wins: %s",
          ev_last(&e));
    CHECK(strcmp(focus(ui), "ok") == 0, "focus preserved by id");

    /* reset:true drops everything. */
    size_t rlen = strlen(changed) + 32;
    char *reset = malloc(rlen);
    snprintf(reset, rlen, "{\"reset\":true,%s", changed + 1);
    CHECK(set(ui, reset) == 0, "reset doc");
    CHECK(strcmp(focus(ui), "name") == 0, "reset restores default focus");
    key(ui, RBUI_KEY_RETURN, 0);
    CHECK(strstr(ev_last(&e), "\"name\":\"Mac\"") && strstr(ev_last(&e), "\"share\":false"), "reset values: %s",
          ev_last(&e));

    /* A different document id never inherits state. */
    text(ui, "zzz");
    char *other = strdup(kForm);
    memcpy(strstr(other, "\"form\""), "\"frm2\"", 6);
    CHECK(set(ui, other) == 0, "other doc");
    key(ui, RBUI_KEY_RETURN, 0);
    CHECK(strstr(ev_last(&e), "\"doc\":\"frm2\"") && strstr(ev_last(&e), "\"name\":\"Mac\""), "new doc id fresh: %s",
          ev_last(&e));
    CHECK(rbui_set_doc(ui, NULL, 0, NULL, 0) == 0 && strcmp(rbui_doc_id(ui), "") == 0, "clear doc");
    key(ui, RBUI_KEY_RETURN, 0);
    free(changed);
    free(reset);
    free(other);
    ev_clear(&e);
    rbui_free(ui);
}

static void test_errors(void)
{
    rbui *ui = new_ui(640, 480, NULL);
    static const struct { const char *doc, *want; } cases[] = {
        {"{", "invalid JSON"},
        {"[]", "must be a JSON object"},
        {"{\"items\":[]}", "missing \"id\""},
        {"{\"id\":\"x\",\"kind\":\"sheet\"}", "unknown kind"},
        {"{\"id\":\"x\",\"items\":[{\"type\":\"knob\"}]}", "items[0]: unknown type"},
        {"{\"id\":\"x\",\"items\":[{\"type\":\"text\",\"style\":\"huge\"}]}", "unknown style"},
        {"{\"id\":\"x\",\"items\":[{\"type\":\"field\",\"id\":\"a\"},{\"type\":\"checkbox\",\"id\":\"a\"}]}", "duplicate id"},
        {"{\"id\":\"x\",\"items\":[{\"type\":\"qr\",\"matrix\":[\"101\",\"10\"]}]}", "matrix row 1"},
        {"{\"id\":\"x\",\"items\":[{\"type\":\"qr\",\"matrix\":[\"1x1\"]}]}", "only '0' and '1'"},
        {"{\"id\":\"x\",\"items\":[{\"type\":\"buttons\",\"items\":[{\"label\":\"OK\"}]}]}", "missing \"id\""},
        {"{\"id\":\"x\",\"items\":[{\"type\":\"list\",\"items\":[{\"id\":\"a\"},{\"id\":\"a\"}]}]}", "duplicate option id"},
        {"{\"id\":\"x\",\"alert_icon\":\"bomb\"}", "unknown alert_icon"},
        {"{\"id\":\"x\",\"items\":[{\"type\":\"list\",\"items\":[{\"id\":\"a\",\"icon\":\"toaster\"}]}]}", "unknown icon"},
        {"{\"id\":\"x\",\"items\":[{\"type\":\"group\",\"items\":[{\"type\":\"group\",\"items\":[{\"type\":\"group\",\"items\":["
         "{\"type\":\"group\",\"items\":[{\"type\":\"group\",\"items\":[{\"type\":\"group\",\"items\":[{\"type\":\"group\",\"items\":["
         "{\"type\":\"group\",\"items\":[{\"type\":\"group\",\"items\":[]}]}]}]}]}]}]}]}]}]}",
         "nested more than"},
        {"{\"id\":\"x\",\"items\":[{\"type\":\"checkbox\",\"value\":\"yes\"}]}", "true or false"},
        {"{\"id\":\"x\",\"items\":[{\"type\":\"list\",\"activate\":\"hover\"}]}", "\"activate\" must be"},
        {"{\"id\":\"x\",\"items\":[{\"type\":\"field\",\"id\":\"pw\"},{\"type\":\"checkbox\",\"id\":\"s\",\"reveals\":\"pw\"}]}",
         "must name one secure field"},
        {"{\"id\":\"x\",\"items\":[{\"type\":\"checkbox\",\"id\":\"s\",\"reveals\":\"nowhere\"}]}", "must name one secure field"},
        {"{\"id\":\"x\",\"items\":[{\"type\":\"field\",\"id\":\"pw\",\"secure\":true},{\"type\":\"checkbox\",\"id\":\"s\",\"reveals\":\"pw\"},"
         "{\"type\":\"checkbox\",\"id\":\"t\",\"reveals\":\"pw\"}]}", "checkbox \"t\""},
    };
    for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); ++i) {
        char err[256] = "";
        int rc = rbui_set_doc(ui, cases[i].doc, strlen(cases[i].doc), err, sizeof(err));
        CHECK(rc == -1 && strstr(err, cases[i].want), "case %zu: rc %d err \"%s\" (want \"%s\")", i, rc, err, cases[i].want);
    }
    CHECK(strcmp(rbui_doc_id(ui), "") == 0, "failed docs leave nothing shown");
    rbui_free(ui);
}

/* ---- Wi-Fi: click-to-join lists and the "Show password" checkbox ----------------- */

static const char *kNets =
    "{\"id\":\"wifi\",\"kind\":\"window\",\"title\":\"Wi-Fi\",\"width\":440,\"items\":["
    "{\"type\":\"list\",\"id\":\"network\",\"rows\":4,\"activate\":\"click\",\"value\":\"Home\",\"items\":["
    "{\"id\":\"Home\",\"label\":\"Home\",\"checked\":true,\"lock\":true,\"signal\":4},"
    "{\"id\":\"Cafe\",\"label\":\"Cafe\",\"signal\":2},{\"id\":\"Work\",\"label\":\"Work\",\"lock\":true,\"signal\":1}]},"
    "{\"type\":\"buttons\",\"items\":[{\"id\":\"cancel\",\"label\":\"Done\",\"default\":true}]}]}";

/* A copy of s with the first `from` replaced by `to` (the caller frees it). */
static char *subst(const char *s, const char *from, const char *to)
{
    const char *p = strstr(s, from);
    if (!p) return strdup(s);
    size_t a = (size_t)(p - s), fl = strlen(from), tl = strlen(to), sl = strlen(s);
    char *out = malloc(sl - fl + tl + 1);
    memcpy(out, s, a);
    memcpy(out + a, to, tl);
    memcpy(out + a + tl, p + fl, sl - a - fl + 1);
    return out;
}

static int count_actions(events *e, int from, const char *id)
{
    char pat[96];
    snprintf(pat, sizeof(pat), "\"id\":\"%s\"", id);
    int n = 0;
    for (int i = from; i < e->n; ++i)
        if (strstr(e->ev[i], "\"ui_action\"") && strstr(e->ev[i], pat)) ++n;
    return n;
}

static const char *kIcons =
    "{\"id\":\"rb\",\"kind\":\"window\",\"title\":\"RetroBoot\",\"width\":510,\"focus\":\"pane\",\"items\":["
    "{\"type\":\"list\",\"id\":\"pane\",\"view\":\"icons\",\"columns\":4,\"header\":\"8 items\",\"value\":null,\"items\":["
    "{\"id\":\"a\",\"label\":\"Startup Disk\",\"icon\":\"startup\"},{\"id\":\"b\",\"label\":\"Download a Mac\",\"icon\":\"mac\"},"
    "{\"id\":\"c\",\"label\":\"Software Update\",\"icon\":\"update\"},{\"id\":\"d\",\"label\":\"Network\",\"icon\":\"network\"},"
    "{\"id\":\"e\",\"label\":\"Display\",\"icon\":\"display\"},{\"id\":\"f\",\"label\":\"Date & Time\",\"icon\":\"clock\"}]},"
    "{\"type\":\"buttons\",\"items\":[{\"id\":\"cancel\",\"label\":\"Done\",\"default\":true}]}]}";

/* The icon view (a list with "view": "icons"): Finder clicks, double-click and keys. */
static void test_icon_view(void)
{
    events e = {0};
    rbui *ui = new_ui(960, 540, &e);
    uint64_t t = 1000;
    CHECK(set(ui, kIcons) == 0, "icon view doc");
    int r[4];
    CHECK(widget_rect(ui, "pane", r), "icon view rect");
    /* Cells of 128 centred in the white area below the 21-pixel header; icons 4 below it. */
    int ax = r[0] + 1, aw = r[2] - 2, ay = r[1] + 1 + 21;
#define ICX(i) (ax + (aw + (2 * ((i) % 4) - 3) * 128) / 2)
#define ICY(i) (ay + 4 + ((i) / 4) * 64 + 16)
    char *d = rbui_describe(ui);
    char want[96];
    snprintf(want, sizeof want, "\"id\":\"b\",\"rect\":[%d,%d,32,32]", ICX(1) - 16, ICY(1) - 16);
    CHECK(d && strstr(d, want), "describe gives the icon's rect (%s)", want);
    free(d);
    click(ui, ICX(1), ICY(1));
    CHECK(e.n == 1 && strstr(e.ev[0], "\"ui_change\"") && strstr(e.ev[0], "\"value\":\"b\""),
          "a click selects: %s", e.n ? e.ev[0] : "");
    rbui_tick(ui, t += 150);
    click(ui, ICX(1), ICY(1) + 22);   /* the second click lands on its name */
    CHECK(count_actions(&e, 0, "pane") == 1 && strstr(ev_last(&e), "\"values\":{\"pane\":\"b\"}"),
          "a double-click opens it: %s", ev_last(&e));
    rbui_tick(ui, t += 600);
    int n = e.n;
    click(ui, ICX(1) + 64, ICY(1));   /* the white between two cells */
    CHECK(e.n == n + 1 && strstr(ev_last(&e), "\"value\":null"), "a click on the white deselects: %s", ev_last(&e));
    n = e.n;
    key(ui, RBUI_KEY_RIGHT, 0);
    CHECK(strstr(ev_last(&e), "\"value\":\"a\""), "Right from nothing selects the first");
    key(ui, RBUI_KEY_RIGHT, 0);
    key(ui, RBUI_KEY_DOWN, 0);
    CHECK(strstr(ev_last(&e), "\"value\":\"f\""), "Down moves a row: %s", ev_last(&e));
    key(ui, RBUI_KEY_DOWN, 0);
    CHECK(strstr(ev_last(&e), "\"value\":\"f\""), "no row below: the selection stays");
    key(ui, RBUI_KEY_UP, 0);
    CHECK(strstr(ev_last(&e), "\"value\":\"b\""), "Up moves back");
    CHECK(count_actions(&e, n, "pane") == 0, "arrows only select");
    rbui_key(ui, 0, "o", 1, RBUI_MOD_COMMAND);
    CHECK(count_actions(&e, n, "pane") == 1, "Command-O opens");
    key(ui, RBUI_KEY_RETURN, 0);
    CHECK(count_actions(&e, n, "cancel") == 1, "Return presses the default button");
#undef ICX
#undef ICY
    rbui_free(ui);
}

static const char *kAbout =
    "{\"id\":\"about\",\"kind\":\"window\",\"title\":\"RetroBoot\",\"width\":477,\"items\":["
    "{\"type\":\"facts\",\"items\":[{\"label\":\"Version:\",\"value\":\"Mac OS 9.2.2\"},"
    "{\"label\":\"Built-in Memory:\",\"value\":\"512 MB\"},{\"label\":\"Virtual Memory:\"}]},"
    "{\"type\":\"meters\",\"items\":[{\"icon\":\"mac\",\"label\":\"Mac OS\",\"value\":\"48.6 MB\",\"share\":1},"
    "{\"icon\":\"chip\",\"label\":\"All processors\",\"value\":\"41%\",\"share\":0.41},"
    "{\"label\":\"Nothing yet\",\"share\":null}]}]}";

static void test_about_items(void)
{
    events e = {0};
    rbui *ui = new_ui(960, 540, &e);
    CHECK(set(ui, kAbout) == 0, "facts and meters doc");
    char *d = rbui_describe(ui);
    /* Facts 16 px a row; meters a frame around rows 19 px apart (no line after the last). */
    const char *f = d ? strstr(d, "\"type\":\"facts\",\"rect\":[") : NULL;
    const char *m = d ? strstr(d, "\"type\":\"meters\",\"rect\":[") : NULL;
    int fr[4] = {0}, mr[4] = {0};
    CHECK(f && sscanf(strchr(f, '[') + 1, "%d,%d,%d,%d", &fr[0], &fr[1], &fr[2], &fr[3]) == 4 && fr[3] == 48,
          "three facts are 48 px: %s", d ? d : "");
    CHECK(m && sscanf(strchr(m, '[') + 1, "%d,%d,%d,%d", &mr[0], &mr[1], &mr[2], &mr[3]) == 4 && mr[3] == 2 + 3 * 19 - 1,
          "three meters rows are 58 px");
    free(d);
    uint32_t *px = calloc(960 * 540, 4);
    rbui_render(ui, px, 960, RBUI_BG_CHECKERBOARD, NULL, 0, 0);
#define PX(x, y) (px[(y) * 960 + (x)] & 0xFFFFFFu)
    int in_x = mr[0] + 1, in_y = mr[1] + 1, in_r = mr[0] + mr[2] - 2;
    CHECK(PX(in_x + 2, in_y + 2) == 0xEEEEEEu, "meters are on #EEEEEE");
    CHECK(PX(in_x + 2, in_y + 18) == 0xFFFFFFu && PX(in_x + 2, in_y + 37) == 0xFFFFFFu &&
          PX(in_x + 2, in_y + 55) == 0xEEEEEEu, "a white line between rows, none after the last");
    /* The bar's box ends 9 px from the right: its highlight there, the black frame inside it. */
    CHECK(PX(in_r - 9, in_y + 9) == 0xFFFFFFu && PX(in_r - 10, in_y + 9) == 0x000000u,
          "the bar ends 9 px from the right: %06x %06x", PX(in_r - 9, in_y + 9), PX(in_r - 10, in_y + 9));
#undef PX
    free(px);
    char err[256];
    const char *bad[] = {
        "{\"id\":\"x\",\"kind\":\"window\",\"title\":\"t\",\"items\":[{\"type\":\"meters\",\"items\":[{\"label\":\"a\",\"share\":\"50%\"}]}]}",
        "{\"id\":\"x\",\"kind\":\"window\",\"title\":\"t\",\"items\":[{\"type\":\"meters\",\"items\":[{\"label\":\"a\",\"icon\":\"nope\"}]}]}",
        "{\"id\":\"x\",\"kind\":\"window\",\"title\":\"t\",\"items\":[{\"type\":\"facts\",\"items\":[{\"value\":\"no label\"}]}]}",
    };
    for (int i = 0; i < 3; ++i)
        CHECK(rbui_set_doc(ui, bad[i], strlen(bad[i]), err, sizeof err) != 0, "bad facts/meters %d rejected", i);
    rbui_free(ui);
}

static const char *kMonitors =
    "{\"id\":\"display-sound\",\"kind\":\"window\",\"title\":\"Display & Sound\",\"width\":507,\"items\":["
    "{\"type\":\"band\",\"height\":76,\"rule\":\"bottom\",\"items\":["
    "{\"type\":\"icon\",\"icon\":\"display\",\"size\":64,\"x\":10,\"y\":4},"
    "{\"type\":\"bevels\",\"id\":\"page\",\"value\":\"monitor\",\"x\":89,\"y\":10,\"items\":["
    "{\"id\":\"monitor\",\"label\":\"Monitor\",\"icon\":\"monitor_page\"},{\"id\":\"sound\",\"label\":\"Sound\",\"icon\":\"sound_page\"},"
    "{\"id\":\"keys\",\"label\":\"Keyboard\",\"icon\":\"keyboard\",\"disabled\":true}]}]},"
    "{\"type\":\"text\",\"text\":\"Page\"},"
    "{\"type\":\"band\",\"height\":33,\"rule\":\"top\",\"items\":["
    "{\"type\":\"slider\",\"id\":\"vol\",\"label\":\"Main Volume:\",\"value\":40,\"min\":0,\"max\":100,\"step\":5,"
    "\"icons\":[\"speaker_low\",\"speaker_high\"],\"x\":13,\"y\":10}]},"
    "{\"type\":\"buttons\",\"items\":[{\"id\":\"ok\",\"label\":\"OK\",\"default\":true}]}]}";

static void test_band_bevels_slider(void)
{
    events e = {0};
    rbui *ui = new_ui(960, 540, &e);
    CHECK(set(ui, kMonitors) == 0, "Monitors doc");
    char *d = rbui_describe(ui);
    int c[4] = {0}, band[4] = {0};
    const char *cp = d ? strstr(d, "\"content\":[") : NULL;
    const char *bp = d ? strstr(d, "\"type\":\"band\",\"rect\":[") : NULL;
    CHECK(cp && sscanf(cp + 11, "%d,%d,%d,%d", &c[0], &c[1], &c[2], &c[3]) == 4, "content rect");
    CHECK(bp && sscanf(strchr(bp, '[') + 1, "%d,%d,%d,%d", &band[0], &band[1], &band[2], &band[3]) == 4 &&
          band[0] == c[0] && band[1] == c[1] && band[2] == c[2] && band[3] == 76,
          "the first band runs to the content's edges: %d,%d,%d,%d in %d,%d,%d,%d", band[0], band[1], band[2],
          band[3], c[0], c[1], c[2], c[3]);
    free(d);
    int r[4];
    CHECK(widget_rect(ui, "page", r) && r[0] == c[0] + 89 && r[1] == c[1] + 10 && r[2] == 2 * 55 + 40, "bevels at x 89, y 10");
    uint32_t *px = calloc(960 * 540, 4);
    rbui_render(ui, px, 960, RBUI_BG_CHECKERBOARD, NULL, 0, 0);
#define PX(x, y) (px[(y) * 960 + (x)] & 0xFFFFFFu)
    CHECK(PX(r[0] + 20, r[1] + 38) == 0x888888u && PX(r[0] + 55 + 20, r[1] + 38) == 0x777777u,
          "the chosen bevel is pressed, the other raised: %06x %06x", PX(r[0] + 20, r[1] + 38), PX(r[0] + 75, r[1] + 38));
    CHECK((PX(c[0], c[1] + 74) == 0x888888u || PX(c[0], c[1] + 74) == 0x949494u) && PX(c[0] + 1, c[1] + 75) == 0xFFFFFFu,
          "the band's separator (9.2.2 or 8.1 gray)");
#undef PX
    free(px);
    click(ui, r[0] + 55 + 20, r[1] + 20);
    CHECK(strstr(ev_last(&e), "\"ui_change\"") && strstr(ev_last(&e), "\"id\":\"page\"") &&
          strstr(ev_last(&e), "\"value\":\"sound\""), "a click on a bevel chooses it: %s", ev_last(&e));
    int n = e.n;
    click(ui, r[0] + 110 + 20, r[1] + 20);
    CHECK(e.n == n, "a disabled bevel can't be chosen");
    rbui_pointer(ui, r[0] + 20, r[1] + 20, 1);
    rbui_pointer(ui, r[0] + 20, r[1] + 120, 1);
    rbui_pointer(ui, r[0] + 20, r[1] + 120, 0);
    CHECK(e.n == n, "released off the bevel: nothing chosen");
    CHECK(rbui_focus(ui, "page") == 0, "bevels take the keyboard focus");
    key(ui, RBUI_KEY_LEFT, 0);
    CHECK(strstr(ev_last(&e), "\"value\":\"monitor\""), "Left chooses the bevel before: %s", ev_last(&e));
    n = e.n;
    key(ui, RBUI_KEY_LEFT, 0);
    CHECK(e.n == n, "nothing before the first");

    int s[4];
    CHECK(widget_rect(ui, "vol", s) && s[2] == 137 && s[3] == 16, "the slider's track is 137 px");
    CHECK(rbui_focus(ui, "vol") == 0, "the slider takes the focus");
    key(ui, RBUI_KEY_RIGHT, 0);
    CHECK(strstr(ev_last(&e), "\"id\":\"vol\"") && strstr(ev_last(&e), "\"value\":45"), "Right steps up: %s", ev_last(&e));
    key(ui, RBUI_KEY_END, 0);
    CHECK(strstr(ev_last(&e), "\"value\":100"), "End: the maximum");
    click(ui, s[0] + 6, s[1] + 8);
    CHECK(strstr(ev_last(&e), "\"value\":0"), "a click at the track's start: the minimum (%s)", ev_last(&e));
    rbui_pointer(ui, s[0] + 6, s[1] + 8, 1);   /* drag the thumb half way */
    rbui_pointer(ui, s[0] + 6 + (137 - 13) / 2, s[1] + 30, 1);
    rbui_pointer(ui, s[0] + 6 + (137 - 13) / 2, s[1] + 30, 0);
    CHECK(strstr(ev_last(&e), "\"value\":50"), "dragged half way: 50 (%s)", ev_last(&e));
    /* hostd re-sends the document with the old value: the slider keeps where it was moved. */
    CHECK(set(ui, kMonitors) == 0, "same doc again");
    char *v = rbui_describe(ui);
    CHECK(v && strstr(v, "\"id\":\"vol\",\"rect\"") && strstr(strstr(v, "\"id\":\"vol\""), "\"value\":50"),
          "the moved value survives an unchanged document");
    free(v);
    const char *bad[] = {
        "{\"id\":\"x\",\"items\":[{\"type\":\"slider\",\"id\":\"s\",\"min\":5,\"max\":5}]}",
        "{\"id\":\"x\",\"items\":[{\"type\":\"slider\",\"id\":\"s\",\"icons\":[\"speaker_low\"]}]}",
        "{\"id\":\"x\",\"items\":[{\"type\":\"bevels\",\"items\":[{\"id\":\"a\"}]}]}",
        "{\"id\":\"x\",\"items\":[{\"type\":\"band\",\"rule\":\"middle\"}]}",
    };
    char err[256];
    for (int i = 0; i < 4; ++i)
        CHECK(rbui_set_doc(ui, bad[i], strlen(bad[i]), err, sizeof err) != 0, "bad band/bevels/slider %d rejected", i);
    rbui_free(ui);
}

static void test_click_list(void)
{
    events e = {0};
    rbui *ui = new_ui(960, 540, &e);
    uint64_t t = 1000;
    CHECK(set(ui, kNets) == 0, "networks doc");
    int r[4];
    CHECK(widget_rect(ui, "network", r), "list rect");
    int x = r[0] + 40, row2 = r[1] + 1 + 18 + 9, row3 = row2 + 18;
    click(ui, x, row2);
    CHECK(e.n == 2 && strstr(e.ev[0], "\"ui_change\"") && strstr(e.ev[0], "\"value\":\"Cafe\""),
          "press selects the row: %s", e.n ? e.ev[0] : "");
    CHECK(count_actions(&e, 0, "network") == 1 && strstr(ev_last(&e), "\"values\":{\"network\":\"Cafe\"}"),
          "release on the row acts on it: %s", ev_last(&e));
    int n = e.n;
    rbui_tick(ui, t += 150);
    click(ui, x, row2);   /* the second click of a double-click */
    CHECK(e.n == n, "a double-click acts once (%d events)", e.n - n);
    rbui_tick(ui, t += 600);
    click(ui, x, row2);
    CHECK(e.n == n + 1 && count_actions(&e, n, "network") == 1, "a later click acts again");

    /* Press, drag off the list, release: the selection moves but nothing happens. */
    rbui_tick(ui, t += 600);
    n = e.n;
    rbui_pointer(ui, x, row3, 1);
    rbui_pointer(ui, x, r[1] + r[3] + 40, 1);
    rbui_pointer(ui, x, r[1] + r[3] + 40, 0);
    CHECK(count_actions(&e, n, "network") == 0 && strstr(e.ev[n], "\"value\":\"Work\""), "drag away cancels the action");
    /* Press one row, release on another: that one is chosen, as in a menu. */
    rbui_tick(ui, t += 600);
    n = e.n;
    rbui_pointer(ui, x, row3, 1);
    rbui_pointer(ui, x, row2, 1);
    rbui_pointer(ui, x, row2, 0);
    CHECK(count_actions(&e, n, "network") == 1 && strstr(ev_last(&e), "\"network\":\"Cafe\""), "release row chosen: %s",
          ev_last(&e));

    /* The keyboard only selects; Return is the default button's. */
    rbui_tick(ui, t += 600);
    n = e.n;
    key(ui, RBUI_KEY_DOWN, 0);
    CHECK(count_actions(&e, n, "network") == 0 && strstr(ev_last(&e), "\"value\":\"Work\""), "arrow selects only");
    key(ui, RBUI_KEY_RETURN, 0);
    CHECK(count_actions(&e, n, "cancel") == 1 && count_actions(&e, n, "network") == 0, "Return presses the default button");

    /* The guard survives a new document: the doubled click must not hit what replaced the row. */
    rbui_tick(ui, t += 600);
    click(ui, x, row2);
    n = e.n;
    char *again = subst(kNets, "\"title\"", "\"reset\":true,\"title\"");
    CHECK(set(ui, again) == 0, "replacement doc");
    rbui_tick(ui, t += 120);
    click(ui, x, row2);
    CHECK(e.n == n, "double-click into a replaced document is swallowed");
    rbui_tick(ui, t += 120);
    click(ui, x + 10, row2);   /* a third click is a click */
    CHECK(count_actions(&e, n, "network") == 1, "only one swallowed click");
    free(again);

    /* A plain list keeps double-click = default button. */
    char *plain = subst(kNets, "\"activate\":\"click\",", "");
    CHECK(set(ui, plain) == 0, "plain list doc");
    rbui_tick(ui, t += 1000);
    n = e.n;
    click(ui, x, row3);
    rbui_tick(ui, t += 100);
    click(ui, x, row3);
    CHECK(count_actions(&e, n, "network") == 0 && count_actions(&e, n, "cancel") == 1, "double-click on a plain list");
    free(plain);

    /* The check mark column: checked rows draw a mark, and every row's text moves over. */
    uint32_t *a = calloc(960 * 540, 4), *b = calloc(960 * 540, 4);
    CHECK(set(ui, kNets) == 0, "networks doc");
    rbui_render(ui, a, 960, RBUI_BG_CHECKERBOARD, NULL, 0, 0);
    char *nock = subst(kNets, "\"checked\":true,", "");
    CHECK(set(ui, nock) == 0, "unchecked doc");
    rbui_render(ui, b, 960, RBUI_BG_CHECKERBOARD, NULL, 0, 0);
    int diff_rows = 0;
    for (int yy = r[1]; yy < r[1] + r[3]; ++yy)
        diff_rows += memcmp(a + yy * 960 + r[0], b + yy * 960 + r[0], (size_t)r[2] * 4) != 0;
    CHECK(diff_rows > 18, "check column drawn (%d rows differ)", diff_rows);
    free(nock);
    /* "checked": false keeps the column: only the mark itself goes. */
    char *off = subst(kNets, "\"checked\":true,", "\"checked\":false,");
    CHECK(set(ui, off) == 0, "checked:false doc");
    rbui_render(ui, b, 960, RBUI_BG_CHECKERBOARD, NULL, 0, 0);
    int first = -1, last = -1;
    for (int yy = r[1]; yy < r[1] + r[3]; ++yy)
        if (memcmp(a + yy * 960 + r[0], b + yy * 960 + r[0], (size_t)r[2] * 4) != 0) {
            if (first < 0) first = yy;
            last = yy;
        }
    CHECK(first > r[1] && last < r[1] + 1 + 18, "only the mark differs (rows %d..%d of %d..)", first, last, r[1]);
    free(off);
    free(a);
    free(b);
    ev_clear(&e);
    rbui_free(ui);
}

static const char *kJoin =
    "{\"id\":\"wifi-join\",\"kind\":\"window\",\"title\":\"Wi-Fi\",\"width\":420,\"focus\":\"psk\",\"items\":["
    "{\"type\":\"field\",\"id\":\"psk\",\"label\":\"Password:\",\"secure\":true,\"value\":\"\"},"
    "{\"type\":\"checkbox\",\"id\":\"show\",\"label\":\"Show password\",\"reveals\":\"psk\"},"
    "{\"type\":\"buttons\",\"items\":[{\"id\":\"cancel\",\"label\":\"Cancel\"},{\"id\":\"join\",\"label\":\"Join\",\"default\":true}]}]}";

static uint64_t frame_hash(rbui *ui)
{
    uint32_t *px = calloc(960 * 540, 4);
    rbui_render(ui, px, 960, RBUI_BG_CHECKERBOARD, NULL, 0, 0);
    uint64_t h = fnv(px, 960 * 540);
    free(px);
    return h;
}

static void test_reveal(void)
{
    events e = {0};
    rbui *ui = new_ui(960, 540, &e);
    CHECK(set(ui, kJoin) == 0, "join doc");
    CHECK(strcmp(focus(ui), "psk") == 0, "password field focused");
    text(ui, "hunter22 ok");
    uint64_t masked = frame_hash(ui);
    int r[4];
    CHECK(widget_rect(ui, "show", r), "checkbox rect");
    int n = e.n;
    click(ui, r[0] + 6, r[1] + r[3] / 2);
    CHECK(e.n == n + 1 && strstr(ev_last(&e), "\"id\":\"show\",\"value\":true"), "toggle: %s", ev_last(&e));
    for (int i = n; i < e.n; ++i) CHECK(!strstr(e.ev[i], "hunter"), "reveal sends nothing about the value: %s", e.ev[i]);
    CHECK(strcmp(focus(ui), "psk") == 0, "focus stays in the field");
    uint64_t shown = frame_hash(ui);
    CHECK(shown != masked, "text shown");

    /* Shown exactly as a plain field with the same text. */
    rbui *ui2 = new_ui(960, 540, NULL);
    char *twin1 = subst(kJoin, "\"secure\":true,\"value\":\"\"", "\"value\":\"hunter22 ok\"");
    char *twin = subst(twin1, "\"reveals\":\"psk\"", "\"value\":true");
    CHECK(set(ui2, twin) == 0, "plain twin: %s", twin);
    key(ui2, RBUI_KEY_END, 0);
    CHECK(frame_hash(ui2) == shown, "revealed field draws like a plain field");
    rbui_free(ui2);
    free(twin1);
    free(twin);

    /* Option-arrow moves by words only while the text is visible. */
    key(ui, RBUI_KEY_LEFT, RBUI_MOD_OPTION);
    text(ui, "X");
    CHECK(strstr(ev_last(&e), "\"value\":\"hunter22 Xok\",\"secure\":true"), "word move when shown: %s", ev_last(&e));
    CHECK(widget_rect(ui, "show", r), "checkbox rect");
    click(ui, r[0] + 6, r[1] + r[3] / 2);
    CHECK(strstr(ev_last(&e), "\"id\":\"show\",\"value\":false"), "toggle back");
    key(ui, RBUI_KEY_LEFT, RBUI_MOD_OPTION);
    text(ui, "Y");
    CHECK(strstr(ev_last(&e), "\"value\":\"hunter22 YXok\",\"secure\":true"), "no word moves when masked: %s", ev_last(&e));
    /* Keyboard toggle, then a refresh of the same document keeps the choice. */
    key(ui, RBUI_KEY_TAB, 0);
    CHECK(strcmp(focus(ui), "show") == 0, "tab to the checkbox");
    key(ui, RBUI_KEY_SPACE, 0);
    uint64_t shown2 = frame_hash(ui);
    CHECK(set(ui, kJoin) == 0, "same doc again");
    CHECK(frame_hash(ui) == shown2, "refresh keeps the field shown and its text");
    ev_clear(&e);
    rbui_free(ui);
}

static void test_builtin(void)
{
    events e = {0};
    rbui *ui = new_ui(960, 540, &e);
    const char *d = rbui_builtin_doc("hostd-lost");
    CHECK(d && set(ui, d) == 0, "hostd-lost parses");
    CHECK(rbui_builtin_doc("fallback") == d, "fallback alias");
    CHECK(rbui_builtin_doc("nope") == NULL, "unknown builtin");
    int r[4];
    CHECK(widget_rect(ui, "restart", r) && widget_rect(ui, "shutdown", r), "restart and shutdown buttons");
    key(ui, RBUI_KEY_RETURN, 0);
    CHECK(strcmp(ev_last(&e), "{\"event\":\"ui_action\",\"doc\":\"hostd-lost\",\"id\":\"restart\",\"values\":{}}") == 0,
          "return restarts: %s", ev_last(&e));
    ev_clear(&e);
    rbui_free(ui);
}

/* Render, apply an input, render again: every changed pixel must lie inside
 * the reported damage. */
typedef void (*step_fn)(rbui *ui, unsigned r);

static int damage_ok(rbui *ui, uint32_t *a, uint32_t *b, int w, int h, const char *what)
{
    int x, y, dw, dh;
    int has = rbui_damage(ui, &x, &y, &dw, &dh);
    rbui_render(ui, b, w, RBUI_BG_CHECKERBOARD, NULL, 0, 1);
    for (int yy = 0; yy < h; ++yy)
        for (int xx = 0; xx < w; ++xx)
            if (a[yy * w + xx] != b[yy * w + xx]) {
                int in = has && xx >= x && yy >= y && xx < x + dw && yy < y + dh;
                if (!in) {
                    CHECK(0, "%s: pixel %d,%d changed outside damage (%d %d,%d %dx%d)", what, xx, yy, has, x, y, dw, dh);
                    return 0;
                }
            }
    memcpy(a, b, (size_t)w * (size_t)h * 4);
    return 1;
}

static unsigned rng_state = 12345;
static unsigned rnd(void)
{
    rng_state ^= rng_state << 13;
    rng_state ^= rng_state >> 17;
    rng_state ^= rng_state << 5;
    return rng_state;
}

static void random_input(rbui *ui, int w, int h, uint64_t *t)
{
    static const int keys[] = {RBUI_KEY_TAB, RBUI_KEY_RETURN, RBUI_KEY_ESCAPE, RBUI_KEY_BACKSPACE, RBUI_KEY_DELETE,
                               RBUI_KEY_LEFT, RBUI_KEY_RIGHT, RBUI_KEY_UP, RBUI_KEY_DOWN, RBUI_KEY_HOME, RBUI_KEY_END,
                               RBUI_KEY_PAGEUP, RBUI_KEY_PAGEDOWN, RBUI_KEY_SPACE, RBUI_KEY_KP_ENTER};
    static const char *texts[] = {"a", "Z", " ", "\xC3\xA9", "\xE2\x80\xA2", "\xF0\x9F\x8D\x8E", "\xFF", "7", ".", "\x01"};
    static unsigned buttons;
    switch (rnd() % 7) {
    case 6: rbui_wheel(ui, (int)(rnd() % 7) - 3); break;
    case 0: rbui_key(ui, keys[rnd() % 15], NULL, 1, rnd() % 16); break;
    case 1: rbui_key(ui, 0, texts[rnd() % 10], 1, (rnd() % 4 == 0) ? RBUI_MOD_COMMAND : 0); break;
    case 2:
    case 3: {
        /* Pointer: favour the window area. */
        int x = (int)(rnd() % (unsigned)w), y = (int)(rnd() % (unsigned)h);
        if (rnd() % 2) buttons ^= 1;
        rbui_pointer(ui, x, y, buttons);
        break;
    }
    case 4: *t += rnd() % 700; rbui_tick(ui, *t); break;
    default: rbui_pointer(ui, (int)(rnd() % (unsigned)w), (int)(rnd() % (unsigned)h), buttons); break;
    }
}

static int list_examples(char names[][64], int max)
{
    DIR *dir = opendir(examples_dir);
    int n = 0;
    if (!dir) return 0;
    struct dirent *de;
    while ((de = readdir(dir)) && n < max) {
        size_t l = strlen(de->d_name);
        if (l > 5 && l < 60 && strcmp(de->d_name + l - 5, ".json") == 0) {
            memcpy(names[n], de->d_name, l + 1);
            ++n;
        }
    }
    closedir(dir);
    /* Deterministic order. */
    for (int i = 0; i < n; ++i)
        for (int j = i + 1; j < n; ++j)
            if (strcmp(names[i], names[j]) > 0) {
                char tmp[64];
                memcpy(tmp, names[i], 64);
                memcpy(names[i], names[j], 64);
                memcpy(names[j], tmp, 64);
            }
    return n;
}

static char *load_example(const char *name, size_t *len)
{
    char path[1024];
    snprintf(path, sizeof(path), "%s/%s", examples_dir, name);
    return read_file(path, len);
}

static void test_damage(void)
{
    char names[64][64];
    int n = list_examples(names, 64);
    CHECK(n > 0, "examples found in %s", examples_dir);
    const int w = 800, h = 520;
    uint32_t *a = malloc((size_t)w * h * 4), *b = malloc((size_t)w * h * 4);
    for (int i = 0; i < n; ++i) {
        size_t len;
        char *json = load_example(names[i], &len);
        rbui *ui = new_ui(w, h, NULL);
        CHECK(json && rbui_set_doc(ui, json, len, NULL, 0) == 0, "%s parses", names[i]);
        rbui_render(ui, a, w, RBUI_BG_CHECKERBOARD, NULL, 0, 1);
        uint64_t t = 1000;
        for (int s = 0; s < 400; ++s) {
            random_input(ui, w, h, &t);
            char what[128];
            snprintf(what, sizeof(what), "%.60s step %d", names[i], s);
            if (!damage_ok(ui, a, b, w, h, what)) break;
            if (s % 97 == 0) {
                /* Replacing the doc (same id) must damage correctly too. */
                rbui_set_doc(ui, json, len, NULL, 0);
                if (!damage_ok(ui, a, b, w, h, what)) break;
            }
        }
        free(json);
        rbui_free(ui);
    }
    free(a);
    free(b);
}

/* ---- Golden renders and layout determinism ------------------------------------ */

typedef struct { char name[96]; uint64_t hash; } golden;

static int load_golden(const char *path, golden *g, int max)
{
    FILE *f = fopen(path, "r");
    int n = 0;
    if (!f) return 0;
    while (n < max && fscanf(f, "%95s %llx", g[n].name, (unsigned long long *)&g[n].hash) == 2) ++n;
    fclose(f);
    return n;
}

static void test_golden(void)
{
    char names[64][64];
    int n = list_examples(names, 64);
    static const int sizes[][2] = {{960, 540}, {640, 480}};
    char gpath[1024];
    snprintf(gpath, sizeof(gpath), "%s/../tests/golden.txt", examples_dir);
    golden *g = calloc(512, sizeof(golden));
    int ng = load_golden(gpath, g, 512);
    golden *out = calloc(512, sizeof(golden));
    int nout = 0;
    for (int i = 0; i < n + 1; ++i) {
        size_t len;
        char *json;
        const char *name = i < n ? names[i] : "builtin:hostd-lost";
        if (i < n) json = load_example(names[i], &len);
        else {
            json = strdup(rbui_builtin_doc("hostd-lost"));
            len = strlen(json);
        }
        for (int s = 0; s < 2; ++s) {
            int w = sizes[s][0], h = sizes[s][1];
            uint32_t *a = malloc((size_t)w * h * 4), *b = malloc((size_t)w * h * 4);
            rbui *ui = new_ui(w, h, NULL);
            CHECK(rbui_set_doc(ui, json, len, NULL, 0) == 0, "%s parses", name);
            rbui_render(ui, a, w, RBUI_BG_CHECKERBOARD, NULL, 0, 0);
            /* Determinism: same doc again, and a fresh instance, render identically. */
            rbui_set_doc(ui, json, len, NULL, 0);
            rbui_render(ui, b, w, RBUI_BG_CHECKERBOARD, NULL, 0, 0);
            CHECK(memcmp(a, b, (size_t)w * h * 4) == 0, "%s %dx%d: re-set changes the render", name, w, h);
            rbui *ui2 = new_ui(w, h, NULL);
            rbui_set_doc(ui2, json, len, NULL, 0);
            rbui_render(ui2, b, w, RBUI_BG_CHECKERBOARD, NULL, 0, 0);
            CHECK(memcmp(a, b, (size_t)w * h * 4) == 0, "%s %dx%d: not deterministic", name, w, h);
            /* The window stays inside the canvas. */
            char *d = rbui_describe(ui);
            int wr[4] = {0};
            char *p = d ? strstr(d, "\"window\":[") : NULL;
            CHECK(p && sscanf(p + 10, "%d,%d,%d,%d", &wr[0], &wr[1], &wr[2], &wr[3]) == 4, "%s window", name);
            CHECK(wr[0] >= 0 && wr[1] >= 0 && wr[0] + wr[2] < w && wr[1] + wr[3] < h, "%s %dx%d: window %d,%d %dx%d off canvas",
                  name, w, h, wr[0], wr[1], wr[2], wr[3]);
            free(d);
            uint64_t hash = fnv(a, (size_t)w * h);
            golden *o = &out[nout++];
            snprintf(o->name, sizeof(o->name), "%s@%dx%d", name, w, h);
            o->hash = hash;
            int found = 0;
            for (int k = 0; k < ng; ++k)
                if (strcmp(g[k].name, o->name) == 0) {
                    found = 1;
                    if (!update_golden)
                        CHECK(g[k].hash == hash, "%s: render changed (golden %016llx, now %016llx); review PNGs and "
                              "run with --update-golden", o->name, (unsigned long long)g[k].hash, (unsigned long long)hash);
                }
            if (!found && !update_golden) CHECK(0, "%s: no golden hash (run with --update-golden)", o->name);
            rbui_free(ui);
            rbui_free(ui2);
            free(a);
            free(b);
        }
        free(json);
    }
    if (update_golden) {
        FILE *f = fopen(gpath, "w");
        CHECK(f != NULL, "write %s", gpath);
        if (f) {
            for (int i = 0; i < nout; ++i) fprintf(f, "%s %016llx\n", out[i].name, (unsigned long long)out[i].hash);
            fclose(f);
            printf("updated %s (%d hashes)\n", gpath, nout);
        }
    }
    free(g);
    free(out);
}

static void test_long_menu(void)
{
    /* A popup with more options than fit on the canvas gets a scrolling menu. */
    char doc[8192];
    int o = snprintf(doc, sizeof(doc), "{\"id\":\"m\",\"items\":[{\"type\":\"popup\",\"id\":\"p\",\"value\":\"o10\",\"options\":[");
    for (int i = 0; i < 60; ++i) o += snprintf(doc + o, sizeof(doc) - (size_t)o, "%s{\"id\":\"o%d\",\"label\":\"Option %d\"}", i ? "," : "", i, i);
    snprintf(doc + o, sizeof(doc) - (size_t)o, "]},{\"type\":\"list\",\"id\":\"l\",\"rows\":3,\"items\":["
             "{\"id\":\"a\"},{\"id\":\"b\"},{\"id\":\"c\"},{\"id\":\"d\"},{\"id\":\"e\"},{\"id\":\"f\"}]}]}");
    events e = {0};
    rbui *ui = new_ui(480, 320, &e);
    CHECK(set(ui, doc) == 0, "long popup doc");
    rbui_focus(ui, "p");
    key(ui, RBUI_KEY_SPACE, 0);
    char *d = rbui_describe(ui);
    int m[4] = {0};
    char *p = d ? strstr(d, "\"menu\":[") : NULL;
    CHECK(p && sscanf(p + 8, "%d,%d,%d,%d", &m[0], &m[1], &m[2], &m[3]) == 4 && m[1] >= 0 && m[1] + m[3] <= 320,
          "menu fits the canvas (%d,%d %dx%d)", m[0], m[1], m[2], m[3]);
    free(d);
    key(ui, RBUI_KEY_END, 0);
    key(ui, RBUI_KEY_RETURN, 0);
    CHECK(strstr(ev_last(&e), "\"value\":\"o59\""), "end+return picks the last option: %s", ev_last(&e));
    int r[4];
    CHECK(widget_rect(ui, "l", r), "list rect");
    rbui_pointer(ui, r[0] + 10, r[1] + 10, 0);
    rbui_wheel(ui, 2);
    d = rbui_describe(ui);
    p = d ? strstr(d, "\"id\":\"l\"") : NULL;
    CHECK(p && strstr(p, "\"scroll\":2"), "wheel scrolls the list under the pointer");
    free(d);
    ev_clear(&e);
    rbui_free(ui);
}

/* Regressions from the code review (tracking across doc updates, radio focus,
 * scrolled clicks, scroll preservation, double-click across re-sends). */
static char *after(char *hay, const char *pat)
{
    char *p = hay ? strstr(hay, pat) : NULL;
    return p ? p + strlen(pat) : NULL;
}

static void test_regressions(void)
{
    events e = {0};
    rbui *ui = new_ui(960, 540, &e);
    const char *four = "{\"id\":\"d\",\"items\":[{\"type\":\"radio\",\"id\":\"r\",\"options\":["
                       "{\"id\":\"a\"},{\"id\":\"b\"},{\"id\":\"c\"},{\"id\":\"d\"}]},"
                       "{\"type\":\"buttons\",\"items\":[{\"id\":\"ok\",\"label\":\"OK\"}]}]}";
    const char *one = "{\"id\":\"d\",\"items\":[{\"type\":\"radio\",\"id\":\"r\",\"options\":[{\"id\":\"a\"}]},"
                      "{\"type\":\"checkbox\",\"id\":\"ok\",\"label\":\"OK\"}]}";
    CHECK(set(ui, four) == 0, "four options");
    int r[4];
    char *d = rbui_describe(ui);
    char *p = after(d, "{\"id\":\"d\",\"rect\":[");
    CHECK(p && sscanf(p, "%d,%d,%d,%d", &r[0], &r[1], &r[2], &r[3]) == 4, "option d rect");
    free(d);
    rbui_pointer(ui, r[0] + 4, r[1] + 4, 1);
    CHECK(set(ui, one) == 0, "radio shrinks while tracking");
    rbui_pointer(ui, r[0] + 6, r[1] + 6, 1);
    rbui_pointer(ui, r[0] + 6, r[1] + 6, 0);
    CHECK(e.n == 0, "no event from a cancelled radio track");
    CHECK(set(ui, four) == 0, "four options again");
    CHECK(widget_rect(ui, "ok", r), "ok button");
    rbui_pointer(ui, r[0] + 4, r[1] + 4, 1);
    CHECK(set(ui, one) == 0, "button becomes a checkbox while pressed");
    rbui_pointer(ui, r[0] + 4, r[1] + 4, 0);
    CHECK(e.n == 0, "no ui_action for a widget that changed type: %s", ev_last(&e));

    /* Clicking group B leaves the focused group A's keyboard option alone. */
    const char *two = "{\"id\":\"g\",\"items\":[{\"type\":\"radio\",\"id\":\"A\",\"value\":\"a1\",\"options\":["
                      "{\"id\":\"a1\"},{\"id\":\"a2\"},{\"id\":\"a3\"}]},{\"type\":\"radio\",\"id\":\"B\",\"options\":["
                      "{\"id\":\"b1\"},{\"id\":\"b2\"},{\"id\":\"b3\"}]}]}";
    CHECK(set(ui, two) == 0, "two groups");
    rbui_focus(ui, "A");
    d = rbui_describe(ui);
    p = after(d, "{\"id\":\"b3\",\"rect\":[");
    CHECK(p && sscanf(p, "%d,%d", &r[0], &r[1]) == 2, "b3 rect");
    free(d);
    click(ui, r[0] + 4, r[1] + 4);
    ev_clear(&e);
    key(ui, RBUI_KEY_SPACE, 0);
    CHECK(e.n == 0, "space on A changes nothing: %s", ev_last(&e));

    /* A same-id update keeps the user's window scroll; a click near the
     * bottom of the viewport selects the row under the pointer. */
    char tall[16384];
    int o = snprintf(tall, sizeof(tall), "{\"id\":\"t\",\"items\":[{\"type\":\"field\",\"id\":\"f\"},");
    for (int i = 0; i < 20; ++i) o += snprintf(tall + o, sizeof(tall) - (size_t)o, "{\"type\":\"text\",\"text\":\"Line %d\"},", i);
    snprintf(tall + o, sizeof(tall) - (size_t)o, "{\"type\":\"list\",\"id\":\"L\",\"rows\":6,\"items\":[{\"id\":\"i0\"},{\"id\":\"i1\"},"
             "{\"id\":\"i2\"},{\"id\":\"i3\"},{\"id\":\"i4\"},{\"id\":\"i5\"}]},{\"type\":\"progress\",\"value\":0.1},{\"type\":\"buttons\",\"items\":[{\"id\":\"ok\",\"label\":\"OK\",\"default\":true}]}]}");
    rbui_resize(ui, 640, 400);
    CHECK(set(ui, tall) == 0, "tall doc");
    rbui_pointer(ui, 320, 200, 0);
    rbui_wheel(ui, 5);
    CHECK(set(ui, tall) == 0, "same doc again");
    d = rbui_describe(ui);
    p = d ? strstr(d, "\"scroll\":") : NULL;
    CHECK(p && atoi(p + 9) == 80, "scroll kept across same-id update (%d)", p ? atoi(p + 9) : -1);
    free(d);
    int ok = 0;
    for (int i = 0; i < 60 && !ok; ++i) {
        /* Scroll until row i1 shows just above the bottom of the viewport. */
        d = rbui_describe(ui);
        p = after(d, "{\"id\":\"i1\",\"rect\":[");
        ok = p && sscanf(p, "%d,%d,%d,%d", &r[0], &r[1], &r[2], &r[3]) == 4 && r[1] + 9 < 320 && r[1] > 40;
        free(d);
        if (!ok) rbui_wheel(ui, 1);
    }
    if (ok) {
        ev_clear(&e);
        click(ui, r[0] + 10, r[1] + 9);
        CHECK(strstr(ev_last(&e), "\"id\":\"L\",\"value\":\"i1\""), "partly visible list click selects: %s", ev_last(&e));
        /* hostd re-sends the doc after ui_change: the second click still counts. */
        CHECK(set(ui, tall) == 0, "re-send");
        rbui_tick(ui, 1100);
        click(ui, r[0] + 10, r[1] + 9);
        CHECK(strstr(ev_last(&e), "\"ui_action\"") && strstr(ev_last(&e), "\"id\":\"ok\""),
              "double-click survives a re-sent doc: %s", ev_last(&e));
    } else {
        CHECK(0, "list row i1 not clickable in the scrolled view (%d)", r[1]);
    }
    ev_clear(&e);
    rbui_free(ui);

    /* Labelled controls placed directly in columns keep their labels. */
    ui = new_ui(960, 540, NULL);
    CHECK(set(ui, "{\"id\":\"c\",\"items\":[{\"type\":\"columns\",\"items\":[{\"type\":\"field\",\"id\":\"f\","
                  "\"label\":\"Name:\"},{\"type\":\"text\",\"text\":\"x\"}]}]}") == 0, "columns field");
    char *dd = rbui_describe(ui);
    int cr[4], fr[4];
    char *cp = after(dd, "\"type\":\"columns\",\"rect\":[");
    char *fp = after(dd, "\"id\":\"f\",\"rect\":[");
    CHECK(cp && fp && sscanf(cp, "%d", &cr[0]) == 1 && sscanf(fp, "%d", &fr[0]) == 1 && fr[0] > cr[0] + 30,
          "field sits right of its label");
    free(dd);
    rbui_free(ui);
}

static void test_tick(void)
{
    rbui *ui = new_ui(640, 480, NULL);
    uint32_t *frame = malloc(640 * 480 * 4);
    const char *plain = "{\"id\":\"t\",\"kind\":\"alert\",\"items\":[{\"type\":\"text\",\"text\":\"Hi\"},"
                        "{\"type\":\"buttons\",\"items\":[{\"id\":\"ok\",\"label\":\"OK\",\"default\":true}]}]}";
    CHECK(set(ui, plain) == 0, "plain doc");
    CHECK(rbui_tick(ui, 1100) == 1, "pending damage asks for a render");
    rbui_render(ui, frame, 640, RBUI_BG_CHECKERBOARD, NULL, 0, 1);
    CHECK(rbui_tick(ui, 1200) == 0, "idle panel needs no ticks");
    key(ui, RBUI_KEY_RETURN, 0);
    int t = rbui_tick(ui, 1210);
    CHECK(t > 0 && t <= 140, "button flash wants a tick within its 140 ms (%d)", t);
    rbui_render(ui, frame, 640, RBUI_BG_CHECKERBOARD, NULL, 0, 1);
    rbui_tick(ui, 1400);
    rbui_render(ui, frame, 640, RBUI_BG_CHECKERBOARD, NULL, 0, 1);
    CHECK(rbui_tick(ui, 1500) == 0, "flash over");
    const char *busy = "{\"id\":\"b\",\"items\":[{\"type\":\"progress\",\"indeterminate\":true},"
                       "{\"type\":\"field\",\"id\":\"f\"}]}";
    CHECK(set(ui, busy) == 0, "busy doc");
    rbui_render(ui, frame, 640, RBUI_BG_CHECKERBOARD, NULL, 0, 1);
    t = rbui_tick(ui, 1600);
    CHECK(t > 0 && t <= 50, "barber pole and caret keep ticking (%d)", t);
    int x, y, w, h;
    rbui_render(ui, frame, 640, RBUI_BG_CHECKERBOARD, NULL, 0, 1);
    rbui_tick(ui, 1700);
    CHECK(rbui_damage(ui, &x, &y, &w, &h) && w <= 640 && h < 100, "animation damage is local (%dx%d)", w, h);
    free(frame);
    rbui_free(ui);
}

static void test_scroll(void)
{
    /* A tall document on a small canvas scrolls inside the window. */
    char doc[16384];
    int o = snprintf(doc, sizeof(doc), "{\"id\":\"tall\",\"title\":\"Tall\",\"items\":[");
    for (int i = 0; i < 30; ++i)
        o += snprintf(doc + o, sizeof(doc) - (size_t)o, "{\"type\":\"checkbox\",\"id\":\"c%d\",\"label\":\"Option %d\"},", i, i);
    snprintf(doc + o, sizeof(doc) - (size_t)o, "{\"type\":\"buttons\",\"items\":[{\"id\":\"ok\",\"label\":\"OK\",\"default\":true}]}]}");
    events e = {0};
    rbui *ui = new_ui(640, 360, &e);
    CHECK(set(ui, doc) == 0, "tall doc");
    char *d = rbui_describe(ui);
    CHECK(d && strstr(d, "\"scrollable\":true") && strstr(d, "\"scroll\":0"), "scrollable at top");
    free(d);
    rbui_focus(ui, "c29");
    d = rbui_describe(ui);
    int scroll = -1;
    char *p = d ? strstr(d, "\"scroll\":") : NULL;
    if (p) scroll = atoi(p + 9);
    CHECK(scroll > 0, "focus reveals the last checkbox (scroll %d)", scroll);
    free(d);
    int r[4];
    CHECK(widget_rect(ui, "c29", r) && r[1] > 0 && r[1] + r[3] < 360, "revealed widget on screen (%d)", r[1]);
    key(ui, RBUI_KEY_SPACE, 0);
    CHECK(strstr(ev_last(&e), "\"id\":\"c29\",\"value\":true"), "toggle after scroll");
    CHECK(widget_rect(ui, "ok", r), "ok rect");
    click(ui, r[0] + 5, r[1] + 5);
    CHECK(strstr(ev_last(&e), "\"ui_action\""), "pinned button clickable while scrolled");
    /* Page keys and the wheel scroll the window when no list has focus. */
    rbui_focus(ui, NULL);
    for (int i = 0; i < 10; ++i) key(ui, RBUI_KEY_PAGEUP, 0);
    d = rbui_describe(ui);
    CHECK(d && strstr(d, "\"scroll\":0,"), "page up to the top");
    free(d);
    rbui_pointer(ui, 320, 120, 0);
    rbui_wheel(ui, 3);
    d = rbui_describe(ui);
    p = d ? strstr(d, "\"scroll\":") : NULL;
    CHECK(p && atoi(p + 9) == 48, "wheel scrolls 3 lines (%s)", p ? p : "?");
    free(d);
    ev_clear(&e);
    rbui_free(ui);
}

/* ---- Fuzzing ------------------------------------------------------------------- */

static void fuzz_one(const char *buf, size_t len, int inputs)
{
    int cw = 320 + (int)(rnd() % 700), ch = 200 + (int)(rnd() % 500);
    if (rnd() % 16 == 0) {   /* degenerate canvases too */
        cw = 1 + (int)(rnd() % 40);
        ch = 1 + (int)(rnd() % 40);
    }
    rbui *ui = new_ui(cw, ch, NULL);
    char err[64];
    int rc = rbui_set_doc(ui, buf, len, err, sizeof(err));
    CHECK(rc == 0 || rc == -1, "rc %d", rc);
    int w = 0, h = 0;
    char *d = rbui_describe(ui);
    char *p = d ? strstr(d, "\"canvas\":[") : NULL;
    if (p) sscanf(p + 10, "%d,%d", &w, &h);
    free(d);
    uint32_t *frame = malloc((size_t)w * (size_t)h * 4);
    uint64_t t = 1000;
    for (int i = 0; i < inputs; ++i) random_input(ui, w, h, &t);
    rbui_render(ui, frame, w, (int)(rnd() % 3), NULL, 0, 1);
    free(frame);
    rbui_free(ui);
}

static void test_fuzz(void)
{
    char names[64][64];
    int n = list_examples(names, 64);
    char *docs[64];
    size_t lens[64];
    for (int i = 0; i < n; ++i) docs[i] = load_example(names[i], &lens[i]);
    static const char *frags[] = {
        "{", "}", "[", "]", ",", ":", "\"", "null", "true", "1e309", "-1", "0.5", "\"type\"", "\"items\"",
        "\"text\"", "\"list\"", "\"field\"", "\"buttons\"", "\"id\"", "\"value\"", "\"options\"", "\"\\u0000\"",
        "\"\\ud800\"", "\"\xff\xfe\"", "\"columns\"", "\"group\"", "\"qr\"", "\"matrix\"", "\"rows\"", "999999999",
    };
    for (long it = 0; it < fuzz_iters; ++it) {
        char buf[8192];
        size_t len = 0;
        unsigned mode = rnd() % 4;
        if (mode == 0 || n == 0) {
            /* Garbage soup of JSON fragments and random bytes. */
            int parts = (int)(rnd() % 40);
            for (int i = 0; i < parts && len < sizeof(buf) - 32; ++i) {
                if (rnd() % 4 == 0) buf[len++] = (char)(rnd() % 256);
                else {
                    const char *f = frags[rnd() % (sizeof(frags) / sizeof(frags[0]))];
                    size_t fl = strlen(f);
                    memcpy(buf + len, f, fl);
                    len += fl;
                }
            }
        } else {
            /* Mutate a real example: flips, truncation, splices. */
            int k = (int)(rnd() % (unsigned)n);
            len = lens[k] < sizeof(buf) ? lens[k] : sizeof(buf);
            memcpy(buf, docs[k], len);
            int muts = 1 + (int)(rnd() % 6);
            for (int m = 0; m < muts && len > 0; ++m) {
                size_t pos = rnd() % len;
                switch (rnd() % 4) {
                case 0: buf[pos] = (char)(rnd() % 256); break;
                case 1: len = pos; break;
                case 2: {
                    const char *f = frags[rnd() % (sizeof(frags) / sizeof(frags[0]))];
                    size_t fl = strlen(f);
                    if (len + fl < sizeof(buf)) {
                        memmove(buf + pos + fl, buf + pos, len - pos);
                        memcpy(buf + pos, f, fl);
                        len += fl;
                    }
                    break;
                }
                default: {
                    size_t a = rnd() % len, l = rnd() % 64;
                    if (a + l > len) l = len - a;
                    memmove(buf + a, buf + a + l, len - a - l);
                    len -= l;
                }
                }
            }
        }
        fuzz_one(buf, len, mode == 3 ? 60 : 8);
    }
    /* Structural extremes that parse. */
    char big[200000];
    int o = snprintf(big, sizeof(big), "{\"id\":\"big\",\"items\":[{\"type\":\"list\",\"id\":\"l\",\"rows\":40,\"items\":[");
    for (int i = 0; i < 1024 && o < 180000; ++i)
        o += snprintf(big + o, sizeof(big) - (size_t)o, "%s{\"id\":\"i%d\",\"label\":\"%0100d\",\"detail\":\"d\",\"icon\":\"cd\",\"signal\":%d,\"lock\":true}",
                      i ? "," : "", i, i, i % 5);
    o += snprintf(big + o, sizeof(big) - (size_t)o, "]},{\"type\":\"text\",\"text\":\"");
    for (int i = 0; i < 4000 && o < (int)sizeof(big) - 100; ++i) big[o++] = (char)('a' + i % 26);
    snprintf(big + o, sizeof(big) - (size_t)o, "\"}]}");
    fuzz_one(big, strlen(big), 200);
    char deep[4096];
    o = snprintf(deep, sizeof(deep), "{\"id\":\"d\",\"items\":");
    for (int i = 0; i < 300; ++i) o += snprintf(deep + o, sizeof(deep) - (size_t)o, "[");
    fuzz_one(deep, (size_t)o, 4);
    for (int i = 0; i < n; ++i) free(docs[i]);
}

/* Platinum bundle fonts and icons (assets.c): a synthetic bundle, Mac OS 9 blending. */
static void write_file(const char *path, const unsigned char *b, size_t n)
{
    FILE *f = fopen(path, "wb");
    if (!f) return;
    fwrite(b, 1, n, f);
    fclose(f);
}

static void test_assets(void)
{
    char dir[] = "/tmp/rbui-assets-XXXXXX";
    if (!mkdtemp(dir)) {
        CHECK(0, "mkdtemp");
        return;
    }
    char path[256];
    snprintf(path, sizeof(path), "%s/fonts", dir);
    mkdir(path, 0700);
    snprintf(path, sizeof(path), "%s/icons", dir);
    mkdir(path, 0700);
    /* RBF1: size 12, ascent 12, descent 4, leading 0, smooth; 2 glyphs: 'A' 3x1 coverage 16/8/1, 'H' */
    unsigned char rbf[64] = {'R', 'B', 'F', '1', 12, 12, 4, 0, 1, 0, 0, 0, 2, 0};
    size_t n = 14;
    const unsigned char ga[] = {65, 0, 0, 0, 7, 0, 9, 3, 1, 16, 8, 1};
    memcpy(rbf + n, ga, sizeof(ga));
    n += sizeof(ga);
    const unsigned char gh[] = {72, 0, 0, 0, 8, 0, 9, 1, 1, 16};
    memcpy(rbf + n, gh, sizeof(gh));
    n += sizeof(gh);
    snprintf(path, sizeof(path), "%s/fonts/charcoal-12.rbf", dir);
    write_file(path, rbf, n);
    unsigned char rbi[8 + 16 * 16 * 4] = {'R', 'B', 'I', '1', 16, 0, 16, 0};
    for (int i = 0; i < 16 * 16; ++i) {
        rbi[8 + i * 4] = 0xDD;
        rbi[8 + i * 4 + 3] = 255;
    }
    snprintf(path, sizeof(path), "%s/icons/stop-16.rbi", dir);
    write_file(path, rbi, sizeof(rbi));

    assets_reset();
    CHECK(rbui_load_assets(dir) == 2, "loads the font and the icon");
    const rbf_font *f = rbf_get("system12");
    CHECK(f && f->levels == 1 && f->cap == 9, "system12 is the bundle strike");
    CHECK(rbf_advance(f, 'A') == 7, "bundle advance");
    uint32_t px[10 * 12];
    for (int i = 0; i < 10 * 12; ++i) px[i] = 0xDDDDDDu;
    rb_gfx g = {px, 10, 10, 12, {0, 0, 10, 12}, 0, 0};
    tx_draw(&g, f, 1, 1, 10, "A", 1, 0x000000u);
    /* level 16 is the text colour, 8 and 1 blend as QuickDraw does */
    CHECK(px[1 * 10 + 1] == 0x000000u, "level 16");
    CHECK(px[1 * 10 + 2] == 0x676767u, "level 8 = dd + ((-221*136)>>8)");
    CHECK(px[1 * 10 + 3] == 0xCECECEu, "level 1 = dd + ((-221*17)>>8)");
    const rbi_icon *ic = icon_get("stop", 16);
    CHECK(ic && ic->argb && ic->argb[0] == 0xFFDD0000u, "bundle icon replaces the original art");
    /* hostd rebuilds the bundle with a newer rbplatinum while the panel runs: the next
     * document picks up what it added (here Monitors' 40 px sun) without a restart. */
    CHECK(assets_refresh() == 0, "an unchanged bundle isn't reloaded");
    CHECK(assets_icon("monitor_page", 40) == NULL, "no 40 px sun yet");
    unsigned char big[8 + 40 * 40 * 4] = {'R', 'B', 'I', '1', 40, 0, 40, 0};
    for (int i = 0; i < 40 * 40; ++i) big[8 + i * 4 + 3] = 255;
    char sun[256], man[256];
    snprintf(sun, sizeof(sun), "%s/icons/monitor-page-40.rbi", dir);
    write_file(sun, big, sizeof(big));
    snprintf(man, sizeof(man), "%s/manifest.json", dir);
    const char *json = "{\"generator\": \"rbplatinum 6\"}";
    write_file(man, (const unsigned char *)json, strlen(json));
    CHECK(assets_refresh() == 1 && assets_icon("monitor_page", 40) != NULL, "a rebuilt bundle is reloaded");
    CHECK(icon_get("stop", 16) && icon_get("stop", 16)->argb[0] == 0xFFDD0000u, "and keeps what it had");
    CHECK(assets_refresh() == 0, "once");
    remove(sun);
    remove(man);
    assets_reset();
    rbui_load_assets("");
    CHECK(rbf_get("system12")->levels == 0, "original fonts again");
    remove(path);
    snprintf(path, sizeof(path), "%s/fonts/charcoal-12.rbf", dir);
    remove(path);
    snprintf(path, sizeof(path), "%s/fonts", dir);
    rmdir(path);
    snprintf(path, sizeof(path), "%s/icons", dir);
    rmdir(path);
    rmdir(dir);
}

/* Mac OS 9.2.1 controls from a (synthetic) bundle: every control sprite and the
 * palette, or the 8.1 look throughout. */
static const char *const kNine[] = {
    "movable", "alert", "button-normal", "button-pressed", "button-disabled", "button-default",
    "button-default-pressed", "checkbox-off", "checkbox-on", "checkbox-off-pressed", "checkbox-on-pressed",
    "checkbox-off-disabled", "checkbox-on-disabled", "radio-off", "radio-on", "radio-off-pressed",
    "radio-on-pressed", "radio-off-disabled", "radio-on-disabled", "popup", "popup-disabled", "field",
    "field-focus", "group", "progress", "progress-fill", "scroll-track-v", "scroll-thumb-v", "scroll-grip-v",
    "scroll-up", "scroll-down", "menu",
};
#define NNINE (int)(sizeof(kNine) / sizeof(kNine[0]))

static void write_nine(const char *dir, int skip)
{
    char path[512];
    snprintf(path, sizeof(path), "%s/chrome", dir);
    mkdir(path, 0700);
    char man[8192];
    int m = snprintf(man, sizeof(man), "{\"palette\": {\"ramp\": [\"#eeeeee\", \"#ccccff\", \"#9999ff\", "
                     "\"#6666cc\", \"#333399\", \"#000088\", \"#000055\", \"#000000\"], \"highlight\": \"#ccccff\"}, "
                     "\"chrome\": {");
    for (int i = 0; i < NNINE; ++i) {
        if (i == skip) continue;
        /* 8 x 8, a 2-pixel border of one colour around another; slices 2 */
        unsigned char rbi[8 + 8 * 8 * 4] = {'R', 'B', 'I', '1', 8, 0, 8, 0};
        for (int k = 0; k < 64; ++k) {
            int x = k % 8, y = k / 8, edge = x < 2 || y < 2 || x > 5 || y > 5;
            rbi[8 + k * 4] = (unsigned char)(edge ? 0x10 + i : 0x80 + i);
            rbi[8 + k * 4 + 3] = 255;
        }
        snprintf(path, sizeof(path), "%s/chrome/%s.rbi", dir, kNine[i]);
        write_file(path, rbi, sizeof(rbi));
        m += snprintf(man + m, sizeof(man) - (size_t)m, "%s\"%s\": {\"rbi\": \"chrome/%s.rbi\", \"slice\": [2, 2, 2, 2]}",
                      m > 0 && man[m - 1] == '{' ? "" : ", ", kNine[i], kNine[i]);
    }
    m += snprintf(man + m, sizeof(man) - (size_t)m, "}}");
    snprintf(path, sizeof(path), "%s/manifest.json", dir);
    write_file(path, (const unsigned char *)man, (size_t)m);
}

static void remove_nine(const char *dir)
{
    char path[512];
    for (int i = 0; i < NNINE; ++i) {
        snprintf(path, sizeof(path), "%s/chrome/%s.rbi", dir, kNine[i]);
        remove(path);
    }
    snprintf(path, sizeof(path), "%s/manifest.json", dir);
    remove(path);
    snprintf(path, sizeof(path), "%s/chrome", dir);
    rmdir(path);
}

static void test_controls9(void)
{
    char dir[] = "/tmp/rbui-nine-XXXXXX";
    if (!mkdtemp(dir)) {
        CHECK(0, "mkdtemp");
        return;
    }
    assets_reset();
    CHECK(!pt_v9() && pt_window_extra_h(1) == PT_TITLE_H + 7, "8.1 look without a bundle");
    write_nine(dir, -1);
    rbui_load_assets(dir);
    CHECK(pt_v9(), "every sprite and the palette: the 9.2.1 look");
    CHECK(g_pal.face == 0xDDDDDDu && g_pal.menu_hi == 0x333399u && g_pal.hilite == 0xCCCCFFu, "9.2.1 colours");
    CHECK(pt_window_extra_h(1) == 28 && pt_window_extra_h(0) == 12, "movable modal and alert frames");
    rb_rect c = pt_window_content(rr(0, 0, 100, 100), 1);
    CHECK(c.x == 6 && c.y == 24 && c.w == 88 && c.h == 72, "content below the title bar and bevel");
    uint32_t px[40 * 30];
    for (int i = 0; i < 40 * 30; ++i) px[i] = 0x123456u;
    rb_gfx g = {px, 40, 40, 30, {0, 0, 40, 30}, 0, 0};
    pt_button(&g, rr(5, 5, 30, 20), NULL, PT_NORMAL, 0, NULL);   /* button-normal is kNine[2] */
    CHECK(px[5 * 40 + 5] == 0x120000u && px[15 * 40 + 20] == 0x820000u && px[4 * 40 + 5] == 0x123456u,
          "a button is its sprite, nine-sliced to the rect");
    int ty, th;
    pt_bar_track(rr(0, 0, 16, 100), &ty, &th);
    CHECK(ty == 1 && th == 68 && pt_bar_arrow(rr(0, 0, 16, 100), 75) == SB_UP &&
          pt_bar_arrow(rr(0, 0, 16, 100), 90) == SB_DOWN && pt_bar_arrow(rr(0, 0, 16, 100), 10) == SB_NONE,
          "both scroll arrows at the bottom");
    CHECK(pt_check_text_dx() == 16 && pt_field_text_dx() == 4, "9.2.1 label and text insets");
    uint32_t p_en[40 * 30], p_dis[40 * 30];
    for (int i = 0; i < 40 * 30; ++i) p_en[i] = p_dis[i] = 0xDDDDDDu;
    rb_gfx ge = {p_en, 40, 40, 30, {0, 0, 40, 30}, 0, 0};
    rb_gfx gd = {p_dis, 40, 40, 30, {0, 0, 40, 30}, 0, 0};
    pt_progress(&ge, rr(4, 8, 32, 12), 0.5, 0, 0, 0);
    pt_progress(&gd, rr(4, 8, 32, 12), 0.5, 0, 0, 1);
    CHECK(memcmp(p_en, p_dis, sizeof p_en) != 0, "progress9 disabled dims fill");
    pt_progress(&ge, rr(4, 8, 32, 12), 0, 1, 2, 0);
    pt_progress(&gd, rr(4, 8, 32, 12), 0, 1, 2, 1);
    CHECK(memcmp(p_en, p_dis, sizeof p_en) != 0, "progress9 disabled dims indeterminate stripes");
    assets_reset();
    CHECK(!pt_v9() && g_pal.face == C_E6, "forgotten: 8.1 again");
    remove_nine(dir);
    write_nine(dir, 5);   /* no default button */
    rbui_load_assets(dir);
    CHECK(!pt_v9() && g_pal.face == C_E6, "a partial bundle keeps the 8.1 look throughout");
    assets_reset();
    rbui_load_assets("");
    remove_nine(dir);
    rmdir(dir);
}


/* Button feedback: fixed size, Platinum frame, title, icons and a level bar in
 * RBUI_FEEDBACK_STEPS steps (empty while muted). RBUI_FEEDBACK_PNG=DIR writes
 * the renders for review. */
static void test_feedback(void)
{
    int w = 0, h = 0;
    rbui_feedback_size(&w, &h);
    CHECK(w == 212 && h == 58, "size %dx%d", w, h);
    CHECK(rbui_feedback_steps(0) == 0 && rbui_feedback_steps(-1) == 0, "zero");
    CHECK(rbui_feedback_steps(0.001) == 1, "any level shows one step");
    CHECK(rbui_feedback_steps(0.5) == 8 && rbui_feedback_steps(1.0) == 16 && rbui_feedback_steps(7.0) == 16, "steps");
    uint32_t *a = calloc((size_t)w * h, 4), *b = calloc((size_t)w * h, 4);
    static const struct { int kind; double level; int muted; const char *name; } cases[] = {
        {RBUI_FEEDBACK_VOLUME, 0.5, 0, "volume"}, {RBUI_FEEDBACK_MUTE, 0.5, 1, "mute-on"},
        {RBUI_FEEDBACK_MUTE, 0.5, 0, "mute-off"}, {RBUI_FEEDBACK_BRIGHTNESS, 0.75, 0, "brightness"},
    };
    const char *dir = getenv("RBUI_FEEDBACK_PNG");
    for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); ++i) {
        memset(a, 0x55, (size_t)w * h * 4);
        rbui_draw_feedback(a, w, cases[i].kind, cases[i].level, cases[i].muted);
        int opaque = 1;
        for (int k = 0; k < w * h; ++k) if (a[k] == 0x55555555u) opaque = 0;
        CHECK(opaque, "%s: every pixel drawn", cases[i].name);
        CHECK((a[0] & 0xFFFFFFu) == 0 && (a[(size_t)w * h - 1] & 0xFFFFFFu) == 0, "%s: black outline", cases[i].name);
        CHECK((a[(size_t)w + 1] & 0xFFFFFFu) == 0xFFFFFFu, "%s: white bevel", cases[i].name);
        if (dir) {
            char path[512];
            snprintf(path, sizeof path, "%s/feedback-%s.png", dir, cases[i].name);
            rbui_write_png(path, a, w, h, w);
        }
    }
    /* The bar follows the level; mute empties it; titles differ. */
    rbui_draw_feedback(a, w, RBUI_FEEDBACK_VOLUME, 0.25, 0);
    rbui_draw_feedback(b, w, RBUI_FEEDBACK_VOLUME, 0.75, 0);
    CHECK(memcmp(a, b, (size_t)w * h * 4) != 0, "level changes the bar");
    rbui_draw_feedback(a, w, RBUI_FEEDBACK_VOLUME, 0.0, 0);
    rbui_draw_feedback(b, w, RBUI_FEEDBACK_VOLUME, 0.75, 1);
    CHECK(memcmp(a, b, (size_t)w * h * 4) != 0, "muted volume shows the muted speaker");
    int bar_same = 1;
    for (int y = 30; y < 46; ++y)
        for (int x = 40; x < w - 40; ++x)
            if (a[(size_t)y * w + x] != b[(size_t)y * w + x]) bar_same = 0;
    CHECK(bar_same, "muted: the bar is empty, as at level 0");
    rbui_draw_feedback(a, w, RBUI_FEEDBACK_MUTE, 0.5, 1);
    rbui_draw_feedback(b, w, RBUI_FEEDBACK_MUTE, 0.5, 0);
    CHECK(memcmp(a, b, w * 22 * 4) != 0, "Mute On / Mute Off titles differ");
    free(a);
    free(b);
}

static void test_ui_310(void)
{
    events e = {0};
    /* #29: Escape / Cmd+. fall back from cancel -> back -> done. */
    rbui *ui = new_ui(640, 480, &e);
    const char *back_doc = "{\"id\":\"b\",\"items\":[{\"type\":\"text\",\"text\":\"Step\"},"
                           "{\"type\":\"buttons\",\"items\":[{\"id\":\"back\",\"label\":\"Back\",\"align\":\"left\"},"
                           "{\"id\":\"next\",\"label\":\"Continue\",\"default\":true}]}]}";
    CHECK(set(ui, back_doc) == 0, "back_doc");
    key(ui, RBUI_KEY_ESCAPE, 0);
    CHECK(strstr(ev_last(&e), "\"id\":\"back\""), "escape activates back when cancel absent: %s", ev_last(&e));
    ev_clear(&e);
    rbui_key(ui, RBUI_KEY_NONE, ".", 1, RBUI_MOD_COMMAND);
    rbui_key(ui, RBUI_KEY_NONE, NULL, 0, RBUI_MOD_COMMAND);
    CHECK(strstr(ev_last(&e), "\"id\":\"back\""), "Cmd+. activates back: %s", ev_last(&e));

    const char *done_doc = "{\"id\":\"d\",\"items\":[{\"type\":\"text\",\"text\":\"Finished\"},"
                           "{\"type\":\"buttons\",\"items\":[{\"id\":\"done\",\"label\":\"Done\"}]}]}";
    CHECK(set(ui, done_doc) == 0, "done_doc");
    ev_clear(&e);
    key(ui, RBUI_KEY_ESCAPE, 0);
    CHECK(strstr(ev_last(&e), "\"id\":\"done\""), "escape activates done when back/cancel absent: %s", ev_last(&e));

    /* #34a: Return activates a Tab-focused button when no default button exists. */
    ev_clear(&e);
    rbui_focus(ui, "done");
    key(ui, RBUI_KEY_RETURN, 0);
    CHECK(strstr(ev_last(&e), "\"id\":\"done\""), "Return activates focused button without default: %s", ev_last(&e));

    /* #30: long popup menu scroll arrows step immediately by 1 on click & honor repeat delay. */
    char mdoc[8192];
    int mo = snprintf(mdoc, sizeof mdoc, "{\"id\":\"m\",\"items\":[{\"type\":\"popup\",\"id\":\"p\",\"value\":\"o10\",\"options\":[");
    for (int i = 0; i < 60; ++i)
        mo += snprintf(mdoc + mo, sizeof mdoc - (size_t)mo, "%s{\"id\":\"o%d\",\"label\":\"Option %d\"}", i ? "," : "", i, i);
    snprintf(mdoc + mo, sizeof mdoc - (size_t)mo, "]}]}");
    rbui_resize(ui, 480, 320);
    CHECK(set(ui, mdoc) == 0, "long menu doc");
    int pr[4];
    CHECK(widget_rect(ui, "p", pr), "popup rect");
    click(ui, pr[0] + 10, pr[1] + 10);   /* click to open sticky menu */
    char *desc = rbui_describe(ui);
    int mr[4] = {0};
    char *mp = desc ? strstr(desc, "\"menu\":[") : NULL;
    CHECK(mp && sscanf(mp + 8, "%d,%d,%d,%d", &mr[0], &mr[1], &mr[2], &mr[3]) == 4, "menu rect");
    free(desc);
    uint32_t *before = malloc(480 * 320 * 4), *after_up = malloc(480 * 320 * 4), *after_early = malloc(480 * 320 * 4);
    rbui_render(ui, before, 480, RBUI_BG_CHECKERBOARD, NULL, 0, 1);
    /* Single quick click on bottom arrow row (hold 50ms < 300ms repeat delay). */
    rbui_pointer(ui, mr[0] + mr[2] / 2, mr[1] + mr[3] - 10, 1);
    rbui_tick(ui, 1050);
    rbui_render(ui, after_up, 480, RBUI_BG_CHECKERBOARD, NULL, 0, 1);
    CHECK(memcmp(before, after_up, 480 * 320 * 4) != 0, "scroll arrow press steps menu immediately");
    /* Tick before RB_REPEAT_FIRST_MS (300ms) must not step again prematurely. */
    rbui_tick(ui, 1150);
    rbui_render(ui, after_early, 480, RBUI_BG_CHECKERBOARD, NULL, 0, 1);
    CHECK(memcmp(after_up, after_early, 480 * 320 * 4) == 0, "repeat_at waits RB_REPEAT_FIRST_MS");
    rbui_pointer(ui, mr[0] + mr[2] / 2, mr[1] + mr[3] - 10, 0);
    desc = rbui_describe(ui);
    CHECK(desc && strstr(desc, "\"menu\":["), "menu stays open after clicking scroll arrow");
    free(desc);
    free(before);
    free(after_up);
    free(after_early);

    /* #31a: narrow screen button row wrapping avoids collisions. */
    const char *five = "{\"id\":\"narrow\",\"width\":480,\"items\":[{\"type\":\"text\",\"text\":\"Pick\"},"
                       "{\"type\":\"buttons\",\"items\":["
                       "{\"id\":\"back\",\"label\":\"Back\",\"align\":\"left\"},"
                       "{\"id\":\"more\",\"label\":\"Details...\",\"align\":\"left\"},"
                       "{\"id\":\"cancel\",\"label\":\"Cancel\"},"
                       "{\"id\":\"other\",\"label\":\"Use Another\"},"
                       "{\"id\":\"ok\",\"label\":\"Continue\",\"default\":true}]}]}";
    rbui_resize(ui, 480, 480);
    CHECK(set(ui, five) == 0, "five buttons narrow doc");
    const char *bids[5] = {"back", "more", "cancel", "other", "ok"};
    int br[5][4];
    for (int i = 0; i < 5; ++i) {
        CHECK(widget_rect(ui, bids[i], br[i]), "narrow btn %s", bids[i]);
        CHECK(br[i][0] >= 0 && br[i][0] + br[i][2] <= 480, "btn %s inside canvas", bids[i]);
    }
    int overlap = 0;
    for (int i = 0; i < 5; ++i)
        for (int j = i + 1; j < 5; ++j)
            if (br[i][0] < br[j][0] + br[j][2] && br[i][0] + br[i][2] > br[j][0] &&
                br[i][1] < br[j][1] + br[j][3] && br[i][1] + br[i][3] > br[j][1])
                overlap++;
    CHECK(overlap == 0, "narrow button layout has zero overlaps");

    /* #31b: scrollable view with pinned_band + pinned buttons does not overlap pinned_band. */
    char pdoc[8192];
    int po = snprintf(pdoc, sizeof pdoc, "{\"id\":\"pb\",\"title\":\"Sound\",\"width\":420,\"items\":[");
    for (int i = 0; i < 18; ++i)
        po += snprintf(pdoc + po, sizeof pdoc - (size_t)po, "{\"type\":\"checkbox\",\"id\":\"c%d\",\"label\":\"Opt %d\"},", i, i);
    snprintf(pdoc + po, sizeof pdoc - (size_t)po,
             "{\"type\":\"band\",\"pin\":true,\"height\":40,\"items\":[{\"type\":\"slider\",\"id\":\"v\",\"label\":\"Vol\","
             "\"min\":0,\"max\":100,\"step\":5,\"value\":50,\"x\":12,\"y\":12}]},"
             "{\"type\":\"buttons\",\"items\":[{\"id\":\"ok\",\"label\":\"OK\",\"default\":true}]}]}");
    rbui_resize(ui, 480, 320);
    CHECK(set(ui, pdoc) == 0, "scrollable pinned_band doc");
    CHECK(ui->scrollable == 1 && ui->pinned_band && ui->pinned, "pinned_band + pinned scrollable");
    CHECK(ui->view.y + ui->view.h <= ui->pinned_band->r.y,
          "scrollable view (%d) does not overlap pinned_band (%d)", ui->view.y + ui->view.h, ui->pinned_band->r.y);
    CHECK(ui->pinned_band->r.y + ui->pinned_band->r.h < ui->pinned->r.y,
          "pinned_band above pinned buttons");

    /* #31c: narrow slider label uses tx_draw_fit instead of colliding with track. */
    const char *sldr = "{\"id\":\"sl\",\"width\":220,\"items\":[{\"type\":\"slider\",\"id\":\"vs\","
                       "\"label\":\"VeryLongSliderLabelText\",\"min\":0,\"max\":100,\"step\":1,\"value\":50}]}";
    rbui_resize(ui, 480, 320);
    CHECK(set(ui, sldr) == 0, "narrow slider doc");
    int srect[4];
    CHECK(widget_rect(ui, "vs", srect), "slider track rect");
    uint32_t *sp = calloc(480 * 320, 4);
    rbui_render(ui, sp, 480, RBUI_BG_CHECKERBOARD, NULL, 0, 1);
    CHECK(srect[0] + srect[2] <= ui->content.x + ui->content.w - RB_MARGIN, "narrow slider track stays inside dialog");
    free(sp);

    /* #34b: smooth scrollbar thumb drag inside damage bounds. */
    char sdoc[8192];
    int so = snprintf(sdoc, sizeof sdoc, "{\"id\":\"sb\",\"items\":[{\"type\":\"list\",\"id\":\"L\",\"rows\":4,\"items\":[");
    for (int i = 0; i < 8; ++i)
        so += snprintf(sdoc + so, sizeof sdoc - (size_t)so, "%s{\"id\":\"i%d\",\"label\":\"Item %d\"}", i ? "," : "", i, i);
    snprintf(sdoc + so, sizeof sdoc - (size_t)so, "]}]}");
    rbui_resize(ui, 640, 480);
    CHECK(set(ui, sdoc) == 0, "listbar thumb drag doc");
    int lr[4];
    CHECK(widget_rect(ui, "L", lr), "list rect");
    int bx = lr[0] + lr[2] - 8, ty0 = lr[1] + 24;
    uint32_t *f0 = malloc(640 * 480 * 4), *f1 = malloc(640 * 480 * 4);
    rbui_render(ui, f0, 640, RBUI_BG_CHECKERBOARD, NULL, 0, 1);
    rbui_pointer(ui, bx, ty0, 1);
    rbui_render(ui, f0, 640, RBUI_BG_CHECKERBOARD, NULL, 0, 1);
    rbui_pointer(ui, bx, ty0 + 2, 1);
    CHECK(damage_ok(ui, f0, f1, 640, 480, "smooth thumb 2px drag"), "smooth thumb drag passes damage check");
    rbui_pointer(ui, bx, ty0 + 2, 0);
    free(f0);
    free(f1);
    ev_clear(&e);
    rbui_free(ui);
}

int main(int argc, char **argv)
{
    /* Goldens use the original MIT fonts and icons, never a bundle on this machine. */
    setenv("RBUI_ASSETS", "", 1);
    if (argc < 2) {
        fprintf(stderr, "usage: test_rbui EXAMPLES_DIR [--update-golden] [--only NAME] [--fuzz N]\n");
        return 2;
    }
    examples_dir = argv[1];
    const char *only = NULL;
    for (int i = 2; i < argc; ++i) {
        if (strcmp(argv[i], "--update-golden") == 0) update_golden = 1;
        else if (strcmp(argv[i], "--only") == 0 && i + 1 < argc) only = argv[++i];
        else if (strcmp(argv[i], "--fuzz") == 0 && i + 1 < argc) fuzz_iters = atol(argv[++i]);
    }
    static const struct { const char *name; void (*fn)(void); } tests[] = {
        {"checkerboard", test_checkerboard}, {"keyboard", test_keyboard}, {"field", test_field},
        {"pointer", test_pointer}, {"state", test_state}, {"errors", test_errors}, {"builtin", test_builtin},
        {"scroll", test_scroll}, {"tick", test_tick}, {"long_menu", test_long_menu}, {"regressions", test_regressions}, {"golden", test_golden}, {"damage", test_damage}, {"fuzz", test_fuzz},
        {"assets", test_assets}, {"click_list", test_click_list}, {"icon_view", test_icon_view},
        {"about_items", test_about_items},
        {"band_bevels_slider", test_band_bevels_slider}, {"reveal", test_reveal},
        {"controls9", test_controls9}, {"feedback", test_feedback}, {"ui_310", test_ui_310},
    };
    for (size_t i = 0; i < sizeof(tests) / sizeof(tests[0]); ++i) {
        if (only && strcmp(only, tests[i].name) != 0) continue;
        int before = failures;
        tests[i].fn();
        printf("%-13s %s\n", tests[i].name, failures == before ? "ok" : "FAILED");
    }
    printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
