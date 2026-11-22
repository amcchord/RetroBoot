"""Graphics card (retroboot/graphics.py, research/gfx): std-VGA stays the
default and its arguments are unchanged; "3D acceleration (ATI Radeon)" is a
visible choice that is offered only with a capable package, a working host GL
(libEGL, a render node, a GLES 3 context) and ATI's drivers on the Mac's disk,
takes effect at the next Mac OS start, replaces the VGA arguments, starts Mac
OS at ATI's 640 x 480 and asks RetroBoot Display for the largest ATI screen
size that fits, or, with a package whose emulated monitor reports ati-vga's
xres/yres in its EDID (ati-edid-modes), for exactly the standard card's Mac
screen; otherwise a notice says why Mac OS got the standard card."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import helpers  # noqa: F401  (sys.path)
from retroboot import graphics, schema
from retroboot.config import Paths
from retroboot.machines import LaunchOptions, MachineRegistry, build_argv

STD = ("-vga std -global VGA.vgamem_mb=64 -global VGA.host-resize=on -global VGA.packed-lowbpp=on "
       "-global VGA.hardware-cursor=off -global VGA.untracked-vram=off")
ATI = "-vga none -device ati-vga,model=r100,vgamem_mb=32,romfile=,guest_hwcursor=on"
DISPLAY = {"mode": [1920, 1080, 60], "suggested_scale": 2}


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


GL_OK = {"ok": True, "renderer": "AMD Radeon 890M (radeonsi, gfx1150)", "software": False,
         "render_nodes": ["/dev/dri/renderD128"]}


def setup(capable=True, gl=GL_OK, edid=False):
    graphics.set_host_gl(gl)
    td, root = helpers.scratch()
    pkg = helpers.make_package(root)
    if capable:
        m = json.loads((pkg / "manifest.json").read_text())
        m["features"] = ["display-retroboot", graphics.FEATURE] + ([graphics.EDID_FEATURE] if edid else [])
        (pkg / "manifest.json").write_text(json.dumps(m))
    helpers.make_machine(root)
    paths = Paths(root)
    paths.ensure()
    return td, paths, MachineRegistry(paths).get("mac-os-9")


def test_setting_is_visible_and_restart_class():
    s = schema.BY_KEY["display.graphics"]
    assert s.default == "standard" and s.choices == ("standard", "ati-radeon") and s.hidden_choices == ()
    assert s.choice_labels["ati-radeon"] == "3D acceleration (ATI Radeon)"
    assert s.apply == schema.GUEST_RESTART
    assert schema.defaults()["display.graphics"] == "standard"


def _ctx(available, reason=None, value="standard", screen=(1024, 768)):
    return schema.ValidationContext(values={"display.graphics": value}, graphics={
        "available": available, "reason": reason,
        "message": graphics.AVAILABILITY.get(reason), "ati_screen": list(screen) if screen else None})


def test_choice_disabled_with_the_reason_when_unavailable():
    d = schema.BY_KEY["display.graphics"].describe(_ctx(False, "guest-software"))
    ati = next(c for c in d["choices"] if c["value"] == "ati-radeon")
    assert ati["disabled"] is True and "Mac OS 9.2.1 CD" in ati["reason"]
    assert d["available"] is False and d["unavailable_reason"] == ati["reason"]
    # already on (e.g. the host GL went away): the setting stays usable so it can be turned off
    d = schema.BY_KEY["display.graphics"].describe(_ctx(False, "host-gl", value="ati-radeon"))
    assert d["available"] is True and next(c for c in d["choices"] if c["value"] == "ati-radeon")["disabled"]
    # available: the help names the Mac screen size it gives
    d = schema.BY_KEY["display.graphics"].describe(_ctx(True))
    assert d["available"] is True and "1024 × 768" in d["help"]
    assert not any(c.get("disabled") for c in d["choices"])


def test_turning_it_on_is_refused_when_unavailable():
    cur = schema.defaults()
    try:
        schema.validate_changes(cur, {"display.graphics": "ati-radeon"}, _ctx(False, "host-gl"))
        raise AssertionError("accepted")
    except schema.ValidationError as e:
        assert "3D" in e.fields["display.graphics"]
    assert schema.validate_changes(cur, {"display.graphics": "ati-radeon"}, _ctx(True))["display.graphics"] \
        == "ati-radeon"
    # switching to a Mac that has ATI's software and turning it on in one change
    ctx = _ctx(False, "guest-software")
    ctx.graphics["by_machine"] = {"v4": {"available": True, "reason": None, "message": None},
                                  "v2": {"available": False, "reason": "guest-software", "message": "no ATI"}}
    ctx.machines = None
    both = schema.validate_changes(cur, {"machine.active": "v4", "display.graphics": "ati-radeon"}, ctx)
    assert both["display.graphics"] == "ati-radeon" and both["machine.active"] == "v4"
    try:
        schema.validate_changes(cur, {"machine.active": "v2", "display.graphics": "ati-radeon"}, ctx)
        raise AssertionError("accepted")
    except schema.ValidationError as e:
        assert e.fields["display.graphics"] == "no ATI"
    # a stored "on" never blocks other changes, and it can always be turned off
    on = dict(cur, **{"display.graphics": "ati-radeon"})
    assert schema.validate_changes(on, {"input.pointer_speed": 1.5}, _ctx(False, "host-gl"))["input.pointer_speed"] == 1.5
    assert schema.validate_changes(on, {"display.graphics": "standard"}, _ctx(False, "host-gl"))


def test_resolution_policy_largest_ati_mode_that_fits():
    def screen(w, h, ps="auto", gs="auto"):
        return graphics.mac_screen({"mode": [w, h, 60]}, {"display.pixel_scale": ps, "display.guest_size": gs})
    assert screen(1920, 1080) == (640, 480)          # auto 2x: 960 x 540 -> 640 x 480
    assert screen(1920, 1080, "1") == (1920, 1080)   # 1x: an exact ATI mode
    assert screen(1280, 720) == (832, 624)           # GPD MicroPC: 1024 x 768 is too tall
    assert screen(2160, 1440) == (832, 624)          # N5105 at 2x: 1080 x 720
    assert screen(2560, 1600) == (1280, 800)         # 2x
    assert screen(3840, 2160, "1") == (1920, 1440)
    assert screen(1366, 768) == (1024, 768)          # 1x: 1280 x 800 is too tall
    assert screen(1920, 1080, "1", "1024x768") == (1024, 768)   # every classic size is an ATI mode
    assert screen(1920, 1080, "2", "1152x870") == (1152, 870)
    assert screen(1080, 1920) == (640, 480)          # portrait: ATI has landscape modes only
    assert graphics.ati_size((600, 400)) == graphics.STARTUP_SIZE


def test_resolution_policy_exact_with_the_edid_package():
    """ati-edid-modes: the card's monitor reports the standard card's Mac screen as its
    EDID detailed timing, so Mac OS gets exactly that size, widescreen included."""
    def screen(w, h, ps="auto", gs="auto"):
        return graphics.mac_screen({"mode": [w, h, 60]}, {"display.pixel_scale": ps, "display.guest_size": gs},
                                   exact=True)
    assert screen(1920, 1080) == (960, 540)          # auto 2x (the Ryzen): was 640 x 480
    assert screen(2160, 1440) == (1080, 720)         # N5105 at 2x: was 832 x 624
    assert screen(1280, 720) == (1280, 720)          # GPD MicroPC: was 832 x 624
    assert screen(1366, 768) == (1360, 768)          # the standard card's 8-pixel width
    assert screen(2560, 1600) == (1280, 800)
    assert screen(1920, 1080, "1") == (1920, 1080)
    assert screen(1920, 1200, "1") == (1920, 1200)
    assert screen(1920, 1080, "1", "1024x768") == (1024, 768)
    assert screen(1920, 1080, "2", "1152x870") == (1152, 870)
    # beyond ATI's largest mode, or portrait: the largest fixed mode, as before
    assert screen(3840, 2160, "1") == (1920, 1440)
    assert screen(2560, 1440, "1") == (1920, 1440)
    assert screen(1080, 1920) == (640, 480)
    assert graphics.ati_size((600, 400), exact=True) == graphics.STARTUP_SIZE
    assert graphics.exact_size((1366, 768)) == (1360, 768) and graphics.exact_size((768, 1024)) is None
    assert graphics.exact_size((1928, 1080)) is None and graphics.exact_size((632, 480)) is None
    assert screen(1680, 1050, "1") == (1680, 1050) and screen(1920, 1440, "1") == (1920, 1440)


def test_edid_refresh_keeps_the_pixel_clock_acceptable():
    """ATI's driver ignored the whole EDID for 1680 x 1050 at 75 Hz (QEMU's default:
    184.7 MHz) and at 70 Hz (172.4 MHz), and took it at 66 Hz (162.6 MHz) and 60 Hz:
    the monitor's refresh is the highest of 75/60/50 Hz within the limit."""
    clk = graphics.edid_clock_khz
    assert clk(1680, 1050, 75) == 184720 and clk(1680, 1050, 60) == 147780
    assert clk(1080, 720, 75) == 81460 and clk(1920, 1080, 50) == 144760   # QEMU's own values
    assert graphics.edid_refresh(1080, 720) == 75 and graphics.edid_refresh(1600, 900) == 75
    assert graphics.edid_refresh(1680, 1050) == 60 and graphics.edid_refresh(1920, 1080) == 50
    assert graphics.edid_refresh(1920, 1200) == 50 and graphics.edid_refresh(1920, 1440) is None
    assert graphics.exact_size((1920, 1440)) is None              # the fixed 1920 x 1440 instead
    assert graphics.device_args(graphics.ATI, (1680, 1050))[-1].endswith(",xres=1680,yres=1050,refresh_rate=60000")
    assert graphics.device_args(graphics.ATI, (1920, 1440)) == graphics.ATI_ARGS


