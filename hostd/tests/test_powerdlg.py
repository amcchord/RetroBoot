"""powerdlg: Mac OS's power-key dialog recognized by its content, and the guest's CPU load.

Frames are synthetic (drawn here after the geometry of Mac OS 9.2.2's dialog measured in
the lab: 402 x 104 with its shadow, Platinum gray inside, four push buttons; no Apple
pixels are committed)."""
import helpers  # noqa: F401  (sys.path)
from retroboot import powerdlg

BLUE = b"\x65\x65\x9e"
GRAY = b"\xdd\xdd\xdd"
WHITE = b"\xff\xff\xff"
BLACK = b"\x00\x00\x00"
DIM = b"\x99\x99\x99"


def rect(px, w, x, y, rw, rh, color):
    for yy in range(y, y + rh):
        o = (yy * w + x) * 3
        px[o:o + rw * 3] = color * rw


def desktop(w=1024, h=768, back=BLUE, titles=BLACK):
    px = bytearray(back * (w * h))
    rect(px, w, 0, 0, w, 20, WHITE)
    rect(px, w, 0, 19, w, 1, BLACK)                # the menu bar's bottom line
    for i in range(6):                             # File Edit View Window Special Help
        for k in range(5):
            rect(px, w, 42 + i * 55 + k * 8, 5, 5, 9, titles)
    rect(px, w, 940, 40, 32, 32, b"\x30\x30\x30")  # Macintosh HD
    return px


def dialog(px, w, x0, y0, buttons=4, body=GRAY):
    """Mac OS 9's power-key alert: 1-pixel black frame, bevel, shadow, icon, text, buttons."""
    bw, bh = 402, 102
    rect(px, w, x0, y0, bw, bh, BLACK)
    rect(px, w, x0 + 1, y0 + 1, bw - 2, bh - 2, WHITE)
    rect(px, w, x0 + 3, y0 + 3, bw - 5, bh - 5, body)
    rect(px, w, x0 + 2, y0 + bh, bw, 2, BLACK)
    rect(px, w, x0 + bw, y0 + 2, 2, bh, BLACK)
    rect(px, w, x0 + 22, y0 + 18, 32, 28, b"\xff\xee\x00")     # caution icon
    for line in range(2):
        for i in range(28):
            rect(px, w, x0 + 84 + i * 10, y0 + 20 + line * 16, 6, 9, BLACK)
    for i in range(buttons):
        bx = x0 + 24 + i * 94
        rect(px, w, bx, y0 + 64, 80, 20, BLACK)
        rect(px, w, bx + 1, y0 + 65, 78, 18, b"\xee\xee\xee")
        rect(px, w, bx + 20, y0 + 70, 40, 8, BLACK)


def frame(px, w=1024, h=768):
    return (w, h, bytes(px))


def test_dialog_found_where_mac_os_puts_it():
    before = desktop()
    after = desktop(titles=DIM)
    dialog(after, 1024, 310, 148)
    res = powerdlg.find_power_dialog(frame(before), frame(after))
    assert res["found"] and res["buttons"] == 4 and res["menus_dimmed"] is True
    assert res["box"] == (310, 148, 712, 250)


def test_dialog_found_off_centre_and_without_a_before_frame():
    """With the ATI card Mac OS places it for its 640x480 startup screen, even at 1024x768."""
    after = desktop(titles=DIM)
    dialog(after, 1024, 118, 91)
    assert powerdlg.find_power_dialog(None, frame(after))["found"]
    small = desktop(640, 480, titles=DIM)
    dialog(small, 640, 119, 92)
    assert powerdlg.find_power_dialog(frame(desktop(640, 480)), frame(small, 640, 480))["found"]


def test_dialog_over_a_dark_desktop_picture():
    before = desktop(back=b"\x08\x08\x08")
    after = desktop(back=b"\x08\x08\x08", titles=DIM)
    dialog(after, 1024, 310, 148)
    res = powerdlg.find_power_dialog(frame(before), frame(after))
    assert res["found"] and res["via"] == "change"


