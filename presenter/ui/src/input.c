/* input.c - keyboard focus, text editing, Mac-style pointer tracking */
#include <stdlib.h>
#include <string.h>

#include "rbui_internal.h"

static void view_scroll_to(rbui *ui, int s);

/* ---- Focus order ----------------------------------------------------------------- */

typedef struct { rb_node **v; int n, cap; } nodelist;

static int collect_fn(rb_node *n, void *ctx)
{
    nodelist *l = ctx;
    if (!node_focusable(n)) return 0;
    if (l->n == l->cap) {
        int nc = l->cap ? l->cap * 2 : 32;
        rb_node **nv = realloc(l->v, (size_t)nc * sizeof(rb_node *));
        if (!nv) return 1;
        l->v = nv;
        l->cap = nc;
    }
    l->v[l->n++] = n;
    return 0;
}

static void move_focus(rbui *ui, int dir)
{
    nodelist l = {NULL, 0, 0};
    node_walk(ui->doc.root, collect_fn, &l);
    if (l.n == 0) {
        free(l.v);
        return;
    }
    int cur = -1;
    for (int i = 0; i < l.n; ++i)
        if (l.v[i] == ui->focus) cur = i;
    int next = cur < 0 ? (dir > 0 ? 0 : l.n - 1) : (cur + dir + l.n) % l.n;
    rb_node *n = l.v[next];
    free(l.v);
    ui_set_focus(ui, n);
    if (n->type == N_FIELD) {
        n->anchor = 0;
        n->caret = n->len;
        field_fix_scroll(ui, n);
        ui_damage_node(ui, n);
    }
}

typedef struct { rb_node *found; const char *id; int want_default; } btn_ctx;

static int btn_fn(rb_node *n, void *ctx)
{
    btn_ctx *b = ctx;
    if (n->type != N_BUTTON || !node_focusable(n)) return 0;
    if ((b->want_default && n->is_default) || (b->id && n->id && strcmp(n->id, b->id) == 0)) {
        b->found = n;
        return 1;
    }
    return 0;
}

static rb_node *find_button(rbui *ui, const char *id, int want_default)
{
    btn_ctx b = {NULL, id, want_default};
    node_walk(ui->doc.root, btn_fn, &b);
    return b.found;
}

void activate_button(rbui *ui, rb_node *b, int flash)
{
    if (!b || !node_focusable(b)) return;
    if (flash) {
        if (ui->flash && ui->flash != b) ui_damage_node(ui, ui->flash);
        ui->flash = b;
        ui->flash_until = ui->now + RB_FLASH_MS;
        ui_damage_node(ui, b);
    }
    ui_emit_action(ui, b);
}

static void double_click_default(rbui *ui)
{
    rb_node *b = find_button(ui, NULL, 1);
    if (b) activate_button(ui, b, 1);
}

/* ---- Value changes --------------------------------------------------------------- */

static void select_opt(rbui *ui, rb_node *n, int idx)
{
    if (idx < 0 || idx >= n->nopts || n->opts[idx].disabled) return;
    if (n->type == N_LIST) list_reveal(n, idx);
    if ((n->type == N_RADIO || n->type == N_BEVELS) && ui->focus == n) ui->focus_opt = idx;
    ui_damage_node(ui, n);
    if (n->sel == idx) return;
    n->sel = idx;
    ui_emit_change(ui, n);
}

static void toggle_check(rbui *ui, rb_node *n)
{
    n->checked = !n->checked;
    ui_damage_node(ui, n);
    if (n->reveal_field) {
        /* "Show password": bullets <-> text, here only; nothing about the value is sent. */
        field_fix_scroll(ui, n->reveal_field);
        ui_damage_node(ui, n->reveal_field);
    }
    ui_emit_change(ui, n);
}

static int step_opt(const rb_node *n, int from, int dir)
{
    for (int i = from + dir; i >= 0 && i < n->nopts; i += dir)
        if (!n->opts[i].disabled) return i;
    return -1;
}

static int edge_opt(const rb_node *n, int last)
{
    return last ? step_opt(n, n->nopts, -1) : step_opt(n, -1, 1);
}

/* The rectangle of option i of a radio group or a row of bevel buttons (node coordinates). */
static rb_rect opt_rect(rbui *ui, const rb_node *n, int i)
{
    return n->type == N_BEVELS ? bevel_rect(n, i) : radio_opt_rect(ui, n, i);
}

static void slider_set(rbui *ui, rb_node *n, double v)
{
    if (v < n->vmin) v = n->vmin;
    if (v > n->vmax) v = n->vmax;
    if (v == n->val) return;
    n->val = v;
    ui_damage_node(ui, n);
    ui_emit_change(ui, n);
}

/* ---- Text field editing ---------------------------------------------------------- */

static void field_changed(rbui *ui, rb_node *n, int emit)
{
    field_fix_scroll(ui, n);
    ui_caret_reset(ui);
    ui_damage_node(ui, n);
    if (emit) ui_emit_change(ui, n);
}

static int sel_lo(const rb_node *n) { return n->caret < n->anchor ? n->caret : n->anchor; }
static int sel_hi(const rb_node *n) { return n->caret > n->anchor ? n->caret : n->anchor; }

static void field_delete_range(rb_node *n, int a, int b)
{
    if (b <= a) return;
    memmove(n->buf + a, n->buf + b, (size_t)(n->len - b) + 1);
    n->len -= b - a;
    n->caret = n->anchor = a;
}

