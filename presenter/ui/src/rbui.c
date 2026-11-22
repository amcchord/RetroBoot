/* rbui.c - public API: documents, state preservation, events, damage, render */
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "rbui_internal.h"

/* ---- Event queue --------------------------------------------------------------
 * Events are queued and delivered when the public entry point returns, so an
 * emitter that replaces the document from inside its callback never pulls
 * nodes out from under the input code. */

static void enqueue(rbui *ui, cJSON *obj)
{
    char *s = cJSON_PrintUnformatted(obj);
    cJSON_Delete(obj);
    if (!s) return;
    if (ui->evq_n == ui->evq_cap) {
        int nc = ui->evq_cap ? ui->evq_cap * 2 : 8;
        char **ni = realloc(ui->evq, (size_t)nc * sizeof(char *));
        if (!ni) {
            cJSON_free(s);
            return;
        }
        ui->evq = ni;
        ui->evq_cap = nc;
    }
    ui->evq[ui->evq_n++] = s;
}

static void flush(rbui *ui)
{
    /* Re-entrant calls (an emitter that feeds input back) only queue; the
     * outermost flush delivers everything in order. */
    if (ui->evq_flushing) return;
    ui->evq_flushing = 1;
    for (int i = 0; i < ui->evq_n; ++i) {
        char *s = ui->evq[i];
        ui->evq[i] = NULL;
        if (ui->emit) ui->emit(ui->emit_ctx, s);
        cJSON_free(s);
    }
    ui->evq_n = 0;
    ui->evq_flushing = 0;
}

static cJSON *value_json(const rb_node *n)
{
    switch (n->type) {
    case N_FIELD: return cJSON_CreateString(n->buf ? n->buf : "");
    case N_CHECKBOX: return cJSON_CreateBool(n->checked);
    case N_SLIDER: return cJSON_CreateNumber(n->val);
    default: {
        const char *id = node_value_id(n);
        return id ? cJSON_CreateString(id) : cJSON_CreateNull();
    }
    }
}

static cJSON *event_base(rbui *ui, const char *event, const char *id)
{
    cJSON *o = cJSON_CreateObject();
    if (!o) return NULL;
    cJSON_AddStringToObject(o, "event", event);
    cJSON_AddStringToObject(o, "doc", ui->doc.id ? ui->doc.id : "");
    cJSON_AddStringToObject(o, "id", id);
    return o;
}

void ui_emit_change(rbui *ui, rb_node *n)
{
    if (!n->id || !node_has_value(n)) return;
    cJSON *o = event_base(ui, "ui_change", n->id);
    if (!o) return;
    cJSON_AddItemToObject(o, "value", value_json(n));
    if (n->type == N_FIELD && n->secure) cJSON_AddTrueToObject(o, "secure");
    enqueue(ui, o);
}

typedef struct { cJSON *values; } values_ctx;

static int values_fn(rb_node *n, void *ctx)
{
    values_ctx *v = ctx;
    if (node_has_value(n)) cJSON_AddItemToObject(v->values, n->id, value_json(n));
    return 0;
}

void ui_emit_action(rbui *ui, rb_node *button)
{
    if (!button->id) return;
    cJSON *o = event_base(ui, "ui_action", button->id);
    if (!o) return;
    values_ctx v = {cJSON_CreateObject()};
    if (v.values) {
        node_walk(ui->doc.root, values_fn, &v);
        cJSON_AddItemToObject(o, "values", v.values);
    }
    enqueue(ui, o);
}

/* ---- Damage ------------------------------------------------------------------- */

void ui_damage(rbui *ui, rb_rect r)
{
    r = r_and(r, rr(0, 0, ui->cw, ui->ch));
    if (r_empty(r)) return;
    ui->damage = ui->dmg ? r_or(ui->damage, r) : r;
    ui->dmg = 1;
}

void ui_damage_all(rbui *ui) { ui_damage(ui, rr(0, 0, ui->cw, ui->ch)); }

rb_rect ui_node_screen(const rbui *ui, const rb_node *n, rb_rect r)
{
    if (n->scrolled) r.y -= ui->scroll;
    return r;
}

