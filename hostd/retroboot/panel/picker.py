"""Installer: "Choose Macs to Install" (ARCHITECTURE section 8).

One checkable list of every Mac the installer can put on the disk, by source:

* **On this disc** -- machines embedded in the installer image
  (``installer/embedded/machines/<id>/`` in build order, the older single
  ``embedded/machine/``, and ``embedded/*.tar.gz`` archives).  Checked by default.
* **RetroBoot downloads** -- the private lab catalog (``catalog.json`` under the lab
  base URL, HTTP Basic auth; :mod:`retroboot.labauth`).  The base URL and sign-in
  come from the installation medium's ``retroboot/lab-auth.env``, hostd.json or the
  Sign In sheet.  Older versions of a family (``<family>-v<N>`` ids) are behind
  "Show older versions"; a Mac that is also on the disc is listed once, on the disc.
* **Internet** -- hostd's catalog (``catalog_url`` or the board's public catalog).
* **USB disk** -- machine archives on removable disks (:class:`ArchiveSources`).

Clicking a row checks or unchecks it (``activate: click``); with the keyboard,
arrows select and the Add/Remove button toggles.  The popup chooses the Mac that
starts first (default: the first Mac on the disc).  The total size is compared
with the destination's room for Macs and Continue is refused when it doesn't fit.
Hosted Macs are downloaded later, by the install job, straight onto the new disk.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .. import catalogs as _catalogs, labauth
from ..catalog import CatalogClient, ImportError_, download_of
from ..catalogs import (arrange, custom_label, entry_family, family_of, known_from, os_label, runnable,  # noqa: F401
                        runnable_families)
from ..jobs import Job, JobFailed, SUCCEEDED
from ..machines import QUADRA800, machine_slug
from . import ui
from .controller import ImageServers, Screen, WiFi

log = logging.getLogger("retroboot.installer")

MiB = 1024 ** 2
GiB = 1024 ** 3
# install-to-disk's fixed partitions (RBEFI, BIOS boot, two system slots) and alignment.
FIXED_BYTES = (256 + 1 + 3072 + 3072) * MiB + 2 * MiB
MARGIN = GiB                 # install-to-disk keeps 1 GiB free beside the Macs it copies
DOWNLOAD_RESERVE = 256 * MiB  # catalog.RESERVE: kept free while a download is stored
ARCHIVE_GROWTH = 2.5          # a USB archive's Mac takes about this much more than the archive

SOURCES = ("disc", "lab", "catalog", "usb")
SOURCE_LABEL = {"disc": "On this disc", "lab": "RetroBoot downloads", "catalog": "Internet", "usb": "USB disk"}
SOURCE_ICON = {"disc": "cd", "lab": "web", "catalog": "web", "usb": "usb"}

def room_for_macs(disk: dict) -> int:
    """Bytes the destination has for Macs (RBDATA less install-to-disk's margin). A
    destination with another layout names its fixed part (``fixed_bytes``: the free space
    after the installer on a Raspberry Pi card holds only the root besides RBDATA)."""
    fixed = disk.get("fixed_bytes")
    fixed = int(fixed) if isinstance(fixed, int) and fixed >= 0 else FIXED_BYTES
    return max(0, int(disk.get("size_bytes") or 0) - fixed - MARGIN)


@dataclass
class Choice:
    key: str                     # row id, unique: "<source>:<id>"
    source: str                  # disc | lab | catalog | usb
    kind: str                    # dir (copied by install-to-disk) | archive | catalog (installed by hostd)
    name: str
    os_version: str | None = None
    size: int | None = None      # bytes on the destination (estimate for archives)
    estimate: bool = False       # size is an estimate (an archive without a catalog entry)
    download: int = 0            # bytes downloaded first
    machine_id: str | None = None  # embedded directory's id, or the catalog entry's id
    path: Path | None = None     # embedded directory or archive file
    sha256: str | None = None    # archive sidecar / catalog download hash
    origin: str | None = None    # archive origin (usb:LABEL, embedded)
    entry: dict | None = None    # catalog entry
    catalog_url: str | None = None
    auth: labauth.LabAuth | None = field(default=None, repr=False)
    family: str | None = None
    version: int | None = None
    older: bool = False

    @property
    def remote(self) -> bool:
        return self.kind != "dir"

    def detail(self) -> str:
        where = SOURCE_LABEL[self.source] if self.source in SOURCE_LABEL else custom_label(self.catalog_url)
        if self.source == "usb" and self.origin and ":" in self.origin:
            where = f"USB disk “{self.origin.split(':', 1)[1][:24]}”"
        parts = [where]
        osl = os_label(self.os_version)
        if osl and osl.lower() not in self.name.lower():
            parts.append(osl)
        if self.entry is not None and entry_family(self.entry) == QUADRA800:
            parts.append("68k")
        if self.size:
            parts.append(("about " if self.estimate else "") + ui.human_bytes(self.size))
        return " · ".join(parts)


# --- discovering the Macs ----------------------------------------------------------------

def _dir_bytes(mdir: Path) -> int:
    """Bytes a machine directory will take on the destination: the disk's data (its build-time
    extent map, else its allocation) plus the other files."""
    total = 0
    for f in mdir.iterdir():
        try:
            if not f.is_file() or f.name.endswith(".extents"):
                continue
            if f.name == "disk.img":
                total += _disk_data_bytes(f)
            else:
                total += f.stat().st_size
        except OSError:
            continue
    return total


def _disk_data_bytes(disk: Path) -> int:
    st = disk.stat()
    try:
        ext = Path(str(disk) + ".extents")
        if ext.is_file() and ext.stat().st_size <= 64 * MiB:
            doc = json.loads(ext.read_text())
            if doc.get("format") == "retroboot-extents-1" and doc.get("size") == st.st_size and \
                    isinstance(doc.get("extents"), list):
                return sum(int(n) for _, n in doc["extents"])
    except (OSError, ValueError, TypeError):
        pass
    return min(st.st_size, st.st_blocks * 512)


def _machine_record(mdir: Path) -> dict | None:
    if not (mdir / "disk.img").is_file():
        return None
    try:
        if (mdir / "machine.json").is_file():
            meta = json.loads((mdir / "machine.json").read_text())
            return {"id": str(meta.get("id") or mdir.name), "name": str(meta.get("name") or "Mac OS")[:100],
                    "os_version": meta.get("os_version") if isinstance(meta.get("os_version"), str) else None,
                    "fixture": ((meta.get("source") or {}).get("fixture")
                                if isinstance(meta.get("source"), dict) else None)}
        if (mdir / "config.json").is_file():
            cfg = json.loads((mdir / "config.json").read_bytes()[:65536])
            name = str(cfg.get("name") or "Mac OS")[:100] if isinstance(cfg, dict) else "Mac OS"
            return {"id": machine_slug(name), "name": name, "os_version": None, "fixture": None}
    except (OSError, ValueError, AttributeError):
        log.warning("embedded machine %s is unreadable", mdir.name)
    return None


def embedded_choices(embedded: Path) -> list[Choice]:
    """Machines on the installer image: ``machines/<id>/`` in the order the build gave
    (``machines/order``), then the old single ``machine/``, then ``*.tar.gz`` archives."""
    out: list[Choice] = []
    dirs: list[Path] = []
    many = embedded / "machines"
    if many.is_dir():
        try:
            order = [l.strip() for l in (many / "order").read_text().splitlines() if l.strip()]
        except OSError:
            order = []
        found = sorted(d.name for d in many.iterdir() if d.is_dir() and not d.name.startswith("."))
        dirs += [many / n for n in order if n in found] + [many / n for n in found if n not in order]
    if (embedded / "machine").is_dir():
        dirs.append(embedded / "machine")
    seen: set[str] = set()
    for d in dirs:
        rec = _machine_record(d)
        if not rec or rec["id"] in seen:
            continue
        seen.add(rec["id"])
        fam, ver = family_of(rec["fixture"] or rec["id"])
        out.append(Choice(f"disc:{rec['id']}", "disc", "dir", rec["name"], rec["os_version"], _dir_bytes(d),
                          machine_id=rec["id"], path=d, family=fam, version=ver))
    return out


def archive_choice(a: dict) -> Choice:
    """An archive from ArchiveSources.list(): embedded (on the disc) or on a USB disk."""
    source = "disc" if a["origin"] == "embedded" else "usb"
    name = Path(a["name"]).name
    for suffix in (".tar.gz", ".tgz"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
    return Choice(f"{'discarc' if source == 'disc' else 'usb'}:{a['id']}", source, "archive", name[:100] or "Mac OS",
                  None, int(a["bytes"] * ARCHIVE_GROWTH), estimate=True, path=Path(a["_path"]), sha256=a.get("sha256"),
                  origin=a["origin"])


def hosted_choices(sources: list[tuple[str, list[dict], str, object]], disc: list[Choice],
                   families: set[str] | None = None) -> dict[str, list[Choice]]:
    """Catalog entries as choices per source (``(source, entries, url, auth)`` in priority
    order) with :func:`catalogs.arrange`, the same list rules as the installed system's: an id
    in two catalogs once, families together newest first, versions older than the newest
    (disc included) ``older``.  An entry that is also on the disc is left out (the disc's copy
    is offered, checked)."""
    known, newest = known_from([c.machine_id for c in disc] +
                               [f"{c.family}-v{c.version}" for c in disc if c.family])
    meta = {src: (url, auth) for src, _, url, auth in sources}
    out: dict[str, list[Choice]] = {src: [] for src, *_ in sources}
    for it in arrange(runnable([(src, entries) for src, entries, _, _ in sources], families), known, newest):
        if it.known:
            continue
        e = it.entry
        dl = download_of(e)
        url, auth = meta[it.source]
        out[it.source].append(Choice(f"{it.source}:{e['id']}", it.source, "catalog", e["name"], e.get("osVersion"),
                                     e.get("requiredStorageBytes") or e.get("installedBytes"),
                                     download=dl["archiveBytes"], machine_id=e["id"], sha256=dl["sha256"], entry=e,
                                     catalog_url=url, auth=auth, family=it.family, version=it.version,
                                     older=it.older))
    return out


def hero_picks(choices: list[Choice]) -> tuple[Choice | None, Choice | None]:
    """Return ``(featured_deluxe, featured_base)`` for first-boot Mac selection.

    Uses :func:`catalogs.first_boot_picks` when present and matches ``os922-mega-deluxe``
    + clean ``os922-full`` across Choice lists. Returns ``(None, None)`` unless both exist.
    """
    fn = getattr(_catalogs, "first_boot_picks", None)
    if callable(fn):
        try:
            got = fn(choices)
            if isinstance(got, tuple) and len(got) == 2 and all(got):
                return got
        except Exception:
            pass

    def _id(c: Choice) -> str:
        return ((c.entry or {}).get("id") or c.machine_id or "").lower()

    deluxe = next((c for c in choices if not c.older and ("os922-mega-deluxe" in _id(c) or "mega-deluxe" in _id(c))), None)
    base = next((c for c in choices if not c.older and (
        "os922-full" in _id(c)
        or (c.os_version == "9.2.2" and "deluxe" not in _id(c) and "deluxe" not in c.name.lower())
    )), None)
    if deluxe and base and deluxe.key != base.key:
        return deluxe, base
    return None, None


class Picker:
    """What the Choose Macs window knows; kept in InstallerState across its sub-windows."""

    def __init__(self, panel):
        self.panel = panel
        self.app = panel.app
        self.found: dict[str, list[Choice]] = {s: [] for s in SOURCES}
        # Per hosted source: absent | loading | ready | offline | signin | rejected | error
        self.status: dict[str, dict] = {"lab": {"state": "absent"}, "catalog": {"state": "absent"},
                                        "usb": {"state": "loading"}, "disc": {"state": "loading"}}
        self.checked: set[str] = set()
        self.defaults_done = False
        self.touched = False                  # the user checked or unchecked a Mac
        self.recommended: str | None = None   # the download checked for a new computer
        self.startup: str | None = None
        self.show_older = False
        self.lab_client: CatalogClient | None = None
        self.loading = None
        self.hosted_lock = asyncio.Lock()
        self.custom: list[str] = []
        self.raw: dict[str, tuple[list[dict], str, object]] = {}   # hosted source -> (entries, url, auth)

    @property
    def st(self):
        return self.panel.st

    # -- sources -------------------------------------------------------------------
    def lab_source(self) -> tuple[str | None, labauth.LabAuth | None]:
        """(base URL, sign-in) of RetroBoot downloads: the Sign In sheet's, the medium's,
        hostd.json's; or only a base URL (from the medium) that needs a sign-in."""
        st = self.st
        for auth in (st.lab_auth, st.media_lab.auth if st.media_lab else None, self.app.lab_auth):
            if auth is not None:
                return auth.base_url, auth
        if st.media_lab:
            return st.media_lab.base_url, None
        return None, None

    async def load(self, hosted: bool = True, usb: bool = True) -> None:
        app = self.app
        self.found["disc"] = embedded_choices(app.paths.installer_dir / "embedded")
        self.status["disc"] = {"state": "ready"}
        if usb:
            try:
                await app.archives.list()            # mounts USB disks read-only until the end
                archives = list(app.archives.known.values())
            except Exception:  # noqa: BLE001 -- a USB problem never blocks the installer
                log.exception("archive scan failed")
                archives = []
            self.found["disc"] += [archive_choice(a) for a in archives if a["origin"] == "embedded"]
            self.found["usb"] = [archive_choice(a) for a in archives if a["origin"] != "embedded"]
            self.status["usb"] = {"state": "ready"}
        self._defaults()
        self._prune(keep_hosted=True)
        await self._changed()
        if hosted:
            await self.load_hosted()

    async def load_hosted(self) -> None:
        async with self.hosted_lock:
            await self._load_hosted()

    async def _load_hosted(self) -> None:
        base, auth = self.lab_source()
        public = self.app.catalog.url
        lab_url = labauth.catalog_url(base) if base else None
        if lab_url and public == lab_url:   # a lab device whose hostd.json already names the lab catalog
            public = None
        custom = self.app.catalogs.custom_clients((public, lab_url))
        for src in self.custom:
            self.raw.pop(src, None)
            self.status.pop(src, None)
            self.found.pop(src, None)
        self.custom = [src for src, _ in custom]
        self.status.update({src: {"state": "loading"} for src in self.custom})
        self.found.update({src: [] for src in self.custom})
        self.status["lab"] = {"state": "loading" if base else "absent"}
        self.status["catalog"] = {"state": "loading" if public else "absent"}
        await self._changed()
        online = bool(await self.app.network.all_addresses())
        if base:
            if not online:
                self.status["lab"] = {"state": "offline"}
            elif auth is None:
                self.status["lab"] = {"state": "signin"}
            else:
                await self._fetch_lab(lab_url, auth)
        if public:
            if not online:
                self.status["catalog"] = {"state": "offline"}
            else:
                try:
                    self.raw["catalog"] = (await self.app.catalog.fetch(), public, self.app.lab_auth)
                    self.status["catalog"] = {"state": "ready"}
                except ImportError_ as exc:
                    self.raw.pop("catalog", None)
                    self.status["catalog"] = {"state": "error", "message": exc.message}
        else:
            self.raw.pop("catalog", None)
        if not base:
            self.raw.pop("lab", None)
        if online:
            await asyncio.gather(*(self._fetch_custom(src, client) for src, client in custom))
        else:
            self.status.update({src: {"state": "offline"} for src in self.custom})
        self._rebuild_hosted()
        self._prune()
        self._recommend()
        await self._changed()

    def _recommend(self) -> None:
        """Check the recommended download (Mega Deluxe when offered on first boot, else the
        newest standard Mac OS this computer runs) once, when it fits."""
        if self.touched or self.checked or self.recommended is not None:
            return
        from ..catalogs import recommended
        hosted = [c for src in ("lab", "catalog", *self.custom) for c in self.found.get(src, [])]
        deluxe, base = hero_picks(hosted)
        target = self.st.target
        room = room_for_macs(target) if target else None
        def _fits(c: Choice) -> bool:
            return room is None or (c.size or 0) + c.download + DOWNLOAD_RESERVE <= room

        d_id = ((deluxe.entry or {}).get("id") or deluxe.machine_id or "").lower() if deluxe else ""
        has_922 = any(str(c.os_version or "").startswith("9.2.2") for c in hosted)
        hero_ok = deluxe is not None and base is not None and ("mega-deluxe" in d_id or not has_922)
        pick = None
        if hero_ok:
            pick = deluxe if _fits(deluxe) else (base if _fits(base) else None)
        if pick is None:
            cand = recommended(hosted)
            if cand is not None and _fits(cand):
                pick = cand
        if pick is None:
            return
        self.recommended = pick.key
        self.checked.add(pick.key)
        self._fix_startup()

    async def _fetch_custom(self, src, client):
        try:
            self.raw[src] = (await client.fetch(), client.url, None)
            self.status[src] = {"state": "ready"}
        except ImportError_ as exc:
            self.status[src] = {"state": "error", "message": exc.message}

    def _rebuild_hosted(self) -> None:
        sources = ("lab", "catalog", *self.custom)
        order = [(src, *self.raw[src]) for src in sources if src in self.raw]
        got = hosted_choices(order, self.found["disc"], runnable_families(self.app.paths, self.app.platform))
        for src in sources:
            self.found[src] = got.get(src, [])

    async def _fetch_lab(self, url: str, auth: labauth.LabAuth) -> None:
        if self.lab_client is None or self.lab_client.url != url or self.lab_client.auth is not auth:
            self.lab_client = CatalogClient(url, self.app.paths.downloads, ca_file=self.app.cfg.catalog_ca_file,
                                            auth=auth)
        try:
            self.raw["lab"] = (await self.lab_client.fetch(), url, auth)
            self.status["lab"] = {"state": "ready"}
        except ImportError_ as exc:
            self.raw.pop("lab", None)
            self.status["lab"] = {"state": "rejected" if exc.code == "auth_required" else "error",
                                  "message": exc.message}

    async def sign_in(self, auth: labauth.LabAuth) -> str | None:
        """Try a sign-in against the lab catalog; None when accepted, else the reason."""
        url = labauth.catalog_url(auth.base_url)
        client = CatalogClient(url, self.app.paths.downloads, ca_file=self.app.cfg.catalog_ca_file, auth=auth)
        try:
            entries = await client.fetch(max_age=0)
        except ImportError_ as exc:
            return "The user name or password is not correct." if exc.code == "auth_required" else exc.message
        self.lab_client = client
        self.raw["lab"] = (entries, url, auth)
        self._rebuild_hosted()
        self.status["lab"] = {"state": "ready"}
        self._recommend()
        return None

    async def _changed(self) -> None:
        scr = self.panel.screen
        if isinstance(scr, ChooseMacs) and self.panel.visible:
            await self.panel.refresh()

    # -- choices -------------------------------------------------------------------
    def all(self) -> list[Choice]:
        return [c for s in (*SOURCES[:-1], *self.custom, "usb") for c in self.found[s]]

    def get(self, key: str | None) -> Choice | None:
        return next((c for c in self.all() if c.key == key), None)

    def visible(self) -> list[Choice]:
        return [c for c in self.all() if self.show_older or not c.older or c.key in self.checked]

    def has_older(self) -> bool:
        return any(c.older for c in self.all())

    def chosen(self) -> list[Choice]:
        return [c for c in self.all() if c.key in self.checked]

    def _defaults(self) -> None:
        if self.defaults_done:
            return
        self.defaults_done = True
        self.checked |= {c.key for c in self.found["disc"]}
        if self.startup is None:
            self.startup = next((c.key for c in self.found["disc"] if c.kind == "dir"),
                                next((c.key for c in self.found["disc"]), None))

    def _prune(self, keep_hosted: bool = False) -> None:
        """Forget checks of Macs that are no longer offered (a USB disk was removed, a
        catalog no longer lists them)."""
        keys = {c.key for c in self.all()}
        self.checked = {k for k in self.checked if k in keys or
                        (keep_hosted and k.split(":", 1)[0] in ("lab", "catalog", *self.custom))}
        self._fix_startup()

    def _fix_startup(self) -> None:
        if self.startup not in self.checked:
            chosen = self.chosen()
            self.startup = chosen[0].key if chosen else None

    def startup_choice(self) -> Choice | None:
        """The Mac that starts first: the chosen one, else the first checked."""
        chosen = self.chosen()
        return next((c for c in chosen if c.key == self.startup), chosen[0] if chosen else None)

    def toggle(self, key: str) -> None:
        if not self.get(key):
            return
        self.touched = True
        self.checked ^= {key}
        self._fix_startup()

    # -- space ---------------------------------------------------------------------
    def needed(self) -> int:
        chosen = self.chosen()
        need = sum(c.size or 0 for c in chosen)
        downloads = [c.download for c in chosen if c.kind == "catalog"]
        if downloads:   # one archive is stored at a time, then removed
            need += max(downloads) + DOWNLOAD_RESERVE
        return need

    def download_bytes(self) -> int:
        return sum(c.download for c in self.chosen() if c.kind == "catalog")