def test_edid_package_gives_the_card_the_mac_screen():
    td, paths, m = setup(edid=True)
    try:
        m.update_meta(graphics={"ati_software": "present"})
        settings = dict(schema.defaults(), **{"display.graphics": "ati-radeon"})
        assert graphics.edid_modes(paths.qemu_package)
        argv = build_argv(m, settings, paths, DISPLAY)
        assert ATI + ",xres=960,yres=540,refresh_rate=75000" in " ".join(argv)
        assert "retroboot-display=960x540" in argv and argv[argv.index("-g") + 1] == "640x480x32"
        assert sum(1 for x in argv if x.startswith("ati-vga")) == 1
        # the standard card's arguments are untouched by the feature
        assert STD in " ".join(build_argv(m, schema.defaults(), paths, DISPLAY))
    finally:
        td.cleanup()
    td, paths, m = setup()                           # no ati-edid-modes: the fixed modes, no xres
    try:
        m.update_meta(graphics={"ati_software": "present"})
        settings = dict(schema.defaults(), **{"display.graphics": "ati-radeon"})
        argv = build_argv(m, settings, paths, DISPLAY)
        assert not graphics.edid_modes(paths.qemu_package)
        assert argv[argv.index("-device", argv.index("-vga")) + 1] == graphics.ATI_DEVICE
        assert not any("xres=" in x for x in argv) and "retroboot-display=640x480" in argv
    finally:
        td.cleanup()


