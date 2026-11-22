/* doc.c - parse and validate UI documents (ARCHITECTURE §4.2) into a node tree */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "rbui_internal.h"

#define DOC_MAX_BYTES (1u << 20)
#define DOC_MAX_NODES 1024
#define DOC_MAX_DEPTH 8
#define DOC_MAX_STR 4096
#define DOC_MAX_OPTS 1024
#define DOC_MAX_QR 177
#define FIELD_DEFAULT_MAX 256
#define FIELD_HARD_MAX 1024

typedef struct {
    char *err;
    size_t errlen;
    int nodes;
} pctx;

static int fail(pctx *p, const char *path, const char *fmt, ...)
{
    if (p->err && p->errlen) {
        char msg[256];
        va_list ap;
        va_start(ap, fmt);
        vsnprintf(msg, sizeof(msg), fmt, ap);
        va_end(ap);
        if (path && path[0]) snprintf(p->err, p->errlen, "%s: %s", path, msg);
        else snprintf(p->err, p->errlen, "%s", msg);
    }
    return -1;
}

static char *dupn(const char *s, size_t n)
{
    char *d = malloc(n + 1);
    if (!d) return NULL;
    memcpy(d, s, n);
    d[n] = 0;
    return d;
}

/* Optional string member. Returns 0 (out may stay NULL), -1 on error. */
static int get_str(pctx *p, const char *path, const cJSON *o, const char *key, int required, char **out)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, key);
    *out = NULL;
    if (!v || cJSON_IsNull(v)) {
        if (required) return fail(p, path, "missing \"%s\"", key);
        return 0;
    }
    if (!cJSON_IsString(v) || !v->valuestring) return fail(p, path, "\"%s\" must be a string", key);
    size_t n = strlen(v->valuestring);
    if (n > DOC_MAX_STR) return fail(p, path, "\"%s\" is longer than %d bytes", key, DOC_MAX_STR);
    *out = dupn(v->valuestring, n);
    if (!*out) return fail(p, path, "out of memory");
    return 0;
}

static int get_bool(pctx *p, const char *path, const cJSON *o, const char *key, int def, int *out)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, key);
    *out = def;
    if (!v || cJSON_IsNull(v)) return 0;
    if (!cJSON_IsBool(v)) return fail(p, path, "\"%s\" must be true or false", key);
    *out = cJSON_IsTrue(v) ? 1 : 0;
    return 0;
}

static int get_int(pctx *p, const char *path, const cJSON *o, const char *key, int def, int lo, int hi, int *out)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, key);
    *out = def;
    if (!v || cJSON_IsNull(v)) return 0;
    if (!cJSON_IsNumber(v)) return fail(p, path, "\"%s\" must be a number", key);
    double d = v->valuedouble;
    if (!(d >= lo)) d = lo;      /* also catches NaN */
    if (d > hi) d = hi;
    *out = (int)d;
    return 0;
}

static char *canon_value(const cJSON *o)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, "value");
    if (!v) return dupn("", 0);
    char *s = cJSON_PrintUnformatted(v);
    if (!s) return NULL;
    char *d = dupn(s, strlen(s));
    cJSON_free(s);
    return d;
}

void node_free(rb_node *n)
{
    if (!n) return;
    for (int i = 0; i < n->nkids; ++i) node_free(n->kids[i]);
    free(n->kids);
    for (int i = 0; i < n->nopts; ++i) {
        free(n->opts[i].id);
        free(n->opts[i].label);
        free(n->opts[i].detail);
    }
    free(n->opts);
    free(n->id);
    free(n->text);
    free(n->placeholder);
    free(n->reveals);
    free(n->header);
    free(n->doc_value);
    free(n->buf);
    free(n->qr);
    free(n->lines);
    free(n);
}

void doc_free(rb_doc *d)
{
    node_free(d->root);
    free(d->id);
    free(d->title);
    free(d->focus);
    memset(d, 0, sizeof(*d));
}