# --- windows -------------------------------------------------------------------------------

class ChooseMacs(Screen):
    doc_id = "installer-macs"
    guest_background_ok = False
    tick = 5.0          # while offline, notice a network (Wi-Fi from network.conf joins late)

    def __init__(self, panel):
        super().__init__(panel)
        st = panel.st
        if st.picker is None:
            st.picker = Picker(panel)
        self.ps: Picker = st.picker
        self.selected: str | None = None
        self.show_library: bool | None = None
        self._online_check = False
        if self.ps.loading is None or self.ps.loading.done():
            self.ps.loading = self.app.loop.create_task(self.ps.load())

    @property
    def st(self):
        return self.panel.st

    def status_rows(self) -> list[dict]:
        rows = []
        for src in ("lab", "catalog", *self.ps.custom):
            state = self.ps.status[src]["state"]
            reason = {"loading": "Looking…", "offline": "Not connected to a network",
                      "signin": "Sign in to see these Macs", "rejected": "The sign-in was not accepted",
                      "error": "Could not be loaded"}.get(state)
            if reason and not self.ps.found[src]:
                rows.append(ui.row(f"status:{src}", SOURCE_LABEL.get(src) or self.app.catalogs.label(src), reason, SOURCE_ICON.get(src, "web"), checked=False,
                                   disabled=True))
        return rows

    def notes(self) -> list[str]:
        """Why hosted Macs are missing, one sentence each."""
        out = []
        lab, cat = self.ps.status["lab"], self.ps.status["catalog"]
        if "offline" in (lab["state"], cat["state"]):
            out.append("Macs to download appear when this computer is connected. Click Wi-Fi… to join a network.")
        if lab["state"] == "signin":
            out.append("RetroBoot downloads needs a user name and password. Click Sign In….")
        elif lab["state"] == "rejected":
            out.append("RetroBoot downloads did not accept the sign-in. Click Sign In… to try again.")
        elif lab["state"] == "error":
            out.append(f"RetroBoot downloads: {lab.get('message') or 'the list could not be loaded.'}")
        if cat["state"] == "error":
            out.append(f"Internet: {cat.get('message') or 'the catalog could not be loaded.'}")
        if self.st.media_lab_error:
            out.append(f"The sign-in file on the installation media was not used: {self.st.media_lab_error}.")
        return out

    def fits(self) -> bool:
        return self.ps.needed() <= room_for_macs(self.st.target)

    def doc(self):
        ps, d = self.ps, self.st.target
        chosen = ps.chosen()
        vis = ps.visible()
        deluxe, base = hero_picks(ps.all())
        d_id = ((deluxe.entry or {}).get("id") or deluxe.machine_id or "").lower() if deluxe else ""
        has_922 = any(str(c.os_version or "").startswith("9.2.2") for c in ps.all())
        hero_shown = (
            not (ps.found["disc"] or ps.found["usb"])
            and deluxe is not None and base is not None
            and ("mega-deluxe" in d_id or not has_922)
        )
        non_hero_checked = any(c.key not in {deluxe.key, base.key} for c in chosen) if hero_shown else False
        show_lib = (not hero_shown) or bool(self.show_library) or non_hero_checked or bool(ps.found["disc"] or ps.found["usb"])

        rows = [ui.row(c.key, c.name, recommended_detail(c.detail()) if c.key == ps.recommended else c.detail(),
                       SOURCE_ICON.get(c.source, "web"), checked=c.key in ps.checked)
                for c in vis]
        rows += self.status_rows()
        if not rows:
            rows = [ui.row("status:none", "Looking for Macs…" if ps.status["disc"]["state"] == "loading"
                           else "No Macs found", None, "mac", checked=False, disabled=True)]
        if not ps.get(self.selected) or not any(c.key == self.selected for c in vis):
            self.selected = (ps.recommended if ps.get(ps.recommended) else (vis[0].key if vis else None))
        sel = self.selected if ps.get(self.selected) else None

        items = []
        if hero_shown:
            hero_val = deluxe.key if deluxe.key in ps.checked else (base.key if base.key in ps.checked else "")
            is_mega = "mega-deluxe" in d_id
            d_label = "Mac OS 9.2.2 Mega Deluxe (Recommended)" if is_mega else f"{deluxe.name} (Recommended)"
            b_label = "Mac OS 9.2.2 (Classic Clean desktop)" if is_mega else f"{base.name} (Classic Clean desktop)"
            d_desc = ("Mega Deluxe: Kids & Family — Kid Pix Studio Deluxe, Zoombinis & 111 classic games · "
                      if is_mega else f"{deluxe.name}: {ui.clip_words((deluxe.entry or {}).get('summary') or '', 110)} · ")
            b_desc = ("Mac OS 9.2.2: Classic Clean Mac OS 9 desktop · "
                      if is_mega else f"{base.name}: Classic Clean desktop · ")
            items.append(ui.group("Recommended for First-Time Setup", [
                ui.radio("hero_pick", "", [
                    (deluxe.key, d_label),
                    (base.key, b_label),
                ], hero_val),
                ui.text(d_desc + ui.human_bytes(deluxe.size), "small"),
                ui.text(b_desc + ui.human_bytes(base.size), "small"),
            ]))
            lib_n = len(ps.all())
            items.append(ui.checkbox("library", f"Browse all {lib_n} Library Macs", show_lib))

        if show_lib:
            items += [ui.text("Choose the Macs to install. Click a Mac or press Space to check or uncheck it."),
                      ui.lst("macs", rows, sel, rows=5 if hero_shown else 6, activate="click")]
            if ps.has_older():
                items.append(ui.checkbox("older", "Show older versions", ps.show_older))
        elif not vis:
            items += [ui.lst("macs", rows, None, rows=3)]

        first = ps.startup_choice()
        if show_lib or len(chosen) >= 2:
            items.append(ui.popup("startup", "Starts first:", [(c.key, c.name) for c in chosen] or [("none", "—")],
                                  first.key if first else "none", disabled=len(chosen) < 2))
        room = room_for_macs(d)
        need = ps.needed()
        if not chosen:
            items.append(ui.text("Choose a Mac now, or add one after installing RetroBoot."))
        elif need > room:
            items.append(ui.text(f"These Macs need {ui.human_bytes(need)}, but “{d['model']}” has room for "
                                 f"{ui.human_bytes(room)}. Uncheck a Mac or choose a larger disk.", "emphasis"))
        else:
            n = len(chosen)
            dl = ps.download_bytes()
            items.append(ui.text(f"{n} Mac{'s' if n != 1 else ''}: {ui.human_bytes(need)} of "
                                 f"{ui.human_bytes(room)} available on “{d['model']}”"
                                 + (f", {ui.human_bytes(dl)} to download during installation." if dl else "."),
                                 "small"))
        for note in self.notes():
            items.append(ui.text(note, "small"))
        items.append(ui.buttons(ui.btn("servers", "Image Servers…", align="left"),
                                ui.btn("host-only", "Install RetroBoot Only…")))
        sel_c = ps.get(sel)
        buttons = []
        if show_lib and sel_c is not None:
            buttons.append(ui.btn("toggle", "Remove" if sel_c.key in ps.checked else "Add", align="left"))
        if "offline" in (ps.status["lab"]["state"], ps.status["catalog"]["state"]) or \
                ps.status["catalog"]["state"] == "error":
            buttons.append(ui.btn("wifi", "Wi-Fi…"))
        if ps.status["lab"]["state"] in ("signin", "rejected"):
            buttons.append(ui.btn("signin", "Sign In…"))
        buttons += [ui.btn("back", "Back"),
                    ui.btn("continue", "Continue", default=True, disabled=not chosen or need > room)]
        items.append(ui.buttons(*buttons))
        return ui.window(self.doc_id, "Choose Macs to Install", items, width=540)

    async def on_tick(self):
        if self._online_check or "offline" not in (self.ps.status["lab"]["state"], self.ps.status["catalog"]["state"]):
            return
        self._online_check = True
        try:
            if await self.app.network.all_addresses():
                await self.ps.load_hosted()
        finally:
            self._online_check = False

    async def change(self, id, value):
        ps = self.ps
        if id == "hero_pick" and isinstance(value, str):
            deluxe, base = hero_picks(ps.all())
            hero_keys = {c.key for c in (deluxe, base) if c is not None}
            if value in hero_keys and ps.get(value):
                ps.checked -= hero_keys
                ps.checked.add(value)
                ps.touched = True
                ps._fix_startup()
                self.selected = value
                await self.panel.refresh()
        elif id == "library":
            self.show_library = bool(value)
            await self.panel.refresh()
        elif id == "macs":
            before = ps.get(self.selected)
            self.selected = value if isinstance(value, str) else None
            after = ps.get(self.selected)
            if (before is None) != (after is None) or (before and after and
                                                       (before.key in ps.checked) != (after.key in ps.checked)):
                await self.panel.refresh()     # the Add/Remove button follows the selection
        elif id == "older":
            ps.show_older = bool(value)
            await self.panel.refresh()
        elif id == "startup" and ps.get(value) and value in ps.checked:
            ps.startup = value

    async def action(self, id, values):
        from .installer import Destination, DisplayCheck
        p, ps = self.panel, self.ps
        if values.get("startup") in ps.checked:
            ps.startup = values["startup"]
        if id == "macs":
            key = values.get("macs")
            if ps.get(key):
                self.selected = key
                ps.toggle(key)
                await p.refresh()
        elif id == "toggle" and ps.get(self.selected):
            ps.toggle(self.selected)
            await p.refresh()
        elif id == "wifi":
            await p.show(WiFi(p, back=lambda: p.show(ChooseMacs(p))))
        elif id == "servers":
            async def back():
                await p.show(self)
                await ps.load_hosted()
            await p.show(ImageServers(p, back))
        elif id == "host-only":
            ps.checked.clear()          # RetroBoot alone: the Mac comes later (first-time setup)
            ps.touched = True
            ps._fix_startup()
            self.st.plan = []
            self.st.host_only = True
            await p.show(DisplayCheck(p))
        elif id == "signin":
            await p.show(SignIn(p))
        elif id == "back":
            await p.show(Destination(p))
        elif id == "continue":
            chosen = ps.chosen()
            if not chosen or not self.fits():
                await p.refresh()
                return
            self.st.host_only = False
            self.st.plan = plan_install(ps, self.app)
            self.app.lifecycle.event("installer_macs_chosen", count=len(chosen),
                                     sources=sorted({c.source for c in chosen}),
                                     startup=next((m["id"] for m in self.st.plan if m["startup"]), None),
                                     needed_bytes=ps.needed(), room_bytes=room_for_macs(self.st.target))
            await p.show(DisplayCheck(p))


