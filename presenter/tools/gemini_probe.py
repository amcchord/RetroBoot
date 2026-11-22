#!/usr/bin/env python3
"""Read-only hardware probe for RetroBoot on the Planet Computers Gemini PDA.

Run on the device (as root for dmesg, debugfs and other users' /proc entries;
it writes nothing, grabs no input device and requests no GPIO line):

    python3 gemini_probe.py              # report: everything below, once
    python3 gemini_probe.py drm-stats 10 # geminipda-drm copy statistics over 10 s
    python3 gemini_probe.py keys 30      # key events from every keyboard (code, name, device)
    python3 gemini_probe.py touch 20     # touchscreen contacts (raw device coordinates)
    python3 gemini_probe.py cpu 10       # per-CPU load, cluster clocks and QEMU's threads, once a second

The report covers: board and kernel, CPUs (online, cpufreq policies, governors,
thermal zones and cooling), the unbound workqueue mask, QEMU's, hostd's and the
presenter's threads with their affinities, DRM (cards, connectors, orientation,
geminipda_drm parameters and kernel log), input devices (keys present, touch
ranges, lid state), ALSA (cards, PCMs, controls, stream state), power supplies,
backlight, Wi-Fi, Bluetooth, the speaker GPIOs (debugfs), and what RetroBoot
decided (hostd's platform report, the lifecycle log, the presenter's journal).
Standard library only.
"""
from __future__ import annotations

import fcntl
import glob
import json
import os
import re
import select
import shutil
import struct
import subprocess
import sys
import time

KEY_NAMES = dict(enumerate((
    "RESERVED ESC 1 2 3 4 5 6 7 8 9 0 MINUS EQUAL BACKSPACE TAB Q W E R T Y U I O P LEFTBRACE RIGHTBRACE ENTER "
    "LEFTCTRL A S D F G H J K L SEMICOLON APOSTROPHE GRAVE LEFTSHIFT BACKSLASH Z X C V B N M COMMA DOT SLASH "
    "RIGHTSHIFT KPASTERISK LEFTALT SPACE CAPSLOCK F1 F2 F3 F4 F5 F6 F7 F8 F9 F10 NUMLOCK SCROLLLOCK KP7 KP8 KP9 "
    "KPMINUS KP4 KP5 KP6 KPPLUS KP1 KP2 KP3 KP0 KPDOT 84 ZENKAKUHANKAKU 102ND F11 F12 RO KATAKANA HIRAGANA "
    "HENKAN KATAKANAHIRAGANA MUHENKAN KPJPCOMMA KPENTER RIGHTCTRL KPSLASH SYSRQ RIGHTALT LINEFEED HOME UP PAGEUP "
    "LEFT RIGHT END DOWN PAGEDOWN INSERT DELETE MACRO MUTE VOLUMEDOWN VOLUMEUP POWER KPEQUAL KPPLUSMINUS PAUSE "
    "SCALE KPCOMMA HANGEUL HANJA YEN LEFTMETA RIGHTMETA COMPOSE").split()))
KEY_NAMES.update({142: "SLEEP", 143: "WAKEUP", 224: "BRIGHTNESSDOWN", 225: "BRIGHTNESSUP", 0x110: "BTN_LEFT",
                  0x14a: "BTN_TOUCH"})
SW_LID = 0
ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0x2f, 0x35, 0x36, 0x39
EV_SYN, EV_KEY, EV_ABS, EV_SW = 0, 1, 3, 5
EVENT = struct.Struct("llHHi")      # struct input_event on 64-bit Linux


def ioc(direction, nr, size, typ=ord("E")):
    return (direction << 30) | (size << 16) | (typ << 8) | nr


def read(path, limit=65536):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read(limit).strip()
    except OSError as exc:
        return f"<{type(exc).__name__}: {exc.strerror or exc}>"


def read_bytes(path, limit=4096):
    try:
        with open(path, "rb") as f:
            return f.read(limit)
    except OSError:
        return b""


def run(argv, timeout=10):
    if not shutil.which(argv[0]):
        return f"<{argv[0]} not installed>"
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + (("\n" + r.stderr) if r.returncode and r.stderr else "")).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"<{type(exc).__name__}: {exc}>"


def section(title):
    print(f"\n===== {title} " + "=" * max(0, 70 - len(title)))


