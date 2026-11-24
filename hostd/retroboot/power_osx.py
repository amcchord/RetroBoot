"""Measured native OS X power UI; unknown pixels never authorize Return.

The references are hashes of threshold masks, not guest pixels or software.
English Tiger 10.4.6/8I128 and Panther 10.3.9/7W98 were measured in private native
captures. Navigation and actual clean exit remain separate runtime checks.
"""
from __future__ import annotations

import hashlib

REFERENCE = "tiger-8I128-en"
SHARED = (
    ((10, 0, 44, 22), "blue", "eef0d54857a272b88f7149d901945817467346845dd860c6adbd0c7f264ea736"),
    ((30, 226, 80, 243), "dark", "2d0a40de324ad08526c2d99445f7d40cd373bb56fbdc8fb2435166a781260126"),
    ((30, 246, 115, 263), "dark", "3e74be511439d4a5ebecf268b336372aa361529255fb1f3e0193b0a5c8defd8f"),
)
TIGER_8S165_SHARED = (
    SHARED[0],
    (SHARED[1][0], SHARED[1][1], (SHARED[1][2],
     # Measured C9q ATI menu: Sleep edge pixel (64, 232) is 94, not 99.
     "71e0136e77a064924b2e13b48421b18b8af0f3b044adf1187d56b3e3a4216e97")),
    SHARED[2],
)
MENU = ((30, 266, 128, 282), "dark", (
    "a6edc9faf95ee4d06aa7698e04273826547f9deae3d5f15b312fceba0b038686",
    "6f984e651c73e05c4f7beb61e02428f2d5f7258b7c6e464ba14bd22e2801be17",
))
MENU_VARIANTS = {
    ("macosx-10.4", "10.4.11", "8S165"): (
        MENU[0], MENU[1], (*MENU[2],
        # Measured post-wake ATI label: six gray edge pixels round95 to96.
        "e1e7ee25c4552c826e1e050397e27d0ef126d2b666bbc36dac24f8c761044c48"),
    ),
}
SELECTED = (
    ((30, 266, 128, 282), "light", "c61e3ff88effff54a3a1668a6944413116ee5cab4426dde16cf29cd8d7b1b62e"),
    # Apple-menu width changes with the foreground application's shortcuts.
    # Check the selection beneath the measured label, never its variable right edge.
    ((10, 264, 128, 283), "blue", "97aec955eeccd0872fe8964091c721c90a070c40d657d8b2a2ae59bcade16891"),
)
QUESTION = "4c5dbb9e3775e1f4782e583adb4b907c6f8236cfcfb6a9dd6830a89d7a84e591"
BUTTONS = (
    ((247, 75, 317, 90), "dark", "545111c06c341bfcbfc7b7efc518a1499e90523e3e2a4e1e9755af9530d09d10"),
    ((154, 75, 198, 90), "dark", "3e71222e38df3410f472988b3baab80be691f09c5993507fb8d70fab2fe8f0f9"),
)

# Panther 10.3.9/7W98 uses the same measured text and alert geometry, with
# its own Aqua Apple-menu highlight and striped selection background.
REFERENCES = {
    ("macosx-10.4", "10.4.6", "8I128"): (REFERENCE, SHARED, SELECTED, BUTTONS),
    ("macosx-10.4", "10.4.11", "8S165"): (
        "tiger-8S165-en", TIGER_8S165_SHARED, SELECTED,
        ((BUTTONS[0][0], "dark", (BUTTONS[0][2],
          "fe9ec441312d9e408ce536b28c10eda16f68a0112e11913e6dc5d4c8636a0b0a",
          # Native final-edition ATI confirmation; question and Cancel unchanged.
          "2991e286b7dfd450096cb95327c7bf8b9691e4189cad45cf98d925aaeb2f900d")), BUTTONS[1]),
    ),
    ("macosx-10.3", "10.3.9", "7W98"): (
        "panther-7W98-en",
        ((SHARED[0][0], "blue", "7159e1d5dd4ba1a82ab366858a1ceaa87854572fe3bbc918f2145481e97a6c07"),
         *SHARED[1:]),
        (SELECTED[0], (SELECTED[1][0], "blue",
                       "e9be69478cad544430d39b29ef193ea55b50e29ee9e2df68edbaa1f895a37514")),
        (BUTTONS[0], (BUTTONS[1][0], "dark",
                      "81181007aa2a9836039635e99ea607dc04d4f1898e1a3d2386629a082b02e418")),
    ),
}


