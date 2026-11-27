"""The machine catalogs this device offers, and how every window lists them.

* **RetroBoot downloads** (``lab``): ``catalog.json`` under the base URL of the stored
  sign-in (hostd.json / hostd.local.json ``lab_http_auth_file``, :mod:`labauth`).  Offered
  only when there is a sign-in; the installer keeps the sign-in of its installation medium
  (or one typed with "Remember on this computer") on the installed system, so another Mac
  can be downloaded later.  The password goes to the log redactor and nowhere else.
* **Internet** (``internet``): hostd's ``catalog_url``, or the public RetroBoot Mac library
  (``RETROBOOT_CATALOG``, every board) unless that is the lab catalog itself.

:func:`arrange` merges the catalogs the way the installer's Choose Macs window, the local
panel's Install a Mac window and the web page show them: an id listed by both appears once
(RetroBoot downloads first); a family's versions (``<family>-v<N>`` ids, e.g.
``os922-deluxe-v1`` .. ``v4``) stay together, newest first, and versions older than the
newest one known (in the catalogs or among the Macs the caller already has) are ``older``
(shown after "Show older versions"); ``known`` marks an entry the caller already has (on
the installer's disc: left out; on the device: "Installed").  Downloads stay resumable,
size- and SHA-256-checked (:class:`catalog.CatalogClient`); the sign-in is only sent to
HTTPS URLs under the lab base.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import labauth
from .catalog import CatalogClient, ImportError_

log = logging.getLogger("retroboot.catalogs")

LAB, INTERNET = "lab", "internet"
LABELS = {LAB: "RetroBoot downloads", INTERNET: "Internet"}
CLASSICMAC_CATALOG = "https://mcchord.net/classicmac/catalog.json"
RETROBOOT_CATALOG = "https://mcchord.net/retroboot-downloads/catalog.json"
FAMILY_RE = re.compile(r"^(?P<family>[a-z0-9._-]+?)-v(?P<version>\d{1,4})$")
RETRY_S = 30.0
CUSTOM_PREFIX = "custom-"


def custom_id(url: str) -> str:
    return CUSTOM_PREFIX + hashlib.sha256(url.encode()).hexdigest()[:24]


def custom_label(url: str) -> str:
    parsed = urlsplit(url)
    return parsed.netloc + parsed.path


def family_of(machine_id: str | None) -> tuple[str | None, int | None]:
    """``os922-deluxe-v4`` -> (``os922-deluxe``, 4); ids without a version suffix have no family."""
    m = FAMILY_RE.match(machine_id or "")
    return (m.group("family"), int(m.group("version"))) if m else (None, None)


def os_label(version: str | None) -> str | None:
    """A catalog's bare "9.2.2" reads "Mac OS 9.2.2"; "Copland D11E4" stays."""
    if not version:
        return None
    if re.fullmatch(r"10\.[234](?:\.\d+)*", version):
        return f"Mac OS X {version}"
    return f"Mac OS {version}" if version[:1].isdigit() else version


@dataclass
class Listed:
    source: str
    entry: dict
    family: str | None = None
    version: int | None = None
    older: bool = False
    known: bool = False

    @property
    def key(self) -> str:
        return f"{self.source}:{self.entry['id']}"


def known_from(ids) -> tuple[set[str], dict[str, int]]:
    """Ids a caller already has (machine ids, catalog template ids, fixture ids) and the
    newest version of each family among them."""
    known = {i for i in ids if isinstance(i, str) and i}
    families: dict[str, int] = {}
    for i in known:
        fam, ver = family_of(i)
        if fam:
            families[fam] = max(families.get(fam, 0), ver)
    return known, families


def arrange(results: list[tuple[str, list[dict]]], known_ids=frozenset(),
            known_families: dict[str, int] | None = None) -> list[Listed]:
    """Entries of several catalogs (in priority order) as one list; see the module doc."""
    seen = set()
    newest = {(None, fam): version for fam, version in (known_families or {}).items()}
    def namespace(source):
        return source if source.startswith(CUSTOM_PREFIX) else None
    items: list[Listed] = []
    for source, entries in results:
        for e in entries or []:
            identity = (namespace(source), e["id"])
            if identity in seen:
                continue
            seen.add(identity)
            fam, ver = family_of(e["id"])
            if fam:
                group = (namespace(source), fam)
                newest[group] = max(newest.get(group, 0), ver)
            installed = identity in known_ids if namespace(source) else e["id"] in known_ids
            items.append(Listed(source, e, fam, ver, known=installed))
    for it in items:
        it.older = bool(it.family) and (it.version or 0) < newest.get((namespace(it.source), it.family), 0)
    first = {}
    for i, it in enumerate(items):
        first.setdefault((namespace(it.source), it.family or it.key), i)
    return sorted(items, key=lambda it: (first[namespace(it.source), it.family or it.key], -(it.version or 0)))