def kv(name, value):
    print(f"{name}: {value}")


# --- report ------------------------------------------------------------------------------
def board():
    section("board and kernel")
    kv("model", read_bytes("/proc/device-tree/model").split(b"\0")[0].decode(errors="replace"))
    kv("compatible", [c.decode(errors="replace") for c in read_bytes("/proc/device-tree/compatible").split(b"\0")
                      if c])
    kv("uname", " ".join(os.uname()))
    kv("cmdline", read("/proc/cmdline"))
    kv("uptime_s", read("/proc/uptime").split()[0])
    for line in read("/proc/meminfo").splitlines()[:3]:
        print("  " + line)
    kv("os-release", (re.search(r'PRETTY_NAME="([^"]*)"', read("/etc/os-release")) or [None, "?"])[1])


def cpus():
    section("CPUs, cpufreq, thermal")
    base = "/sys/devices/system/cpu"
    for f in ("possible", "present", "online", "offline"):
        kv(f, read(f"{base}/{f}"))
    for pol in sorted(glob.glob(f"{base}/cpufreq/policy*")):
        vals = {f: read(f"{pol}/{f}") for f in ("related_cpus", "affected_cpus", "scaling_driver", "scaling_governor",
                                               "scaling_available_governors", "cpuinfo_min_freq",
                                               "cpuinfo_max_freq", "scaling_min_freq", "scaling_max_freq",
                                               "scaling_cur_freq", "scaling_available_frequencies")}
        print(os.path.basename(pol) + ": " + json.dumps(vals))
    for c in range(16):
        d = f"{base}/cpu{c}"
        if os.path.isdir(d):
            mpidr = read(f"{d}/regs/identification/midr_el1")
            print(f"  cpu{c}: online={read(d + '/online') if os.path.exists(d + '/online') else 'n/a'} "
                  f"midr={mpidr} cpufreq={'yes' if os.path.isdir(d + '/cpufreq') else 'no'}")
    for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        trips = []
        for t in sorted(glob.glob(z + "/trip_point_*_temp")):
            n = t.rsplit("_", 2)[0]
            trips.append(f"{read(t)}({read(n + '_type')})")
        print(f"{os.path.basename(z)}: type={read(z + '/type')} temp={read(z + '/temp')} "
              f"policy={read(z + '/policy')} trips={trips}")
    for c in sorted(glob.glob("/sys/class/thermal/cooling_device*")):
        print(f"{os.path.basename(c)}: type={read(c + '/type')} cur={read(c + '/cur_state')} "
              f"max={read(c + '/max_state')}")
    kv("workqueue cpumask (unbound)", read("/sys/devices/virtual/workqueue/cpumask"))
    kv("irq default affinity", read("/proc/irq/default_smp_affinity"))
    for line in read("/proc/interrupts").splitlines():
        if re.search(r"geminipda|i2c|mmc|aw9523|ssd2092|nvt|matrix|wlan|consys|afe|audio", line, re.I):
            print("  " + " ".join(line.split()))


def task_line(pid, tid):
    st = read(f"/proc/{pid}/task/{tid}/status")
    allowed = (re.search(r"Cpus_allowed_list:\s*(\S+)", st) or [None, "?"])[1]
    stat = read(f"/proc/{pid}/task/{tid}/stat").rpartition(")")[2].split()
    last_cpu = stat[36] if len(stat) > 36 else "?"
    ticks = int(stat[11]) + int(stat[12]) if len(stat) > 12 and stat[11].isdigit() else -1
    return (f"    tid {tid:>7} {read(f'/proc/{pid}/task/{tid}/comm'):<16} allowed={allowed:<8} last_cpu={last_cpu:<3} "
            f"cpu_s={ticks / 100:.1f}")


def processes():
    section("RetroBoot processes and placement")
    found = False
    for p in sorted(glob.glob("/proc/[0-9]*")):
        pid = os.path.basename(p)
        comm = read(p + "/comm")
        cmd = read_bytes(p + "/cmdline", 400).replace(b"\0", b" ").decode(errors="replace").strip()
        if comm.startswith("qemu-system") or comm == "rb-presenter" or "retroboot.hostd" in cmd or \
                "retroboot.launcher" in cmd or comm == "rb-splash":
            found = True
            print(f"pid {pid} {comm}: {cmd[:160]}")
            for t in sorted(glob.glob(p + "/task/[0-9]*"), key=lambda x: int(os.path.basename(x))):
                print(task_line(pid, os.path.basename(t)))
    if not found:
        print("(no RetroBoot process found)")


