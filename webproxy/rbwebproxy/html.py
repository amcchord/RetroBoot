"""Today's HTML, rewritten for Internet Explorer 5 and Netscape 4.

Two views share one cleaner:

* **Full page** keeps the page's own structure and removes what breaks the
  old browsers: scripts, style sheets (Netscape 4 crashes on modern CSS and
  IE 5 lays it out unreadably), elements hidden by CSS or attributes, inline
  SVG, embeds and media players, modern attributes.  HTML5 sectioning
  elements become ``div``, modern input types become text fields, ``button``
  becomes a submit button, lazy-loaded and responsive images get a plain
  ``src`` with a size the old browser can lay out.
* **Simplified** keeps only the main content (readability's algorithm, as
  FrogFind does), with a one-line bar to switch to the full page.

``auto`` uses the simplified view for article-like pages (not a site's front
page, enough paragraphs, readability finds a substantial text) and the full
page for everything else.  Pages from sites already written for vintage
browsers pass with only the charset and https changes.

Every link, image, form and refresh address goes through ``urls.rewrite``.
"""
from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from lxml import etree
from lxml import html as lhtml

from . import charset
from .urls import CONTROL_PREFIX, TLSMemory, rewrite

DROP = {"script", "style", "template", "object", "embed", "applet", "canvas", "track", "slot",
        "noembed", "portal", "math", "param", "source", "link", "meta", "base", "svg", "dialog",
        "audio", "video", "iframe", "picture", "button", "noscript"}
TO_DIV = {"header", "footer", "nav", "main", "article", "section", "aside", "figure", "figcaption",
          "hgroup", "details", "search", "menu", "address", "fieldset", "legend"}
# HTML5-only inline elements become span; del/ins become what Netscape 4 knows.
TO_SPAN = {"mark", "time", "data", "output", "bdi", "meter", "progress", "ruby", "rt", "rp",
           "del", "ins", "summary"}
OLD_INLINE = {"del": "strike", "ins": "u", "summary": "b"}

ATTRS = {
    "href", "src", "alt", "title", "name", "id", "value", "type", "action", "method", "enctype",
    "target", "width", "height", "colspan", "rowspan", "border", "cellpadding", "cellspacing",
    "align", "valign", "bgcolor", "color", "size", "face", "checked", "selected", "multiple",
    "disabled", "readonly", "maxlength", "rows", "cols", "for", "label", "lang", "dir", "start",
    "usemap", "shape", "coords", "nowrap", "background", "summary", "headers", "scope", "span",
    "compact", "clear", "hspace", "vspace", "ismap", "frameborder", "scrolling", "noresize",
    "marginwidth", "marginheight", "text", "link", "vlink", "alink", "http-equiv", "content",
}
TEXT_INPUTS = {"email", "search", "tel", "url", "number", "date", "datetime-local", "month", "week",
               "time", "color", "range", "datetime"}
HIDDEN_CLASSES = {"hidden", "d-none", "sr-only", "visually-hidden", "screen-reader-text", "is-hidden",
                  "u-hidden", "hide", "invisible", "visuallyhidden", "a11y-hidden", "offscreen",
                  "noprint", "mw-jump-link", "skip-link", "skiplink", "skip-to-content"}
RESPONSIVE_SHOW = re.compile(r"^(sm|md|lg|xl|2xl):(block|flex|inline|inline-block|grid|table|contents)$"
                             r"|^d-(sm|md|lg|xl|xxl)-(block|flex|inline|inline-block|grid|table)$")
HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
LAZY_SRC = ("data-src", "data-lazy-src", "data-original", "data-lazy", "data-url", "data-hi-res-src",
            "data-srcset", "data-lazy-srcset")