void ui_damage_node(rbui *ui, rb_node *n)
{
    if (!n) return;
    rb_rect r = r_out(r_or(n->r, n->ctl), 6);
    r = ui_node_screen(ui, n, r);
    if (n->scrolled) r = r_and(r, ui->view);
    ui_damage(ui, r);
}

static rb_rect window_area(const rbui *ui)
{
    if (!ui->have_doc) return rr(0, 0, 0, 0);
    rb_rect r = rr(ui->frame.x, ui->frame.y, ui->frame.w + 1, ui->frame.h + 1);
    if (ui->menu) r = r_or(r, r_out(ui->menu_r, 3));
    return r;
}

/* ---- Focus helpers -------------------------------------------------------------- */

void ui_caret_reset(rbui *ui)
{
    ui->caret_on = 1;
    ui->caret_next = ui->now + RB_CARET_MS;
}

void ui_reveal(rbui *ui, rb_node *n)
{
    if (!n || !ui->scrollable || !n->scrolled) return;
    rb_rect r = r_or(n->r, n->ctl);
    int top = r.y - ui->view.y - 8, bottom = r.y + r.h - ui->view.y + 8;
    int s = ui->scroll;
    if (bottom - s > ui->view.h) s = bottom - ui->view.h;
    if (top < s) s = top;
    int max = ui->content_h - ui->view.h;
    if (s > max) s = max;
    if (s < 0) s = 0;
    if (s != ui->scroll) {
        ui->scroll = s;
        ui_damage(ui, ui->view);
        if (ui->menu) menu_close(ui);
    }
}

static int first_enabled_opt(const rb_node *n)
{
    for (int i = 0; i < n->nopts; ++i)
        if (!n->opts[i].disabled) return i;
    return -1;
}

void ui_set_focus(rbui *ui, rb_node *n) { ui_set_focus_ex(ui, n, 1); }

void ui_set_focus_ex(rbui *ui, rb_node *n, int reveal)
{
    if (ui->focus == n) return;
    ui_damage_node(ui, ui->focus);
    ui->focus = n;
    if (n) {
        if (n->type == N_RADIO || n->type == N_BEVELS)
            ui->focus_opt = n->sel >= 0 && !n->opts[n->sel].disabled ? n->sel : first_enabled_opt(n);
        ui_damage_node(ui, n);
        if (reveal) ui_reveal(ui, n);
    }
    ui_caret_reset(ui);
}

typedef struct { rb_node *found; int pass; } deffocus_ctx;

static int deffocus_fn(rb_node *n, void *ctx)
{
    deffocus_ctx *d = ctx;
    if (!node_focusable(n)) return 0;
    if ((d->pass == 0 && n->type == N_FIELD) || (d->pass == 1 && n->type == N_LIST)) {
        d->found = n;
        return 1;
    }
    return 0;
}

static rb_node *default_focus(rbui *ui)
{
    for (int pass = 0; pass < 2; ++pass) {
        deffocus_ctx d = {NULL, pass};
        node_walk(ui->doc.root, deffocus_fn, &d);
        if (d.found) return d.found;
    }
    return NULL;
}

static rb_node *focusable_by_id(rbui *ui, const char *id)
{
    rb_node *n = id ? node_by_id(ui->doc.root, id) : NULL;
    return n && node_focusable(n) ? n : NULL;
}

/* ---- Lifetime ------------------------------------------------------------------- */

rbui *rbui_new(void)
{
    rbui *ui = calloc(1, sizeof(*ui));
    if (!ui) return NULL;
    assets_auto();
    ui->f_sys = rbf_get("system12");
    ui->f_reg = rbf_get("regular12");
    ui->f_small = rbf_get("small10");
    ui->f_tiny = assets_font("small9");
    if (!ui->f_tiny) ui->f_tiny = ui->f_small;
    ui->cw = 960;
    ui->ch = 540;
    ui->px = ui->cw / 2;
    ui->py = ui->ch / 2;
    ui->caret_on = 1;
    ui_damage_all(ui);
    return ui;
}

void rbui_free(rbui *ui)
{
    if (!ui) return;
    for (int i = 0; i < ui->evq_n; ++i) cJSON_free(ui->evq[i]);
    free(ui->evq);
    doc_free(&ui->doc);
    free(ui);
}