def test_default_keeps_the_std_vga_arguments():
    td, paths, m = setup()
    try:
        argv = " ".join(build_argv(m, schema.defaults(), paths, DISPLAY))
        assert STD in argv and "ati-vga" not in argv
    finally:
        td.cleanup()


def test_direct_framebuffer_is_explicit_and_per_machine():
    td, paths, m = setup()
    try:
        baseline = build_argv(m, schema.defaults(), paths, DISPLAY)
        m.meta["template"] = {"directFramebuffer": False}
        assert build_argv(m, schema.defaults(), paths, DISPLAY) == baseline
        m.meta["template"]["directFramebuffer"] = True
        enabled = build_argv(m, schema.defaults(), paths, DISPLAY)
        expected = baseline.copy()
        expected[expected.index("VGA.untracked-vram=off")] = "VGA.untracked-vram=on"
        assert enabled == expected
        # A different machine and subsequent default calls retain dirty tracking.
        helpers.make_machine(paths.root, mid="other")
        other = MachineRegistry(paths).get("other")
        assert STD in " ".join(build_argv(other, schema.defaults(), paths, DISPLAY))
        assert "VGA.untracked-vram=off" in graphics.STANDARD_ARGS
        assert graphics.device_args(graphics.STANDARD) == graphics.STANDARD_ARGS
    finally:
        td.cleanup()