static const struct { const char *name; int type; } kTypes[] = {
    {"text", N_TEXT}, {"separator", N_SEPARATOR}, {"group", N_GROUP}, {"stack", N_STACK},
    {"column", N_STACK}, {"columns", N_COLUMNS}, {"list", N_LIST}, {"field", N_FIELD},
    {"checkbox", N_CHECKBOX}, {"radio", N_RADIO}, {"popup", N_POPUP}, {"progress", N_PROGRESS},
    {"qr", N_QR}, {"buttons", N_BUTTONS}, {"icon", N_ICON}, {"spacer", N_SPACER},
    {"facts", N_FACTS}, {"meters", N_METERS}, {"band", N_BAND}, {"bevels", N_BEVELS}, {"slider", N_SLIDER},
};

static rb_node *new_node(pctx *p, int type, rb_node *parent)
{
    if (p->nodes >= DOC_MAX_NODES) return NULL;
    rb_node *n = calloc(1, sizeof(*n));
    if (!n) return NULL;
    p->nodes++;
    n->type = type;
    n->parent = parent;
    n->sel = -1;
    n->icon = n->icon2 = -1;
    n->ax = n->ay = -1;
    n->scrollbar = 1;
    return n;
}

static int parse_items(pctx *p, const char *path, const cJSON *arr, rb_node *parent, int depth, int buttons_only);

static int parse_options(pctx *p, const char *path, const cJSON *o, const char *key, rb_node *n, int rich)
{
    const cJSON *arr = cJSON_GetObjectItemCaseSensitive(o, key);
    if (!arr || cJSON_IsNull(arr)) return 0;
    if (!cJSON_IsArray(arr)) return fail(p, path, "\"%s\" must be an array", key);
    int count = cJSON_GetArraySize(arr);
    if (count > DOC_MAX_OPTS) return fail(p, path, "more than %d %s", DOC_MAX_OPTS, key);
    if (count == 0) return 0;
    n->opts = calloc((size_t)count, sizeof(rb_opt));
    if (!n->opts) return fail(p, path, "out of memory");
    int i = 0;
    const cJSON *it;
    cJSON_ArrayForEach(it, arr) {
        char sub[160];
        snprintf(sub, sizeof(sub), "%s.%s[%d]", path, key, i);
        rb_opt *op = &n->opts[i];
        op->icon = -1;
        op->signal = -1;
        n->nopts = i + 1;
        if (!cJSON_IsObject(it)) return fail(p, sub, "must be an object");
        if (get_str(p, sub, it, "id", 1, &op->id)) return -1;
        if (get_str(p, sub, it, "label", 0, &op->label)) return -1;
        if (!op->label && !(op->label = dupn(op->id, strlen(op->id)))) return fail(p, sub, "out of memory");
        if (get_bool(p, sub, it, "disabled", 0, &op->disabled)) return -1;
        for (int k = 0; k < i; ++k)
            if (strcmp(n->opts[k].id, op->id) == 0) return fail(p, sub, "duplicate option id \"%s\"", op->id);
        if (rich) {
            char *icon = NULL;
            if (get_str(p, sub, it, "detail", 0, &op->detail)) return -1;
            if (get_str(p, sub, it, "icon", 0, &icon)) return -1;
            if (icon) {
                op->icon = icon_index(icon);
                int bad = op->icon < 0;
                if (bad) fail(p, sub, "unknown icon \"%s\"", icon);
                free(icon);
                if (bad) return -1;
            }
            if (get_int(p, sub, it, "signal", -1, -1, 4, &op->signal)) return -1;
            if (get_bool(p, sub, it, "lock", 0, &op->lock)) return -1;
            if (get_bool(p, sub, it, "checked", 0, &op->checked)) return -1;
            /* Any item saying "checked" (true or false) makes a checkable list: like a menu,
             * it keeps the check-mark column whether or not something is checked now. */
            if (cJSON_GetObjectItemCaseSensitive(it, "checked")) n->has_checks = 1;
        }
        ++i;
    }
    return 0;
}

/* "facts" and "meters" rows: {"label", "value"} (and for meters "icon" and "share", 0..1 or
 * null for an empty bar).  Display only: rows have no ids and nothing to select. */