void rbui_set_emitter(rbui *ui, rbui_emit_fn fn, void *ctx)
{
    if (!ui) return;
    ui->emit = fn;
    ui->emit_ctx = ctx;
}

/* ---- Documents ---------------------------------------------------------------- */

static int same(const char *a, const char *b)
{
    if (!a || !b) return a == b;
    return strcmp(a, b) == 0;
}

static char *dupstr(const char *s) { return s ? strdup(s) : NULL; }

typedef struct { rb_node *old_root; } merge_ctx;

static int merge_fn(rb_node *n, void *ctx)
{
    merge_ctx *m = ctx;
    if (!n->id) return 0;
    rb_node *o = node_by_id(m->old_root, n->id);
    if (!o || o->type != n->type) return 0;
    int unchanged = same(o->doc_value, n->doc_value);
    switch (n->type) {
    case N_FIELD:
        if (unchanged && o->buf) {
            if (field_set_text(n, o->buf, (size_t)o->len) == 0) {
                n->caret = o->caret < n->len ? o->caret : n->len;
                n->anchor = o->anchor < n->len ? o->anchor : n->len;
                n->scroll_x = o->scroll_x;
            }
        }
        break;
    case N_CHECKBOX:
        if (unchanged) n->checked = o->checked;
        break;
    case N_SLIDER:
        if (unchanged) n->val = o->val;
        break;
    case N_RADIO:
    case N_POPUP:
    case N_LIST:
    case N_BEVELS:
        if (unchanged) {
            const char *id = node_value_id(o);
            int idx = opt_index(n, id);
            if (idx >= 0) n->sel = idx;
            else if (!id && n->type != N_POPUP) n->sel = -1;
        }
        if (n->type == N_LIST) n->scroll = o->scroll;
        break;
    default:
        break;
    }
    return 0;
}

typedef struct { rb_node *old_root; } reveal_ctx;

static int reveal_fn(rb_node *n, void *ctx)
{
    reveal_ctx *r = ctx;
    if (n->type != N_LIST) return 0;
    rb_node *o = n->id ? node_by_id(r->old_root, n->id) : NULL;
    if (!o || o->type != N_LIST || !same(o->doc_value, n->doc_value)) list_reveal(n, n->sel);
    else list_clamp_scroll(n);
    return 0;
}