PLACEHOLDER = re.compile(r"(^data:|blank\.gif|spacer\.gif|placeholder|pixel\.gif|1x1|transparent\.(gif|png))", re.I)
ARTICLE_TYPES = re.compile(r'"@type"\s*:\s*"(NewsArticle|Article|BlogPosting|ReportageNewsArticle|TechArticle)"')
ANCHOR_HOSTS = {"li", "p", "div", "span", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "dt", "dd",
                "blockquote", "sup", "cite", "b", "i", "strong", "em", "pre"}
VINTAGE_DOCTYPE = re.compile(r"<!doctype\s+html\s+public\s+\"-//(w3c|ietf)//dtd\s+html\s+(2|3|4\.0?1?)", re.I)


@dataclass
class Result:
    body: bytes
    view: str                  # full | simplified | vintage
    title: str = ""
    article: bool = False
    notes: list = field(default_factory=list)


@dataclass
class Context:
    url: str                   # the page's real (upstream) address
    client_url: str            # the address Mac OS asked for
    tls: TLSMemory
    charset: str = "macroman"
    max_width: int = 640
    view: str = "auto"         # auto | simplified | full
    vintage_hosts: tuple = ()
    control_links: bool = True


# -- helpers ---------------------------------------------------------------------------

def _int(v) -> int | None:
    try:
        return int(str(v).strip().rstrip("px").split(".")[0])
    except (TypeError, ValueError):
        return None


def _classes(el) -> set[str]:
    return set((el.get("class") or "").split())


def _is_hidden(el) -> bool:
    if el.get("hidden") is not None or el.get("aria-hidden") == "true":
        return True
    if el.tag == "input" and (el.get("type") or "").lower() == "hidden":
        return False
    style = el.get("style")
    if style and HIDDEN_STYLE.search(style):
        return True
    cls = _classes(el)
    if cls & HIDDEN_CLASSES:
        return not any(RESPONSIVE_SHOW.match(c) for c in cls)
    return False


def _best_from_srcset(srcset: str, max_width: int) -> str | None:
    """Width descriptors: the narrowest candidate at least max_width wide (else the
    widest).  Density descriptors: 1x (old screens are 1x)."""
    widths, dens = [], []
    for part in re.split(r",\s+(?=\S)", srcset.strip()):
        bits = part.strip().split()
        if not bits:
            continue
        url, desc = bits[0].rstrip(","), (bits[1].lower() if len(bits) > 1 else "1x")
        if desc.endswith("w") and _int(desc[:-1]):
            widths.append((_int(desc[:-1]), url))
        else:
            try:
                dens.append((float(desc[:-1]) if desc.endswith("x") else 1.0, url))
            except ValueError:
                dens.append((1.0, url))
    if widths:
        big = sorted(c for c in widths if c[0] >= max_width)
        return big[0][1] if big else max(widths)[1]
    if dens:
        ones = sorted(c for c in dens if c[0] >= 1.0)
        return (ones[0] if ones else max(dens))[1]
    return None


def _image_src(el, max_width: int) -> str | None:
    src = el.get("src")
    cand = src if src and not PLACEHOLDER.search(src[:200]) else None
    for a in LAZY_SRC:
        v = el.get(a)
        if v and not PLACEHOLDER.search(v[:200]):
            cand = _best_from_srcset(v, max_width) if "srcset" in a else v
            break
    srcset = el.get("srcset")
    if srcset:
        cand = _best_from_srcset(srcset, max_width) or cand
    return cand


def _text_of(el) -> str:
    return re.sub(r"\s+", " ", el.text_content() or "").strip()


def _replace_with_text(el, text: str | None) -> None:
    parent = el.getparent()
    if parent is None:
        return
    if text:
        span = etree.Element("span")
        span.text = text
        span.tail = el.tail
        parent.replace(el, span)
    else:
        el.drop_tree()


def _replace_with_link(el, href: str | None, label: str) -> None:
    parent = el.getparent()
    if parent is None:
        return
    if not href:
        el.drop_tree()
        return
    a = etree.Element("a", href=href)
    a.text = label
    a.tail = el.tail
    parent.replace(el, a)


