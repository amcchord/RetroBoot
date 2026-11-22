"""Charsets for old browsers, form transcoding, redirect table, config validation."""
import unittest

import support  # noqa: F401  (sys.path)
from rbwebproxy import charset, redirects
from rbwebproxy.config import Config, normalize_redirects


class CharsetTests(unittest.TestCase):
    def test_macroman_keeps_mac_characters(self):
        self.assertEqual(charset.encode_text("café — “hi” ™ …", "macroman"),
                         b"caf\x8e \xd1 \xd2hi\xd3 \xaa \xc9")

    def test_transliteration_and_references(self):
        out = charset.encode_text("łódź → × \U0001F600️ x​y 中", "macroman")
        self.assertEqual(out, b"l\x97dz -> x  xy &#20013;")

    def test_latin1(self):
        out = charset.encode_text("‘q’ € é π", "latin1")
        self.assertEqual(out, b"'q' EUR \xe9 &#960;")

    def test_plain_text_has_no_references(self):
        self.assertEqual(charset.encode_plain("中!", "macroman"), b"?!")

    def test_decode_detection(self):
        self.assertEqual(charset.detect("café".encode(), None), "utf-8")
        self.assertEqual(charset.detect(b"<meta charset=\"windows-1251\">\xcf", None), "cp1251")
        self.assertEqual(charset.detect(b"caf\xe9", None), "cp1252")
        self.assertEqual(charset.detect(b"\xef\xbb\xbfx", "iso-8859-1"), "utf-8")
        self.assertEqual(charset.detect(b"x", "x-mac-roman"), "mac-roman")

    def test_form_from_macroman_page(self):
        # IE 5 submits a MacRoman page's form in MacRoman: caf\x8e = "café"
        self.assertEqual(charset.transcode_form("q=caf%8E+au+lait&n=1"), "q=caf%C3%A9+au+lait&n=1")

    def test_utf8_links_untouched(self):
        self.assertEqual(charset.transcode_form("q=%C3%A9t%C3%A9&x=a+b"), "q=%C3%A9t%C3%A9&x=a+b")
        self.assertEqual(charset.transcode_form("plain=ascii"), "plain=ascii")
        self.assertEqual(charset.transcode_form(b"a=%2B%20"), "a=%2B%20")


class RedirectTests(unittest.TestCase):
    table = normalize_redirects([
        {"host": "livepage.apple.com", "path": "", "to": "http://system7today.com/"},
        {"host": "www.excite.com", "path": "/apple/", "to": "search"},
        {"host": "www.excite.com", "path": "", "to": "http://example.org/"},
        {"host": "BAD HOST", "path": "", "to": "http://x/"},
        {"host": "ok.example", "path": "nope", "to": "http://x/"},
        {"host": "ok.example", "path": "", "to": "javascript:alert(1)"},
        {"host": "off.example", "path": "", "to": "http://x/", "enabled": False},
    ])

    def test_invalid_entries_dropped(self):
        self.assertEqual([e["host"] for e in self.table], ["livepage.apple.com", "www.excite.com", "www.excite.com"])

    def test_longest_path_wins(self):
        self.assertEqual(redirects.match(self.table, "www.excite.com", "/apple/search")["to"], "search")
        self.assertEqual(redirects.match(self.table, "www.excite.com", "/apple")["to"], "search")
        self.assertEqual(redirects.match(self.table, "www.excite.com", "/news")["to"], "http://example.org/")
        self.assertEqual(redirects.match(self.table, "LivePage.Apple.com.", "/x")["to"], "http://system7today.com/")
        self.assertIsNone(redirects.match(self.table, "apple.com", "/"))

    def test_front_page_only(self):
        t = normalize_redirects([{"host": "www.msn.com", "path": "/", "to": "http://n/"}])
        self.assertIsNotNone(redirects.match(t, "www.msn.com", "/"))
        self.assertIsNone(redirects.match(t, "www.msn.com", "/en-us/news"))

    def test_search_targets(self):
        e = {"host": "h", "path": "", "to": "search"}
        self.assertEqual(redirects.target(e, "", "frogfind"), "http://frogfind.com/")
        self.assertEqual(redirects.target(e, "MT=mac+os&x=1", "frogfind"), "http://frogfind.com/?q=mac+os")
        self.assertEqual(redirects.target(e, "q=a%26b", "duckduckgo"), "https://html.duckduckgo.com/html/?q=a%26b")
        self.assertIsNone(redirects.target(e, "q=x", "none"))
        # IE 5 address-bar search and Netscape Internet Keywords
        self.assertEqual(redirects.target(e, "p=classic+mac", "frogfind"), "http://frogfind.com/?q=classic+mac")
        self.assertEqual(redirects.target(e, "", "wiby", "/keyword/power%20mac"), "http://wiby.me/?q=power+mac")

    def test_same_place(self):
        self.assertTrue(redirects.same_place("http://a/x", "http://A/x"))
        self.assertFalse(redirects.same_place("http://a/x", "http://a/y"))


class ConfigTests(unittest.TestCase):
    def test_bad_values_fall_back(self):
        c = Config.from_dict({"view": "weird", "search": "nope", "port": "80", "image_max_width": 5,
                              "limits": {"page_s": 3, "bogus": 1}, "unknown": 1})
        self.assertEqual((c.view, c.search, c.port, c.image_max_width, c.limits.page_s), ("auto", "frogfind", 8080, 160, 3.0))


if __name__ == "__main__":
    unittest.main()