static void field_insert(rbui *ui, rb_node *n, const char *s)
{
    /* Accept printable code points only; the field never holds controls. */
    size_t o = 0, i = 0, len = strlen(s);
    if (len > 4096) len = 4096;
    char *clean = malloc(len + 4);
    if (!clean) return;
    while (i < len) {
        uint32_t cp = rbf_utf8_next(s, len, &i);
        if (cp < 0x20 || cp == 0x7F || (cp >= 0x80 && cp < 0xA0) || cp == 0xFFFD) continue;
        o += (size_t)rbf_utf8_put(cp, clean + o);
    }
    if (o == 0) {
        free(clean);
        return;
    }
    int had_sel = n->caret != n->anchor;
    field_delete_range(n, sel_lo(n), sel_hi(n));
    /* Enforce max_length in code points. */
    int room = n->maxlen - field_codepoints(n);
    size_t keep = 0, k = 0;
    while (k < o && room > 0) {
        rbf_utf8_next(clean, o, &k);
        keep = k;
        --room;
    }
    if (keep == 0) {
        free(clean);
        if (had_sel) field_changed(ui, n, 1);
        return;
    }
    if ((size_t)n->len + keep + 1 > (size_t)n->cap) {
        size_t nc = ((size_t)n->len + keep + 1) * 2;
        char *nb = realloc(n->buf, nc);
        if (!nb) {
            free(clean);
            return;
        }
        n->buf = nb;
        n->cap = (int)nc;
    }
    memmove(n->buf + n->caret + keep, n->buf + n->caret, (size_t)(n->len - n->caret) + 1);
    memcpy(n->buf + n->caret, clean, keep);
    free(clean);
    n->len += (int)keep;
    n->caret += (int)keep;
    n->anchor = n->caret;
    field_changed(ui, n, 1);
}

static int is_word_cp(uint32_t cp)
{
    return (cp >= '0' && cp <= '9') || (cp >= 'A' && cp <= 'Z') || (cp >= 'a' && cp <= 'z') || cp == '_' || cp >= 0xC0;
}

static int word_left(const rb_node *n, int pos)
{
    while (pos > 0) {
        int p = (int)rbf_utf8_prev(n->buf, (size_t)pos);
        size_t t = (size_t)p;
        if (is_word_cp(rbf_utf8_next(n->buf, (size_t)n->len, &t))) break;
        pos = p;
    }
    while (pos > 0) {
        int p = (int)rbf_utf8_prev(n->buf, (size_t)pos);
        size_t t = (size_t)p;
        if (!is_word_cp(rbf_utf8_next(n->buf, (size_t)n->len, &t))) break;
        pos = p;
    }
    return pos;
}

static int word_right(const rb_node *n, int pos)
{
    size_t t = (size_t)pos;
    while ((int)t < n->len) {
        size_t s = t;
        if (is_word_cp(rbf_utf8_next(n->buf, (size_t)n->len, &s))) break;
        t = s;
    }
    while ((int)t < n->len) {
        size_t s = t;
        if (!is_word_cp(rbf_utf8_next(n->buf, (size_t)n->len, &s))) break;
        t = s;
    }
    return (int)t;
}

static int field_key(rbui *ui, rb_node *n, int key, const char *utf8, unsigned mods)
{
    int shift = (mods & RBUI_MOD_SHIFT) != 0;
    int word = (mods & (RBUI_MOD_OPTION | RBUI_MOD_CTRL)) != 0 && !field_masked(n);
    int line = (mods & RBUI_MOD_COMMAND) != 0;
    int pos = n->caret;
    switch (key) {
    case RBUI_KEY_BACKSPACE:
    case RBUI_KEY_DELETE: {
        if (n->caret != n->anchor) {
            field_delete_range(n, sel_lo(n), sel_hi(n));
        } else if (key == RBUI_KEY_BACKSPACE) {
            if (n->caret == 0) return 1;
            int a = line ? 0 : word ? word_left(n, n->caret) : (int)rbf_utf8_prev(n->buf, (size_t)n->caret);
            field_delete_range(n, a, n->caret);
        } else {
            if (n->caret >= n->len) return 1;
            size_t t = (size_t)n->caret;
            if (line) t = (size_t)n->len;
            else if (word) t = (size_t)word_right(n, n->caret);
            else rbf_utf8_next(n->buf, (size_t)n->len, &t);
            field_delete_range(n, n->caret, (int)t);
        }
        field_changed(ui, n, 1);
        return 1;
    }
    case RBUI_KEY_LEFT:
        if (!shift && n->caret != n->anchor) pos = sel_lo(n);
        else if (line) pos = 0;
        else if (word) pos = word_left(n, pos);
        else pos = (int)rbf_utf8_prev(n->buf, (size_t)pos);
        break;
    case RBUI_KEY_RIGHT:
        if (!shift && n->caret != n->anchor) pos = sel_hi(n);
        else if (line) pos = n->len;
        else if (word) pos = word_right(n, pos);
        else if (pos < n->len) {
            size_t t = (size_t)pos;
            rbf_utf8_next(n->buf, (size_t)n->len, &t);
            pos = (int)t;
        }
        break;
    case RBUI_KEY_HOME:
    case RBUI_KEY_UP:
        pos = 0;
        break;
    case RBUI_KEY_END:
    case RBUI_KEY_DOWN:
        pos = n->len;
        break;
    case RBUI_KEY_SPACE:
    case RBUI_KEY_NONE:
        if (mods & (RBUI_MOD_COMMAND | RBUI_MOD_CTRL)) {
            if (utf8 && (utf8[0] == 'a' || utf8[0] == 'A') && !utf8[1]) {
                n->anchor = 0;
                n->caret = n->len;
                field_changed(ui, n, 0);
                return 1;
            }
            return 0;
        }
        if (utf8 && utf8[0]) field_insert(ui, n, utf8);
        else if (key == RBUI_KEY_SPACE) field_insert(ui, n, " ");
        return 1;
    default:
        return 0;
    }
    n->caret = pos;
    if (!shift) n->anchor = pos;
    field_changed(ui, n, 0);
    return 1;
}