def test_finder_drawing_is_never_the_dialog():
    """What hostd took for the dialog before: icons and windows the Finder draws after its
    menu bar appears, and a Mac OS screen-size change."""
    before = desktop()
    icons = desktop()
    for i in range(8):
        rect(icons, 1024, 300 + i * 60, 300, 32, 32, b"\x20\x80\x20")
    assert not powerdlg.find_power_dialog(frame(before), frame(icons))["found"]
    window = desktop()                              # a Finder window: frame, striped title bar, white inside
    rect(window, 1024, 300, 150, 420, 300, BLACK)
    rect(window, 1024, 301, 151, 418, 298, WHITE)
    for y in range(152, 170, 2):
        rect(window, 1024, 302, y, 416, 1, b"\x88\x88\x88")
    res = powerdlg.find_power_dialog(frame(before), frame(window))
    assert not res["found"] and not res["new_box"]
    switched = desktop(800, 600)
    assert not powerdlg.find_power_dialog(frame(before), frame(switched, 800, 600))["found"]


def test_other_alerts_and_a_dialog_that_was_already_there():
    before = desktop()
    alert = desktop()                               # an OK/Cancel alert that leaves the menus alone
    dialog(alert, 1024, 310, 148, buttons=2)
    res = powerdlg.find_power_dialog(frame(before), frame(alert))
    assert not res["found"] and res["new_box"]
    up = desktop(titles=DIM)
    dialog(up, 1024, 310, 148)
    res = powerdlg.find_power_dialog(frame(up), frame(up))
    assert not res["found"] and res["reason"] == "box-was-there"


def test_partly_drawn_dialog_is_not_yet_the_dialog():
    before = desktop()
    after = desktop()
    dialog(after, 1024, 310, 148, buttons=0)       # frame and text, buttons not drawn yet
    assert not powerdlg.find_power_dialog(frame(before), frame(after))["found"]


def test_screen_changed_ignores_the_menu_bar_clock():
    a = desktop()
    b = desktop()
    rect(b, 1024, 900, 4, 60, 12, BLACK)
    assert powerdlg.screen_changed(frame(a), frame(b)) == 0.0
    rect(b, 1024, 500, 500, 16, 16, BLACK)          # the watch cursor
    assert 0 < powerdlg.screen_changed(frame(a), frame(b)) < 0.01
    assert powerdlg.screen_changed(frame(a), frame(desktop(800, 600), 800, 600)) == 1.0


def test_guest_load_peak_and_busy():
    load = powerdlg.GuestLoad(None)
    load.source = "tcg"
    t, v = 100.0, 0
    for i in range(12):                 # 3 s busy (100 %), then 3 s idle (20 %)
        load.samples.append((t, v))
        v += int((1.0 if i < 6 else 0.2) * 0.5e9)
        t += 0.5
    now = load.samples[-1][0]
    assert abs(load.busy(now) - 0.2) < 0.01
    assert load.peak(now, 1.5) < 0.6
    assert load.peak(now, 4.0) > 0.9
    assert load.peak(now + 5, 1.5) is None        # stale samples: unknown
    assert load.peak(now + 0.45, 1.5) is not None  # sampled every 0.5 s: a fresh window between samples
    assert powerdlg.GuestLoad(None).peak(now, 1.5) is None


def test_guest_load_reads_this_process(tmp_path):
    import os
    import time
    load = powerdlg.GuestLoad(os.getpid())
    load.sample(time.monotonic())
    end = time.monotonic() + 0.3
    while time.monotonic() < end:
        pass
    load.sample(time.monotonic())
    if load.samples:                    # Linux (/proc); elsewhere nothing is measurable
        assert load.source == "process" and load.busy(time.monotonic(), 0.2) > 0.5


def test_dimmed_menus_count_as_something_appeared():
    """A dialog drawn in another Appearance theme is not recognized, but its dimmed menu titles
    still stop hostd from pressing the power key again (which would close it)."""
    before = desktop()
    after = desktop(titles=DIM)
    dialog(after, 1024, 310, 148, body=b"\x40\x60\xa0")       # a dark blue theme
    res = powerdlg.find_power_dialog(frame(before), frame(after))
    assert res["menus_dimmed"] is True and res["new_box"]


