"""The helper's own pages, drawn like a Mac OS alert in HTML 3.2.

Icons are original pixel art made here at start-up (no Apple artwork), served
from ``/__rbwh__/`` on whatever host the page belongs to, so they arrive
through the helper in both transparent and explicit proxy use.
"""
from __future__ import annotations

import html
import io
from functools import lru_cache

from . import charset
from .urls import CONTROL_PREFIX

FONT = "Geneva,Helvetica,Arial"
TITLE_FONT = "Charcoal,Chicago,Geneva,Helvetica"

MESSAGES = {
    "certificate": ("The secure site “{host}” could not be verified.",
                    "Its security certificate is not valid ({detail}), so the Modern Web Helper did not "
                    "open it. Someone could be pretending to be this site."),
    "unreachable": ("The server “{host}” could not be found.",
                    "Check the address for typing mistakes. {detail}"),
    "refused-connection": ("The server “{host}” did not accept the connection.",
                           "The site may be down for now. Try again later. {detail}"),
    "timeout": ("The server “{host}” is not responding.",
                "It took too long to answer. Try again in a moment."),
    "too-large": ("This page is too large for Mac OS.",
                  "“{host}” sent {detail}, more than the Modern Web Helper translates for "
                  "Mac OS’s browser."),
    "refused-host": ("The Modern Web Helper does not open this address.",
                     "Mac OS can’t reach {detail} through the Modern Web Helper."),
    "refused-lan": ("Pages on your local network are turned off.",
                    "To open “{host}”, allow local network pages in RetroBoot’s "
                    "Network settings."),
    "refused-port": ("The Modern Web Helper does not open this address.",
                     "Web pages can’t use {detail}, which belongs to another kind of service."),
    "bad-request": ("Mac OS’s browser sent a request the Modern Web Helper doesn’t understand.",
                    "{detail}"),
    "unsupported": ("The Modern Web Helper opens only web pages.", "{detail}"),
    "secure": ("Open “{host}” without “https”.",
               "Mac OS’s browser can no longer make secure connections to today’s sites. "
               "The Modern Web Helper does that for it: open the address with “http://” and the "
               "page arrives securely."),
    "not-guest": ("The Modern Web Helper serves Mac OS only.", "{detail}"),
    "upstream": ("“{host}” sent a reply the Modern Web Helper could not read.", "{detail}"),
}


def _button(label: str, href: str, default: bool = False) -> str:
    border = "2" if default else "1"
    return (f'<td><table border="{border}" cellspacing="0" cellpadding="3" bgcolor="#EEEEEE"><tr><td nowrap>'
            f'<font face="{FONT}" size="2">&nbsp;<a href="{html.escape(href, quote=True)}">{html.escape(label)}'
            f'</a>&nbsp;</font></td></tr></table></td><td width="8"></td>')


def alert_html(title: str, detail: str, buttons: list[tuple[str, str]], icon: str = "caution",
               cs: str = "macroman") -> bytes:
    _, label = charset.codec_for(cs)
    btns = "".join(_button(lbl, href, i == len(buttons) - 1) for i, (lbl, href) in enumerate(buttons))
    page = f"""<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">
<html><head><meta http-equiv="Content-Type" content="text/html; charset={label}">
<title>{html.escape(title)}</title></head>
<body bgcolor="#CCCCCC" text="#000000" link="#000066" vlink="#000066" alink="#000066">
<br><br><center>
<table border="0" cellspacing="0" cellpadding="1" width="480"><tr><td bgcolor="#000000">
<table border="0" cellspacing="0" cellpadding="14" width="100%"><tr><td bgcolor="#DDDDDD">
<table border="0" cellspacing="0" cellpadding="0" width="100%"><tr>
<td valign="top" width="52"><img src="{CONTROL_PREFIX}{icon}.gif" width="32" height="32" alt="!"></td>
<td valign="top"><font face="{TITLE_FONT}" size="3"><b>{html.escape(title)}</b></font><br><br>
<font face="{FONT}" size="2">{html.escape(detail)}</font><br><br>
<table border="0" cellspacing="0" cellpadding="0" align="right"><tr>{btns}</tr></table></td>
</tr></table></td></tr></table></td></tr></table>
<br><font face="{FONT}" size="1">Modern Web Helper &middot; RetroBoot</font></center>
</body></html>
"""
    return charset.encode_text(page, cs)


def error_page(kind: str, host: str = "", detail: str = "", retry: str | None = None,
               back: bool = True, cs: str = "macroman", extra: list[tuple[str, str]] | None = None) -> bytes:
    title, text = MESSAGES.get(kind, MESSAGES["upstream"])
    title = title.format(host=host, detail=detail)
    text = text.format(host=host, detail=detail).strip()
    buttons = []
    if back:
        buttons.append(("Go Back", "javascript:history.back()"))
    buttons += list(extra or [])
    if retry:
        buttons.append(("Try Again", retry))
    icon = "stop" if kind in ("certificate", "refused-host", "refused-port", "not-guest") else "caution"
    return alert_html(title, text, buttons, icon, cs)


def secure_page(host: str, http_url: str, cs: str = "macroman") -> bytes:
    title, text = MESSAGES["secure"]
    return alert_html(title.format(host=host), text, [(f"Open http://{host}/", http_url)], "note", cs)


# -- icons (original art) ---------------------------------------------------------------

@lru_cache(maxsize=8)
def icon_gif(name: str) -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if name == "stop":
        pts = [(10, 1), (21, 1), (30, 10), (30, 21), (21, 30), (10, 30), (1, 21), (1, 10)]
        d.polygon(pts, fill=(204, 0, 0, 255), outline=(0, 0, 0, 255))
        d.rectangle([7, 13, 24, 18], fill=(255, 255, 255, 255), outline=(0, 0, 0, 255))
    elif name == "note":
        d.rounded_rectangle([1, 2, 30, 24], radius=6, fill=(255, 255, 255, 255), outline=(0, 0, 0, 255))
        d.polygon([(8, 23), (8, 30), (15, 23)], fill=(255, 255, 255, 255), outline=(0, 0, 0, 255))
        d.line([(9, 23), (14, 23)], fill=(255, 255, 255, 255))
        d.rectangle([14, 7, 17, 9], fill=(0, 0, 0, 255))
        d.rectangle([14, 11, 17, 19], fill=(0, 0, 0, 255))
    else:  # caution
        d.polygon([(16, 1), (31, 29), (1, 29)], fill=(255, 204, 0, 255), outline=(0, 0, 0, 255))
        d.rectangle([14, 9, 17, 20], fill=(0, 0, 0, 255))
        d.rectangle([14, 23, 17, 26], fill=(0, 0, 0, 255))
    pal = img.convert("RGB").quantize(colors=8, dither=Image.Dither.NONE)
    alpha = img.split()[3]
    # Index of a palette slot for transparency: paint transparent pixels with a spare colour.
    pal_img = Image.new("P", img.size)
    pal_img.putpalette(pal.getpalette()[:24] + [255, 0, 255] + [0] * (768 - 27))
    pal_img.paste(pal, (0, 0))
    mask = alpha.point(lambda a: 255 if a < 128 else 0)
    pal_img.paste(8, (0, 0), mask)
    buf = io.BytesIO()
    pal_img.save(buf, "GIF", transparency=8)
    return buf.getvalue()