/* ---- Lists, radios, popups ------------------------------------------------------- */

static int ascii_lower(int c) { return c >= 'A' && c <= 'Z' ? c + 32 : c; }

static int prefix_match(const char *label, const char *p, int plen)
{
    for (int i = 0; i < plen; ++i) {
        if (!label[i]) return 0;
        if (ascii_lower((unsigned char)label[i]) != ascii_lower((unsigned char)p[i])) return 0;
    }
    return 1;
}

static void type_ahead(rbui *ui, rb_node *n, const char *utf8)
{
    size_t len = strlen(utf8);
    if (ui->now - ui->ta_time > RB_TYPEAHEAD_MS) ui->ta_len = 0;
    ui->ta_time = ui->now;
    if (ui->ta_len + len >= sizeof(ui->ta)) ui->ta_len = 0;
    if (len >= sizeof(ui->ta)) return;
    memcpy(ui->ta + ui->ta_len, utf8, len);
    ui->ta_len += (int)len;
    for (int i = 0; i < n->nopts; ++i)
        if (!n->opts[i].disabled && prefix_match(n->opts[i].label, ui->ta, ui->ta_len)) {
            select_opt(ui, n, i);
            return;
        }
}

static int list_key(rbui *ui, rb_node *n, int key, const char *utf8, unsigned mods)
{
    int vis = list_visible_rows(n), to = -1;
    if (n->icon_view) {
        /* Arrows move through the grid; Command-O (or Command-Down) opens, as in the Finder. */
        int c = n->columns > 0 ? n->columns : 4, cur = n->sel;
        if ((key == RBUI_KEY_DOWN && (mods & RBUI_MOD_COMMAND)) ||
            (key == RBUI_KEY_NONE && (mods & RBUI_MOD_COMMAND) && utf8 && (utf8[0] == 'o' || utf8[0] == 'O'))) {
            if (cur >= 0) ui_emit_action(ui, n);
            return 1;
        }
        switch (key) {
        case RBUI_KEY_LEFT: to = cur < 0 ? edge_opt(n, 0) : step_opt(n, cur, -1); break;
        case RBUI_KEY_RIGHT: to = cur < 0 ? edge_opt(n, 0) : step_opt(n, cur, 1); break;
        case RBUI_KEY_UP: to = cur < 0 ? edge_opt(n, 0) : (cur - c >= 0 && !n->opts[cur - c].disabled ? cur - c : -1); break;
        case RBUI_KEY_DOWN: to = cur < 0 ? edge_opt(n, 0) : (cur + c < n->nopts && !n->opts[cur + c].disabled ? cur + c : -1); break;
        case RBUI_KEY_HOME: to = edge_opt(n, 0); break;
        case RBUI_KEY_END: to = edge_opt(n, 1); break;
        default:
            break;
        }
        if (to >= 0) {
            select_opt(ui, n, to);
            return 1;
        }
        if (key == RBUI_KEY_LEFT || key == RBUI_KEY_RIGHT || key == RBUI_KEY_UP || key == RBUI_KEY_DOWN)
            return 1;
    }
    switch (key) {
    case RBUI_KEY_UP: to = n->sel < 0 ? edge_opt(n, 0) : step_opt(n, n->sel, -1); break;
    case RBUI_KEY_DOWN: to = n->sel < 0 ? edge_opt(n, 0) : step_opt(n, n->sel, 1); break;
    case RBUI_KEY_HOME: to = edge_opt(n, 0); break;
    case RBUI_KEY_END: to = edge_opt(n, 1); break;
    case RBUI_KEY_PAGEUP:
    case RBUI_KEY_PAGEDOWN: {
        int dir = key == RBUI_KEY_PAGEUP ? -1 : 1;
        int target = (n->sel < 0 ? 0 : n->sel) + dir * (vis > 1 ? vis - 1 : 1);
        if (target < 0) target = 0;
        if (target >= n->nopts) target = n->nopts - 1;
        to = n->opts[target].disabled ? step_opt(n, target, -dir) : target;
        if (to < 0) to = step_opt(n, target, dir);
        break;
    }
    case RBUI_KEY_SPACE:
        /* A space continues a type-ahead in progress ("Coffee Shop"). */
        if (ui->ta_len > 0 && ui->now - ui->ta_time <= RB_TYPEAHEAD_MS) type_ahead(ui, n, " ");
        return 1;
    case RBUI_KEY_NONE:
        if (utf8 && utf8[0] && !(mods & (RBUI_MOD_COMMAND | RBUI_MOD_CTRL)) && (unsigned char)utf8[0] >= 0x20) {
            type_ahead(ui, n, utf8);
            return 1;
        }
        return 0;
    default:
        return 0;
    }
    if (to >= 0) select_opt(ui, n, to);
    return 1;
}

static int slider_key(rbui *ui, rb_node *n, int key)
{
    double page = n->vstep * 10;
    switch (key) {
    case RBUI_KEY_LEFT: case RBUI_KEY_DOWN: slider_set(ui, n, n->val - n->vstep); break;
    case RBUI_KEY_RIGHT: case RBUI_KEY_UP: slider_set(ui, n, n->val + n->vstep); break;
    case RBUI_KEY_PAGEDOWN: slider_set(ui, n, n->val - page); break;
    case RBUI_KEY_PAGEUP: slider_set(ui, n, n->val + page); break;
    case RBUI_KEY_HOME: slider_set(ui, n, n->vmin); break;
    case RBUI_KEY_END: slider_set(ui, n, n->vmax); break;
    default: return 0;
    }
    return 1;
}