def window(px, w, x0, y0, x1, y1, active=True):
    """A Finder window: black frame, Platinum title bar (stripes when active), white inside."""
    rect(px, w, x0, y0, x1 - x0, y1 - y0, BLACK)
    rect(px, w, x0 + 1, y0 + 1, x1 - x0 - 2, y1 - y0 - 2, GRAY)
    if active:
        for y in range(y0 + 4, y0 + 16, 2):
            rect(px, w, x0 + 4, y, x1 - x0 - 8, 1, b"\x88\x88\x88")
    rect(px, w, x0 + 5, y0 + 30, x1 - x0 - 10, y1 - y0 - 50, WHITE)


def test_dialog_over_a_window_whose_edge_shares_its_rows():
    """Mac OS's dialog over the Macintosh HD window, whose bottom edge lies on the dialog's
    bottom row (the lab's case): the merged line must not hide the dialog."""
    before = desktop()
    window(before, 1024, 8, 26, 423, 251)
    after = desktop(titles=DIM)
    window(after, 1024, 8, 26, 423, 251, active=False)
    dialog(after, 1024, 310, 148)
    res = powerdlg.find_power_dialog(frame(before), frame(after))
    assert res["found"] and res["box"][:2] == (310, 148)
    top = desktop(titles=DIM)                     # a window whose top edge is the dialog's top row
    window(top, 1024, 100, 148, 700, 400, active=False)
    dialog(top, 1024, 350, 148)
    assert powerdlg.find_power_dialog(frame(desktop()), frame(top))["found"]


def aqua_dialog(px, w, x0, y0):
    """The dialog as the Aqua Theme Beta Appearance theme draws it (measured in the lab):
    a 1-pixel light-gray frame, a white pinstriped body, light buttons."""
    rect(px, w, x0, y0, 400, 100, b"\xad\xad\xad")
    for y in range(y0 + 1, y0 + 99):
        rect(px, w, x0 + 1, y, 398, 1, WHITE if y % 2 else b"\xf7\xf7\xf7")
    for line in range(2):
        for i in range(28):
            rect(px, w, x0 + 84 + i * 10, y0 + 20 + line * 16, 6, 9, BLACK)
    for i in range(4):
        rect(px, w, x0 + 24 + i * 94, y0 + 64, 80, 20, b"\x9a\x9a\x9a")
        rect(px, w, x0 + 25 + i * 94, y0 + 65, 78, 18, b"\xe0\xe0\xe8")


def test_another_theme_is_a_modal_change_not_a_platinum_dialog():
    back = b"\x7d\x99\xd7"
    before = desktop(back=back)
    after = desktop(back=back, titles=b"\x8c\x8c\x8c")
    aqua_dialog(after, 1024, 312, 152)
    assert not powerdlg.find_power_dialog(frame(before), frame(after))["found"]
    mc = powerdlg.modal_change(frame(before), frame(after))
    assert mc["modal"] and mc["menus"]["changed"]
    x0, y0, x1, y1 = mc["box"]
    assert abs(x0 - 312) <= 8 and abs(y0 - 152) <= 2 and abs(x1 - 712) <= 8 and abs(y1 - 252) <= 2


def test_menu_state():
    """Recoloured titles (disabled menus) change state; the same titles, other titles (another
    application) or no change do not."""
    enabled = desktop()
    assert powerdlg.menu_state(frame(enabled), frame(desktop()))["changed"] is False
    assert powerdlg.menu_state(frame(enabled), frame(desktop(titles=DIM)))["changed"] is True
    other = desktop()
    rect(other, 1024, 40, 4, 400, 12, WHITE)
    for k in range(9):                      # other titles at other places
        rect(other, 1024, 40 + k * 45, 5, 22, 9, BLACK)
    assert powerdlg.menu_state(frame(enabled), frame(other))["changed"] is False
    assert powerdlg.menu_state(frame(enabled), frame(desktop(800, 600), 800, 600)) is None