def test_direct_framebuffer_does_not_change_ati_arguments():
    td, paths, m = setup()
    try:
        m.update_meta(graphics={"ati_software": "present"})
        settings = dict(schema.defaults(), **{"display.graphics": "ati-radeon"})
        baseline = build_argv(m, settings, paths, DISPLAY)
        m.meta["template"] = {"directFramebuffer": True}
        assert build_argv(m, settings, paths, DISPLAY) == baseline
        assert graphics.device_args(graphics.ATI, direct_framebuffer=True) == graphics.ATI_ARGS
    finally:
        td.cleanup()


def test_ati_needs_package_and_guest_software():
    td, paths, m = setup()
    try:
        settings = dict(schema.defaults(), **{"display.graphics": "ati-radeon"})
        # not checked yet: std-VGA, and status says why
        assert graphics.effective(settings, m, paths.qemu_package) == graphics.STANDARD
        assert graphics.why_not(settings, m, paths.qemu_package) == "guest-software"
        assert STD in " ".join(build_argv(m, settings, paths, DISPLAY))
        m.update_meta(graphics={"ati_software": "absent"})
        assert STD in " ".join(build_argv(m, settings, paths, DISPLAY))
        m.update_meta(graphics={"ati_software": "present"})
        argv = " ".join(build_argv(m, settings, paths, DISPLAY))
        assert ATI in argv and "-vga std" not in argv and "VGA.host-resize" not in argv
        # guest prep's report counts as well
        m.update_meta(graphics={}, prep={"result": "applied", "report": {"ati_software": "present"}})
        assert graphics.effective(settings, m, paths.qemu_package) == graphics.ATI
    finally:
        td.cleanup()


def test_ati_without_capable_package_falls_back():
    td, paths, m = setup(capable=False)
    try:
        m.update_meta(graphics={"ati_software": "present"})
        settings = dict(schema.defaults(), **{"display.graphics": "ati-radeon"})
        assert graphics.why_not(settings, m, paths.qemu_package) == "package"
        assert STD in " ".join(build_argv(m, settings, paths, DISPLAY))
    finally:
        td.cleanup()


def test_ati_needs_a_working_host_gl():
    td, paths, m = setup(gl={"ok": False, "reason": "no-libegl", "render_nodes": []})
    try:
        m.update_meta(graphics={"ati_software": "present"})
        settings = dict(schema.defaults(), **{"display.graphics": "ati-radeon"})
        assert not graphics.capable(paths.qemu_package)
        assert graphics.why_not(settings, m, paths.qemu_package) == "host-gl"
        assert STD in " ".join(build_argv(m, settings, paths, DISPLAY))
        graphics.set_host_gl(None)      # not probed yet: never the card
        assert graphics.effective(settings, m, paths.qemu_package) == graphics.STANDARD
    finally:
        td.cleanup()