static int radio_key(rbui *ui, rb_node *n, int key)
{
    int cur = ui->focus_opt >= 0 ? ui->focus_opt : n->sel, to = -1;
    switch (key) {
    case RBUI_KEY_UP:
    case RBUI_KEY_LEFT: to = cur < 0 ? edge_opt(n, 0) : step_opt(n, cur, -1); break;
    case RBUI_KEY_DOWN:
    case RBUI_KEY_RIGHT: to = cur < 0 ? edge_opt(n, 0) : step_opt(n, cur, 1); break;
    case RBUI_KEY_SPACE: to = cur; break;
    default: return 0;
    }
    if (to >= 0) select_opt(ui, n, to);
    return 1;
}

void menu_close(rbui *ui)
{
    if (!ui->menu) return;
    ui_damage(ui, r_out(ui->menu_r, 3));
    ui_damage_node(ui, ui->menu);
    ui->menu = NULL;
    if (ui->track == TR_MENU) ui->track = TR_NONE;
}

void menu_open(rbui *ui, rb_node *n, int sticky)
{
    if (!n || n->nopts == 0) return;
    ui->menu = n;
    ui->menu_hi = n->sel;
    ui->menu_sticky = sticky;
    ui->menu_moved = 0;
    ui->menu_first = n->sel > 0 ? n->sel - 1 : 0;
    ui->menu_open_time = ui->now;
    menu_layout(ui);
    ui_damage(ui, r_out(ui->menu_r, 3));
    ui_damage_node(ui, n);
}

static void menu_reveal_hi(rbui *ui)
{
    if (!ui->menu_arrows || ui->menu_hi < 0) return;
    int rows = ui->menu_rows - 2;
    if (ui->menu_hi < ui->menu_first) ui->menu_first = ui->menu_hi;
    if (ui->menu_hi >= ui->menu_first + rows) ui->menu_first = ui->menu_hi - rows + 1;
    menu_layout(ui);
}

static void menu_choose(rbui *ui, int idx)
{
    rb_node *n = ui->menu;
    menu_close(ui);
    if (n && idx >= 0 && idx < n->nopts && !n->opts[idx].disabled) select_opt(ui, n, idx);
}

static void menu_key(rbui *ui, int key)
{
    rb_node *n = ui->menu;
    int to = -1;
    switch (key) {
    case RBUI_KEY_UP: to = ui->menu_hi < 0 ? edge_opt(n, 1) : step_opt(n, ui->menu_hi, -1); break;
    case RBUI_KEY_DOWN: to = ui->menu_hi < 0 ? edge_opt(n, 0) : step_opt(n, ui->menu_hi, 1); break;
    case RBUI_KEY_HOME: case RBUI_KEY_PAGEUP: to = edge_opt(n, 0); break;
    case RBUI_KEY_END: case RBUI_KEY_PAGEDOWN: to = edge_opt(n, 1); break;
    case RBUI_KEY_RETURN: case RBUI_KEY_KP_ENTER: case RBUI_KEY_SPACE:
        menu_choose(ui, ui->menu_hi);
        return;
    case RBUI_KEY_ESCAPE: case RBUI_KEY_TAB:
        menu_close(ui);
        return;
    default:
        return;
    }
    if (to >= 0) {
        ui->menu_hi = to;
        menu_reveal_hi(ui);
        ui_damage(ui, r_out(ui->menu_r, 3));
    }
}

static int popup_key(rbui *ui, rb_node *n, int key)
{
    switch (key) {
    case RBUI_KEY_UP: select_opt(ui, n, n->sel < 0 ? edge_opt(n, 0) : step_opt(n, n->sel, -1)); return 1;
    case RBUI_KEY_DOWN: select_opt(ui, n, n->sel < 0 ? edge_opt(n, 0) : step_opt(n, n->sel, 1)); return 1;
    case RBUI_KEY_HOME: select_opt(ui, n, edge_opt(n, 0)); return 1;
    case RBUI_KEY_END: select_opt(ui, n, edge_opt(n, 1)); return 1;
    case RBUI_KEY_SPACE: menu_open(ui, n, 1); return 1;
    default: return 0;
    }
}

static int button_key(rbui *ui, rb_node *n, int key)
{
    if (key == RBUI_KEY_SPACE) {
        activate_button(ui, n, 1);
        return 1;
    }
    if ((key == RBUI_KEY_LEFT || key == RBUI_KEY_RIGHT) && n->parent) {
        rb_node *row = n->parent;
        int i = 0;
        while (i < row->nkids && row->kids[i] != n) ++i;
        int dir = key == RBUI_KEY_LEFT ? -1 : 1;
        for (int j = i + dir; j >= 0 && j < row->nkids; j += dir)
            if (node_focusable(row->kids[j])) {
                ui_set_focus(ui, row->kids[j]);
                break;
            }
        return 1;
    }
    return 0;
}