def supported(profile: str, version: str | None, build: str | None = None) -> bool:
    return (profile, version, build) in REFERENCES


FALLBACK_PROFILES = {"macosx-10.2", "macosx-10.3", "macosx-10.4"}


def fallback_supported(profile: str, version: str | None = None, build: str | None = None) -> bool:
    """Whether keyboard Apple-menu navigation fallback may attempt clean shutdown on
    unrecognized themes (e.g. Graphite) or Jaguar 10.2.8 when Aqua screenshots don't match (#45)."""
    if profile not in FALLBACK_PROFILES:
        return False
    if version is not None and not (version == profile[7:] or version.startswith(profile[7:] + ".")):
        return False
    return True


def reference(profile: str, version: str | None, build: str | None = None) -> str | None:
    ref = REFERENCES.get((profile, version, build))
    return ref[0] if ref else None


def _box_diff_stats(width: int, height: int, before: bytes, current: bytes,
                    box: tuple[int, int, int, int], threshold: int = 24) -> tuple[int, int]:
    x0 = max(0, box[0])
    y0 = max(0, box[1])
    x1 = min(width, box[2])
    y1 = min(height, box[3])
    if x1 <= x0 or y1 <= y0 or len(before) != width * height * 3 or len(current) != width * height * 3:
        return 0, 0
    changed = 0
    total = (x1 - x0) * (y1 - y0)
    for y in range(y0, y1):
        row = y * width * 3
        for x in range(x0, x1):
            off = row + x * 3
            if (abs(before[off] - current[off]) + abs(before[off + 1] - current[off + 1]) +
                    abs(before[off + 2] - current[off + 2])) > threshold:
                changed += 1
    return changed, total


def detect_menu_opened(width: int, height: int, before: bytes, current: bytes) -> bool:
    """Verify Ctrl+F2 + Down visibly opened the Apple menu dropdown cleanly (#45)."""
    if width < 640 or height < 480 or len(before) != width * height * 3 or len(current) != width * height * 3:
        return False
    if before == current:
        return False
    menu_changed, menu_total = _box_diff_stats(width, height, before, current, (0, 22, min(260, width), min(340, height)))
    rest_changed, rest_total = _box_diff_stats(width, height, before, current, (min(300, width), 22, width, height))
    if menu_total == 0 or menu_changed < 300:
        return False
    return (menu_changed / menu_total) >= 0.03 and (rest_total == 0 or (rest_changed / rest_total) < 0.20)


def detect_menu_selection_moved(width: int, height: int, prev_menu: bytes, current: bytes) -> bool:
    """Verify End + Up moved the selection highlight inside the open Apple menu (#45)."""
    if width < 640 or height < 480 or len(prev_menu) != width * height * 3 or len(current) != width * height * 3:
        return False
    if prev_menu == current:
        return False
    changed, total = _box_diff_stats(width, height, prev_menu, current, (4, 180, min(250, width), min(340, height)))
    rest_changed, rest_total = _box_diff_stats(width, height, prev_menu, current, (min(300, width), 22, width, height))
    return total > 0 and changed >= 80 and (rest_total == 0 or (rest_changed / rest_total) < 0.15)


