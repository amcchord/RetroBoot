"""LAB ONLY: HTTP Basic auth for a private lab file server (hostd.json ``lab_http_auth_file``).

The lab keeps test machine catalogs, archives, update feeds and bundles on a
private web server that asks for HTTP Basic auth.  ``lab_http_auth_file`` names a
file (mode 0600, readable only by hostd's user) with three lines::

    RB_LAB_HTTP_USER=...
    RB_LAB_HTTP_PASS=...
    RB_LAB_BASE_URL=https://example.org/lab/

Only HTTPS requests whose URL lies under ``RB_LAB_BASE_URL`` (same host and port,
path below the base) get an ``Authorization`` header: catalog and archive
downloads (catalog.py) and update feed and bundle downloads (update/manager.py).
Nothing else is ever sent the credentials.  The values are never logged, shown
in status, settings or diagnostics, and ``repr()`` shows only the base URL.

The option is off by default, is not a user setting and is never set in an image;
lab devices get it over SSH.  An unreadable, too-open or incomplete file turns the
feature off with a warning that names the problem, never the contents.

**Installer.**  The installer's "Choose Macs to Install" window lists the Macs of
the lab catalog (``catalog.json`` under the base URL, "RetroBoot downloads") when it
knows the base URL: from ``retroboot/lab-auth.env`` on the installation medium (ISO
tree or the ``RBINSTEFI`` FAT partition, like ``network.conf``: ``find_on_media``),
or from hostd.json.  The medium's file has the same keys; it may leave out user and
password (the installer then asks for them in its Sign In sheet).  The installed
system keeps the sign-in so another Mac can be downloaded there later: the installer
writes it as this 0600 file to ``RBDATA/config/lab-auth.env`` and names it in
``hostd.local.json`` -- always for the medium's sign-in, and for a typed one unless
"Remember on this computer" (checked by default) is unchecked.  With a sign-in, hostd
offers the lab catalog beside the Internet catalog (``catalogs.py``), and Software Update
uses the lab's update list (``feed.json`` under the base URL) while setting ``update.url``
is empty; the password is added to the log redactor.
"""
from __future__ import annotations

import base64
import logging
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("retroboot.labauth")

KEYS = ("RB_LAB_HTTP_USER", "RB_LAB_HTTP_PASS", "RB_LAB_BASE_URL")


def _origin(u) -> tuple:
    return (u.hostname or "").lower(), u.port or 443


class LabAuth:
    def __init__(self, base_url: str, user: str, password: str):
        b = urlsplit(base_url)
        if b.scheme != "https" or not b.hostname or b.username or b.password or b.query or b.fragment:
            raise ValueError("RB_LAB_BASE_URL must be a plain https:// address")
        self.base_url = base_url
        self._origin = _origin(b)
        self._prefix = b.path if b.path.endswith("/") else b.path + "/"
        token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        self._header = {"Authorization": f"Basic {token}"}
        self._user, self._password = user, password    # only for env_text() ("Remember on this computer")

    def __repr__(self) -> str:   # never the credentials
        return f"LabAuth(base={self.base_url!r})"

    def covers(self, url: str) -> bool:
        try:
            u = urlsplit(url)
        except ValueError:
            return False
        if u.scheme != "https" or u.username or u.password or _origin(u) != self._origin:
            return False
        path = u.path or "/"
        return path.startswith(self._prefix) and "/../" not in path + "/"

    def headers_for(self, url: str) -> dict:
        return dict(self._header) if self.covers(url) else {}

    def secrets(self) -> tuple[str, str]:
        """User name and password, for the log redactor only."""
        return self._user, self._password

    def env_text(self) -> str:
        """This file's format (``load`` reads it back; values single-quoted)."""
        return (f"RB_LAB_HTTP_USER='{self._user}'\nRB_LAB_HTTP_PASS='{self._password}'\n"
                f"RB_LAB_BASE_URL={self.base_url}\n")

    @property
    def catalog_url(self) -> str:
        return catalog_url(self.base_url)

    @property
    def feed_url(self) -> str:
        return feed_url(self.base_url)


def headers_for(auth: LabAuth | None, url: str) -> dict:
    return auth.headers_for(url) if auth is not None else {}


def catalog_url(base_url: str) -> str:
    """The lab catalog ("RetroBoot downloads") under a base URL."""
    return base_url.rstrip("/") + "/catalog.json"