int rbui_set_doc(rbui *ui, const char *json, size_t len, char *err, size_t errlen)
{
    if (!ui) return -1;
    if (err && errlen) err[0] = 0;
    rb_rect before = window_area(ui);
    if (!json || len == 0) {
        menu_close(ui);
        doc_free(&ui->doc);
        ui->have_doc = 0;
        ui->focus = ui->tnode = ui->flash = ui->click_node = NULL;
        ui->track = TR_NONE;
        ui->pinned = ui->pinned_band = NULL;
        ui->scroll = 0;
        ui_damage(ui, before);
        return 0;
    }
    if (assets_refresh()) {
        /* hostd built a newer bundle (a new rbplatinum): its fonts, icons and controls now. */
        ui->f_sys = rbf_get("system12");
        ui->f_reg = rbf_get("regular12");
        ui->f_small = rbf_get("small10");
        ui->f_tiny = assets_font("small9");
        if (!ui->f_tiny) ui->f_tiny = ui->f_small;
        ui_damage_all(ui);
    }
    rb_doc nd;
    if (doc_parse(json, len, &nd, err, errlen)) return -1;

    /* State survives only within the same document id, unless reset. */
    int keep = ui->have_doc && !nd.reset && same(ui->doc.id, nd.id);
    char *focus_id = keep && ui->focus ? dupstr(ui->focus->id) : NULL;
    char *track_id = keep && ui->tnode ? dupstr(ui->tnode->id) : NULL;
    int track_type = ui->tnode ? ui->tnode->type : -1, old_track = keep ? ui->track : TR_NONE;
    char *menu_id = keep && ui->menu ? dupstr(ui->menu->id) : NULL;
    char *flash_id = keep && ui->flash ? dupstr(ui->flash->id) : NULL;
    char *click_id = keep && ui->click_node ? dupstr(ui->click_node->id) : NULL;
    int old_focus_opt = ui->focus_opt;
    int focus_hint_changed = !keep || !same(ui->doc.focus, nd.focus);
    rb_doc old = ui->doc;
    if (keep) {
        merge_ctx m = {old.root};
        node_walk(nd.root, merge_fn, &m);
    } else {
        ui->scroll = 0;
    }
    ui->menu = NULL;
    ui->doc = nd;
    ui->have_doc = 1;
    ui->focus = ui->tnode = ui->flash = ui->click_node = NULL;
    layout_doc(ui);
    {
        reveal_ctx r = {keep ? old.root : NULL};
        node_walk(ui->doc.root, reveal_fn, &r);
    }
    doc_free(&old);

    /* Focus: a changed "focus" hint wins, then the previous focus, then the
     * first field or list. */
    rb_node *f = NULL;
    if (focus_hint_changed && ui->doc.focus) f = focusable_by_id(ui, ui->doc.focus);
    if (!f && focus_id) f = focusable_by_id(ui, focus_id);
    if (!f && (!keep || focus_id)) f = default_focus(ui);
    ui->focus = f;
    if (f && (f->type == N_RADIO || f->type == N_BEVELS)) {
        ui->focus_opt = keep && focus_id && same(f->id, focus_id) && old_focus_opt >= 0 && old_focus_opt < f->nopts
                            ? old_focus_opt
                            : (f->sel >= 0 ? f->sel : first_enabled_opt(f));
    }
    if (f && f->type == N_FIELD && (!keep || !focus_id || !same(f->id, focus_id))) {
        f->anchor = 0;
        f->caret = f->len;
    }
    if (flash_id) {
        rb_node *t = node_by_id(ui->doc.root, flash_id);
        if (t && t->type == N_BUTTON) ui->flash = t;
    }
    if (click_id) {
        rb_node *t = node_by_id(ui->doc.root, click_id);
        if (t && t->type == N_LIST) ui->click_node = t;
    }
    if (menu_id) {
        rb_node *t = node_by_id(ui->doc.root, menu_id);
        if (t && t->type == N_POPUP && node_focusable(t)) {
            ui->menu = t;
            if (ui->menu_hi >= t->nopts || (ui->menu_hi >= 0 && t->opts[ui->menu_hi].disabled)) ui->menu_hi = t->sel;
            menu_layout(ui);
        }
    }
    /* Pointer tracking continues only on a widget of the same kind (and, for
     * radios, a still-enabled option); otherwise it is cancelled. */
    ui->track = TR_NONE;
    if (old_track == TR_VBAR && ui->scrollable) {
        ui->track = TR_VBAR;
    } else if (old_track == TR_MENU && ui->menu) {
        ui->track = TR_MENU;
    } else if (old_track != TR_NONE && track_id) {
        rb_node *t = node_by_id(ui->doc.root, track_id);
        int ok = t && t->type == track_type && node_focusable(t);
        if (ok && old_track == TR_RADIO)
            ok = ui->track_opt >= 0 && ui->track_opt < t->nopts && !t->opts[ui->track_opt].disabled;
        if (ok) {
            ui->track = old_track;
            ui->tnode = t;
        }
    }
    /* Scroll the focused widget into view only when focus moved. */
    if (ui->focus && (!keep || !focus_id || !same(ui->focus->id, focus_id))) ui_reveal(ui, ui->focus);
    free(focus_id);
    free(track_id);
    free(menu_id);
    free(flash_id);
    free(click_id);
    ui_caret_reset(ui);
    ui_damage(ui, before);
    ui_damage(ui, window_area(ui));
    return 0;
}

void rbui_resize(rbui *ui, int w, int h)
{
    if (!ui) return;
    if (w < 1) w = 1;
    if (h < 1) h = 1;
    if (w > 16384) w = 16384;
    if (h > 16384) h = 16384;
    ui->cw = w;
    ui->ch = h;
    if (ui->px >= w) ui->px = w - 1;
    if (ui->py >= h) ui->py = h - 1;
    layout_doc(ui);
    if (ui->focus) ui_reveal(ui, ui->focus);
    ui_damage_all(ui);
}

/* ---- Animation ------------------------------------------------------------------- */

typedef struct { rbui *ui; int any; } barber_ctx;