def entry_family(entry: dict) -> str:
    """The capability-filtered catalog kind; OS X is separate so an older emulator
    never downloads a Mac that would receive classic disk preparation."""
    from .machines import POWERMAC, TEMPLATE_FAMILIES, OSX_CATALOG_FAMILY, OSX_TEMPLATE_FAMILY
    fam = entry.get("machineFamily") or entry.get("family")
    if fam in (OSX_TEMPLATE_FAMILY, OSX_CATALOG_FAMILY):
        return OSX_CATALOG_FAMILY
    if fam in (POWERMAC, "quadra800"):
        return fam
    return TEMPLATE_FAMILIES.get(fam, POWERMAC)


EDITION_RE = re.compile(r"deluxe|edition|activities|period", re.I)


def _os_version_key(version) -> tuple[int, ...]:
    """"9.2.2" -> (9, 2, 2); "Mac OS 9.2.1" -> (9, 2, 1); text without digits -> ()."""
    v = str(version or "")
    if v.startswith("macos-"):
        v = v[6:]
    return tuple(int(n) for n in re.findall(r"\d+", v)[:4])


def _item_known(it) -> bool:
    if hasattr(it, "known"):
        return bool(it.known)
    return bool(isinstance(it, dict) and it.get("installed"))


def _item_entry(it) -> dict | None:
    e = getattr(it, "entry", None)
    if isinstance(e, dict):
        return e
    if isinstance(it, dict) and "id" in it:
        return it
    mid = getattr(it, "machine_id", None)
    if not mid:
        return None
    name = str(getattr(it, "name", "") or "")
    osv = str(getattr(it, "os_version", "") or "")
    fam = "quadra800" if ("7.5" in osv or "8.1" in osv or "68k" in name.lower()) else None
    out = {"id": mid, "name": name, "osVersion": osv}
    if fam:
        out["machineFamily"] = fam
    return out


def _item_bytes(it, e: dict) -> int:
    sz = getattr(it, "size", None)
    if isinstance(sz, int) and sz > 0:
        return sz
    if isinstance(e, dict):
        req = e.get("requiredStorageBytes") or e.get("install_bytes") or e.get("installedBytes") or e.get("size") or 0
        return int(req) if isinstance(req, int) else 0
    return 0


def first_boot_picks(items, *, max_bytes: int | None = None):
    """Choose the two hero options ``(featured_deluxe, featured_base)`` for first-boot &
    Download a Mac: ``os922-mega-deluxe-v4`` and ``os922-full-v5`` when compatible and fitting,
    falling back gracefully on constrained hardware (such as Raspberry Pi Zero 2 W or smaller disks)."""
    from .machines import POWERMAC, QUADRA800

    cands = []
    for it in items:
        if getattr(it, "older", False):
            continue
        if isinstance(it, dict) and it.get("compatible") is False:
            continue
        e = _item_entry(it)
        if not isinstance(e, dict) or not e.get("id"):
            continue
        fam = entry_family(e)
        if fam not in (POWERMAC, QUADRA800):
            continue
        need = _item_bytes(it, e)
        if max_bytes is not None and need > max_bytes:
            continue
        ver = _os_version_key(e.get("osVersion") or e.get("guest_os"))
        cands.append((it, e, fam, ver))

    def pick_deluxe():
        best, best_score = None, None
        for it, e, fam, ver in cands:
            eid = str(e.get("id", ""))
            is_ed = bool(EDITION_RE.search(str(e.get("name", ""))) or EDITION_RE.search(eid))
            if not is_ed:
                continue
            tier = (3 if "mega-deluxe" in eid else
                    2 if "superdeluxe" in eid else
                    1 if "deluxe" in eid else 0)
            score = (fam == POWERMAC, tier, ver)
            if best_score is None or score > best_score:
                best, best_score = it, score
        return best

    def pick_base(exclude=None):
        best, best_score = None, None
        for it, e, fam, ver in cands:
            if it is exclude:
                continue
            eid = str(e.get("id", ""))
            is_ed = bool(EDITION_RE.search(str(e.get("name", ""))) or EDITION_RE.search(eid))
            if is_ed:
                continue
            exact922 = eid.startswith("os922-full")
            score = (fam == POWERMAC, exact922, ver)
            if best_score is None or score > best_score:
                best, best_score = it, score
        return best

    deluxe = pick_deluxe()
    base = pick_base(exclude=deluxe)
    return deluxe, base