def _fake_run(stdout):
    def run(argv, **kw):
        assert argv[1:3] == ["-I", "-c"] and "eglGetPlatformDisplay" in argv[3]
        assert kw["env"]["EGL_PLATFORM"] == "surfaceless"
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)
    return run


def test_probe_host_gl(tmp_path, monkeypatch):
    dri = tmp_path / "dri"
    dri.mkdir()
    ok = json.dumps({"ok": True, "renderer": "AMD Radeon Graphics (radeonsi, gfx1150)", "version": "OpenGL ES 3.2"})
    monkeypatch.setattr(graphics.subprocess, "run", _fake_run(ok + "\n"))
    # no render node: refused unless the lab allows software rendering
    info = graphics.probe_host_gl(dev=str(dri))
    assert info["ok"] is False and info["reason"] == "no-render-node" and info["renderer"].startswith("AMD")
    (dri / "renderD128").touch()
    info = graphics.probe_host_gl(dev=str(dri))
    assert info["ok"] is True and info["software"] is False and info["render_nodes"] == [str(dri / "renderD128")]
    # a render node, but Mesa fell back to llvmpipe (e.g. no access to the node)
    sw = json.dumps({"ok": True, "renderer": "llvmpipe (LLVM 19.1.7, 256 bits)"})
    monkeypatch.setattr(graphics.subprocess, "run", _fake_run(sw))
    info = graphics.probe_host_gl(dev=str(dri))
    assert info["ok"] is False and info["reason"] == "software-only" and info["software"] is True
    assert graphics.probe_host_gl(dev=str(dri), allow_software=True)["ok"] is True
    # libEGL missing, or the probe dies
    monkeypatch.setattr(graphics.subprocess, "run", _fake_run(json.dumps({"ok": False, "reason": "no-libegl"})))
    assert graphics.probe_host_gl(dev=str(dri))["reason"] == "no-libegl"
    monkeypatch.setattr(graphics.subprocess, "run", _fake_run(""))
    assert graphics.probe_host_gl(dev=str(dri))["reason"] == "probe-failed"

    def boom(argv, **kw):
        raise graphics.subprocess.TimeoutExpired(argv, 20)
    monkeypatch.setattr(graphics.subprocess, "run", boom)
    assert graphics.probe_host_gl(dev=str(dri))["reason"] == "probe-failed"


def test_ati_turns_the_accelerated_boot_off_and_never_takes_the_cd_route():
    td, paths, m = setup()
    try:
        m.update_meta(graphics={"ati_software": "present"})
        settings = dict(schema.defaults(), **{"display.graphics": "ati-radeon",
                                              "machine.boot_speed": "accelerated"})
        argv = " ".join(build_argv(m, settings, paths, DISPLAY))
        assert ATI in argv and "-icount" not in argv and "classicmac-boot-handoff" not in argv
        cd = paths.root / "install.iso"
        cd.write_bytes(b"cd")
        argv = " ".join(build_argv(m, settings, paths, DISPLAY, LaunchOptions(boot_cd=cd)))
        assert STD in argv and "ati-vga" not in argv
    finally:
        td.cleanup()


def test_ati_starts_at_640x480_and_asks_for_the_chosen_size():
    td, paths, m = setup()
    try:
        m.update_meta(graphics={"ati_software": "present"})
        settings = dict(schema.defaults(), **{"display.graphics": "ati-radeon", "display.pixel_scale": "1"})
        argv = build_argv(m, settings, paths, DISPLAY)
        assert argv[argv.index("-g") + 1] == "640x480x32"
        assert "retroboot-display=1920x1080" in argv and argv[argv.index("retroboot-display=1920x1080") - 1] \
            == "-prom-env"
        std = build_argv(m, dict(settings, **{"display.graphics": "standard"}), paths, DISPLAY)
        assert std[std.index("-g") + 1] == "1920x1080x32" and not any("retroboot-display" in a for a in std)
    finally:
        td.cleanup()