# -- the cleaner -----------------------------------------------------------------------

class Cleaner:
    def __init__(self, ctx: Context, base: str):
        self.ctx = ctx
        self.base = base
        self.notes: list[str] = []

    def url(self, raw: str | None) -> str | None:
        return rewrite(raw, self.base, self.ctx.tls)

    def clean(self, root) -> None:
        for bad in root.xpath("//comment()|//processing-instruction()"):
            parent = bad.getparent()
            if parent is not None:
                tail = bad.tail
                prev = bad.getprevious()
                parent.remove(bad)
                if tail:
                    if prev is not None:
                        prev.tail = (prev.tail or "") + tail
                    else:
                        parent.text = (parent.text or "") + tail
        # <base href> changes how every relative address resolves.
        for b in root.iter("base"):
            href = b.get("href")
            if href:
                from urllib.parse import urljoin
                self.base = urljoin(self.base, href.strip())
                break
        for el in list(root.iter()):
            if not isinstance(el.tag, str):
                continue
            if el.getparent() is None and el is not root:
                continue                       # already removed with an ancestor
            if self._detached(el, root):
                continue
            self.element(el)
        for a in list(root.iter("a")):
            self._fix_empty_link(a)
        self._prune_ids(root)

    @staticmethod
    def _prune_ids(root) -> None:
        """Keep only ids that a same-page link points at (they are most of a page's attributes)."""
        targets = set()
        for a in root.iter("a"):
            href = a.get("href") or ""
            if "#" in href:
                targets.add(href.rsplit("#", 1)[1])
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            i = el.get("id")
            if i is None:
                continue
            if i not in targets and el.tag not in ("map", "form"):
                del el.attrib["id"]
            elif el.tag == "a":
                if el.get("name") is None:
                    el.set("name", i)    # Netscape 4 jumps to <a name>, not to id
            elif i in targets and el.tag in ANCHOR_HOSTS:
                anchor = etree.Element("a", name=i)
                anchor.tail = el.text
                el.text = None
                el.insert(0, anchor)

    @staticmethod
    def _detached(el, root) -> bool:
        p = el
        while p is not None:
            if p is root:
                return False
            p = p.getparent()
        return True

    def element(self, el) -> None:
        tag = el.tag.lower() if isinstance(el.tag, str) else ""
        if tag in ("html", "head", "body", "title"):
            self.attrs(el)
            return
        if _is_hidden(el) and tag not in ("html", "body"):
            el.drop_tree()
            return
        if tag in DROP:
            self.drop(el, tag)
            return
        if tag == "img":
            self.image(el)
            return
        if tag == "a" or tag == "area":
            href = self.url(el.get("href"))
            if href is None:
                el.attrib.pop("href", None)
            else:
                el.set("href", href)
        elif tag in ("frame",):
            el.set("src", self.url(el.get("src")) or "about:blank")
        elif tag == "form":
            action = el.get("action")
            el.set("action", self.url(action) if action and action.strip() else
                   rewrite(self.ctx.url, self.ctx.url, self.ctx.tls) or "")
        elif tag in ("input", "select", "textarea") and not any(p.tag == "form" for p in el.iterancestors()):
            # Outside a form a control does nothing without scripts (menu toggles, filters).
            el.drop_tree()
            return
        elif tag == "input":
            self.input(el)
        if el.get("background"):
            bg = self.url(el.get("background"))
            if bg:
                el.set("background", bg)
            else:
                el.attrib.pop("background")
        if tag in TO_DIV:
            el.tag = "div"
        elif tag in TO_SPAN:
            el.tag = OLD_INLINE.get(tag) or "span"
        self.attrs(el)

    def attrs(self, el) -> None:
        for k in list(el.attrib):
            kl = k.lower()
            if kl not in ATTRS or kl.startswith("on"):
                del el.attrib[k]
            elif kl in ("href", "src", "action") and el.attrib[k].lower().startswith("javascript:"):
                del el.attrib[k]

    def drop(self, el, tag: str) -> None:
        if tag == "noscript":
            # Scripts are gone, so the no-script fallback is what the page wants shown,
            # except in <head> (styles, tracking pixels).
            if any(p.tag == "head" for p in el.iterancestors()):
                el.drop_tree()
            else:
                el.drop_tag()              # its children come next in document order
            return
        if tag == "meta":
            if (el.get("http-equiv") or "").lower() == "refresh":
                content = el.get("content") or ""
                m = re.match(r"\s*(\d+)\s*[;,]?\s*(url\s*=\s*)?['\"]?([^'\"]*)", content, re.I)
                if m and m.group(3):
                    target = self.url(m.group(3))
                    if target:
                        el.set("content", f"{m.group(1)}; URL={target}")
                        self.attrs(el)
                        return
                elif m:
                    self.attrs(el)
                    return
            el.drop_tree()
            return
        if tag == "dialog" and el.get("open") is not None:
            el.tag = "div"
            self.attrs(el)
            return
        if tag == "picture":
            # libxml2 doesn't know <source> is empty, so the <img> may sit inside a <source>:
            # lift it out, preferring a JPEG/PNG/GIF <source> over AVIF/WebP ones.
            img = next(el.iter("img"), None)
            if img is None or el.getparent() is None:
                el.drop_tree()
                return
            plain = next((s.get("srcset") for s in el.iter("source")
                          if (s.get("type") or "").lower() in ("image/jpeg", "image/png", "image/gif")
                          and s.get("srcset")), None)
            if plain and not img.get("srcset"):
                img.set("srcset", plain)
            img.tail = el.tail
            el.getparent().replace(el, img)
            self.image(img)
            return
        if tag == "svg":
            title = el.find(".//{*}title")
            if title is None:
                title = el.find(".//title")
            label = el.get("aria-label") or (title.text if title is not None else None)
            _replace_with_text(el, label.strip() if label and len(label.strip()) < 80 else None)
            return
        if tag in ("audio", "video"):
            src = el.get("src")
            if not src:
                s = next((s for s in el.iter("source") if s.get("src")), None)
                src = s.get("src") if s is not None else None
            label = "Video" if tag == "video" else "Sound"
            title = el.get("title") or el.get("aria-label")
            _replace_with_link(el, self.url(src), f"[{label}{': ' + title if title else ''}]")
            return
        if tag == "iframe":
            src = self.url(el.get("src"))
            host = urlsplit(src).hostname if src and src.startswith("http") else None
            title = el.get("title") or (host or "")
            if src and host:
                _replace_with_link(el, src, f"[Embedded page: {title}]")
            else:
                el.drop_tree()
            return
        if tag == "button":
            self.button(el)
            return
        el.drop_tree()

    def button(self, el) -> None:
        btype = (el.get("type") or "submit").lower()
        in_form = any(p.tag == "form" for p in el.iterancestors())
        label = _text_of(el) or el.get("aria-label") or el.get("title") or el.get("value") or \
            ("Submit" if btype == "submit" else "")
        if not in_form or btype not in ("submit", "reset") or el.get("formaction"):
            el.drop_tree()
            return
        inp = etree.Element("input", type=btype, value=label[:60] or "Submit")
        if el.get("name"):
            inp.set("name", el.get("name"))
            if el.get("value") is not None:
                inp.set("value", el.get("value") if not label else label[:60])
        inp.tail = el.tail
        el.getparent().replace(el, inp)

    def input(self, el) -> None:
        t = (el.get("type") or "text").lower()
        if t in TEXT_INPUTS:
            el.set("type", "text")
        elif t == "image":
            src = self.url(el.get("src"))
            if src:
                el.set("src", src)
            else:
                el.set("type", "submit")
        elif t in ("button",):
            el.drop_tree()
            return
        if el.get("size") is None and t in TEXT_INPUTS | {"text", "password"}:
            el.set("size", "30")

    def image(self, el) -> None:
        mw = self.ctx.max_width
        src = self.url(_image_src(el, mw))
        if not src:
            alt = (el.get("alt") or "").strip()
            _replace_with_text(el, f"[{alt}]" if alt and len(alt) < 120 else None)
            return
        el.set("src", src)
        w, h = _int(el.get("width")), _int(el.get("height"))
        for k in ("width", "height"):
            el.attrib.pop(k, None)
        if w and w > 0:
            if w > mw:
                h = int(h * mw / w) if h else None
                w = mw
            el.set("width", str(w))
            if h and h > 0:
                el.set("height", str(h))
        if el.get("alt") is None:
            el.set("alt", "")
        if el.get("border") is None and any(p.tag == "a" for p in el.iterancestors()):
            el.set("border", "0")            # Netscape 4 frames linked images in blue otherwise
        self.attrs(el)

    def _fix_empty_link(self, a) -> None:
        if a.getparent() is None or a.get("href") is None:
            return                              # anchors (<a name>) stay
        if _text_of(a) or a.find(".//img") is not None or a.find(".//input") is not None:
            return
        label = a.get("title")
        if label and a.get("href"):
            a.text = label.strip()[:80]
        else:
            a.drop_tag()