def recommended(items):
    """The Mac to offer first on a new computer: the newest standard classic Mac OS it can
    start (Mac OS 9.2.2 on a PC or a Raspberry Pi 4/5, the newest 68k system on a Zero 2 W).
    Once any 9.2.2 Mac is already installed, never recommend downgrading to 9.2.1; recommend
    ``os922-mega-deluxe-v4`` if not yet installed, otherwise ``None``."""
    from .machines import POWERMAC, QUADRA800

    all_items = list(items)
    has_922 = False
    for it in all_items:
        e = _item_entry(it)
        if not isinstance(e, dict) or not _item_known(it):
            continue
        fam = entry_family(e)
        if fam in (POWERMAC, QUADRA800) and _os_version_key(e.get("osVersion") or e.get("guest_os")) >= (9, 2, 2):
            has_922 = True
            break

    if has_922:
        for it in all_items:
            if _item_known(it) or getattr(it, "older", False):
                continue
            e = _item_entry(it)
            if not isinstance(e, dict):
                continue
            eid = str(e.get("id", ""))
            if eid == "os922-mega-deluxe-v4" or eid.startswith("os922-mega-deluxe"):
                return it
        return None

    best, best_score = None, None
    for it in all_items:
        e = _item_entry(it)
        if not isinstance(e, dict) or _item_known(it) or getattr(it, "older", False):
            continue
        family = entry_family(e)
        if family not in (POWERMAC, QUADRA800):
            continue
        standard = not EDITION_RE.search(str(e.get("name", ""))) and not EDITION_RE.search(str(e.get("id", "")))
        score = (family == POWERMAC, standard, _os_version_key(e.get("osVersion") or e.get("guest_os")))
        if best_score is None or score > best_score:
            best, best_score = it, score
    return best


def runnable_families(paths, platform=None) -> set[str]:
    """The kinds of Mac this computer can start: Power Macs always, 68k Macs with the 68k
    emulator package (quadra.py; not in every build yet)."""
    from .machines import POWERMAC, QUADRA800, OSX_CATALOG_FAMILY
    from .osx import package_capable as osx_capable
    from .quadra import package_present
    families = {POWERMAC, QUADRA800} if package_present(paths) else {POWERMAC}
    if osx_capable(paths.qemu_osx_package):
        families.add(OSX_CATALOG_FAMILY)
    if platform is None:
        return families
    # The catalog separates OS X by package capability; the board policy names
    # CPU families. Preserve OS X on capable PowerPC hosts and reject it on Zero.
    return {kind for kind in families
            if (POWERMAC if kind == OSX_CATALOG_FAMILY else kind) in platform.machine_families}


def runnable(results: list[tuple[str, list[dict]]], families: set[str] | None) -> list[tuple[str, list[dict]]]:
    """``results`` without the entries for a kind of Mac this computer can't start."""
    if families is None:
        return results
    return [(sid, [e for e in entries or [] if entry_family(e) in families]) for sid, entries in results]