def drm():
    section("DRM / display")
    for card in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        drv = os.path.basename(os.path.realpath(card + "/device/driver")) if os.path.exists(card + "/device/driver") \
            else "?"
        print(f"{os.path.basename(card)}: driver={drv}")
        for conn in sorted(glob.glob(card + "-*")):
            modes = read(conn + "/modes").splitlines()
            print(f"  {os.path.basename(conn)}: status={read(conn + '/status')} enabled={read(conn + '/enabled')} "
                  f"dpms={read(conn + '/dpms')} modes={modes[:4]}")
    for node in sorted(glob.glob("/dev/dri/*")):
        print("  node " + node)
    params = "/sys/module/geminipda_drm/parameters"
    if os.path.isdir(params):
        print("geminipda_drm parameters:")
        for f in sorted(os.listdir(params)):
            print(f"  {f} = {read(os.path.join(params, f))}")
    else:
        print("geminipda_drm: module parameters not found")
    for line in run(["dmesg"]).splitlines():
        if re.search(r"geminipda|drm|panel|backlight|fbcon", line, re.I):
            print("  dmesg: " + line)
    for g in sorted(glob.glob("/sys/class/backlight/*")):
        print(f"backlight {os.path.basename(g)}: {read(g + '/brightness')}/{read(g + '/max_brightness')} "
              f"type={read(g + '/type')} bl_power={read(g + '/bl_power')}")
    kv("fbcon", read("/sys/class/graphics/fbcon/rotate") if os.path.exists("/sys/class/graphics/fbcon") else "none")
    kv("fb devices", sorted(os.path.basename(f) for f in glob.glob("/sys/class/graphics/fb*")))


def parse_input_devices():
    devs, cur = [], {}
    for line in read("/proc/bus/input/devices").splitlines() + [""]:
        if not line.strip():
            if cur:
                devs.append(cur)
            cur = {}
            continue
        tag, _, rest = line.partition(": ")
        if tag == "I":
            cur.update(dict(x.split("=", 1) for x in rest.split()))
        elif tag == "N":
            cur["name"] = rest.partition("=")[2].strip('"')
        elif tag == "P":
            cur["phys"] = rest.partition("=")[2]
        elif tag == "H":
            cur["handlers"] = rest.partition("=")[2].split()
        elif tag == "B":
            k, _, v = rest.partition("=")
            cur["B_" + k] = v
    return devs


def bitmap(words: str) -> set[int]:
    bits, out = 64 if struct.calcsize("l") == 8 else 32, set()
    for i, w in enumerate(reversed(words.split())):
        v = int(w, 16)
        for b in range(bits):
            if v >> b & 1:
                out.add(i * bits + b)
    return out


def event_node(dev):
    return next((f"/dev/input/{h}" for h in dev.get("handlers", []) if h.startswith("event")), None)


def absinfo(node, code):
    try:
        fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        buf = bytearray(24)
        fcntl.ioctl(fd, ioc(2, 0x40 + code, 24), buf)
        return dict(zip(("value", "min", "max", "fuzz", "flat", "resolution"), struct.unpack("6i", buf)))
    except OSError:
        return None
    finally:
        os.close(fd)