def test_menu_state_with_button_titles():
    """Hi-Tech draws each title as a button, the buttons adjoining, on a bar shaded row by row.
    The most common colour among the titles is the buttons' fill in the Finder but the bar's
    under a dialog (disabled buttons are not one colour), so the titles' own background finds
    other "ink" in the two frames.  The whole row's background (the bar) decides (lab colours,
    finderdet)."""
    def bar(enabled):
        px = bytearray(BLUE * (1024 * 768))
        for y in range(22):
            rect(px, 1024, 0, y, 1024, 1, bytes([49 + 5 * y] * 3))
        x = 8
        for wd in (26, 32, 32, 38, 58, 46, 31):          # the Apple menu and six titles
            for y in range(1, 21):
                rect(px, 1024, x, y, wd, 1, bytes([160 - 5 * y if enabled else 41] * 3))
                if not enabled:                          # disabled: a fill of two close shades
                    for xx in range(x, x + wd, 2):
                        rect(px, 1024, xx, y, 1, 1, b"\x2c\x2c\x2c")
            for k in range(x + 6, x + wd - 6, 3):
                rect(px, 1024, k, 6, 2, 9, b"\xef\xef\xef" if enabled else b"\x84\x84\x84")
            x += wd
        return px
    old = powerdlg._menu_state(frame(bar(True)), frame(bar(False)), False)
    assert old["changed"] is False and old["same_titles"] < 0.7
    ms = powerdlg.menu_state(frame(bar(True)), frame(bar(False)))
    assert ms["changed"] is True and ms["background"] == "row"
    assert powerdlg.menu_state(frame(bar(True)), frame(bar(True)))["changed"] is False


def test_finder_drawing_and_a_window_are_no_modal_change():
    before = desktop()
    after = desktop()
    window(after, 1024, 300, 150, 720, 450)
    assert not powerdlg.modal_change(frame(before), frame(after))["modal"]


def test_platinum_black_and_white_variation():
    """Platinum's Black & White variation draws the dialog white: still the dialog (four
    buttons, dimmed menus), while a white box without dimmed menus is not."""
    before = desktop()
    after = desktop(titles=DIM)
    dialog(after, 1024, 310, 148, body=WHITE)
    assert powerdlg.find_power_dialog(frame(before), frame(after))["found"]
    undimmed = desktop()
    dialog(undimmed, 1024, 310, 148, body=WHITE)
    assert not powerdlg.find_power_dialog(frame(before), frame(undimmed))["found"]


def test_black_and_white_screen_of_a_68k_mac():
    """Mac OS 8.1 on a 68k Mac at 1 bit (VM165): dimmed titles are a 50 % pattern and Help stays
    black, the alert is white with three buttons (Restart, Cancel, Shut Down).  Still the dialog;
    a white box with undimmed titles is still not."""
    def mono_desktop(dimmed):
        px = desktop(back=WHITE)
        if dimmed:
            for i in range(5):                     # all titles but Help get the gray pattern
                for k in range(5):
                    for yy in range(5, 14):
                        for xx in range(5):
                            if (xx + yy) % 2:
                                rect(px, 1024, 42 + i * 55 + k * 8 + xx, yy, 1, 1, WHITE)
        return px
    before = mono_desktop(False)
    after = mono_desktop(True)
    dialog(after, 1024, 310, 148, buttons=3, body=WHITE)
    assert powerdlg.monochrome(frame(after))
    res = powerdlg.find_power_dialog(frame(before), frame(after))
    assert res["found"] and res["buttons"] == 3 and res["menus_dimmed"] is True
    undimmed = mono_desktop(False)
    dialog(undimmed, 1024, 310, 148, buttons=3, body=WHITE)
    assert not powerdlg.find_power_dialog(frame(before), frame(undimmed))["found"]
    assert not powerdlg.monochrome(frame(desktop()))
