"""The machine catalogs this device offers, and how every window lists them.

* **RetroBoot downloads** (``lab``): ``catalog.json`` under the base URL of the stored
  sign-in (hostd.json / hostd.local.json ``lab_http_auth_file``, :mod:`labauth`).  Offered
  only when there is a sign-in; the installer keeps the sign-in of its installation medium
  (or one typed with "Remember on this computer") on the installed system, so another Mac
  can be downloaded later.  The password goes to the log redactor and nowhere else.
* **Internet** (``internet``): hostd's ``catalog_url`` (the public ClassicMac catalog),
  unless that is the lab catalog itself.

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
import logging
import re
from dataclasses import dataclass

from . import labauth
from .catalog import CatalogClient, ImportError_

log = logging.getLogger("retroboot.catalogs")

LAB, INTERNET = "lab", "internet"
LABELS = {LAB: "RetroBoot downloads", INTERNET: "Internet"}
FAMILY_RE = re.compile(r"^(?P<family>[a-z0-9._-]+?)-v(?P<version>\d{1,4})$")
RETRY_S = 30.0


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
    seen: set[str] = set()
    newest = dict(known_families or {})
    items: list[Listed] = []
    for source, entries in results:
        for e in entries or []:
            if e["id"] in seen:
                continue
            seen.add(e["id"])
            fam, ver = family_of(e["id"])
            if fam:
                newest[fam] = max(newest.get(fam, 0), ver)
            items.append(Listed(source, e, fam, ver, known=e["id"] in known_ids))
    for it in items:
        it.older = bool(it.family) and (it.version or 0) < newest.get(it.family, 0)
    first: dict[str, int] = {}
    for i, it in enumerate(items):
        first.setdefault(it.family or it.key, i)
    return sorted(items, key=lambda it: (first[it.family or it.key], -(it.version or 0)))


def entry_family(entry: dict) -> str:
    """The capability-filtered catalog kind; OS X is separate so an older emulator
    never downloads a Mac that would receive classic disk preparation."""
    from .machines import POWERMAC, TEMPLATE_FAMILIES, OSX_CATALOG_FAMILY, OSX_TEMPLATE_FAMILY
    if entry.get("machineFamily") == OSX_TEMPLATE_FAMILY:
        return OSX_CATALOG_FAMILY
    return TEMPLATE_FAMILIES.get(entry.get("machineFamily"), POWERMAC)


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

    def clients(self) -> list[tuple[str, CatalogClient]]:
        out = []
        lab = self.lab_client()
        if lab is not None:
            out.append((LAB, lab))
        if lab is None or self.app.catalog.url != lab.url:
            out.append((INTERNET, self.app.catalog))
        return out

    def client(self, source: str) -> CatalogClient | None:
        return dict(self.clients()).get(source)

    def known(self) -> tuple[set[str], dict[str, int]]:
        """What the device has: its machines' ids and the catalog/fixture ids they came from."""
        ids = []
        for m in self.app.machines.list():
            src = m.meta.get("source") if isinstance(m.meta.get("source"), dict) else {}
            ids += [m.id, src.get("template_id"), src.get("fixture")]
        return known_from(ids)

    async def fetch_all(self, max_age: float = 60.0) -> tuple[list[tuple[str, list[dict]]], dict[str, str]]:
        """Every catalog's entries (waiting for them) and a user-facing reason per failed one."""
        clients = self.clients()
        got = await asyncio.gather(*(c.fetch(max_age=max_age) for _, c in clients), return_exceptions=True)
        results, errors = [], {}
        for (sid, _), res in zip(clients, got):
            if isinstance(res, BaseException):
                errors[sid] = getattr(res, "message", None) or "The catalog could not be loaded."
                self.errors[sid] = errors[sid]
                results.append((sid, []))
            else:
                self.errors[sid] = None
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
                states.append({"id": sid, "label": LABELS[sid], "state": "ready", "error": None})
                continue
            task = self._warming.get(sid)
            failure = self.errors.get(sid)
            if failure and task is not None and task.done():
                state = "error"
                if self._retry_at.get(sid, 0.0) < loop.time():
                    self._retry_at[sid] = loop.time() + RETRY_S
                    self._warming[sid] = loop.create_task(self._warm(sid, client))
            else:
                state = "loading"
                if task is None or task.done():
                    self._warming[sid] = loop.create_task(self._warm(sid, client))
            states.append({"id": sid, "label": LABELS[sid], "state": state,
                           "error": failure if state == "error" else None})
        return results, states

    async def _warm(self, sid: str, client: CatalogClient) -> None:
        try:
            await client.fetch()
            self.errors[sid] = None
        except Exception as exc:  # reported as that catalog's "error" state with a user-facing reason
            self.errors[sid] = getattr(exc, "message", None) or "The catalog could not be loaded."


def overall(states: list[dict]) -> tuple[str, str | None]:
    """One state for older clients: error when every catalog failed, loading while any loads."""
    if states and all(s["state"] == "error" for s in states):
        return "error", states[0]["error"]
    if any(s["state"] == "loading" for s in states):
        return "loading", None
    return "ready", None
