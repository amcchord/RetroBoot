"""A/B system slots: the GRUB environment on the ESP, slot discovery, per-slot boot files.

Standard library only and no imports from the ``retroboot`` package, so the
slot guard (``guard.py``) still runs when the rest of hostd in a new slot is
broken (ARCHITECTURE section 13).

Disk layout (section 8): partition 1 RBEFI (ESP), 3 RBROOT_A (slot ``a``),
4 RBROOT_B (slot ``b``), 5 RBDATA, all on the disk holding the running root.

ESP files (``grub/retroboot/``):

* ``slots.env`` and ``slots.env.bak``: GRUB environment blocks (1024 bytes) with
  ``rb_active`` (a|b, the committed slot), ``rb_next`` (a|b to try once, or empty),
  ``rb_tries`` (1: the next boot tries ``rb_next``; GRUB writes 0 before booting it)
  and ``rb_other_ok`` (1: the slot that is not active holds a working version).
  Written in place (one sector holds every value), primary first, then the copy.
* ``slot-a.cfg`` / ``slot-b.cfg``: ``set rb_cmdline_<x>="..."`` (that slot's kernel
  arguments) plus comments naming its version.  A slot without one boots with the
  arguments of the build that wrote ``grub.cfg``.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ENV_SIZE = 1024
ENV_SIG = b"# GRUB Environment Block\n"
ENV_NAME = "grub/retroboot/slots.env"
RPI_ENV_NAME = "retroboot/slots.env"
SLOTS = ("a", "b")
SLOT_PARTN = {"a": 3, "b": 4}
SLOT_LABEL = {"a": "RBROOT_A", "b": "RBROOT_B"}
ESP_LABEL, ESP_PARTN = "RBEFI", 1
DATA_LABEL = "RBDATA"
DEFAULTS = {"rb_active": "a", "rb_next": "", "rb_tries": "0", "rb_other_ok": "0"}
GRUB_CFG_MARKER = "# retroboot-slots: 1"
RPI_CFG_MARKER = "# retroboot-slots: 1"
# Kernel arguments that may appear in a slot file (GRUB script context: no quotes,
# no $, no ; or newlines).
CMDLINE_RE = re.compile(r"^[A-Za-z0-9 ._=,:/+@-]{1,1024}$")
VALUE_RE = re.compile(r"^[A-Za-z0-9._:+-]{0,64}$")


def is_rpi_esp(esp: Path) -> bool:
    p = Path(esp)
    return (p / "config.txt").is_file() and not (p / "grub" / "grub.cfg").is_file()


def _env_name(esp: Path, env_name: str | None = None) -> str:
    if env_name:
        return env_name
    return RPI_ENV_NAME if is_rpi_esp(Path(esp)) else ENV_NAME


class SlotError(Exception):
    pass


def other(slot: str) -> str:
    return "b" if slot == "a" else "a"


# --- GRUB environment block ------------------------------------------------------------

def parse_env(data: bytes) -> dict | None:
    """Values of a GRUB environment block, or None when it is not one."""
    if not data.startswith(ENV_SIG):
        return None
    out = {}
    for raw in data[len(ENV_SIG):].split(b"\n"):
        if not raw or raw.startswith(b"#"):
            continue
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if "=" not in line:
            return None
        k, v = line.split("=", 1)
        out[k] = v.replace("\\\\", "\\")
    return out


def format_env(values: dict) -> bytes:
    body = b""
    for k, v in values.items():
        k, v = str(k), str(v)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,31}", k) or not VALUE_RE.fullmatch(v):
            raise SlotError(f"invalid environment value {k}={v!r}")
        body += f"{k}={v}\n".encode()
    data = ENV_SIG + body
    if len(data) > 480:   # stay inside the first sector: a torn write keeps a valid block
        raise SlotError("environment too large")
    return data + b"#" * (ENV_SIZE - len(data))


def normalize(values: dict | None) -> dict:
    v = dict(DEFAULTS)
    for k in DEFAULTS:
        if values and k in values:
            v[k] = str(values[k])
    if v["rb_active"] not in SLOTS:
        v["rb_active"] = "a"
    if v["rb_next"] not in SLOTS or v["rb_next"] == v["rb_active"]:
        v["rb_next"] = ""
    if v["rb_tries"] not in ("0", "1"):
        v["rb_tries"] = "0"
    if v["rb_other_ok"] not in ("0", "1"):
        v["rb_other_ok"] = "0"
    return v


def read_env(esp: Path, env_name: str | None = None) -> tuple[dict, str]:
    """(values, source): source is ``primary``, ``backup`` or ``none`` (defaults)."""
    rel = _env_name(esp, env_name)
    for path, source in ((Path(esp) / rel, "primary"), (Path(esp) / (rel + ".bak"), "backup")):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        values = parse_env(data)
        if values is not None:
            return normalize(values), source
    return dict(DEFAULTS), "none"


def _write_in_place(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # In place when the file exists at full size: GRUB's own save_env writes the same
    # blocks, and no directory entry changes.  Created once otherwise.
    flags = os.O_WRONLY
    try:
        if path.stat().st_size != ENV_SIZE:
            raise FileNotFoundError
    except FileNotFoundError:
        flags |= os.O_CREAT | os.O_TRUNC
    fd = os.open(str(path), flags, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def write_env(esp: Path, values: dict, env_name: str | None = None) -> dict:
    values = normalize(values)
    data = format_env(values)
    base = Path(esp) / _env_name(esp, env_name)
    _write_in_place(base, data)
    _write_in_place(base.with_name(base.name + ".bak"), data)
    fsync_dir(base.parent)
    return values


def update_env(esp: Path, env_name: str | None = None, **changes) -> dict:
    values, _ = read_env(esp, env_name=env_name)
    values.update({k: str(v) for k, v in changes.items()})
    return write_env(esp, values, env_name=env_name)


def fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_file_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".new")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    fsync_dir(path.parent)


# --- per-slot boot files on the ESP ------------------------------------------------------

def slot_cfg_path(esp: Path, slot: str) -> Path:
    return Path(esp) / "grub" / "retroboot" / f"slot-{slot}.cfg"


def write_slot_cfg(esp: Path, slot: str, cmdline: str, version: str = "", build_id: str = "") -> None:
    if slot not in SLOTS:
        raise SlotError("unknown slot")
    if re.search(r"[\x00-\x1f\x7f]", str(cmdline)):
        raise SlotError("unsupported characters in the kernel arguments")
    cmdline = " ".join(str(cmdline).split())
    if not CMDLINE_RE.fullmatch(cmdline):
        raise SlotError("unsupported characters in the kernel arguments")
    for word in cmdline.split():
        if word.split("=", 1)[0] in ("root", "retroboot.slot", "retroboot.trial", "init"):
            raise SlotError(f"kernel argument {word.split('=', 1)[0]} is set by the boot loader")
    info = re.sub(r"[^A-Za-z0-9._:+-]", "", f"{version} {build_id}".replace(" ", "_"))[:120]
    text = ("# Written by RetroBoot Software Update; do not edit.\n"
            f"# slot {slot}: {info}\n"
            f'set rb_cmdline_{slot}="{cmdline}"\n')
    write_file_atomic(slot_cfg_path(esp, slot), text.encode())


def read_slot_cfg(esp: Path, slot: str) -> dict | None:
    if is_rpi_esp(Path(esp)):
        meta_p = Path(esp) / "retroboot" / f"slot-{slot}.json"
        cmd_p = Path(esp) / "retroboot" / f"slot-{slot}" / "cmdline.txt"
        try:
            cmd = cmd_p.read_text(errors="replace").strip()
        except OSError:
            cmd = None
        info = ""
        try:
            doc = json.loads(meta_p.read_text())
            if isinstance(doc, dict):
                info = str(doc.get("info") or "")
        except (OSError, ValueError):
            pass
        return {"cmdline": cmd, "info": info} if (cmd or info) else None
    try:
        text = slot_cfg_path(esp, slot).read_text()
    except OSError:
        return None
    m = re.search(rf'^set rb_cmdline_{slot}="([^"]*)"$', text, re.M)
    info = re.search(r"^# slot [ab]: (\S*)$", text, re.M)
    return {"cmdline": m.group(1) if m else None, "info": info.group(1) if info else ""}


def rpi_slot_prefix(slot: str) -> str:
    if slot not in SLOTS:
        raise SlotError("unknown slot")
    return f"retroboot/slot-{slot}/"


def rpi_cfg_is_slotted(esp: Path) -> bool:
    try:
        text = (Path(esp) / "config.txt").read_text(errors="replace")
    except OSError:
        return False
    return RPI_CFG_MARKER in text or "os_prefix=retroboot/slot-" in text


def rpi_legacy_cmdline(esp: Path) -> str | None:
    try:
        text = (Path(esp) / "cmdline.txt").read_text(errors="replace").strip()
        return " ".join(text.split()) or None
    except OSError:
        return None


def render_rpi_config(base_text: str, prefix: str, cmdline_name: str = "cmdline.txt") -> str:
    """Return config.txt content configured for ``os_prefix`` and ``cmdline`` under ``[all]``."""
    lines = []
    active = True
    for raw in base_text.splitlines():
        s = raw.strip()
        if s == RPI_CFG_MARKER:
            continue
        if s.startswith("[") and s.endswith("]"):
            active = s.lower() == "[all]"
            lines.append(raw)
            continue
        k = s.split("=", 1)[0].strip().lower() if "=" in s and not s.startswith("#") else ""
        if active and k in ("os_prefix", "cmdline"):
            continue
        lines.append(raw)
    out = [RPI_CFG_MARKER]
    placed = False
    for raw in lines:
        out.append(raw)
        if not placed and raw.strip().lower() == "[all]":
            if prefix:
                out.append(f"os_prefix={prefix}")
            if cmdline_name:
                out.append(f"cmdline={cmdline_name}")
            placed = True
    if not placed:
        ins = ["[all]"] + ([f"os_prefix={prefix}"] if prefix else []) + ([f"cmdline={cmdline_name}"] if cmdline_name else [])
        out[1:1] = ins
    return "\n".join(out).rstrip("\n") + "\n"


def render_rpi_cmdline(cmdline: str, slot: str, root_arg: str, *, trial: bool = False) -> str:
    if slot not in SLOTS:
        raise SlotError("unknown slot")
    words = []
    has_root = False
    has_panic = False
    for w in " ".join(str(cmdline).split()).split():
        k, _, _v = w.partition("=")
        if k in ("retroboot.slot", "retroboot.trial", "retroboot.fallback"):
            continue
        if k == "root":
            words.append(f"root={root_arg}")
            has_root = True
        elif k == "panic" and trial:
            words.append("panic=10")
            has_panic = True
        else:
            words.append(w)
    if not has_root:
        words.append(f"root={root_arg}")
    words.append(f"retroboot.slot={slot}")
    if trial:
        if not has_panic:
            words.append("panic=10")
        words.append("retroboot.trial=1")
    return " ".join(words) + "\n"


def write_rpi_slot_meta(esp: Path, slot: str, version: str = "", build_id: str = "") -> None:
    if slot not in SLOTS:
        raise SlotError("unknown slot")
    info = re.sub(r"[^A-Za-z0-9._:+-]", "", f"{version} {build_id}".replace(" ", "_"))[:120]
    p = Path(esp) / "retroboot" / f"slot-{slot}.json"
    write_file_atomic(p, (json.dumps({"slot": slot, "info": info, "version": version, "build_id": build_id}) + "\n").encode())


def grub_cfg_is_slotted(esp: Path) -> bool:
    try:
        return GRUB_CFG_MARKER in (Path(esp) / "grub" / "grub.cfg").read_text(errors="replace")
    except OSError:
        return False


def legacy_cmdline(esp: Path) -> str | None:
    """The kernel arguments a pre-slot grub.cfg boots RBROOT_A with (``set rb_cmdline="..."``)."""
    try:
        text = (Path(esp) / "grub" / "grub.cfg").read_text(errors="replace")
    except OSError:
        return None
    m = re.search(r'^set rb_cmdline="([^"]*)"\s*$', text, re.M)
    return m.group(1) if m else None


# --- kernel command line and the running slot --------------------------------------------

def read_cmdline(path: str = "/proc/cmdline") -> dict:
    try:
        words = Path(path).read_text(errors="replace").split()
    except OSError:
        return {}
    out = {}
    for w in words:
        k, _, v = w.partition("=")
        out[k] = v
    return out


def _uevent(sysdir: Path) -> dict:
    try:
        text = (sysdir / "uevent").read_text()
    except OSError:
        return {}
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def layout(sysfs: str = "/sys", root_dev: tuple[int, int] | None = None) -> dict:
    """Partitions of the disk holding the running root, by role.

    ``{"disk", "root", "current", "esp", "a", "b", "data", "labels"}`` with /dev paths;
    ``current`` is the slot the running root is (from its partition label), or None
    (live installer, a root that is not a RetroBoot slot)."""
    if root_dev is None:
        st = os.stat("/")
        root_dev = (os.major(st.st_dev), os.minor(st.st_dev))
    link = Path(sysfs) / "dev" / "block" / f"{root_dev[0]}:{root_dev[1]}"
    try:
        part = link.resolve(strict=True)
    except OSError:
        raise SlotError("the running system is not on a disk partition")
    if not (part / "partition").exists():
        raise SlotError("the running system is not on a disk partition")
    out = disk_layout(part.parent)
    out["root"] = "/dev/" + part.name
    root_label = _uevent(part).get("PARTNAME", "")
    for s in SLOTS:
        if root_label == SLOT_LABEL[s]:
            out["current"] = s
    return out


def disk_layout(disk: Path) -> dict:
    """Roles of a RetroBoot disk's partitions from its sysfs directory (/sys/block/<disk>).
    Raises SlotError unless partitions 1, 3, 4 and 5 carry the RetroBoot labels."""
    disk = Path(disk)
    out = {"disk": "/dev/" + disk.name, "root": None, "current": None, "labels": {}}
    by_num = {}
    for child in sorted(disk.iterdir()):
        try:
            num = int((child / "partition").read_text())
        except (OSError, ValueError):
            continue
        ev = _uevent(child)
        by_num[num] = ("/dev/" + child.name, ev.get("PARTNAME", ""))
        out["labels"]["/dev/" + child.name] = ev.get("PARTNAME", "")
    expect = {"esp": (ESP_PARTN, ESP_LABEL), "a": (3, SLOT_LABEL["a"]), "b": (4, SLOT_LABEL["b"]),
              "data": (5, DATA_LABEL)}
    for role, (num, label) in expect.items():
        dev, got = by_num.get(num, (None, None))
        if dev is None or got != label:
            raise SlotError(f"partition {num} of {out['disk']} is not {label}")
        out[role] = dev
    return out


def booted_slot(cmdline: dict, lay: dict | None) -> str | None:
    s = cmdline.get("retroboot.slot")
    if s in SLOTS:
        return s
    return (lay or {}).get("current")


# --- boot state ------------------------------------------------------------------------

def boot_state(env: dict, cmdline: dict, booted: str | None, grub_slotted: bool, *, mode: str = "grub") -> dict:
    """What this boot is, from the environment and kernel command line.

    kind:
      ``normal``      the committed slot (or a pre-slot config booting slot a)
      ``trial``       a new slot on its one try, not confirmed yet
      ``fell-back``   previous boot tried ``rb_next`` and came back on active slot
      ``not-tried``   ``rb_next`` is armed on GRUB with ``rb_tries=1`` (GRUB could not write)
      ``other``       booted a slot bootloader was not asked for (fallback entry, manual)"""
    trial = cmdline.get("retroboot.trial") == "1"
    nxt = env.get("rb_next") or ""
    active = env.get("rb_active", "a")
    if not grub_slotted:
        if mode == "rpi" and nxt and trial and booted == nxt:
            return {"kind": "trial", "slot": booted, "active": active}
        if mode == "rpi" and nxt and booted == active:
            return {"kind": "fell-back", "slot": booted, "active": active, "failed": nxt}
        return {"kind": "normal", "slot": booted, "active": "a", "legacy_grub": mode == "grub"}
    if trial and booted and booted == nxt:
        return {"kind": "trial", "slot": booted, "active": active}
    if nxt and booted == active and env.get("rb_tries") == "0":
        return {"kind": "fell-back", "slot": booted, "active": active, "failed": nxt}
    if nxt and booted == active and env.get("rb_tries") == "1":
        if mode == "rpi":
            return {"kind": "fell-back", "slot": booted, "active": active, "failed": nxt}
        return {"kind": "not-tried", "slot": booted, "active": active, "pending": nxt}
    if booted and booted != active:
        return {"kind": "other", "slot": booted, "active": active,
                "fallback": cmdline.get("retroboot.fallback") == "1"}
    return {"kind": "normal", "slot": booted, "active": active}


def health_flag(run_dir: str = "/run/retroboot") -> Path:
    return Path(run_dir) / "update" / "healthy.json"


def mark_healthy(run_dir: str, info: dict) -> None:
    write_file_atomic(health_flag(run_dir), json.dumps(info).encode())


def init_esp(esp: Path, cmdline: str, version: str = "", build_id: str = "") -> None:
    """A newly installed disk: slot a active, its kernel arguments recorded."""
    write_slot_cfg(esp, "a", cmdline, version, build_id)
    write_env(esp, DEFAULTS)


def main(argv: list[str]) -> int:
    """``slotenv.py init-esp ESP CMDLINE VERSION BUILD_ID`` (image build, installer)
    and ``slotenv.py show ESP`` (diagnostics)."""
    import sys
    if argv[:1] == ["init-esp"] and len(argv) == 5:
        init_esp(Path(argv[1]), argv[2], argv[3], argv[4])
        return 0
    if argv[:1] == ["show"] and len(argv) == 2:
        env, source = read_env(Path(argv[1]))
        print(json.dumps({"env": env, "source": source, "slotted": grub_cfg_is_slotted(Path(argv[1])),
                          "slots": {s: read_slot_cfg(Path(argv[1]), s) for s in SLOTS}}, indent=1))
        return 0
    print(main.__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