def detect_confirmation_dialog(width: int, height: int, before: bytes, current: bytes) -> bool:
    """Verify a centered OS X Shut Down confirmation modal appeared (#45)."""
    if width < 640 or height < 480 or len(before) != width * height * 3 or len(current) != width * height * 3:
        return False
    if before == current:
        return False
    from .powerdlg import modal_change
    mc = modal_change((width, height, before), (width, height, current))
    if mc.get("modal") and mc.get("box"):
        x0, y0, x1, y1 = mc["box"]
        cx = (x0 + x1) / 2.0
        if (x1 - x0) >= 160 and (y1 - y0) >= 60 and width * 0.2 <= cx <= width * 0.8:
            return True
    # Also accept a centered dialog change even if desktop menu bar closed at the same frame.
    center_changed, center_total = _box_diff_stats(
        width, height, before, current, (width // 2 - 220, 40, width // 2 + 220, height - 80)
    )
    edge_changed, edge_total = _box_diff_stats(
        width, height, before, current, (width - 140, 22, width, height)
    )
    return center_total > 0 and center_changed >= 2000 and (edge_total == 0 or (edge_changed / edge_total) < 0.25)


def _mask(width: int, height: int, rgb: bytes, box: tuple, kind: str) -> bytes | None:
    x0, y0, x1, y1 = box
    if x0 < 0 or y0 < 0 or x1 > width or y1 > height:
        return None
    data = bytearray()
    for y in range(y0, y1):
        for x in range(x0, x1):
            offset = (y * width + x) * 3
            r, g, b = rgb[offset], rgb[offset + 1], rgb[offset + 2]
            if kind == "dark":
                data.append(r < 96 and g < 96 and b < 96)
            elif kind == "light":
                data.append(r > 220 and g > 220 and b > 220)
            else:
                data.append(b > 150 and b - r > 70 and g - r > 20)
    return bytes(data)


def _matches(width: int, height: int, rgb: bytes, checks: tuple) -> bool:
    for box, kind, expected in checks:
        mask = _mask(width, height, rgb, box, kind)
        known = expected if isinstance(expected, tuple) else (expected,)
        if mask is None:
            return False
        if hashlib.sha256(mask).hexdigest() in known:
            continue
        # GPU readback can round an antialiased pixel by one color value.
        # Accept only one such threshold-sensitive bit, and only if the
        # entire resulting mask exactly matches an existing reference.
        candidate = bytearray(mask)
        x0, y0, x1, y1 = box
        matched = False
        for y in range(y0, y1):
            for x in range(x0, x1):
                offset = (y * width + x) * 3
                r, g, b = rgb[offset:offset + 3]
                lo = (max(0, r - 1), max(0, g - 1), max(0, b - 1))
                hi = (min(255, r + 1), min(255, g + 1), min(255, b + 1))
                if kind == "dark":
                    uncertain = all(v < 96 for v in lo) != all(v < 96 for v in hi)
                elif kind == "light":
                    uncertain = all(v > 220 for v in lo) != all(v > 220 for v in hi)
                else:
                    low = lo[2] > 150 and lo[2] - hi[0] > 70 and lo[1] - hi[0] > 20
                    high = hi[2] > 150 and hi[2] - lo[0] > 70 and hi[1] - lo[0] > 20
                    uncertain = low != high
                if not uncertain:
                    continue
                index = (y - y0) * (x1 - x0) + x - x0
                candidate[index] ^= 1
                matched = hashlib.sha256(candidate).hexdigest() in known
                candidate[index] ^= 1
                if matched:
                    break
            if matched:
                break
        if not matched:
            return False
    return True


def state(profile: str, version: str | None, width: int, height: int, rgb: bytes, *, build: str | None = None) -> str | None:
    if not supported(profile, version, build) or width < 640 or height < 480 or len(rgb) != width * height * 3:
        return None
    _, shared, selected, buttons = REFERENCES[(profile, version, build)]
    menu = MENU_VARIANTS.get((profile, version, build), MENU)
    if _matches(width, height, rgb, shared):
        if _matches(width, height, rgb, selected):
            return "shutdown-selected"
        if _matches(width, height, rgb, (menu,)):
            return "apple-menu"
    # Alerts center horizontally. Search vertically because the Dock/visible
    # frame changes their position. The countdown body is intentionally omitted.
    x0 = width // 2 - 123
    stripe = _mask(width, height, rgb, (x0, 0, x0 + 310, height), "dark")
    if stripe is None:
        return None
    for y in range(22, height - 110):
        if hashlib.sha256(stripe[y * 310:(y + 34) * 310]).hexdigest() != QUESTION:
            continue
        checks = tuple(((x0 + a, y + b, x0 + c, y + d), kind, expected)
                       for (a, b, c, d), kind, expected in buttons)
        if _matches(width, height, rgb, checks):
            return "shutdown-confirmation"
    return None