static int parse_rows(pctx *p, const char *path, const cJSON *o, rb_node *n, int meters)
{
    const cJSON *arr = cJSON_GetObjectItemCaseSensitive(o, "items");
    if (!arr || cJSON_IsNull(arr)) return 0;
    if (!cJSON_IsArray(arr)) return fail(p, path, "\"items\" must be an array");
    int count = cJSON_GetArraySize(arr);
    if (count > DOC_MAX_OPTS) return fail(p, path, "more than %d items", DOC_MAX_OPTS);
    if (count == 0) return 0;
    if (!(n->opts = calloc((size_t)count, sizeof(rb_opt)))) return fail(p, path, "out of memory");
    int i = 0;
    const cJSON *it;
    cJSON_ArrayForEach(it, arr) {
        char sub[160];
        snprintf(sub, sizeof(sub), "%s.items[%d]", path, i);
        rb_opt *op = &n->opts[i];
        op->icon = -1;
        op->signal = -1;
        op->share = -1;
        n->nopts = ++i;
        if (!cJSON_IsObject(it)) return fail(p, sub, "must be an object");
        if (get_str(p, sub, it, "label", 1, &op->label)) return -1;
        if (get_str(p, sub, it, "value", 0, &op->detail)) return -1;
        if (!meters) continue;
        char *icon = NULL;
        if (get_str(p, sub, it, "icon", 0, &icon)) return -1;
        if (icon) {
            op->icon = icon_index(icon);
            int bad = op->icon < 0;
            if (bad) fail(p, sub, "unknown icon \"%s\"", icon);
            free(icon);
            if (bad) return -1;
        }
        const cJSON *s = cJSON_GetObjectItemCaseSensitive(it, "share");
        if (s && !cJSON_IsNull(s)) {
            if (!cJSON_IsNumber(s)) return fail(p, sub, "\"share\" must be a number or null");
            double d = s->valuedouble;
            op->share = d >= 0 ? (d > 1 ? 1 : d) : 0;
        }
    }
    return 0;
}

static int parse_value_sel(pctx *p, const char *path, const cJSON *o, rb_node *n)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, "value");
    if (!v || cJSON_IsNull(v)) return 0;
    if (!cJSON_IsString(v)) return fail(p, path, "\"value\" must be an option id string or null");
    n->sel = opt_index(n, v->valuestring);
    return 0;
}

static int parse_qr(pctx *p, const char *path, const cJSON *o, rb_node *n)
{
    const cJSON *m = cJSON_GetObjectItemCaseSensitive(o, "matrix");
    if (!cJSON_IsArray(m)) return fail(p, path, "\"matrix\" must be an array of strings");
    int h = cJSON_GetArraySize(m), w = -1, y = 0;
    if (h < 1 || h > DOC_MAX_QR) return fail(p, path, "\"matrix\" must have 1..%d rows", DOC_MAX_QR);
    const cJSON *row;
    cJSON_ArrayForEach(row, m) {
        if (!cJSON_IsString(row) || !row->valuestring) return fail(p, path, "matrix rows must be strings");
        int len = (int)strlen(row->valuestring);
        if (w < 0) {
            w = len;
            if (w < 1 || w > DOC_MAX_QR) return fail(p, path, "matrix rows must have 1..%d modules", DOC_MAX_QR);
            n->qr = calloc((size_t)w * (size_t)h, 1);
            if (!n->qr) return fail(p, path, "out of memory");
        } else if (len != w) {
            return fail(p, path, "matrix row %d has %d modules, expected %d", y, len, w);
        }
        for (int x = 0; x < w; ++x) {
            char c = row->valuestring[x];
            if (c != '0' && c != '1') return fail(p, path, "matrix row %d: only '0' and '1' allowed", y);
            n->qr[y * w + x] = c == '1';
        }
        ++y;
    }
    n->qr_w = w;
    n->qr_h = h;
    if (get_int(p, path, o, "module", 0, 0, 16, &n->module)) return -1;
    if (get_int(p, path, o, "quiet", 4, 0, 8, &n->quiet)) return -1;
    return 0;
}