def switch_state(node, code):
    try:
        fd = os.open(node, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        buf = bytearray(8)
        fcntl.ioctl(fd, ioc(2, 0x1b, 8), buf)
        return bool(buf[code // 8] >> (code % 8) & 1)
    except OSError:
        return None
    finally:
        os.close(fd)


def inputs():
    section("input devices")
    for d in parse_input_devices():
        node = event_node(d)
        keys = bitmap(d.get("B_KEY", "")) if d.get("B_KEY") else set()
        print(f"{d.get('name')!r} bus={d.get('Bus')} vendor={d.get('Vendor')} product={d.get('Product')} "
              f"phys={d.get('phys')} node={node} EV={d.get('B_EV')}")
        if keys:
            names = [KEY_NAMES.get(k, str(k)) for k in sorted(keys) if k < 0x100]
            print(f"  keys ({len(keys)}): {' '.join(names)}")
            special = {n: (c in keys) for n, c in (("ESC", 1), ("RIGHTALT=Fn", 100), ("POWER", 116), ("SLEEP", 142),
                                                   ("LEFTMETA", 125))}
            print(f"  special: {special}")
        if d.get("B_ABS") and node:
            for name, code in (("MT_X", ABS_MT_POSITION_X), ("MT_Y", ABS_MT_POSITION_Y), ("MT_SLOT", ABS_MT_SLOT)):
                if code in bitmap(d["B_ABS"]):
                    print(f"  {name}: {absinfo(node, code)}")
        if d.get("B_SW") and node and SW_LID in bitmap(d["B_SW"]):
            print(f"  SW_LID now: {'closed' if switch_state(node, SW_LID) else 'open'}")
        if node:
            ud = run(["udevadm", "info", "-q", "property", "-n", node])
            props = [l for l in ud.splitlines() if re.match(r"(ID_INPUT|LIBINPUT|RETROBOOT|WL_OUTPUT)", l)]
            if props:
                print("  udev: " + " ".join(props))
    kv("matrix_keypad poll_ms", read("/sys/module/matrix_keypad/parameters/poll_ms"))
    kv("mt6351_keys longpress_ms", read("/sys/module/mt6351_keys/parameters/longpress_ms"))
    kv("/etc/default/keyboard", read("/etc/default/keyboard").replace("\n", " | "))


def sound():
    section("ALSA")
    print(read("/proc/asound/cards"))
    print(read("/proc/asound/pcm"))
    for c in sorted(glob.glob("/proc/asound/card[0-9]*")):
        n = os.path.basename(c)[4:]
        for s in sorted(glob.glob(c + "/pcm*p/sub0/status")):
            print(f"  {s}: {read(s).splitlines()[0] if read(s) else '?'}")
        print(f"--- amixer -c {n} scontrols")
        print(run(["amixer", "-c", n, "scontrols"]))
        print(f"--- amixer -c {n} sget Headphone / route controls")
        print(run(["amixer", "-c", n, "sget", "Headphone"]))
        for ctl in ("ADDA_DL_CH1 DL1_CH1", "ADDA_DL_CH2 DL1_CH2"):
            print(run(["amixer", "-c", n, "cget", f"name={ctl}"]))
        for mux in ("HPL Mux", "HPR Mux"):
            print(run(["amixer", "-c", n, "sget", mux]))
    print("--- aplay -L (card lines)")
    print("\n".join(l for l in run(["aplay", "-L"]).splitlines() if l and not l[0].isspace()))
    print("--- speaker amplifier GPIOs 243/244 (debugfs; nothing requested)")
    dbg = read("/sys/kernel/debug/gpio", 400000)
    lines = [l for l in dbg.splitlines() if re.search(r"gpio-24[34]\b|\b24[34]\b", l)]
    print("\n".join(lines) if lines else f"(not visible: {dbg[:80]})")
    info = run(["gpioinfo", "-c", "gpiochip0"])          # libgpiod v2; v1 below (lists, requests nothing)
    if "line" not in info:
        info = run(["gpioinfo", "gpiochip0"])
    print("\n".join(l for l in info.splitlines() if re.search(r"line\s+24[34]:", l)) or info[:200])


def power():
    section("power supplies")
    for ps in sorted(glob.glob("/sys/class/power_supply/*")):
        ue = read(ps + "/uevent").replace("POWER_SUPPLY_", "")
        print(f"{os.path.basename(ps)}: " + " ".join(l for l in ue.splitlines() if not l.startswith(("DEVTYPE",))))
    kv("mt6351_gauge system", read("/sys/module/mt6351_gauge/parameters/system"))


def network():
    section("Wi-Fi and Bluetooth")
    for n in sorted(glob.glob("/sys/class/net/*")):
        print(f"{os.path.basename(n)}: operstate={read(n + '/operstate')} wireless={os.path.isdir(n + '/wireless')} "
              f"phy80211={os.path.exists(n + '/phy80211')}")
    print(run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]))
    print(run(["rfkill", "list"]))
    for h in sorted(glob.glob("/sys/class/bluetooth/*")):
        print(f"bluetooth {os.path.basename(h)}: {read(h + '/uevent').replace(chr(10), ' ')}")
    print(run(["systemctl", "is-active", "bluetooth", "NetworkManager"]))


def retroboot():
    section("RetroBoot's decisions")
    hd = "/usr/lib/retroboot/hostd"
    if os.path.isdir(hd):
        env = dict(os.environ, PYTHONPATH=hd, PYTHONDONTWRITEBYTECODE="1")
        try:
            r = subprocess.run([sys.executable, "-m", "retroboot.platform"], capture_output=True, text=True,
                               timeout=30, env=env)
            print(r.stdout.strip() or r.stderr.strip()[-2000:])
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"<platform report: {exc}>")
    log = "/var/lib/retroboot/logs/lifecycle.jsonl"
    want = re.compile(r'"(platform|cpu_placement|cpu_online_changed|cpu_placement_released|audio_output|'
                      r'speaker_amps|audio_output_switch|guest_start|guest_content|finder_detected|guest_exited|'
                      r'power_button|lid|host_key)"')
    try:
        with open(log, "r", errors="replace") as f:
            lines = [l.strip() for l in f if want.search(l)]
        print(f"--- {log} (last 40 of {len(lines)} matching)")
        print("\n".join(lines[-40:]))
    except OSError as exc:
        print(f"<{log}: {exc.strerror}>")
    print("--- presenter journal (this boot)")
    j = run(["journalctl", "-b", "-u", "retroboot-presenter", "--no-pager", "-o", "cat"], timeout=20)
    print("\n".join(l for l in j.splitlines() if re.search(
        r"display|input:|Gemini|atomic|DAMAGE|modeset|touch|keyboard|splash|orientation", l))[-6000:])
    print("--- splash (kernel log)")
    print("\n".join(l for l in run(["dmesg"]).splitlines() if "splash" in l)[-3000:])