static int barber_fn(rb_node *n, void *ctx)
{
    barber_ctx *b = ctx;
    if (n->type == N_PROGRESS && n->indeterminate) {
        ui_damage_node(b->ui, n);
        b->any = 1;
    }
    return 0;
}

typedef struct { int any; } indet_ctx;

static int indet_fn(rb_node *n, void *ctx)
{
    if (n->type == N_PROGRESS && n->indeterminate) {
        ((indet_ctx *)ctx)->any = 1;
        return 1;
    }
    return 0;
}

/* 0 when nothing is pending; otherwise the milliseconds (1..100) until rbui
 * next needs a tick, and at least 1 while damage waits for a render. */
static int next_tick_ms(rbui *ui)
{
    uint64_t best = UINT64_MAX;
#define CONSIDER(t) do { uint64_t t_ = (t); if (t_ < best) best = t_; } while (0)
    if (ui->have_doc) {
        if (ui->focus && ui->focus->type == N_FIELD) CONSIDER(ui->caret_next);
        if (ui->flash) CONSIDER(ui->flash_until);
        indet_ctx ic = {0};
        node_walk(ui->doc.root, indet_fn, &ic);
        if (ic.any) CONSIDER(ui->barber_next);
        if (ui->track == TR_LISTBAR || ui->track == TR_VBAR || ui->track == TR_LIST ||
            (ui->track == TR_MENU && ui->menu_arrows))
            CONSIDER(ui->repeat_at);
    }
#undef CONSIDER
    if (best == UINT64_MAX) return ui->dmg ? 1 : 0;
    uint64_t d = best > ui->now ? best - ui->now : 1;
    if (d > 100) d = 100;
    return (int)d;
}

int rbui_tick(rbui *ui, uint64_t now)
{
    if (!ui) return 0;
    ui->now = now;
    rb_node *f = ui->focus;
    if (f && f->type == N_FIELD && ui->have_doc) {
        if (ui->caret_next == 0) ui->caret_next = now + RB_CARET_MS;
        if (now >= ui->caret_next) {
            ui->caret_on = !ui->caret_on;
            ui->caret_next = now + RB_CARET_MS;
            ui_damage_node(ui, f);
        }
    }
    if (ui->flash && now >= ui->flash_until) {
        ui_damage_node(ui, ui->flash);
        ui->flash = NULL;
    }
    if (ui->have_doc && now >= ui->barber_next) {
        barber_ctx b = {ui, 0};
        node_walk(ui->doc.root, barber_fn, &b);
        if (b.any) {
            ui->barber = (ui->barber + 1) % 16;
            ui->barber_next = now + RB_BARBER_MS;
        }
    }
    input_tick(ui);
    flush(ui);
    return next_tick_ms(ui);
}

/* ---- Input entry points ------------------------------------------------------- */

void rbui_pointer(rbui *ui, int x, int y, unsigned buttons)
{
    if (!ui) return;
    if (x != ui->px || y != ui->py) {
        ui_damage(ui, cursor_bounds(ui->px, ui->py));
        ui_damage(ui, cursor_bounds(x, y));
    }
    if (ui->have_doc) input_pointer(ui, x, y, buttons);
    ui->px = x;
    ui->py = y;
    ui->buttons = buttons;
    flush(ui);
}

void rbui_wheel(rbui *ui, int dy)
{
    if (!ui) return;
    if (dy > 1000) dy = 1000;
    if (dy < -1000) dy = -1000;
    if (ui->have_doc && ui->track == TR_NONE) input_wheel(ui, dy);
    flush(ui);
}

void rbui_key(rbui *ui, int key, const char *utf8, int down, unsigned mods)
{
    if (!ui) return;
    if (down && ui->have_doc) input_key(ui, key, utf8, mods);
    flush(ui);
}

void rbui_cursor_pos(const rbui *ui, int *x, int *y)
{
    if (x) *x = ui ? ui->px : 0;
    if (y) *y = ui ? ui->py : 0;
}

/* ---- Rendering ---------------------------------------------------------------- */

static int checker_phase;

void rbui_set_checker_phase(int phase) { checker_phase = phase & 1; }