void input_key(rbui *ui, int key, const char *utf8, unsigned mods)
{
    if (ui->menu) {
        menu_key(ui, key);
        return;
    }
    int cmd = (mods & RBUI_MOD_COMMAND) != 0;
    if (key == RBUI_KEY_TAB) {
        move_focus(ui, (mods & RBUI_MOD_SHIFT) ? -1 : 1);
        return;
    }
    if (key == RBUI_KEY_RETURN || key == RBUI_KEY_KP_ENTER) {
        rb_node *btn = find_button(ui, NULL, 1);
        if (!btn && ui->focus && ui->focus->type == N_BUTTON) btn = ui->focus;
        activate_button(ui, btn, 1);
        return;
    }
    if (key == RBUI_KEY_ESCAPE || (cmd && utf8 && utf8[0] == '.' && !utf8[1])) {
        rb_node *btn = find_button(ui, "cancel", 0);
        if (!btn) btn = find_button(ui, "back", 0);
        if (!btn) btn = find_button(ui, "done", 0);
        activate_button(ui, btn, 1);
        return;
    }
    rb_node *f = ui->focus;
    if ((key == RBUI_KEY_PAGEUP || key == RBUI_KEY_PAGEDOWN) && ui->scrollable && (!f || (f->type != N_LIST && f->type != N_SLIDER))) {
        int page = ui->view.h - 16;
        view_scroll_to(ui, ui->scroll + (key == RBUI_KEY_PAGEUP ? -page : page));
        return;
    }
    if (!f) {
        if (key >= RBUI_KEY_LEFT && key <= RBUI_KEY_DOWN) move_focus(ui, 1);
        return;
    }
    switch (f->type) {
    case N_FIELD: field_key(ui, f, key, utf8, mods); break;
    case N_LIST: list_key(ui, f, key, utf8, mods); break;
    case N_RADIO: case N_BEVELS: radio_key(ui, f, key); break;
    case N_SLIDER: slider_key(ui, f, key); break;
    case N_POPUP: popup_key(ui, f, key); break;
    case N_CHECKBOX:
        if (key == RBUI_KEY_SPACE) toggle_check(ui, f);
        break;
    case N_BUTTON: button_key(ui, f, key); break;
    default: break;
    }
}

/* ---- Pointer ------------------------------------------------------------------------ */

typedef struct { rbui *ui; int x, y; rb_node *found; int pdis; } hit_ctx;

static int hit_node(rbui *ui, rb_node *n, int x, int y, int pdis, rb_node **out)
{
    int dis = pdis || n->disabled;
    for (int i = n->nkids - 1; i >= 0; --i)
        if (hit_node(ui, n->kids[i], x, y, dis, out)) return 1;
    if (n->scrolled && !r_has(ui->view, x, y)) return 0;
    int cy = n->scrolled ? y + ui->scroll : y;
    rb_rect r = rr(0, 0, 0, 0);
    switch (n->type) {
    case N_BUTTON: case N_CHECKBOX: case N_FIELD: case N_LIST: case N_SLIDER: r = n->ctl; break;
    case N_POPUP: r = rr(n->r.x, n->ctl.y, n->ctl.x + n->ctl.w - n->r.x, n->ctl.h); break;
    case N_RADIO:
    case N_BEVELS:
        for (int i = 0; i < n->nopts; ++i)
            if (r_has(opt_rect(ui, n, i), x, cy)) r = n->ctl;
        break;
    default: return 0;
    }
    if (!r_has(r, x, cy)) return 0;
    *out = dis ? NULL : n;
    return 1;
}

static rb_node *hit_test(rbui *ui, int x, int y)
{
    rb_node *out = NULL;
    if (ui->pinned && hit_node(ui, ui->pinned, x, y, 0, &out)) return out;
    if (ui->pinned_band && hit_node(ui, ui->pinned_band, x, y, 0, &out)) return out;
    rb_node *root = ui->doc.root;
    for (int i = root->nkids - 1; i >= 0; --i) {
        if (root->kids[i] == ui->pinned || root->kids[i] == ui->pinned_band) continue;
        if (hit_node(ui, root->kids[i], x, y, 0, &out)) return out;
    }
    return NULL;
}

static int bar_part(rb_rect bar, int total, int visible, int first, int y)
{
    if (total <= visible) return SB_NONE;
    int arrow = pt_bar_arrow(bar, y);
    if (arrow) return arrow;
    int ty = pt_thumb_pos(bar, total, visible, first);
    if (y < ty) return SB_PAGEUP;
    if (y >= ty + PT_THUMB_H) return SB_PAGEDOWN;
    return SB_THUMB;
}

static int thumb_to_first(rb_rect bar, int total, int visible, int thumb_top)
{
    int track_y, track_h;
    pt_bar_track(bar, &track_y, &track_h);
    int span = track_h - PT_THUMB_H, max_first = total - visible;
    if (span <= 0 || max_first <= 0) return 0;
    int v = (int)(((long long)(thumb_top - track_y) * max_first + span / 2) / span);
    if (v < 0) v = 0;
    if (v > max_first) v = max_first;
    return v;
}

static void list_scroll_by(rbui *ui, rb_node *n, int d)
{
    int old = n->scroll;
    n->scroll += d;
    list_clamp_scroll(n);
    if (n->scroll != old) ui_damage_node(ui, n);
}

static void view_scroll_to(rbui *ui, int s)
{
    int max = ui->content_h - ui->view.h;
    if (s > max) s = max;
    if (s < 0) s = 0;
    if (s == ui->scroll) return;
    ui->scroll = s;
    ui_damage(ui, ui->view);
    ui_damage(ui, ui->vbar);
}

static void bar_step(rbui *ui)
{
    /* One repeat step of a held scroll arrow or page region. */
    if (!ui->track_inside) return;
    if (ui->track == TR_LISTBAR) {
        rb_node *n = ui->tnode;
        int vis = list_visible_rows(n);
        rb_rect bar = ui_node_screen(ui, n, list_bar_rect(n));
        switch (ui->track_part) {
        case SB_UP: list_scroll_by(ui, n, -1); break;
        case SB_DOWN: list_scroll_by(ui, n, 1); break;
        case SB_PAGEUP:
        case SB_PAGEDOWN:
            if (bar_part(bar, n->nopts, vis, n->scroll, ui->py) == ui->track_part)
                list_scroll_by(ui, n, ui->track_part == SB_PAGEUP ? -(vis - 1 > 0 ? vis - 1 : 1) : (vis - 1 > 0 ? vis - 1 : 1));
            break;
        default: break;
        }
    } else if (ui->track == TR_VBAR) {
        int page = ui->view.h - 16;
        switch (ui->track_part) {
        case SB_UP: view_scroll_to(ui, ui->scroll - 16); break;
        case SB_DOWN: view_scroll_to(ui, ui->scroll + 16); break;
        case SB_PAGEUP:
        case SB_PAGEDOWN:
            if (bar_part(ui->vbar, ui->content_h, ui->view.h, ui->scroll, ui->py) == ui->track_part)
                view_scroll_to(ui, ui->scroll + (ui->track_part == SB_PAGEUP ? -page : page));
            break;
        default: break;
        }
    }
}