class _Engine:
    def __init__(self):
        self.pushed = 0
        self.boot = []

    async def push_display(self):
        self.pushed += 1
        return True

    async def update_boot_display(self, why=""):
        self.boot.append(why)
        return True


class _Startup:
    """startup.StartupProgress: startup_config (happy_mac follows the card)."""
    def __init__(self):
        self.pushed = 0

    async def push_config(self):
        self.pushed += 1


def _app(paths, m, applied, seen):
    events = []
    app = SimpleNamespace(
        paths=paths, settings=SimpleNamespace(applied=applied, desired=applied),
        guestprep=SimpleNamespace(ati_software_present=lambda disk: seen.append(disk) or True),
        lifecycle=SimpleNamespace(event=lambda kind, **f: events.append((kind, f))),
        supervisor=SimpleNamespace(active_machine=lambda: m, active=False),
        apply_engine=_Engine(), presenter=SimpleNamespace(display={"mode": [1280, 720, 60]}), startup=_Startup())
    return app, events


def test_prepare_launch_records_the_check_and_public():
    td, paths, m = setup()
    try:
        seen = []
        app, events = _app(paths, m, {"display.graphics": "ati-radeon"}, seen)
        g = graphics.Graphics(app)
        run(g.prepare_launch(m))
        assert seen == [m.disk] and m.meta["graphics"] == {"ati_software": "present"}
        assert events == [("graphics_check", {"machine": m.id, "ati_software": "present"})]
        pub = g.public()
        assert {k: pub[k] for k in ("capable", "requested", "effective", "guest_software", "reason",
                                    "available")} == \
            {"capable": True, "requested": "ati-radeon", "effective": "ati-radeon",
             "guest_software": "present", "reason": None, "available": True}
        assert pub["experimental"] is False and pub["host_gl"]["ok"] is True and pub["host_gl"]["render_nodes"] == 1
        assert pub["mac_screen"] == [832, 624] and pub["startup_screen"] == [640, 480]
        assert pub["ati_screen"] == [832, 624]
        assert g.notices() == []
        # the card of the next launch: the presenter's startup geometry and GRUB's hint
        assert g.startup_guest_size() == [640, 480] and app.apply_engine.pushed == 1
        assert app.startup.pushed == 1      # startup_config: Mac OS's Happy Mac comes late with the card
        assert graphics.read_hint(paths.state) == {"card": "ati-radeon", "machine": m.id,
                                                   "startup_guest_size": "640x480"}
        assert app.apply_engine.boot == ["display.graphics"]
        run(g.prepare_launch(m))                         # unchanged: no pushes
        assert app.apply_engine.pushed == 1 and app.apply_engine.boot == ["display.graphics"]
        graphics.set_host_gl({"ok": False, "reason": "no-render-node", "render_nodes": []})
        assert g.public()["reason"] == "host-gl" and g.public()["effective"] == "standard"
        assert g.public()["mac_screen"] == [1280, 720] and g.public()["ati_screen"] == [832, 624]
        assert g.availability()["reason"] == "host-gl"
        (n,) = g.notices()
        assert n["id"] == "graphics-fallback" and "graphics (OpenGL ES through EGL)" in n["message"]
        graphics.set_host_gl(None)                       # still probing: the last launch's card stands in
        assert g.next_card() == "ati-radeon" and g.availability()["reason"] == "checking"
        graphics.set_host_gl(GL_OK)
        # not requested: the disk is still checked (the panel and web offer it only then)
        app.settings.applied = {"display.graphics": "standard"}
        run(g.prepare_launch(m))
        assert len(seen) == 3 and g.startup_guest_size() is None and app.apply_engine.pushed == 2
        assert app.startup.pushed == 2
        assert graphics.read_hint(paths.state)["card"] == "standard"
    finally:
        td.cleanup()