def feed_url(base_url: str) -> str:
    """The lab's Software Update list under a base URL."""
    return base_url.rstrip("/") + "/feed.json"


def _values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        if k in KEYS:
            values[k] = v
    return values


# --- Installation media (retroboot/lab-auth.env) ---------------------------------

MEDIA_REL_PATH = "retroboot/lab-auth.env"
MEDIA_MAX_BYTES = 4096


class LabAuthError(Exception):
    """A problem with a lab-auth file on the media; the message never contains a value."""


class MediaLab:
    """The lab location (and optionally the sign-in) from the installation medium."""

    def __init__(self, base_url: str, auth: LabAuth | None, remember: bool, source: str):
        self.base_url, self.auth, self.remember, self.source = base_url, auth, remember, source

    def __repr__(self) -> str:   # never the credentials
        return f"MediaLab(base={self.base_url!r}, signed_in={self.auth is not None}, source={self.source!r})"


def parse_media(text: str, source: str = "iso") -> MediaLab:
    """``RB_LAB_BASE_URL`` (required), ``RB_LAB_HTTP_USER`` and ``RB_LAB_HTTP_PASS`` (both
    or neither).  Other keys (a copy of a whole lab environment file) are ignored.  A
    sign-in from the medium is always kept on the installed system (``remember``)."""
    values = _values(text)
    base = values.get("RB_LAB_BASE_URL", "")
    if not base:
        raise LabAuthError("RB_LAB_BASE_URL is missing")
    user, password = values.get("RB_LAB_HTTP_USER", ""), values.get("RB_LAB_HTTP_PASS", "")
    if bool(user) != bool(password):
        raise LabAuthError("RB_LAB_HTTP_USER and RB_LAB_HTTP_PASS must be given together")
    try:
        if user:
            auth = LabAuth(base, user, password)
        else:
            LabAuth(base, "-", "-")      # validates the URL only
            auth = None
    except ValueError:
        raise LabAuthError("RB_LAB_BASE_URL must be a plain https:// address") from None
    return MediaLab(base, auth, auth is not None, source)


def read_media_file(path: Path, source: str) -> MediaLab | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        if path.stat().st_size > MEDIA_MAX_BYTES:
            raise LabAuthError("the file is larger than 4 KB")
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LabAuthError(f"the file could not be read ({type(exc).__name__})") from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise LabAuthError("the file is not UTF-8 text") from None
    return parse_media(text, source)


async def find_on_media(medium: Path, runner=None, mount_root: Path = Path("/run/retroboot"),
                        fat: bool = True) -> MediaLab | None:
    """retroboot/lab-auth.env in the boot medium's ISO tree, else its RBINSTEFI partition."""
    from .netconf import find_media_file
    return await find_media_file(MEDIA_REL_PATH, read_media_file, medium, runner, mount_root, fat=fat,
                                 prefix="labauth-")


def write_env(path: Path, auth: LabAuth) -> None:
    """Write the 0600 file ``load`` reads (temp file, fsync, rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(auth.env_text())
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def load(path: str | os.PathLike | None) -> LabAuth | None:
    if not path:
        return None
    p = Path(path)
    try:
        st = p.stat()
        if not stat.S_ISREG(st.st_mode):
            log.warning("lab_http_auth_file %s is not a regular file; lab HTTP auth is off", p)
            return None
        if st.st_mode & 0o077:
            log.warning("lab_http_auth_file %s must be mode 0600; lab HTTP auth is off", p)
            return None
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("lab_http_auth_file %s can't be read (%s); lab HTTP auth is off", p, e.strerror or type(e).__name__)
        return None
    values = _values(text)
    missing = [k for k in KEYS if not values.get(k)]
    if missing:
        log.warning("lab_http_auth_file %s lacks %s; lab HTTP auth is off", p, ", ".join(missing))
        return None
    try:
        auth = LabAuth(values["RB_LAB_BASE_URL"], values["RB_LAB_HTTP_USER"], values["RB_LAB_HTTP_PASS"])
    except ValueError as e:
        log.warning("lab_http_auth_file %s: %s; lab HTTP auth is off", p, e)
        return None
    log.info("lab HTTP auth on for %s", auth.base_url)
    return auth