static int list_row_at(rbui *ui, rb_node *n, int y, int clamp)
{
    rb_rect rows = ui_node_screen(ui, n, list_rows_rect(n));
    int rel = y - rows.y;
    if (!clamp && (rel < 0 || rel >= rows.h)) return -1;
    if (rel < 0) rel = 0;
    if (rel >= rows.h) rel = rows.h - 1;
    int row = n->scroll + rel / list_row_h();
    if (row >= n->nopts) return clamp ? n->nopts - 1 : -1;
    return row;
}

static int menu_item_at(rbui *ui, int x, int y)
{
    rb_rect m = ui->menu_r;
    if (!r_has(m, x, y)) return -1;
    int top = m.y + 3 + (ui->menu_arrows ? RB_MENU_ITEM_H : 0);
    int rows = ui->menu_rows - (ui->menu_arrows ? 2 : 0);
    if (y < top) return -1;
    int i = (y - top) / RB_MENU_ITEM_H;
    if (i >= rows) return -1;
    int idx = ui->menu_first + i;
    return idx < ui->menu->nopts ? idx : -1;
}

static void menu_hover(rbui *ui, int x, int y)
{
    int idx = menu_item_at(ui, x, y);
    if (idx >= 0 && ui->menu->opts[idx].disabled) idx = -1;
    if (idx != ui->menu_hi) {
        ui->menu_hi = idx;
        ui_damage(ui, r_out(ui->menu_r, 3));
    }
}

static void field_set_caret_from_x(rbui *ui, rb_node *n, int x, int extend)
{
    rb_rect c = ui_node_screen(ui, n, n->ctl);
    int off = field_x_to_offset(ui, n, x - (c.x + pt_field_text_dx()) + n->scroll_x);
    n->caret = off;
    if (!extend) n->anchor = off;
    field_fix_scroll(ui, n);
    ui_caret_reset(ui);
    ui_damage_node(ui, n);
}