# -- article detection and readability ---------------------------------------------------

def _looks_like_article(doc, ctx: Context, raw: str) -> bool:
    path = urlsplit(ctx.url).path or "/"
    if path in ("/", "") or re.match(r"^/(index\.\w+|home|[a-z]{2}(-[a-z]{2})?/?)$", path, re.I):
        return False
    if doc.xpath("//input[@type='password']"):
        return False
    if ARTICLE_TYPES.search(raw[:200000]) or doc.xpath("//meta[@property='og:type' and @content='article']"):
        return True
    if _main_container(doc) is not None:
        return True
    paras = [p for p in doc.iter("p") if len(_text_of(p)) >= 80]
    return len(paras) >= 5


# Containers that hold exactly the article on well-known sites (MediaWiki, most CMSs).
MAIN_CONTAINERS = ("//*[@id='mw-content-text']", "//*[@itemprop='articleBody']",
                   "//*[contains(concat(' ', normalize-space(@class), ' '), ' entry-content ')]",
                   "//*[contains(concat(' ', normalize-space(@class), ' '), ' post-content ')]")
NOISE_CLASSES = {"mw-editsection", "navbox", "navbox-styles", "metadata", "mw-empty-elt", "sistersitebox",
                 "share-buttons", "social-share", "sharedaddy", "jp-relatedposts", "newsletter-signup",
                 "ad", "ads", "advert", "advertisement", "sponsored", "cookie-banner", "cookie-notice"}


