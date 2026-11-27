"""Slot guard: a new RetroBoot version gets one try, and only a limited time to prove itself.

Runs on every boot as ``retroboot-slot-guard.service`` (kernel argument
``retroboot.trial=1`` only).  hostd writes ``/run/retroboot/update/healthy.json`` once
Mac OS has shown genuine content (or the classic panel is up with Mac OS deliberately
not started) and it has committed the slot in the GRUB environment.  Without that
within ``--deadline`` seconds of power-on (CLOCK_BOOTTIME) the guard restarts the
computer; GRUB already spent this slot's try, so the previous version starts.  This
covers a hostd crash loop, a presenter that never shows anything and a Mac OS that
never starts.  A kernel panic reboots by itself (``panic=10`` on trial boots); a
hang before userspace needs a power cycle, after which the previous version starts.

Standard library only; imports ``slotenv`` by file so a broken hostd package in the
new slot cannot break the guard.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import slotenv  # noqa: E402

LIFECYCLE = "/var/lib/retroboot/logs/lifecycle.jsonl"


def boottime() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def record(event: str, **fields) -> None:
    rec = {"t_mono": round(time.monotonic(), 6), "t_wall": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "source": "slot-guard", "event": event, **fields}
    try:
        rec["boot_id"] = open("/proc/sys/kernel/random/boot_id").read().strip()
    except OSError:
        pass
    print(json.dumps(rec), flush=True)
    try:
        fd = os.open(LIFECYCLE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, (json.dumps(rec) + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def committed(esp: str, slot: str) -> bool:
    try:
        os.listdir(esp)
    except OSError:
        pass
    try:
        env, _ = slotenv.read_env(esp)
    except Exception:
        return False
    return env.get("rb_active") == slot and not env.get("rb_next")


def main(argv) -> int:
    deadline = 300.0
    esp = "/boot/efi"
    run_dir = "/run/retroboot"
    if "--deadline" in argv:
        deadline = float(argv[argv.index("--deadline") + 1])
    if "--esp" in argv:
        esp = argv[argv.index("--esp") + 1]
    cmd = slotenv.read_cmdline()
    if cmd.get("retroboot.trial") != "1":
        return 0
    slot = cmd.get("retroboot.slot")
    record("slot_guard_armed", slot=slot, deadline_s=deadline, uptime_s=round(boottime(), 1))
    flag = slotenv.health_flag(run_dir)
    n = 0
    while boottime() < deadline:
        if flag.exists():
            record("slot_guard_done", slot=slot, via="health", uptime_s=round(boottime(), 1))
            return 0
        n += 1
        if n % 5 == 0 and boottime() > 30 and committed(esp, slot):
            record("slot_guard_done", slot=slot, via="environment", uptime_s=round(boottime(), 1))
            return 0
        time.sleep(2)
    if flag.exists() or committed(esp, slot):
        return 0
    record("slot_guard_timeout", slot=slot, uptime_s=round(boottime(), 1),
           action="restart into the previous version")
    subprocess.run(["sync"])
    subprocess.run(["systemctl", "reboot"])
    time.sleep(120)
    record("slot_guard_force", slot=slot)
    subprocess.run(["systemctl", "reboot", "--force", "--force"])
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