def test_availability_reasons():
    td, paths, m = setup()
    try:
        app, _ = _app(paths, m, {"display.graphics": "standard"}, [])
        g = graphics.Graphics(app)
        assert g.availability()["reason"] == "guest-software"
        m.update_meta(graphics={"ati_software": "present"})
        assert g.availability() == {"available": True, "reason": None, "message": None}
        ctx = g.ui_context()
        assert ctx["available"] and ctx["ati_screen"] == [832, 624]
        app.supervisor.active_machine = lambda: None
        assert g.availability()["reason"] == "no-machine"
    finally:
        td.cleanup()
    td, paths, m = setup(capable=False)
    try:
        app, _ = _app(paths, m, {}, [])
        assert graphics.Graphics(app).availability()["reason"] == "package"
    finally:
        td.cleanup()


def test_status_reports_the_mac_screen_mac_os_has():
    """fleet6: status.graphics.mac_screen said 832 x 624 (the ATI size) on laptops whose Mac OS
    ran on the standard card at 1080 x 720.  It is now the screen Mac OS has; ati_screen is the
    size the card would give it."""
    td, paths, m = setup()
    try:
        m.update_meta(graphics={"ati_software": "present"})
        app, _ = _app(paths, m, {"display.graphics": "standard"}, [])
        app.presenter.display = {"mode": [2160, 1440, 60], "suggested_scale": 2}    # the N5105 panel
        g = graphics.Graphics(app)
        pub = g.public()
        assert pub["mac_screen"] == [1080, 720] and pub["ati_screen"] == [832, 624]    # Mac OS shut down
        app.supervisor.active, app.supervisor.guest_surface = True, [1080, 720]
        g.launched("standard")
        assert g.public()["mac_screen"] == [1080, 720]
        app.supervisor.guest_surface = [800, 600]         # a size chosen in Monitors
        assert g.public()["mac_screen"] == [800, 600]
        # 3D on: the size RetroBoot Display sets, the surface once Mac OS shows it
        app.settings.applied = {"display.graphics": "ati-radeon"}
        app.supervisor.active, app.supervisor.guest_surface = False, None
        assert g.public()["mac_screen"] == [832, 624]
        g.launched("ati-radeon")
        app.supervisor.active = True
        assert g.public()["mac_screen"] == [832, 624]
        app.supervisor.guest_surface = [640, 480]           # the extension parade before the switch
        assert g.public()["mac_screen"] == [640, 480]
        app.presenter.display = None                         # no display yet: unknown
        app.supervisor.active = False
        assert g.public()["mac_screen"] is None and g.public()["ati_screen"] is None
    finally:
        td.cleanup()


def test_start_screen_and_the_control_strip():
    """Before each launch the Control Strip's saved place gets the screen Mac OS will start with:
    the standard card's -g size, or the ATI size RetroBoot Display sets."""
    td, paths, m = setup()
    try:
        m.update_meta(graphics={"ati_software": "present"})
        app, events = _app(paths, m, {"display.graphics": "standard"}, [])
        app.presenter.display = {"mode": [2160, 1440, 60], "suggested_scale": 2}
        calls = []
        result = {"result": "moved", "from": [832, 624, 601, 1], "to": [1080, 720, 697, 1], "seconds": 0.02}

        async def control_strip(image, size):
            calls.append((image, tuple(size)))
            return dict(result)
        app.guestprep.control_strip = control_strip
        g = graphics.Graphics(app)
        run(g.place_control_strip(m))
        assert calls == [(m.disk, (1080, 720))]
        assert events == [("control_strip", {"machine": m.id, "screen": "1080x720", **result})]
        app.settings.applied = {"display.graphics": "ati-radeon"}
        result = {"result": "unchanged", "seconds": 0.01}
        run(g.place_control_strip(m))
        assert calls[-1] == (m.disk, (832, 624)) and len(events) == 1           # unchanged: no event
        app.settings.applied = {"display.graphics": "standard", "display.pixel_scale": "1"}
        run(g.place_control_strip(m))
        assert calls[-1] == (m.disk, (2160, 1440))
        app.settings.applied = {"display.graphics": "standard", "display.guest_size": "1024x768"}
        run(g.place_control_strip(m))
        assert calls[-1] == (m.disk, (1024, 768))
        app.presenter.display = {}                                              # no display mode yet
        run(g.place_control_strip(m))
        assert len(calls) == 4
        run(g.place_control_strip(None))
        del app.guestprep.control_strip                                         # an older hostd adapter
        app.presenter.display = {"mode": [1280, 720, 60]}
        run(g.place_control_strip(m))
        assert len(calls) == 4
    finally:
        td.cleanup()