def report():
    for fn in (board, cpus, processes, drm, inputs, sound, power, network, retroboot):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 -- a probe never stops half way
            print(f"<{fn.__name__} failed: {type(exc).__name__}: {exc}>")


# --- live modes ------------------------------------------------------------------------------
def drm_stats(seconds: float) -> None:
    p = "/sys/module/geminipda_drm/parameters"
    names = ("stat_commits", "stat_copy_us_total", "stat_full_frames", "stat_chased", "stat_vblank_irqs")
    if not os.path.exists(f"{p}/stat_commits"):
        print("geminipda_drm has no stat_* parameters")
        return
    a = {n: int(read(f"{p}/{n}") or 0) for n in names}
    time.sleep(seconds)
    b = {n: int(read(f"{p}/{n}") or 0) for n in names}
    d = {n: b[n] - a[n] for n in names}
    commits = d["stat_commits"]
    print(f"window {seconds:g} s: commits {commits} ({commits / seconds:.1f}/s), "
          f"avg copy {d['stat_copy_us_total'] / commits if commits else 0:.0f} us, "
          f"full frames {d['stat_full_frames']} of {commits}, chased {d['stat_chased']}, "
          f"vblank irqs {d['stat_vblank_irqs']} ({d['stat_vblank_irqs'] / seconds:.1f}/s)")
    print(f"last commit {read(p + '/stat_px_last')} px (full = 2332800), copy max {read(p + '/stat_copy_us_max')} us, "
          f"chase line max {read(p + '/stat_chase_line_max')}, fast_copy={read(p + '/fast_copy')} "
          f"cache_sync={read(p + '/cache_sync')} chase={read(p + '/chase')} refresh_hz={read(p + '/refresh_hz')}")


def open_events(pred):
    fds = {}
    for d in parse_input_devices():
        node = event_node(d)
        if node and pred(d):
            try:
                fds[os.open(node, os.O_RDONLY | os.O_NONBLOCK)] = d.get("name")
            except OSError as exc:
                print(f"{node}: {exc.strerror}")
    return fds