def _strip_noise(root) -> None:
    for el in list(root.iter()):
        if isinstance(el.tag, str) and _classes(el) & NOISE_CLASSES and el.getparent() is not None:
            el.drop_tree()


def _main_container(doc):
    for xp in MAIN_CONTAINERS:
        found = doc.xpath(xp)
        if len(found) == 1 and len(_text_of(found[0])) >= 600:
            return found[0]
    return None


def _single_article(doc):
    arts = doc.xpath("//article")
    return arts[0] if len(arts) == 1 else None


def _copy(el):
    return lhtml.fragment_fromstring(lhtml.tostring(el, encoding="unicode"), create_parent="div")


def _readable(raw: str, url: str, doc=None):
    """(title, article element) or None."""
    if doc is not None:
        main = _main_container(doc)
        if main is not None:
            frag = _copy(main)
            _strip_noise(frag)
            if len(_text_of(frag)) >= 600:
                return "", frag
    try:
        from readability import Document
    except ImportError:
        return None
    try:
        d = Document(raw, url=url, min_text_length=25)
        summary = d.summary(html_partial=True)
        title = d.short_title() or d.title() or ""
    except Exception:
        return None
    try:
        frag = lhtml.fragment_fromstring(summary, create_parent="div")
    except (etree.ParserError, ValueError):
        return None
    text_len = len(_text_of(frag))
    if doc is not None:
        art = _single_article(doc)
        if art is not None and len(_text_of(art)) > 2 * max(text_len, 1):
            frag = _copy(art)
            text_len = len(_text_of(frag))
    _strip_noise(frag)
    if text_len < 600:
        return None
    return title, frag