static void press(rbui *ui, int x, int y)
{
    if (ui->swallow_until) {
        int near = x - ui->swallow_x <= 4 && ui->swallow_x - x <= 4 && y - ui->swallow_y <= 4 && ui->swallow_y - y <= 4;
        int soon = ui->now <= ui->swallow_until;
        ui->swallow_until = 0;
        if (near && soon) return;   /* the rest of a double-click on a click-to-act row */
    }
    if (ui->menu) {
        if (r_has(ui->menu_r, x, y)) {
            ui->track = TR_MENU;
            ui->menu_moved = 1;
            ui->repeat_at = ui->now + RB_REPEAT_FIRST_MS;
            if (ui->menu_arrows) {
                rb_rect m = ui->menu_r;
                int rows = ui->menu_rows - 2;
                int scrolled = 0;
                if (y < m.y + 3 + RB_MENU_ITEM_H) {
                    if (ui->menu_first > 0) { ui->menu_first--; scrolled = 1; }
                    ui->menu_sticky = 1;
                } else if (y >= m.y + 3 + (rows + 1) * RB_MENU_ITEM_H) {
                    if (ui->menu_first + rows < ui->menu->nopts) { ui->menu_first++; scrolled = 1; }
                    ui->menu_sticky = 1;
                }
                if (scrolled) {
                    ui->menu_hi = -1;
                    ui_damage(ui, r_out(ui->menu_r, 3));
                    return;
                }
            }
            menu_hover(ui, x, y);
        } else {
            menu_close(ui);
        }
        return;
    }
    if (ui->scrollable && r_has(ui->vbar, x, y)) {
        int part = bar_part(ui->vbar, ui->content_h, ui->view.h, ui->scroll, y);
        if (part == SB_NONE) return;
        ui->track = TR_VBAR;
        ui->track_part = part;
        ui->track_inside = 1;
        ui->track_grab = y - pt_thumb_pos(ui->vbar, ui->content_h, ui->view.h, ui->scroll);
        ui->tnode = NULL;
        bar_step(ui);
        ui->repeat_at = ui->now + RB_REPEAT_FIRST_MS;
        ui_damage(ui, ui->vbar);
        return;
    }
    rb_node *n = hit_test(ui, x, y);
    if (!n) return;
    ui->tnode = n;
    ui->track_inside = 1;
    switch (n->type) {
    case N_BUTTON:
        ui->track = TR_BUTTON;
        ui_damage_node(ui, n);
        break;
    case N_CHECKBOX:
        ui->track = TR_CHECK;
        ui_damage_node(ui, n);
        break;
    case N_RADIO:
    case N_BEVELS: {
        int cy = n->scrolled ? y + ui->scroll : y;
        ui->track_opt = -1;
        for (int i = 0; i < n->nopts; ++i)
            if (r_has(opt_rect(ui, n, i), x, cy) && !n->opts[i].disabled) ui->track_opt = i;
        if (ui->track_opt < 0) {
            ui->tnode = NULL;
            return;
        }
        ui->track = TR_RADIO;
        ui_damage_node(ui, n);
        break;
    }
    case N_POPUP:
        menu_open(ui, n, 0);
        ui->track = TR_MENU;
        break;
    case N_SLIDER: {
        /* On the thumb: drag it.  On the track: the thumb jumps under the pointer, then drags. */
        if (ui->focus != n) ui_set_focus_ex(ui, n, 0);
        slider_parts s = slider_geom(ui, n);
        ui->track = TR_SLIDER;
        ui->track_grab = x >= s.thumb_x && x < s.thumb_x + SLIDER_THUMB_W ? x - s.thumb_x : SLIDER_THUMB_W / 2;
        slider_set(ui, n, slider_value_at(ui, n, x - ui->track_grab));
        ui_damage_node(ui, n);
        break;
    }
    case N_FIELD:
        if (ui->focus != n) ui_set_focus_ex(ui, n, 0);
        ui->track = TR_FIELD;
        field_set_caret_from_x(ui, n, x, 0);
        break;
    case N_LIST: {
        if (ui->focus != n && node_focusable(n)) ui_set_focus_ex(ui, n, 0);
        if (n->icon_view) {
            /* Finder: a click selects, a click on the white deselects, a double-click opens. */
            rb_rect s = ui_node_screen(ui, n, n->ctl);
            int idx = icons_index_at(n, x - s.x + n->ctl.x, y - s.y + n->ctl.y);
            ui->track = TR_NONE;
            ui->tnode = NULL;
            if (idx < 0 || n->opts[idx].disabled) {
                if (n->sel >= 0) {
                    n->sel = -1;
                    ui_damage_node(ui, n);
                    ui_emit_change(ui, n);
                }
                return;
            }
            int dbl = ui->click_node == n && ui->click_row == idx && ui->now - ui->click_time <= RB_DOUBLE_MS;
            select_opt(ui, n, idx);
            ui->click_node = n;
            ui->click_row = idx;
            ui->click_time = ui->now;
            if (dbl) {
                ui->click_node = NULL;
                ui->swallow_until = ui->now + RB_DOUBLE_MS;
                ui->swallow_x = x;
                ui->swallow_y = y;
                ui_emit_action(ui, n);
            }
            return;
        }
        rb_rect bar = ui_node_screen(ui, n, list_bar_rect(n));
        if (list_has_bar(n) && r_has(bar, x, y)) {
            int vis = list_visible_rows(n);
            int part = bar_part(bar, n->nopts, vis, n->scroll, y);
            if (part == SB_NONE) {
                ui->tnode = NULL;
                return;
            }
            ui->track = TR_LISTBAR;
            ui->track_part = part;
            ui->track_grab = y - pt_thumb_pos(bar, n->nopts, vis, n->scroll);
            bar_step(ui);
            ui->repeat_at = ui->now + RB_REPEAT_FIRST_MS;
            ui_damage_node(ui, n);
            return;
        }
        ui->track = TR_LIST;
        int row = list_row_at(ui, n, y, 0);
        if (row >= 0 && !n->opts[row].disabled && n->click_activate) {
            select_opt(ui, n, row);   /* acts on release over the row, like a menu item */
        } else if (row >= 0 && !n->opts[row].disabled) {
            int dbl = ui->click_node == n && ui->click_row == row && ui->now - ui->click_time <= RB_DOUBLE_MS;
            select_opt(ui, n, row);
            ui->click_node = n;
            ui->click_row = row;
            ui->click_time = ui->now;
            if (dbl) {
                ui->click_node = NULL;
                ui->track = TR_NONE;
                double_click_default(ui);
            }
        }
        ui->repeat_at = ui->now + RB_REPEAT_MS;
        break;
    }
    default:
        ui->tnode = NULL;
        break;
    }
}

static void drag(rbui *ui, int x, int y)
{
    rb_node *n = ui->tnode;
    switch (ui->track) {
    case TR_BUTTON:
    case TR_CHECK:
    case TR_RADIO: {
        rb_rect r = ui->track == TR_RADIO ? opt_rect(ui, n, ui->track_opt) : n->ctl;
        int in = r_has(ui_node_screen(ui, n, r), x, y);
        if (in != ui->track_inside) {
            ui->track_inside = in;
            ui_damage_node(ui, n);
        }
        break;
    }
    case TR_FIELD:
        field_set_caret_from_x(ui, n, x, 1);
        break;
    case TR_SLIDER:
        slider_set(ui, n, slider_value_at(ui, n, x - ui->track_grab));
        break;
    case TR_LIST: {
        rb_rect rows = ui_node_screen(ui, n, list_rows_rect(n));
        if (y >= rows.y && y < rows.y + rows.h) {
            int row = list_row_at(ui, n, y, 1);
            if (row >= 0 && !n->opts[row].disabled) select_opt(ui, n, row);
        }
        break;
    }
    case TR_LISTBAR: {
        int vis = list_visible_rows(n);
        rb_rect bar = ui_node_screen(ui, n, list_bar_rect(n));
        if (ui->track_part == SB_THUMB) {
            int first = thumb_to_first(bar, n->nopts, vis, y - ui->track_grab);
            if (first != n->scroll) {
                n->scroll = first;
                ui_damage_node(ui, n);
            } else {
                ui_damage(ui, bar);
            }
        } else {
            int part = bar_part(bar, n->nopts, vis, n->scroll, y);
            int in = r_has(bar, x, y) && (part == ui->track_part ||
                     ((ui->track_part == SB_PAGEUP || ui->track_part == SB_PAGEDOWN) && part != SB_UP && part != SB_DOWN));
            if (in != ui->track_inside) {
                ui->track_inside = in;
                ui_damage_node(ui, n);
            }
        }
        break;
    }
    case TR_VBAR:
        if (ui->track_part == SB_THUMB) {
            int track_y, track_h;
            pt_bar_track(ui->vbar, &track_y, &track_h);
            int span = track_h - PT_THUMB_H, max = ui->content_h - ui->view.h;
            if (span > 0 && max > 0) {
                long long top = y - ui->track_grab - track_y;
                view_scroll_to(ui, (int)((top * max + span / 2) / span));
            }
            ui_damage(ui, ui->vbar);
        } else {
            int part = bar_part(ui->vbar, ui->content_h, ui->view.h, ui->scroll, y);
            int in = r_has(ui->vbar, x, y) && (part == ui->track_part ||
                     ((ui->track_part == SB_PAGEUP || ui->track_part == SB_PAGEDOWN) && part != SB_UP && part != SB_DOWN));
            if (in != ui->track_inside) {
                ui->track_inside = in;
                ui_damage(ui, ui->vbar);
            }
        }
        break;
    case TR_MENU:
        if (ui->menu) {
            int before = ui->menu_hi;
            menu_hover(ui, x, y);
            if (ui->menu_hi != before) ui->menu_moved = 1;
        }
        break;
    default:
        break;
    }
}