static int parse_item(pctx *p, const char *path, const cJSON *o, rb_node *parent, int depth, rb_node **out, int buttons_only)
{
    *out = NULL;
    if (!cJSON_IsObject(o)) return fail(p, path, "item must be an object");
    int type = -1;
    if (buttons_only) {
        type = N_BUTTON;
    } else {
        const cJSON *t = cJSON_GetObjectItemCaseSensitive(o, "type");
        if (!cJSON_IsString(t) || !t->valuestring) return fail(p, path, "missing \"type\"");
        for (size_t i = 0; i < sizeof(kTypes) / sizeof(kTypes[0]); ++i)
            if (strcmp(kTypes[i].name, t->valuestring) == 0) type = kTypes[i].type;
        if (type < 0) return fail(p, path, "unknown type \"%.40s\"", t->valuestring);
    }
    rb_node *n = new_node(p, type, parent);
    if (!n) return fail(p, path, "too many items (limit %d)", DOC_MAX_NODES);
    *out = n;
    if (get_str(p, path, o, "id", type == N_BUTTON, &n->id)) return -1;
    if (get_bool(p, path, o, "disabled", 0, &n->disabled)) return -1;
    if (get_int(p, path, o, "width", 0, 0, 4000, &n->width_hint)) return -1;
    if (parent && parent->type == N_BAND) {
        if (get_int(p, path, o, "x", 0, 0, 4000, &n->ax)) return -1;
        if (get_int(p, path, o, "y", 0, 0, 4000, &n->ay)) return -1;
    }
    switch (type) {
    case N_TEXT: {
        char *style = NULL, *align = NULL;
        if (get_str(p, path, o, "text", 0, &n->text)) return -1;
        if (!n->text && !(n->text = dupn("", 0))) return fail(p, path, "out of memory");
        if (get_str(p, path, o, "style", 0, &style)) return -1;
        if (get_str(p, path, o, "align", 0, &align)) { free(style); return -1; }
        int bad = 0;
        if (!style || strcmp(style, "normal") == 0) n->style = ST_NORMAL;
        else if (strcmp(style, "emphasis") == 0) n->style = ST_EMPHASIS;
        else if (strcmp(style, "small") == 0) n->style = ST_SMALL;
        else if (strcmp(style, "large") == 0) n->style = ST_LARGE;
        else bad = fail(p, path, "unknown style \"%.40s\"", style);
        if (!bad) {
            if (!align || strcmp(align, "left") == 0) n->align = AL_LEFT;
            else if (strcmp(align, "center") == 0) n->align = AL_CENTER;
            else if (strcmp(align, "right") == 0) n->align = AL_RIGHT;
            else bad = fail(p, path, "unknown align \"%.40s\"", align);
        }
        free(style);
        free(align);
        return bad;
    }
    case N_SEPARATOR:
        return 0;
    case N_SPACER:
        return get_int(p, path, o, "height", 8, 0, 400, &n->height_hint);
    case N_ICON: {
        char *icon = NULL;
        if (get_str(p, path, o, "icon", 1, &icon)) return -1;
        n->icon = icon_index(icon);
        int bad = n->icon < 0 ? fail(p, path, "unknown icon \"%.40s\"", icon) : 0;
        free(icon);
        if (bad) return -1;
        if (get_int(p, path, o, "size", 32, 16, 64, &n->icon_size)) return -1;
        n->icon_size = n->icon_size >= 64 ? 64 : n->icon_size >= 40 ? 40 : n->icon_size >= 32 ? 32 : 16;
        return 0;
    }
    case N_BAND: {
        char *rule = NULL;
        if (depth >= DOC_MAX_DEPTH) return fail(p, path, "nested more than %d levels", DOC_MAX_DEPTH);
        if (get_int(p, path, o, "height", 0, 0, 400, &n->height_hint)) return -1;
        if (get_str(p, path, o, "rule", 0, &rule)) return -1;
        if (get_bool(p, path, o, "pin", 0, &n->pin)) { free(rule); return -1; }
        int bad = 0;
        if (!rule) n->rule = 0;
        else if (strcmp(rule, "top") == 0) n->rule = RULE_TOP;
        else if (strcmp(rule, "bottom") == 0) n->rule = RULE_BOTTOM;
        else if (strcmp(rule, "both") == 0) n->rule = RULE_TOP | RULE_BOTTOM;
        else bad = fail(p, path, "unknown rule \"%.40s\"", rule);
        free(rule);
        if (bad) return -1;
        const cJSON *items = cJSON_GetObjectItemCaseSensitive(o, "items");
        if (!items || cJSON_IsNull(items)) return 0;
        return parse_items(p, path, items, n, depth + 1, 0);
    }
    case N_BEVELS:
        if (!n->id) return fail(p, path, "bevels need an \"id\"");
        if (parse_options(p, path, o, "items", n, 1)) return -1;
        if (!(n->doc_value = canon_value(o))) return fail(p, path, "out of memory");
        return parse_value_sel(p, path, o, n);
    case N_SLIDER: {
        if (!n->id) return fail(p, path, "a slider needs an \"id\"");
        if (get_str(p, path, o, "label", 0, &n->text)) return -1;
        const char *keys[3] = {"min", "max", "step"};
        double defs[3] = {0, 100, 1}, *dst[3] = {&n->vmin, &n->vmax, &n->vstep};
        for (int k = 0; k < 3; ++k) {
            const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, keys[k]);
            if (v && !cJSON_IsNull(v) && !cJSON_IsNumber(v)) return fail(p, path, "\"%s\" must be a number", keys[k]);
            *dst[k] = cJSON_IsNumber(v) ? v->valuedouble : defs[k];
        }
        if (!(n->vmax > n->vmin) || !(n->vstep > 0)) return fail(p, path, "a slider needs min < max and step > 0");
        const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, "value");
        if (v && !cJSON_IsNull(v) && !cJSON_IsNumber(v)) return fail(p, path, "\"value\" must be a number");
        n->val = cJSON_IsNumber(v) ? v->valuedouble : n->vmin;
        if (!(n->val >= n->vmin)) n->val = n->vmin;
        if (n->val > n->vmax) n->val = n->vmax;
        const cJSON *ic = cJSON_GetObjectItemCaseSensitive(o, "icons");
        if (ic && !cJSON_IsNull(ic)) {
            if (!cJSON_IsArray(ic) || cJSON_GetArraySize(ic) != 2) return fail(p, path, "\"icons\" must be two icon names");
            int *slot[2] = {&n->icon, &n->icon2};
            for (int k = 0; k < 2; ++k) {
                const cJSON *s = cJSON_GetArrayItem(ic, k);
                if (!cJSON_IsString(s) || (*slot[k] = icon_index(s->valuestring)) < 0)
                    return fail(p, path, "unknown icon in \"icons\"");
            }
        }
        if (!(n->doc_value = canon_value(o))) return fail(p, path, "out of memory");
        return 0;
    }
    case N_GROUP:
    case N_STACK:
    case N_COLUMNS: {
        if (type == N_GROUP && get_str(p, path, o, "label", 0, &n->text)) return -1;
        if (depth >= DOC_MAX_DEPTH) return fail(p, path, "nested more than %d levels", DOC_MAX_DEPTH);
        const cJSON *items = cJSON_GetObjectItemCaseSensitive(o, "items");
        if (!items || cJSON_IsNull(items)) return 0;
        return parse_items(p, path, items, n, depth + 1, 0);
    }
    case N_BUTTONS: {
        const cJSON *items = cJSON_GetObjectItemCaseSensitive(o, "items");
        if (!items) return fail(p, path, "missing \"items\"");
        return parse_items(p, path, items, n, depth + 1, 1);
    }
    case N_BUTTON: {
        char *align = NULL;
        if (get_str(p, path, o, "label", 0, &n->text)) return -1;
        if (!n->text && !(n->text = dupn(n->id, strlen(n->id)))) return fail(p, path, "out of memory");
        if (get_bool(p, path, o, "default", 0, &n->is_default)) return -1;
        if (get_str(p, path, o, "align", 0, &align)) return -1;
        n->align = align && strcmp(align, "left") == 0 ? AL_LEFT : AL_RIGHT;
        free(align);
        return 0;
    }
    case N_LIST: {
        char *activate = NULL, *view = NULL;
        if (get_str(p, path, o, "label", 0, &n->text)) return -1;
        if (get_int(p, path, o, "rows", 5, 1, 40, &n->rows)) return -1;
        if (get_bool(p, path, o, "scrollbar", 1, &n->scrollbar)) return -1;
        if (get_str(p, path, o, "activate", 0, &activate)) return -1;
        int bad = activate && strcmp(activate, "click") != 0 && strcmp(activate, "double-click") != 0;
        n->click_activate = activate && strcmp(activate, "click") == 0;
        free(activate);
        if (bad) return fail(p, path, "\"activate\" must be \"click\" or \"double-click\"");
        /* "view": "icons": a Finder icon view (no scroll bar; double-click or Return opens). */
        if (get_str(p, path, o, "view", 0, &view)) return -1;
        bad = view && strcmp(view, "list") != 0 && strcmp(view, "icons") != 0;
        n->icon_view = view && strcmp(view, "icons") == 0;
        free(view);
        if (bad) return fail(p, path, "\"view\" must be \"list\" or \"icons\"");
        if (get_int(p, path, o, "columns", 4, 1, 8, &n->columns)) return -1;
        if (get_str(p, path, o, "header", 0, &n->header)) return -1;
        if (n->icon_view) n->scrollbar = 0;
        if (parse_options(p, path, o, "items", n, 1)) return -1;
        if (!(n->doc_value = canon_value(o))) return fail(p, path, "out of memory");
        return parse_value_sel(p, path, o, n);
    }
    case N_RADIO:
    case N_POPUP:
        if (get_str(p, path, o, "label", 0, &n->text)) return -1;
        if (parse_options(p, path, o, "options", n, 0)) return -1;
        if (!(n->doc_value = canon_value(o))) return fail(p, path, "out of memory");
        if (parse_value_sel(p, path, o, n)) return -1;
        if (type == N_POPUP && n->sel < 0)
            for (int i = 0; i < n->nopts && n->sel < 0; ++i)
                if (!n->opts[i].disabled) n->sel = i;
        return 0;
    case N_CHECKBOX: {
        const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, "value");
        if (get_str(p, path, o, "label", 0, &n->text)) return -1;
        if (get_str(p, path, o, "reveals", 0, &n->reveals)) return -1;
        if (v && !cJSON_IsNull(v) && !cJSON_IsBool(v)) return fail(p, path, "\"value\" must be true or false");
        n->checked = cJSON_IsTrue(v) ? 1 : 0;
        if (!(n->doc_value = canon_value(o))) return fail(p, path, "out of memory");
        return 0;
    }
    case N_FIELD: {
        char *value = NULL;
        if (get_str(p, path, o, "label", 0, &n->text)) return -1;
        if (get_str(p, path, o, "placeholder", 0, &n->placeholder)) return -1;
        if (get_bool(p, path, o, "secure", 0, &n->secure)) return -1;
        if (get_int(p, path, o, "max_length", FIELD_DEFAULT_MAX, 1, FIELD_HARD_MAX, &n->maxlen)) return -1;
        if (get_str(p, path, o, "value", 0, &value)) return -1;
        int rc = field_set_text(n, value ? value : "", value ? strlen(value) : 0);
        free(value);
        if (rc) return fail(p, path, "out of memory");
        if (!(n->doc_value = canon_value(o))) return fail(p, path, "out of memory");
        return 0;
    }
    case N_PROGRESS: {
        const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, "value");
        if (v && !cJSON_IsNull(v) && !cJSON_IsNumber(v)) return fail(p, path, "\"value\" must be a number");
        double d = cJSON_IsNumber(v) ? v->valuedouble : 0;
        if (!(d >= 0)) d = 0;
        if (d > 1) d = 1;
        n->progress = d;
        return get_bool(p, path, o, "indeterminate", 0, &n->indeterminate);
    }
    case N_QR:
        return parse_qr(p, path, o, n);
    case N_FACTS:
    case N_METERS:
        return parse_rows(p, path, o, n, type == N_METERS);
    default:
        return fail(p, path, "unsupported item");
    }
}