def test_guestprep_control_strip_adapter(tmp_path):
    from retroboot.guestprep import GuestPrep

    class Mod:
        def __init__(self, res):
            self.res, self.calls = res, []

        def fit(self, image, size):
            self.calls.append((image, size))
            if isinstance(self.res, Exception):
                raise self.res
            return dict(self.res)
    prep = helpers.FakePrepModule()
    gp = GuestPrep(tmp_path, module=prep)
    assert run(gp.control_strip(tmp_path / "d.img", (1080, 720)))["result"] == "unavailable"   # test fake prep
    gp.control_strip_module = Mod({"result": "moved", "from": [832, 624, 601, 1], "to": [1080, 720, 697, 1]})
    res = run(gp.control_strip(tmp_path / "d.img", (1080, 720)))
    assert res["result"] == "moved" and res["to"] == [1080, 720, 697, 1] and "seconds" in res
    assert gp.control_strip_module.calls == [(str(tmp_path / "d.img"), (1080, 720))]
    gp.control_strip_module = Mod(OSError("disk busy"))
    res = run(gp.control_strip(tmp_path / "d.img", (1080, 720)))
    assert res["result"] == "failed" and "disk busy" in res["error"]


def test_probe_waits_for_a_late_render_node(tmp_path, monkeypatch):
    """amdgpu on the Ryzen registered renderD128 2 ms after hostd's first probe: hostd waits
    for the node and probes again instead of settling on 'no-render-node' for the boot."""
    dri = tmp_path / "dri"
    dri.mkdir()
    calls = []

    def fake_probe(run_as, allow_software, dev):
        nodes = graphics.render_nodes(dev)
        calls.append(len(nodes))
        if not nodes:
            return {"ok": False, "reason": "no-render-node", "render_nodes": [], "renderer": "llvmpipe"}
        return {"ok": True, "renderer": "AMD Radeon 890M (radeonsi)", "render_nodes": nodes, "software": False}
    monkeypatch.setattr(graphics, "probe_host_gl", fake_probe)
    events = []
    app = SimpleNamespace(cfg=SimpleNamespace(run_as_user=None, graphics_allow_software=False,
                                              graphics_dri_dir=str(dri)),
                          lifecycle=SimpleNamespace(event=lambda kind, **f: events.append(kind)))
    g = graphics.Graphics(app)

    async def scenario():
        async def driver():
            await asyncio.sleep(0.3)
            (dri / "renderD128").touch()
        t = asyncio.get_running_loop().create_task(driver())
        await g.start()
        await t
    run(scenario())
    assert calls == [0, 1] and graphics.host_gl_ok() and "graphics_render_node" in events
    # no GPU at all: one probe after the wait, no endless loop
    calls.clear()
    (dri / "renderD128").unlink()
    g.RENDER_NODE_WAIT_S = 0.3
    run(g.start())
    assert calls == [0] and not graphics.host_gl_ok()
    # a node appears later (before a launch): probe again
    (dri / "renderD128").touch()
    run(g.reprobe_if_changed())
    assert calls == [0, 1] and graphics.host_gl_ok()
    graphics.set_host_gl(GL_OK)