void rbui_draw_checkerboard(uint32_t *xrgb, int stride, int x, int y, int w, int h)
{
    if (!xrgb || w <= 0 || h <= 0) return;
    for (int yy = y; yy < y + h; ++yy) {
        uint32_t *row = xrgb + (size_t)yy * (size_t)stride;
        for (int xx = x; xx < x + w; ++xx) row[xx] = ((xx + yy + checker_phase) & 1) ? 0x000000u : 0xFFFFFFu;
    }
}

void rbui_render(rbui *ui, uint32_t *xrgb, int stride, int background, const uint32_t *bg, int bg_stride, int show_cursor)
{
    if (!ui || !xrgb) return;
    rb_gfx g = {xrgb, stride, ui->cw, ui->ch, rr(0, 0, ui->cw, ui->ch), 0, 0};
    if (background == RBUI_BG_IMAGE && bg) {
        for (int y = 0; y < ui->ch; ++y)
            memcpy(xrgb + (size_t)y * (size_t)stride, bg + (size_t)y * (size_t)bg_stride, (size_t)ui->cw * 4);
    } else if (background == RBUI_BG_IMAGE_DIMMED && bg) {
        for (int y = 0; y < ui->ch; ++y) {
            const uint32_t *s = bg + (size_t)y * (size_t)bg_stride;
            uint32_t *d = xrgb + (size_t)y * (size_t)stride;
            for (int x = 0; x < ui->cw; ++x) d[x] = (s[x] >> 1) & 0x7F7F7Fu;
        }
    } else if (background == RBUI_BG_BLACK || background == RBUI_BG_IMAGE_DIMMED) {
        g_fill(&g, 0, 0, ui->cw, ui->ch, C_BLACK);
    } else {
        rbui_draw_checkerboard(xrgb, stride, 0, 0, ui->cw, ui->ch);
    }
    render_doc(ui, &g);
    if (show_cursor) cursor_draw(&g, ui->px, ui->py, NULL);
    ui->dmg = 0;
    ui->damage = rr(0, 0, 0, 0);
}

int rbui_damage(rbui *ui, int *x, int *y, int *w, int *h)
{
    if (!ui || !ui->dmg) return 0;
    if (x) *x = ui->damage.x;
    if (y) *y = ui->damage.y;
    if (w) *w = ui->damage.w;
    if (h) *h = ui->damage.h;
    return 1;
}

/* ---- Extensions ------------------------------------------------------------------ */

int rbui_focus(rbui *ui, const char *id)
{
    if (!ui) return -1;
    if (!id) {
        ui_set_focus(ui, NULL);
        return 0;
    }
    rb_node *n = ui->have_doc ? focusable_by_id(ui, id) : NULL;
    if (!n) return -1;
    ui_set_focus(ui, n);
    return 0;
}

const char *rbui_doc_id(const rbui *ui) { return ui && ui->have_doc && ui->doc.id ? ui->doc.id : ""; }
const char *rbui_focus_id(const rbui *ui) { return ui && ui->focus ? ui->focus->id : NULL; }

static const char *const kTypeNames[N__COUNT] = {
    "text", "separator", "group", "stack", "columns", "list", "field", "checkbox",
    "radio", "popup", "progress", "qr", "buttons", "button", "icon", "spacer",
    "facts", "meters", "band", "bevels", "slider",
};

static cJSON *rect_json(rb_rect r)
{
    int v[4] = {r.x, r.y, r.w, r.h};
    return cJSON_CreateIntArray(v, 4);
}

typedef struct { const rbui *ui; cJSON *arr; } describe_ctx;