static int parse_items(pctx *p, const char *path, const cJSON *arr, rb_node *parent, int depth, int buttons_only)
{
    if (!cJSON_IsArray(arr)) return fail(p, path, "\"items\" must be an array");
    int count = cJSON_GetArraySize(arr);
    if (count > DOC_MAX_NODES) return fail(p, path, "too many items");
    if (count == 0) return 0;
    parent->kids = calloc((size_t)count, sizeof(rb_node *));
    if (!parent->kids) return fail(p, path, "out of memory");
    int i = 0;
    const cJSON *it;
    cJSON_ArrayForEach(it, arr) {
        char sub[160];
        snprintf(sub, sizeof(sub), "%s%sitems[%d]", path, path[0] ? "." : "", i);
        rb_node *child = NULL;
        int rc = parse_item(p, sub, it, parent, depth, &child, buttons_only);
        if (child) parent->kids[parent->nkids++] = child;
        if (rc) return -1;
        ++i;
    }
    return 0;
}

/* ---- Tree helpers ------------------------------------------------------------ */

int node_walk(rb_node *n, node_fn fn, void *ctx)
{
    if (!n) return 0;
    int rc = fn(n, ctx);
    if (rc) return rc;
    for (int i = 0; i < n->nkids; ++i)
        if ((rc = node_walk(n->kids[i], fn, ctx))) return rc;
    return 0;
}