def _control_href(client_url: str, view: str) -> str:
    s = urlsplit(client_url)
    target = s.path or "/"
    if s.query:
        target += "?" + s.query
    from urllib.parse import quote
    return f"{CONTROL_PREFIX}view?mode={view}&to={quote(target, safe='')}"


def _bar(ctx: Context, view: str) -> str:
    host = _html.escape(urlsplit(ctx.client_url).hostname or "")
    if view == "simplified":
        link = f'<a href="{_html.escape(_control_href(ctx.client_url, "full"))}">Show the full page</a>'
        what = "Simplified view"
    else:
        link = f'<a href="{_html.escape(_control_href(ctx.client_url, "simplified"))}">Show the simplified view</a>'
        what = "Full page"
    return (f'<table width="100%" border="0" cellspacing="0" cellpadding="3" bgcolor="#DDDDDD"><tr>'
            f'<td><font size="2" face="Geneva,Helvetica,Arial">{what} of <b>{host}</b></font></td>'
            f'<td align="right"><font size="2" face="Geneva,Helvetica,Arial">{link}</font></td>'
            f'</tr></table>')


# -- entry points ------------------------------------------------------------------------

DOCTYPE = '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">\n'


def _head(title: str, cs_label: str, refresh: str | None = None) -> str:
    r = f'<meta http-equiv="Refresh" content="{_html.escape(refresh, quote=True)}">' if refresh else ""
    return (f'<head><meta http-equiv="Content-Type" content="text/html; charset={cs_label}">{r}'
            f'<title>{_html.escape(title)}</title></head>')


def _serialize(el) -> str:
    return lhtml.tostring(el, encoding="unicode", method="html")


def is_vintage(raw: str, ctx: Context) -> bool:
    host = (urlsplit(ctx.url).hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in ctx.vintage_hosts):
        return True
    head = raw[:2048]
    return bool(VINTAGE_DOCTYPE.search(head)) and "<script" not in raw[:400000].lower() \
        and "stylesheet" not in raw[:400000].lower() and len(raw) < 300_000


def vintage(raw: str, ctx: Context) -> Result:
    """Light touch for pages made for old browsers: charset and https links only."""
    try:
        doc = lhtml.document_fromstring(raw)
    except (etree.ParserError, ValueError):
        doc = lhtml.document_fromstring("<html><body></body></html>")
    base = ctx.url
    for b in doc.iter("base"):
        if b.get("href"):
            from urllib.parse import urljoin
            base = urljoin(base, b.get("href"))
            b.drop_tree()
            break
    for el, attr, link, _pos in list(doc.iterlinks()):
        if attr is None:
            continue
        new = rewrite(link, base, ctx.tls)
        if new is None:
            continue
        el.set(attr, new)
    for m in doc.xpath("//meta[@charset or translate(@http-equiv,'CONTENT-YP','content-yp')='content-type']"):
        m.drop_tree()
    title = (doc.findtext(".//title") or "").strip()
    _, label = charset.codec_for(ctx.charset)
    # libxml2 drops Content-Type metas while serialising, so the charset goes in as text.
    meta = f'<meta http-equiv="Content-Type" content="text/html; charset={label}">'
    body = _serialize(doc)
    if re.search(r"<head[^>]*>", body[:4096], re.I):
        body = re.sub(r"(<head[^>]*>)", lambda mm: mm.group(1) + meta, body, count=1, flags=re.I)
    else:
        body = re.sub(r"(<html[^>]*>)", lambda mm: mm.group(1) + "<head>" + meta + "</head>", body, count=1,
                      flags=re.I)
    dt = re.match(r"\s*(<!doctype[^>]*>)", raw, re.I)
    out = (dt.group(1) + "\n" if dt else "") + body
    return Result(charset.encode_text(out, ctx.charset), "vintage", title)