static int describe_fn(rb_node *n, void *ctx)
{
    describe_ctx *d = ctx;
    if (n == d->ui->doc.root) return 0;
    cJSON *o = cJSON_CreateObject();
    if (!o) return 0;
    cJSON_AddStringToObject(o, "type", kTypeNames[n->type]);
    if (n->id) cJSON_AddStringToObject(o, "id", n->id);
    rb_rect r = ui_node_screen(d->ui, n, r_empty(n->ctl) ? n->r : n->ctl);
    cJSON_AddItemToObject(o, "rect", rect_json(r));
    if (node_has_value(n)) cJSON_AddItemToObject(o, "value", value_json(n));
    if (n->disabled) cJSON_AddTrueToObject(o, "disabled");
    if (d->ui->focus == n) cJSON_AddTrueToObject(o, "focused");
    if (n->type == N_BUTTON && n->is_default) cJSON_AddTrueToObject(o, "default");
    if (n->type == N_RADIO || n->type == N_LIST || n->type == N_POPUP || n->type == N_BEVELS) {
        cJSON *opts = cJSON_CreateArray();
        for (int i = 0; opts && i < n->nopts; ++i) {
            cJSON *oo = cJSON_CreateObject();
            if (!oo) break;
            cJSON_AddStringToObject(oo, "id", n->opts[i].id);
            rb_rect orr = rr(0, 0, 0, 0);
            if (n->type == N_RADIO) orr = radio_opt_rect(d->ui, n, i);
            else if (n->type == N_BEVELS) orr = bevel_rect(n, i);
            else if (n->type == N_LIST && n->icon_view) orr = icons_icon_rect(n, i);   /* the icon */
            else if (n->type == N_LIST) {
                int vis = i - n->scroll;
                if (vis >= 0 && vis < list_visible_rows(n)) {
                    rb_rect rows = list_rows_rect(n);
                    orr = rr(rows.x, rows.y + vis * list_row_h(), rows.w, list_row_h());
                }
            }
            if (!r_empty(orr)) cJSON_AddItemToObject(oo, "rect", rect_json(ui_node_screen(d->ui, n, orr)));
            cJSON_AddItemToArray(opts, oo);
        }
        if (opts) cJSON_AddItemToObject(o, "options", opts);
    }
    if (n->type == N_LIST) cJSON_AddNumberToObject(o, "scroll", n->scroll);
    cJSON_AddItemToArray(d->arr, o);
    return 0;
}

char *rbui_describe(const rbui *ui)
{
    if (!ui) return NULL;
    cJSON *o = cJSON_CreateObject();
    if (!o) return NULL;
    cJSON_AddStringToObject(o, "doc", rbui_doc_id(ui));
    int cv[2] = {ui->cw, ui->ch};
    cJSON_AddItemToObject(o, "canvas", cJSON_CreateIntArray(cv, 2));
    if (ui->have_doc) {
        cJSON_AddItemToObject(o, "window", rect_json(ui->frame));
        cJSON_AddItemToObject(o, "content", rect_json(ui->content));
        cJSON_AddNumberToObject(o, "scroll", ui->scroll);
        cJSON_AddBoolToObject(o, "scrollable", ui->scrollable);
        if (ui->focus && ui->focus->id) cJSON_AddStringToObject(o, "focus", ui->focus->id);
        if (ui->menu) cJSON_AddItemToObject(o, "menu", rect_json(ui->menu_r));
        describe_ctx d = {ui, cJSON_CreateArray()};
        if (d.arr) {
            node_walk(ui->doc.root, describe_fn, &d);
            cJSON_AddItemToObject(o, "widgets", d.arr);
        }
    }
    char *s = cJSON_PrintUnformatted(o);
    cJSON_Delete(o);
    if (!s) return NULL;
    char *out = strdup(s);
    cJSON_free(s);
    return out;
}

/* ---- Built-in documents ------------------------------------------------------- */

static const char kHostdLost[] =
    "{\"id\":\"hostd-lost\",\"kind\":\"alert\",\"alert_icon\":\"stop\",\"width\":420,"
    "\"items\":["
    "{\"type\":\"text\",\"style\":\"emphasis\",\"text\":\"RetroBoot needs to restart.\"},"
    "{\"type\":\"text\",\"style\":\"small\",\"text\":\"The system stopped responding. Restart to return to "
    "Mac OS, or shut down the computer. Unsaved changes in Mac OS may be lost.\"},"
    "{\"type\":\"buttons\",\"items\":["
    "{\"id\":\"shutdown\",\"label\":\"Shut Down\"},"
    "{\"id\":\"restart\",\"label\":\"Restart\",\"default\":true}]}]}";

const char *rbui_builtin_doc(const char *name)
{
    /* "fallback" is presenter-core's name for the same alert. */
    if (name && (strcmp(name, "hostd-lost") == 0 || strcmp(name, "fallback") == 0)) return kHostdLost;
    return NULL;
}
