"""Builders for UI documents (ARCHITECTURE 4.2).  Pure data; the presenter renders."""
from __future__ import annotations

import re
from typing import Any


def window(id: str, title: str, items: list, width: int = 440, reset: bool = False, focus: str | None = None,
           height: int | None = None) -> dict:
    """height: the least content height, for a window whose pages keep one size."""
    d = {"id": id, "kind": "window", "title": title, "width": width, "items": [i for i in items if i]}
    if height:
        d["height"] = height
    if reset:
        d["reset"] = True
    if focus:
        d["focus"] = focus
    return d


def alert(id: str, icon: str, message: str, detail: str | None, buttons_: dict, extra: list | None = None,
          width: int = 400, reset: bool = False) -> dict:
    items: list = [text(message, "emphasis")]
    if detail:
        items.append(text(detail))
    items += [i for i in (extra or []) if i]
    items.append(buttons_)
    d = {"id": id, "kind": "alert", "alert_icon": icon, "width": width, "items": items}
    if reset:
        d["reset"] = True
    return d


def text(t: str, style: str | None = None, width: int | None = None, align: str | None = None) -> dict:
    d: dict[str, Any] = {"type": "text", "text": t}
    if style:
        d["style"] = style
    if width:
        d["width"] = width
    if align:
        d["align"] = align
    return d


def sep() -> dict:
    return {"type": "separator"}


def btn(id: str, label: str, default: bool = False, disabled: bool = False, align: str | None = None) -> dict:
    d: dict[str, Any] = {"id": id, "label": label}
    if default:
        d["default"] = True
    if disabled:
        d["disabled"] = True
    if align:
        d["align"] = align
    return d


def buttons(*items: dict) -> dict:
    return {"type": "buttons", "items": [i for i in items if i]}


def group(label: str, items: list) -> dict:
    return {"type": "group", "label": label, "items": [i for i in items if i]}


def lst(id: str, items: list[dict], value: str | None, rows: int = 5, disabled: bool = False,
        activate: str | None = None, scrollbar: bool = True) -> dict:
    """activate="click": one click on a row sends a ui_action with the list's id (rbui README).
    scrollbar=False: a list that always shows every row leaves out the (inactive) scroll bar."""
    d: dict[str, Any] = {"type": "list", "id": id, "rows": rows, "items": items, "value": value}
    if disabled:
        d["disabled"] = True
    if activate:
        d["activate"] = activate
    if not scrollbar:
        d["scrollbar"] = False
    return d


def row(id: str, label: str, detail: str | None = None, icon: str | None = None, checked: bool | None = None,
        signal: int | None = None, lock: bool = False, disabled: bool = False) -> dict:
    """checked: True/False for a checkable list (a check-mark column, as in a menu); None for a plain one.
    disabled: a dimmed row that can't be selected (an unavailable item, with the reason as its detail)."""
    d: dict[str, Any] = {"id": id, "label": label}
    if detail:
        d["detail"] = detail
    if icon:
        d["icon"] = icon
    if checked is not None:
        d["checked"] = bool(checked)
    if signal is not None:
        d["signal"] = max(0, min(4, int(signal)))
    if lock:
        d["lock"] = True
    if disabled:
        d["disabled"] = True
    return d


def icon_view(id: str, items: list[dict], value: str | None, columns: int = 4, header: str | None = None) -> dict:
    """A Finder icon view (rbui list "view": "icons"): 32-pixel icons with their names on a
    128 x 64 grid under an optional header strip ("8 items, …"); a click selects (ui_change),
    a double-click or Command-O sends a ui_action with the view's id."""
    d: dict[str, Any] = {"type": "list", "id": id, "view": "icons", "columns": columns, "items": items,
                         "value": value}
    if header:
        d["header"] = header
    return d


def field(id: str, label: str, value: str = "", secure: bool = False, placeholder: str = "",
          max_length: int | None = None, width: int | None = None) -> dict:
    d: dict[str, Any] = {"type": "field", "id": id, "label": label, "secure": secure, "value": value,
                         "placeholder": placeholder}
    if max_length:
        d["max_length"] = max_length
    if width:
        d["width"] = width
    return d


def checkbox(id: str, label: str, value: bool, disabled: bool = False, reveals: str | None = None) -> dict:
    """reveals=FIELD_ID: a "Show password" box; the panel shows that secure field's text by itself."""
    d = {"type": "checkbox", "id": id, "label": label, "value": bool(value)}
    if disabled:
        d["disabled"] = True
    if reveals:
        d["reveals"] = reveals
    return d


def icon(name: str, size: int = 32) -> dict:
    return {"type": "icon", "icon": name, "size": size, "width": size}


def stack(*items: dict) -> dict:
    return {"type": "stack", "items": [i for i in items if i]}


def radio(id: str, label: str, options: list[tuple], value: str) -> dict:
    """options: (id, label) or (id, label, disabled) tuples."""
    opts = []
    for o in options:
        opt = {"id": o[0], "label": o[1]}
        if len(o) > 2 and o[2]:
            opt["disabled"] = True
        opts.append(opt)
    return {"type": "radio", "id": id, "label": label, "options": opts, "value": value}


def popup(id: str, label: str, options: list[tuple], value: Any, disabled: bool = False) -> dict:
    """options: (id, label) or (id, label, disabled) tuples."""
    opts = []
    for o in options:
        opt = {"id": str(o[0]), "label": o[1]}
        if len(o) > 2 and o[2]:
            opt["disabled"] = True
        opts.append(opt)
    d = {"type": "popup", "id": id, "label": label, "options": opts, "value": str(value)}
    if disabled:
        d["disabled"] = True
    return d


