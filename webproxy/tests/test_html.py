"""HTML translation for IE 5 / Netscape 4."""
import re
import unittest

import support  # noqa: F401
from rbwebproxy import html
from rbwebproxy.urls import TLSMemory, for_client, rewrite, upstream

MODERN = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width"><title>Modern — Page</title>
<link rel="stylesheet" href="/app.css"><style>body{display:flex}</style>
<script>alert(1)</script><script type="module" src="/m.js"></script>
<noscript><link rel="stylesheet" href="/ns.css"></noscript>
<meta http-equiv="refresh" content="300; url=https://news.example/latest">
</head><body class="x" style="color:red" onload="go()">
<header><nav><a href="https://news.example/" class="sr-only">Skip to content</a>
<a href="https://news.example/world">World</a> <a href="//cdn.example/x">CDN</a>
<a href="javascript:void(0)" onclick="menu()">Menu</a></nav></header>
<main><article><h1>Hello</h1>
<p hidden>secret</p><div style="display: none">gone</div><div aria-hidden="true">icons</div>
<div class="hidden md:block">desktop nav</div><div class="d-none">mobile only</div>
<p>Café “quotes” → arrows <mark>marked</mark> <time>today</time></p>
<img src="data:image/gif;base64,R0lGOD" data-src="/lazy.jpg" alt="Lazy">
<img srcset="/s-320.jpg 320w, /s-800.jpg 800w, /s-1600.jpg 1600w" src="/s-1600.jpg" width="1600" height="800" alt="">
<img srcset="/d1.jpg 1x, /d2.jpg 2x" alt="dens">
<picture><source type="image/avif" srcset="/p.avif"><source type="image/jpeg" srcset="/p.jpg"><img src="/p.png" alt="pic"></picture>
<svg viewBox="0 0 10 10"><title>Logo</title><path d="M0 0"/></svg><svg><path/></svg>
<iframe src="https://www.youtube.com/embed/abc" title="Video"></iframe>
<video src="/v.mp4" title="Clip"></video>
<noscript><img src="/pixel.png" alt="np"></noscript>
<form action="https://news.example/search" method="get" novalidate>
<input type="search" name="q" placeholder="Search" required><input type="email" name="e">
<button type="submit">Go →</button><button type="button" onclick="x()">JS</button></form>
<input type="checkbox" id="menu-toggle"><select><option>x</option></select><a name="anchor"></a><a href="#sec">jump</a><h2 id="sec">Section</h2><h2 id="unused">Other</h2>
<!-- comment --><template><p>tpl</p></template>
</article></main><footer>Foot</footer></body></html>"""


def ctx(url="https://news.example/2024/story", view="full", **kw):
    return html.Context(url=url, client_url=url.replace("https://", "http://"), tls=TLSMemory(), view=view, **kw)


class UrlTests(unittest.TestCase):
    def test_rewrite(self):
        t = TLSMemory()
        self.assertEqual(rewrite("/a b?x=é#f", "https://h.example/p/", t), "http://h.example/a%20b?x=%C3%A9#f")
        self.assertTrue(t.needs("h.example"))
        self.assertEqual(rewrite("//cdn.example/x", "https://h.example/", t), "http://cdn.example/x")
        self.assertTrue(t.needs("cdn.example"))
        self.assertEqual(rewrite("https://h.example:8443/x", "http://a/", t), "http://h.example:8443/x")
        self.assertTrue(t.needs("h.example:8443"))
        self.assertIsNone(rewrite("javascript:alert(1)", "http://a/", t))
        self.assertIsNone(rewrite("data:text/html,x", "http://a/", t))
        self.assertEqual(rewrite("mailto:a@b", "http://a/", t), "mailto:a@b")
        self.assertEqual(rewrite("#top", "https://a/", t), "#top")
        self.assertEqual(rewrite("http://bücher.example/", "http://a/", t), "http://xn--bcher-kva.example/")

    def test_upstream(self):
        t = TLSMemory()
        self.assertEqual(upstream("http://h.example/x?y", t), "http://h.example/x?y")
        for_client("https://h.example/", t)
        self.assertEqual(upstream("http://h.example/x?y", t), "https://h.example/x?y")
        for_client("https://h.example:8443/", t)
        self.assertEqual(upstream("http://h.example:8443/", t), "https://h.example:8443/")


class FullViewTests(unittest.TestCase):
    def setUp(self):
        c = ctx()
        self.res = html.translate(MODERN, c)
        self.out = self.res.body.decode("mac_roman")
        self.tls = c.tls

    def test_scripts_styles_and_modern_head_gone(self):
        for bad in ("<script", "alert(1)", "<style", "stylesheet", "viewport", "onload", "onclick", "style=",
                    "<template", "tpl", "<!--", "class="):
            self.assertNotIn(bad, self.out)
        self.assertIn('charset=x-mac-roman', self.out)
        self.assertTrue(self.out.startswith("<!DOCTYPE HTML PUBLIC"))
        self.assertIn("<title>Modern — Page</title>", self.out)
        self.assertEqual(self.res.view, "full")

    def test_hidden_content_removed(self):
        for bad in ("secret", ">gone", "icons", "Skip to content", "mobile only"):
            self.assertNotIn(bad, self.out)
        self.assertIn("desktop nav", self.out)                  # responsive: shown on desktop

    def test_links_forms_refresh(self):
        self.assertIn('href="http://news.example/world"', self.out)
        self.assertIn('href="http://cdn.example/x"', self.out)
        self.assertNotIn("javascript:", self.out)
        self.assertIn('action="http://news.example/search"', self.out)
        self.assertIn('<input type="text" name="q"', self.out)
        self.assertNotIn("placeholder", self.out)
        self.assertNotIn("required", self.out)
        self.assertRegex(self.out, r'<input type="submit" value="Go ->"')
        self.assertNotIn(">JS<", self.out)
        self.assertIn('content="300; URL=http://news.example/latest"', self.out)
        self.assertTrue(self.tls.needs("news.example") and self.tls.needs("cdn.example"))

    def test_images(self):
        self.assertIn('src="http://news.example/lazy.jpg"', self.out)
        self.assertRegex(self.out, r'<img src="http://news.example/s-800.jpg" alt="" width="640" height="320">')
        self.assertIn('src="http://news.example/d1.jpg"', self.out)
        self.assertIn('src="http://news.example/p.jpg"', self.out)   # JPEG source over AVIF, over PNG fallback
        self.assertNotIn("srcset", self.out)
        self.assertIn("<span>Logo</span>", self.out)
        self.assertNotIn("<svg", self.out)
        self.assertIn('src="http://news.example/pixel.png"', self.out)  # noscript fallback shown

    def test_embeds_become_links(self):
        self.assertIn('<a href="http://www.youtube.com/embed/abc">[Embedded page: Video]</a>', self.out)
        self.assertIn('<a href="http://news.example/v.mp4">[Video: Clip]</a>', self.out)

    def test_html5_elements_and_anchors(self):
        for tag in ("<header", "<nav", "<main", "<article", "<footer", "<mark", "<time"):
            self.assertNotIn(tag, self.out)
        self.assertIn('<a name="anchor"></a>', self.out)
        self.assertIn('<h2 id="sec"><a name="sec"></a>Section</h2>', self.out)
        self.assertIn("<h2>Other</h2>", self.out)

    def test_controls_outside_forms_dropped(self):
        self.assertNotIn("checkbox", self.out)
        self.assertNotIn("<select", self.out)
        self.assertIn('<input type="text" name="q"', self.out)

    def test_charset(self):
        self.assertIn("Café “quotes” -> arrows", self.out)


class ViewTests(unittest.TestCase):
    ARTICLE = ("<html><head><title>A long story - Paper</title></head><body><div id=nav>"
               + "".join(f'<a href="/s{i}">Section {i}</a> ' for i in range(40)) + "</div><div class=story>"
               + "".join(f"<p>Paragraph {i}: " + "The quick brown fox jumps over the lazy dog. " * 6 + "</p>"
                         for i in range(12)) + "</div><div class=footer>Copyright</div></body></html>")

    def test_auto_picks_simplified_for_articles(self):
        res = html.translate(self.ARTICLE, ctx("https://paper.example/2024/10/story", view="auto"))
        out = res.body.decode("mac_roman")
        self.assertEqual(res.view, "simplified")
        self.assertIn("Paragraph 11", out)
        self.assertNotIn("Section 39", out)
        self.assertIn("/__rbwh__/view?mode=full&amp;to=%2F2024%2F10%2Fstory", out)

    def test_auto_keeps_front_pages_full(self):
        res = html.translate(self.ARTICLE, ctx("https://paper.example/", view="auto"))
        self.assertEqual(res.view, "full")
        self.assertIn("Section 39", res.body.decode("mac_roman"))

    def test_simplified_falls_back_to_full(self):
        res = html.translate("<html><body><a href=/x>tiny</a></body></html>", ctx(view="simplified"))
        self.assertEqual(res.view, "full")

    def test_full_offers_simplified_on_articles(self):
        res = html.translate(self.ARTICLE, ctx("https://paper.example/2024/10/story", view="full"))
        self.assertIn("mode=simplified", res.body.decode("mac_roman"))

    def test_wikipedia_container(self):
        page = ('<html><head><title>Mac - Wikipedia</title></head><body><div id="mw-navigation">'
                + "<a href=/wiki/A>A</a>" * 50 + '</div><div id="mw-content-text"><div class="mw-parser-output">'
                + "<p><b>Mac</b> is a family of personal computers. " + "Words " * 80 + "</p>"
                + '<h2>History<span class="mw-editsection">[edit]</span></h2>'
                + "<p>" + "More words. " * 60 + "</p></div></div></body></html>")
        res = html.translate(page, ctx("https://en.wikipedia.org/wiki/Mac", view="auto"))
        out = res.body.decode("mac_roman")
        self.assertEqual(res.view, "simplified")
        self.assertIn("is a family of personal computers", out)    # the lead section is kept
        self.assertNotIn("[edit]", out)
        self.assertNotIn(">A</a>", out)

    def test_vintage_site_passes_lightly(self):
        page = ('<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN"><html><head><title>S7</title>'
                '<meta http-equiv="Content-Type" content="text/html; charset=utf-8"></head>'
                '<body bgcolor="#FFFFFF"><font face="Geneva">Café</font> <a href="https://x.example/">x</a>'
                '<table width=600><tr><td>cell</td></tr></table></body></html>')
        res = html.translate(page, ctx("http://system7today.com/", vintage_hosts=("system7today.com",)))
        out = res.body.decode("mac_roman")
        self.assertEqual(res.view, "vintage")
        self.assertIn('<font face="Geneva">Café</font>', out)
        self.assertIn('href="http://x.example/"', out)
        self.assertIn("charset=x-mac-roman", out)
        self.assertNotIn("utf-8", out)


if __name__ == "__main__":
    unittest.main()