typedef struct { const char *id; rb_node *found; } find_ctx;

static int find_fn(rb_node *n, void *ctx)
{
    find_ctx *f = ctx;
    if (n->id && strcmp(n->id, f->id) == 0) {
        f->found = n;
        return 1;
    }
    return 0;
}

rb_node *node_by_id(rb_node *root, const char *id)
{
    if (!id) return NULL;
    find_ctx f = {id, NULL};
    node_walk(root, find_fn, &f);
    return f.found;
}

int node_has_value(const rb_node *n)
{
    switch (n->type) {
    case N_FIELD: case N_CHECKBOX: case N_RADIO: case N_POPUP: case N_LIST: case N_BEVELS: case N_SLIDER:
        return n->id != NULL;
    default:
        return 0;
    }
}

int node_focusable(const rb_node *n)
{
    if (n->disabled) return 0;
    for (const rb_node *p = n->parent; p; p = p->parent)
        if (p->disabled) return 0;
    switch (n->type) {
    case N_FIELD: case N_CHECKBOX: case N_BUTTON:
        return 1;
    case N_LIST:
        return n->nopts > 0;
    case N_RADIO: case N_POPUP: case N_BEVELS:
        for (int i = 0; i < n->nopts; ++i)
            if (!n->opts[i].disabled) return 1;
        return 0;
    case N_SLIDER:
        return 1;
    default:
        return 0;
    }
}