static void release(rbui *ui, int x, int y)
{
    rb_node *n = ui->tnode;
    int track = ui->track, inside = ui->track_inside;
    ui->track = TR_NONE;
    switch (track) {
    case TR_BUTTON:
        ui_damage_node(ui, n);
        if (inside) activate_button(ui, n, 0);
        break;
    case TR_CHECK:
        ui_damage_node(ui, n);
        if (inside) toggle_check(ui, n);
        break;
    case TR_RADIO:
        ui_damage_node(ui, n);
        if (inside) select_opt(ui, n, ui->track_opt);
        break;
    case TR_MENU:
        if (ui->menu) {
            int idx = menu_item_at(ui, x, y);
            rb_rect b = ui_node_screen(ui, ui->menu, ui->menu->ctl);
            if (idx >= 0 && (ui->menu_sticky || ui->menu_moved)) {
                menu_choose(ui, idx);
            } else if (!ui->menu_sticky && (idx >= 0 || r_has(b, x, y))) {
                ui->menu_sticky = 1;   /* click-and-release: the menu stays open */
            } else if (ui->menu_sticky && r_has(ui->menu_r, x, y)) {
                /* released on a scroll arrow or a disabled item: stay open */
            } else {
                menu_close(ui);
            }
        }
        break;
    case TR_LIST:
        if (n && n->click_activate && n->sel >= 0 && list_row_at(ui, n, y, 0) == n->sel &&
            r_has(ui_node_screen(ui, n, list_rows_rect(n)), x, y) && node_focusable(n)) {
            ui->swallow_until = ui->now + RB_DOUBLE_MS;
            ui->swallow_x = x;
            ui->swallow_y = y;
            ui_emit_action(ui, n);
        }
        break;
    case TR_LISTBAR:
    case TR_SLIDER:
        if (n) ui_damage_node(ui, n);
        break;
    case TR_VBAR:
        ui_damage(ui, ui->vbar);
        break;
    default:
        break;
    }
    ui->tnode = NULL;
}

void input_pointer(rbui *ui, int x, int y, unsigned buttons)
{
    int was = ui->buttons & 1, is = buttons & 1;
    if (is && !was) press(ui, x, y);
    else if (is && was) drag(ui, x, y);
    else if (!is && was) release(ui, x, y);
    else if (ui->menu && ui->menu_sticky) menu_hover(ui, x, y);
}

void input_tick(rbui *ui)
{
    if (ui->track == TR_NONE || ui->now < ui->repeat_at) return;
    ui->repeat_at = ui->now + RB_REPEAT_MS;
    if (ui->track == TR_LISTBAR || ui->track == TR_VBAR) {
        if (ui->track_part != SB_THUMB) bar_step(ui);
    } else if (ui->track == TR_LIST) {
        /* Autoscroll while dragging above or below the rows. */
        rb_node *n = ui->tnode;
        rb_rect rows = ui_node_screen(ui, n, list_rows_rect(n));
        int to = -1;
        if (ui->py < rows.y) to = step_opt(n, n->sel < 0 ? n->scroll : n->sel, -1);
        else if (ui->py >= rows.y + rows.h) to = step_opt(n, n->sel < 0 ? n->scroll : n->sel, 1);
        if (to >= 0) select_opt(ui, n, to);
    } else if (ui->track == TR_MENU && ui->menu && ui->menu_arrows) {
        rb_rect m = ui->menu_r;
        if (r_has(m, ui->px, ui->py)) {
            int rows = ui->menu_rows - 2;
            if (ui->py < m.y + 3 + RB_MENU_ITEM_H && ui->menu_first > 0) ui->menu_first--;
            else if (ui->py >= m.y + 3 + (rows + 1) * RB_MENU_ITEM_H && ui->menu_first + rows < ui->menu->nopts) ui->menu_first++;
            else return;
            ui_damage(ui, r_out(ui->menu_r, 3));
        }
    }
}

void input_wheel(rbui *ui, int dy)
{
    if (dy == 0) return;
    if (ui->menu) {
        if (!ui->menu_arrows) return;
        int rows = ui->menu_rows - 2, max = ui->menu->nopts - rows;
        int f = ui->menu_first + dy;
        if (f > max) f = max;
        if (f < 0) f = 0;
        if (f != ui->menu_first) {
            ui->menu_first = f;
            ui_damage(ui, r_out(ui->menu_r, 3));
        }
        return;
    }
    rb_node *n = hit_test(ui, ui->px, ui->py);
    if (n && n->type == N_LIST) {
        list_scroll_by(ui, n, dy);
        return;
    }
    if (ui->scrollable && r_has(ui->view, ui->px, ui->py)) view_scroll_to(ui, ui->scroll + dy * 16);
}