def translate(raw: str, ctx: Context) -> Result:
    """Translate a decoded HTML document."""
    raw = re.sub(r"^\s*<\?xml[^>]*\?>", "", raw)
    if ctx.view != "simplified" and is_vintage(raw, ctx):
        return vintage(raw, ctx)
    try:
        doc = lhtml.document_fromstring(raw)
    except (etree.ParserError, ValueError):
        doc = lhtml.document_fromstring("<html><body><p>This page is empty.</p></body></html>")
    title = re.sub(r"\s+", " ", doc.findtext(".//title") or "").strip()
    refresh = None
    for m in doc.xpath("//meta[translate(@http-equiv,'REFSH','refsh')='refresh']"):
        refresh = m.get("content")
        break
    view = ctx.view
    article = False
    if view == "full" and ctx.control_links:
        article = _looks_like_article(doc, ctx, raw)
    if view in ("auto", "simplified"):
        article = view == "simplified" or _looks_like_article(doc, ctx, raw)
        readable = _readable(raw, ctx.url, doc) if article else None
        if readable:
            rtitle, frag = readable
            cleaner = Cleaner(ctx, ctx.url)
            cleaner.clean(frag)
            _, label = charset.codec_for(ctx.charset)
            t = title or rtitle
            body = (f'<body bgcolor="#FFFFFF" text="#000000">'
                    f'{_bar(ctx, "simplified") if ctx.control_links else ""}'
                    f'<h2>{_html.escape(rtitle or t)}</h2>{_serialize(frag)}'
                    f'<hr><font size="1" face="Geneva,Helvetica,Arial">Simplified by RetroBoot\'s '
                    f'Modern Web Helper from {_html.escape(urlsplit(ctx.client_url).hostname or "")}.</font>'
                    f'</body>')
            out = DOCTYPE + f"<html>{_head(t, label)}{body}</html>"
            return Result(charset.encode_text(out, ctx.charset), "simplified", t, True)
        view = "full"
    cleaner = Cleaner(ctx, ctx.url)
    cleaner.clean(doc)
    head = doc.find("head")
    body = doc.find("body")
    if body is None:
        body = etree.SubElement(doc, "body")
    if body.get("bgcolor") is None:
        body.set("bgcolor", "#FFFFFF")
    _, label = charset.codec_for(ctx.charset)
    head_html = _head(title, label, refresh_target(refresh, cleaner) if refresh else None)
    # The switch to the simplified view is offered where it can help: article-like pages
    # shown in full because the user asked for full pages.
    bar = _bar(ctx, "full") if (ctx.control_links and article and ctx.view == "full") else ""
    body_html = _serialize(body)
    if bar:
        body_html = re.sub(r"^(<body[^>]*>)", lambda m: m.group(1) + bar, body_html, count=1)
    out = DOCTYPE + "<html>" + head_html + body_html + "</html>"
    del head
    return Result(charset.encode_text(out, ctx.charset), "full", title, article)


def refresh_target(content: str, cleaner: Cleaner) -> str | None:
    m = re.match(r"\s*(\d+)\s*[;,]?\s*(url\s*=\s*)?['\"]?([^'\"]*)", content or "", re.I)
    if not m:
        return None
    if not m.group(3):
        return m.group(1)
    target = cleaner.url(m.group(3))
    return f"{m.group(1)}; URL={target}" if target else None


def translate_bytes(body: bytes, ct_charset: str | None, ctx: Context) -> Result:
    return translate(charset.decode(body, ct_charset), ctx)