const char *node_value_id(const rb_node *n)
{
    if (n->sel < 0 || n->sel >= n->nopts) return NULL;
    return n->opts[n->sel].id;
}

int opt_index(const rb_node *n, const char *id)
{
    if (!id) return -1;
    for (int i = 0; i < n->nopts; ++i)
        if (strcmp(n->opts[i].id, id) == 0) return i;
    return -1;
}

int field_codepoints(const rb_node *n)
{
    int count = 0;
    size_t i = 0;
    while (i < (size_t)n->len) {
        rbf_utf8_next(n->buf, (size_t)n->len, &i);
        ++count;
    }
    return count;
}

int field_set_text(rb_node *n, const char *s, size_t len)
{
    /* Keep valid UTF-8 only, without control characters, up to maxlen code points. */
    size_t cap = len * 3 + 16;   /* U+FFFD replacements can grow invalid input */
    char *b = malloc(cap);
    if (!b) return -1;
    size_t i = 0, o = 0;
    int count = 0, maxlen = n->maxlen > 0 ? n->maxlen : FIELD_DEFAULT_MAX;
    while (i < len && count < maxlen) {
        uint32_t cp = rbf_utf8_next(s, len, &i);
        if (cp < 0x20 || cp == 0x7F) continue;
        o += (size_t)rbf_utf8_put(cp, b + o);
        ++count;
    }
    b[o] = 0;
    free(n->buf);
    n->buf = b;
    n->len = (int)o;
    n->cap = (int)cap;
    n->caret = n->anchor = n->len;
    n->scroll_x = 0;
    return 0;
}

/* ---- Duplicate id check ------------------------------------------------------ */

typedef struct { rb_node **seen; int n, cap; const char *dup; } dup_ctx;