def recommended_detail(detail: str) -> str:
    """"Internet · 420 MB" -> "Internet · Recommended · 420 MB"."""
    where, _, rest = detail.partition(" · ")
    return f"{where} · Recommended" + (f" · {rest}" if rest else "")


def plan_install(ps: Picker, app) -> list[dict]:
    """The checked Macs in list order, each with the machine id it gets on the new disk
    (unique among them): an embedded Mac keeps its own id, a download is named like a
    catalog import (after its name)."""
    plan, taken = [], set()
    for c in ps.chosen():
        base = c.machine_id if c.kind == "dir" and c.machine_id else c.name
        mid = machine_slug(base) if c.kind != "dir" else base
        cand, n = mid, 2
        while cand in taken:
            cand = f"{mid[:44]}-{n}"
            n += 1
        taken.add(cand)
        plan.append({"key": c.key, "choice": c, "id": cand, "name": c.name, "startup": False})
    first = ps.startup_choice()
    for m in plan:
        m["startup"] = first is not None and m["key"] == first.key
    return plan


class SignIn(Screen):
    """User name and password for RetroBoot downloads.  Neither is ever put back into a
    document or logged; "Remember on this computer" (checked by default) keeps them on the
    installed system's 0600 lab-auth file, so another Mac can be downloaded there later."""
    doc_id = "installer-signin"
    guest_background_ok = False

    def __init__(self, panel, error: str | None = None):
        super().__init__(panel)
        self.error = error

    def host(self) -> str:
        base, _ = self.panel.st.picker.lab_source() if self.panel.st.picker else (None, None)
        return (urlsplit(base).hostname or "the server") if base else "the server"

    def doc(self):
        items = [ui.columns(ui.icon("web", 32), ui.stack(
                     ui.text("Sign in to RetroBoot downloads.", "emphasis"),
                     ui.text(f"Enter your user name and password for {self.host()}."))),
                 ui.field("user", "User name:", "", max_length=128),
                 ui.field("password", "Password:", "", secure=True, max_length=256),
                 ui.checkbox("remember", "Remember on this computer", True),
                 ui.text("RetroBoot keeps them on the installed computer, so you can download another Mac "
                         "later.", "small")]
        if self.error:
            items.append(ui.text(self.error, "emphasis"))
        items.append(ui.buttons(ui.btn("cancel", "Cancel"), ui.btn("signin", "Sign In", default=True)))
        return ui.window(self.doc_id, "RetroBoot Downloads", items, width=440, focus="user")

    async def action(self, id, values):
        p = self.panel
        if id == "cancel":
            await p.show(ChooseMacs(p))
            return
        if id != "signin":
            return
        user = str(values.get("user") or "").strip()
        password = str(values.get("password") or "")
        remember = bool(values.get("remember"))
        self.app.redactor.add(user)
        self.app.redactor.add(password)
        if not user or not password:
            await p.show(SignIn(p, "Enter a user name and a password."))
            return
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in user + password) or ":" in user:
            await p.show(SignIn(p, "The user name or password contains a character RetroBoot can't send."))
            return
        ps = self.panel.st.picker
        base, _ = ps.lab_source()
        if not base:
            await p.show(ChooseMacs(p))
            return
        auth = labauth.LabAuth(base, user, password)

        async def body(job: Job):
            job.update(None, "Signing in…")
            why = await ps.sign_in(auth)
            if why:
                raise JobFailed(why, None, "signin_failed")
            return {"signed_in": True}

        async def after(job: Job):
            if job.state == SUCCEEDED:
                self.panel.st.lab_auth = auth
                self.panel.st.lab_remember = remember
                self.app.lifecycle.event("installer_lab_signin", accepted=True, remember=remember)
                await p.show(ChooseMacs(p))
            else:
                self.app.lifecycle.event("installer_lab_signin", accepted=False)
                await p.show(SignIn(p, job.message))

        await p.start_job(lambda: self.app.jobs.start("installer.signin", body, resources=("install",)),
                          "Signing In", after)