class Catalogs:
    """The device's catalogs (one per hostd, ``app.catalogs``)."""

    def __init__(self, app):
        self.app = app
        self._lab: CatalogClient | None = None
        self._custom: dict[str, CatalogClient] = {}
        self.errors: dict[str, str | None] = {}
        self._warming: dict[str, asyncio.Task] = {}
        self._retry_at: dict[str, float] = {}

    def lab_client(self) -> CatalogClient | None:
        auth = self.app.lab_auth
        if auth is None:
            return None
        url = labauth.catalog_url(auth.base_url)
        if self._lab is None or self._lab.url != url or self._lab.auth is not auth:
            self._lab = CatalogClient(url, self.app.paths.downloads, ca_file=self.app.cfg.catalog_ca_file, auth=auth)
        return self._lab

    def custom_clients(self, excluded=()) -> list[tuple[str, CatalogClient]]:
        excluded = set(excluded)
        urls = self.app.settings.applied.get("catalog.urls", [])
        wanted = {custom_id(url): url for url in urls if url not in excluded}
        for source in self._custom.keys() - wanted.keys():
            del self._custom[source]
            task = self._warming.pop(source, None)
            if task:
                task.cancel()
            self.errors.pop(source, None)
            self._retry_at.pop(source, None)
        for source, url in wanted.items():
            if source not in self._custom:
                self._custom[source] = CatalogClient(url, self.app.paths.downloads,
                                                     ca_file=self.app.cfg.catalog_ca_file)
        return [(source, self._custom[source]) for source in wanted]

    def label(self, source: str) -> str:
        client = self._custom.get(source)
        return custom_label(client.url) if client else LABELS.get(source, "Image server")

    def clients(self) -> list[tuple[str, CatalogClient]]:
        out = []
        lab = self.lab_client()
        if lab is not None:
            out.append((LAB, lab))
        if lab is None or self.app.catalog.url != lab.url:
            out.append((INTERNET, self.app.catalog))
        return out + self.custom_clients(client.url for _, client in out)

    def client(self, source: str) -> CatalogClient | None:
        return dict(self.clients()).get(source)

    def known(self) -> tuple[set[str], dict[str, int]]:
        """What the device has: its machines' ids and the catalog/fixture ids they came from."""
        ids = []
        custom = set()
        # The built-in URL can have been a custom server on an earlier release.
        internet_alias = custom_id(self.app.catalog.url)
        for m in self.app.machines.list():
            src = m.meta.get("source") if isinstance(m.meta.get("source"), dict) else {}
            template = src.get("template_id")
            source = src.get("catalog")
            if (isinstance(source, str) and source.startswith(CUSTOM_PREFIX)
                    and source != internet_alias and isinstance(template, str)):
                custom.add((source, template))
            else:
                ids += [m.id, template, src.get("fixture")]
        known, families = known_from(ids)
        return known | custom, families

    async def fetch_all(self, max_age: float = 60.0) -> tuple[list[tuple[str, list[dict]]], dict[str, str]]:
        """Every catalog's entries (waiting for them) and a user-facing reason per failed one."""
        clients = self.clients()
        got = await asyncio.gather(*(c.fetch(max_age=max_age) for _, c in clients), return_exceptions=True)
        results, errors = [], {}
        loop = getattr(self.app, "loop", None)
        for (sid, _), res in zip(clients, got):
            if isinstance(res, BaseException):
                errors[sid] = getattr(res, "message", None) or "The catalog could not be loaded."
                self.errors[sid] = errors[sid]
                if loop is not None:
                    self._retry_at[sid] = loop.time() + RETRY_S
                results.append((sid, []))
            else:
                self.errors[sid] = None
                self._retry_at.pop(sid, None)
                results.append((sid, res))
        return results, errors

    def listing(self, results: list[tuple[str, list[dict]]]) -> list[Listed]:
        return arrange(runnable(results, runnable_families(self.app.paths, self.app.platform)), *self.known())

    async def find(self, entry_id: str, source: str | None = None) -> tuple[str, CatalogClient, dict] | None:
        """The catalog entry ``entry_id`` (in ``source``, else the Internet catalog first, as
        before RetroBoot downloads existed); raises the catalog's error when it could not be
        read and the entry was not found elsewhere."""
        clients = dict(self.clients())
        order = [source] if source else [INTERNET, LAB]
        failure = None
        for sid in order:
            client = clients.get(sid)
            if client is None:
                continue
            try:
                entries = await client.fetch()
            except ImportError_ as exc:
                failure = exc
                continue
            entry = next((e for e in entries if e["id"] == entry_id), None)
            if entry is not None:
                return sid, client, entry
        if failure is not None:
            raise failure
        return None

    # -- the web page's view without waiting ---------------------------------------------
    def snapshot(self) -> tuple[list[tuple[str, list[dict]]], list[dict]]:
        """Cached entries per catalog and its state (ready, loading, error).  A catalog not
        fetched yet is fetched in the background; a failed one is retried at most every 30 s."""
        loop = self.app.loop
        results, states = [], []
        for sid, client in self.clients():
            if client.cache is not None:
                results.append((sid, client.cache))
                states.append({"id": sid, "label": self.label(sid), "state": "ready", "error": None})
                continue
            task = self._warming.get(sid)
            failure = self.errors.get(sid)
            if failure and (task is None or task.done()):
                state = "error"
                if self._retry_at.get(sid, 0.0) < loop.time():
                    self._retry_at[sid] = loop.time() + RETRY_S
                    self._warming[sid] = loop.create_task(self._warm(sid, client))
            else:
                state = "loading"
                if task is None or task.done():
                    self._warming[sid] = loop.create_task(self._warm(sid, client))
            states.append({"id": sid, "label": self.label(sid), "state": state,
                           "error": failure if state == "error" else None})
        return results, states

    async def _warm(self, sid: str, client: CatalogClient) -> None:
        try:
            await client.fetch()
            self.errors[sid] = None
            self._retry_at.pop(sid, None)
        except Exception as exc:  # reported as that catalog's "error" state with a user-facing reason
            self.errors[sid] = getattr(exc, "message", None) or "The catalog could not be loaded."
            loop = getattr(self.app, "loop", None)
            if loop is not None:
                self._retry_at[sid] = loop.time() + RETRY_S


def overall(states: list[dict]) -> tuple[str, str | None]:
    """One state for older clients: error when every catalog failed, loading while any loads."""
    if states and all(s["state"] == "error" for s in states):
        return "error", states[0]["error"]
    if any(s["state"] == "loading" for s in states):
        return "loading", None
    return "ready", None