static int dup_fn(rb_node *n, void *ctx)
{
    dup_ctx *d = ctx;
    if (!n->id) return 0;
    for (int i = 0; i < d->n; ++i)
        if (strcmp(d->seen[i]->id, n->id) == 0) {
            d->dup = n->id;
            return 1;
        }
    if (d->n == d->cap) return 2;
    d->seen[d->n++] = n;
    return 0;
}

/* A "Show password" checkbox: link it to the secure field it reveals. */
typedef struct { rb_node *root; rb_node *bad; } reveal_link_ctx;

static int reveal_link_fn(rb_node *n, void *ctx)
{
    reveal_link_ctx *l = ctx;
    if (n->type != N_CHECKBOX || !n->reveals) return 0;
    rb_node *f = node_by_id(l->root, n->reveals);
    if (!f || f->type != N_FIELD || !f->secure || f->revealer) {
        l->bad = n;
        return 1;
    }
    f->revealer = n;
    n->reveal_field = f;
    return 0;
}

int doc_parse(const char *json, size_t len, rb_doc *out, char *err, size_t errlen)
{
    pctx p = {err, errlen, 0};
    memset(out, 0, sizeof(*out));
    if (err && errlen) err[0] = 0;
    if (len > DOC_MAX_BYTES) return fail(&p, "", "document larger than %u bytes", DOC_MAX_BYTES);
    cJSON *root = cJSON_ParseWithLength(json, len);
    if (!root) return fail(&p, "", "invalid JSON");
    int rc = -1;
    char *kind = NULL, *icon = NULL;
    if (!cJSON_IsObject(root)) {
        fail(&p, "", "document must be a JSON object");
        goto done;
    }
    if (get_str(&p, "", root, "id", 1, &out->id)) goto done;
    if (get_str(&p, "", root, "title", 0, &out->title)) goto done;
    if (get_str(&p, "", root, "focus", 0, &out->focus)) goto done;
    if (get_str(&p, "", root, "kind", 0, &kind)) goto done;
    if (!kind || strcmp(kind, "window") == 0) out->kind = 0;
    else if (strcmp(kind, "alert") == 0) out->kind = 1;
    else {
        fail(&p, "", "unknown kind \"%.40s\"", kind);
        goto done;
    }
    if (get_int(&p, "", root, "width", out->kind ? 420 : 460, 160, 4000, &out->width)) goto done;
    if (get_int(&p, "", root, "height", 0, 0, 4000, &out->min_h)) goto done;
    if (get_bool(&p, "", root, "reset", 0, &out->reset)) goto done;
    if (get_str(&p, "", root, "alert_icon", 0, &icon)) goto done;
    out->alert_icon = -1;
    if (icon) {
        out->alert_icon = icon_index(icon);
        if (out->alert_icon < 0) {
            fail(&p, "", "unknown alert_icon \"%.40s\"", icon);
            goto done;
        }
    } else if (out->kind == 1) {
        out->alert_icon = icon_index("note");
    }
    out->root = new_node(&p, N_STACK, NULL);
    if (!out->root) {
        fail(&p, "", "out of memory");
        goto done;
    }
    const cJSON *items = cJSON_GetObjectItemCaseSensitive(root, "items");
    if (items && !cJSON_IsNull(items) && parse_items(&p, "", items, out->root, 1, 0)) goto done;
    {
        dup_ctx d = {NULL, 0, DOC_MAX_NODES + 1, NULL};
        d.seen = calloc((size_t)d.cap, sizeof(rb_node *));
        if (!d.seen) {
            fail(&p, "", "out of memory");
            goto done;
        }
        node_walk(out->root, dup_fn, &d);
        free(d.seen);
        if (d.dup) {
            fail(&p, "", "duplicate id \"%.60s\"", d.dup);
            goto done;
        }
    }
    {
        reveal_link_ctx l = {out->root, NULL};
        node_walk(out->root, reveal_link_fn, &l);
        if (l.bad) {
            fail(&p, "", "\"reveals\" of checkbox \"%.60s\" must name one secure field", l.bad->id ? l.bad->id : "");
            goto done;
        }
    }
    rc = 0;
done:
    free(kind);
    free(icon);
    cJSON_Delete(root);
    if (rc) doc_free(out);
    return rc;
}