def keys(seconds: float) -> None:
    fds = open_events(lambda d: bool(d.get("B_KEY")))
    print(f"listening to {sorted(set(fds.values()))} for {seconds:g} s (not grabbed: RetroBoot still sees them)")
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        r, _, _ = select.select(list(fds), [], [], max(0.0, end - time.monotonic()))
        for fd in r:
            data = os.read(fd, EVENT.size * 64)
            for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
                sec, usec, typ, code, value = EVENT.unpack_from(data, i)
                if typ == EV_KEY and value in (0, 1):
                    print(f"{sec}.{usec:06d} {fds[fd]!r:>20} {'down' if value else 'up  '} {code:3d} "
                          f"KEY_{KEY_NAMES.get(code, code)}")
                elif typ == EV_SW:
                    print(f"{sec}.{usec:06d} {fds[fd]!r:>20} switch {code} = {value}")
    for fd in fds:
        os.close(fd)


def touch(seconds: float) -> None:
    fds = open_events(lambda d: d.get("B_ABS") and ABS_MT_POSITION_X in bitmap(d["B_ABS"]))
    for fd, name in fds.items():
        print(f"{name!r}: x {absinfo('/proc/self/fd/%d' % fd, ABS_MT_POSITION_X)} "
              f"y {absinfo('/proc/self/fd/%d' % fd, ABS_MT_POSITION_Y)}")
    print("touch the four corners of the screen (held landscape, keyboard below), then the centre")
    end = time.monotonic() + seconds
    slot, pos = 0, {}
    while time.monotonic() < end:
        r, _, _ = select.select(list(fds), [], [], max(0.0, end - time.monotonic()))
        for fd in r:
            data = os.read(fd, EVENT.size * 64)
            for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
                _, _, typ, code, value = EVENT.unpack_from(data, i)
                if typ == EV_ABS and code == ABS_MT_SLOT:
                    slot = value
                elif typ == EV_ABS and code in (ABS_MT_POSITION_X, ABS_MT_POSITION_Y):
                    pos.setdefault(slot, [None, None])[code - ABS_MT_POSITION_X] = value
                elif typ == EV_ABS and code == ABS_MT_TRACKING_ID:
                    print(f"slot {slot} {'down' if value >= 0 else 'up'}" + (f" at {pos.get(slot)}" if value < 0 else ""))
                elif typ == EV_SYN and pos:
                    print("  " + " ".join(f"s{s}={p}" for s, p in sorted(pos.items())))
    for fd in fds:
        os.close(fd)


def cpu_watch(seconds: float) -> None:
    def stat():
        out = {}
        for line in read("/proc/stat").splitlines():
            m = re.match(r"cpu(\d+)\s+(.*)", line)
            if m:
                v = [int(x) for x in m.group(2).split()]
                out[int(m.group(1))] = (sum(v), v[3] + (v[4] if len(v) > 4 else 0))
        return out
    prev = stat()
    for _ in range(int(seconds)):
        time.sleep(1)
        cur = stat()
        load = " ".join(f"{c}:{100 * (1 - (cur[c][1] - prev[c][1]) / max(1, cur[c][0] - prev[c][0])):3.0f}%"
                        for c in sorted(cur) if c in prev)
        clocks = " ".join(f"{os.path.basename(p)}={int(read(p + '/scaling_cur_freq') or 0) // 1000}MHz"
                          f"({read(p + '/scaling_governor')})"
                          for p in sorted(glob.glob("/sys/devices/system/cpu/cpufreq/policy*")))
        temps = " ".join(f"{read(z + '/type')}={int(read(z + '/temp') or 0) / 1000:.1f}C"
                         for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*")))
        print(f"{time.strftime('%H:%M:%S')} load {load}\n  {clocks}\n  {temps}")
        for p in glob.glob("/proc/[0-9]*/comm"):
            if read(p).startswith("qemu-system"):
                pid = p.split("/")[2]
                for t in sorted(glob.glob(f"/proc/{pid}/task/[0-9]*")):
                    line = task_line(pid, os.path.basename(t))
                    if "TCG" in line or "qemu-system" in line:
                        print(line)
        prev = cur


def main(argv):
    mode = argv[1] if len(argv) > 1 else "report"
    secs = float(argv[2]) if len(argv) > 2 else 10.0
    if mode == "report":
        report()
    elif mode == "drm-stats":
        drm_stats(secs)
    elif mode == "keys":
        keys(secs)
    elif mode == "touch":
        touch(secs)
    elif mode == "cpu":
        cpu_watch(secs)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