def facts(*rows: tuple[str, str | None]) -> dict | None:
    """About This Computer's upper part: ("Built-in Memory:", "8 GB") rows, the label in bold
    (rows without a value are left out; None when none has one)."""
    items = [{"label": k, "value": v} for k, v in rows if v]
    return {"type": "facts", "items": items} if items else None


def meter(label: str, value: str | None, share: float | None, icon: str | None = None) -> dict:
    d: dict[str, Any] = {"label": label, "share": None if share is None else round(max(0.0, min(1.0, share)), 3)}
    if value:
        d["value"] = value
    if icon:
        d["icon"] = icon
    return d


def meters(*rows: dict) -> dict:
    """About This Computer's list: each row a 16 px icon, a name, a figure and a bar."""
    return {"type": "meters", "items": [r for r in rows if r]}


def band(items: list, height: int, rule: str | None = None, pin: bool = False) -> dict:
    """A strip across the whole window, as Monitors' top and Sound's Main Volume: its items
    placed with ``at``; rule "top", "bottom" or "both" draws Appearance separators; pin keeps
    it at the window's bottom, above the buttons."""
    d: dict[str, Any] = {"type": "band", "height": height, "items": [i for i in items if i]}
    if rule:
        d["rule"] = rule
    if pin:
        d["pin"] = True
    return d


def at(item: dict | None, x: int, y: int) -> dict | None:
    """A band's item at (x, y) from the band's top left."""
    return dict(item, x=x, y=y) if item else None


def bevels(id: str, items: list[tuple], value: str | None) -> dict:
    """Monitors' bevel buttons: (id, label, icon) or (id, label, icon, disabled); the chosen
    one stays pressed, like a radio button."""
    opts = []
    for o in items:
        opt = {"id": o[0], "label": o[1], "icon": o[2]}
        if len(o) > 3 and o[3]:
            opt["disabled"] = True
        opts.append(opt)
    return {"type": "bevels", "id": id, "items": opts, "value": value}


def slider(id: str, label: str, value: float, lo: float = 0, hi: float = 100, step: float = 1,
           icons: tuple[str, str] | None = None, disabled: bool = False) -> dict:
    """Sound's Main Volume: a label, the end icons and the track; each move is a ui_change."""
    d: dict[str, Any] = {"type": "slider", "id": id, "label": label, "value": value, "min": lo, "max": hi,
                         "step": step}
    if icons:
        d["icons"] = list(icons)
    if disabled:
        d["disabled"] = True
    return d


def progress(value: float | None) -> dict:
    if value is None:
        return {"type": "progress", "indeterminate": True}
    return {"type": "progress", "value": round(max(0.0, min(1.0, value)), 3)}


def qr(matrix: list[str] | None) -> dict | None:
    return {"type": "qr", "matrix": matrix} if matrix else None


def columns(*items: dict) -> dict:
    return {"type": "columns", "items": [i for i in items if i]}


def clip_words(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` characters at a word boundary, with an ellipsis."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:.–-") + "…"


def format_fingerprint(fp: str | None, group_bytes: int = 8) -> list[str]:
    """Split a colon-separated SHA-256 fingerprint into symmetrical lines (default 4 x 8 bytes)."""
    tokens = [part.strip() for part in str(fp or "").split(":") if part.strip()]
    if not tokens:
        return []
    return [":".join(tokens[i:i + group_bytes]) for i in range(0, len(tokens), group_bytes)]


def format_download_status(fraction: float | None, message: str = "") -> str:
    """Combine a download percentage and byte/speed summary into clean status line(s)."""
    detail = " ".join(str(message or "").split())
    pct = f"{int(round(max(0.0, min(1.0, fraction)) * 100))}%" if isinstance(fraction, (int, float)) else ""
    named = re.match(r"^(Downloading\s+.+?(?:…|\.\.\.))\s+(.+)$", detail, re.IGNORECASE)
    if named:
        head, tail = named.group(1).strip(), named.group(2).strip()
        if re.search(r"(?:^|\s|·)\d+%(?:\s|·|$)", tail):
            return f"{head}\n{tail}"
        return f"{head}\n{pct} · {tail}" if pct else f"{head}\n{tail}"
    for prefix in ("Downloading…", "Downloading...", "downloading…", "downloading..."):
        if detail.lower().startswith(prefix.lower()):
            detail = detail[len(prefix):].strip()
            break
    if re.search(r"(?:^|\s|·)\d+%(?:\s|·|$)", detail):
        return detail
    if pct and detail:
        return f"{pct} · {detail}"
    return pct or detail or "Downloading…"


def format_job_status(fraction: float | None, message: str = "") -> str:
    """Format a progress job status message without duplicate percentages or awkward wrapping."""
    msg = " ".join(str(message or "").split())
    if not msg:
        return "Working…"
    if msg.lower().startswith("downloading"):
        return format_download_status(fraction, msg)
    named = re.match(r"^((?:Copying|Installing|Unpacking)\s+.+?(?:…|\.\.\.))\s+(.+)$", msg, re.IGNORECASE)
    if named:
        return f"{named.group(1).strip()}\n{named.group(2).strip()}"
    return msg


def human_bytes(n: int | None) -> str:
    if n is None:
        return "unknown size"
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            v = n / div
            return f"{v:.1f} {unit}" if v < 10 else f"{v:.0f} {unit}"
    return f"{n} bytes"
