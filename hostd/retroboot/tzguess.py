"""Best-effort network time zone lookup (IP geolocation, HTTPS only by default).

Used after Wi-Fi / network setup in the installer and first-boot setup to
pre-select the user's likely IANA time zone instead of leaving UTC.  Never
blocks startup, never logs the public IP or raw response, and always falls
back silently to manual selection if offline or unreachable.
"""
from __future__ import annotations

import asyncio
import json
import ssl
import time
from typing import Sequence
from urllib.parse import urlsplit

from . import __version__, schema

DEFAULT_ENDPOINTS: tuple[str, ...] = (
    "https://ipinfo.io/json",
    "https://ipwho.is/",
    "https://ipapi.co/timezone/",
)

MAX_BODY = 64 * 1024

_ALIAS_MAP = {
    "US/Eastern": "America/New_York",
    "US/Central": "America/Chicago",
    "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles",
    "US/Alaska": "America/Anchorage",
    "US/Hawaii": "Pacific/Honolulu",
    "US/Arizona": "America/Phoenix",
    "US/East-Indiana": "America/Indiana/Indianapolis",
    "GMT": "UTC",
    "Etc/UTC": "UTC",
    "Etc/GMT": "UTC",
    "UCT": "UTC",
}


def _normalize_zone(raw: str | None) -> str | None:
    if not isinstance(raw, str):
        return None
    z = raw.strip()
    if not z or not schema.TIMEZONE_RE.fullmatch(z):
        return None
    z = _ALIAS_MAP.get(z, z)
    every = schema.available_timezones()
    if z not in every:
        return None
    listed = schema.listed_timezones()
    if z in listed:
        return z
    # Try matching common canonical zone by suffix if an un-listed alias slips through.
    return z


def _parse_response(body: bytes, content_type: str = "") -> str | None:
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return None
    if text.startswith("{") or "json" in content_type.lower():
        try:
            doc = json.loads(text)
        except ValueError:
            doc = None
        if isinstance(doc, dict):
            if doc.get("success") is False:
                return None
            tz = doc.get("timezone")
            if isinstance(tz, str):
                return _normalize_zone(tz)
            if isinstance(tz, dict):
                return _normalize_zone(tz.get("id") or tz.get("name"))
            if isinstance(doc.get("time_zone"), dict):
                return _normalize_zone(doc["time_zone"].get("name") or doc["time_zone"].get("id"))
            return None
    first_line = text.splitlines()[0].strip()
    return _normalize_zone(first_line)


def _ssl_context(ca_file: str | None):
    if not ca_file:
        return None
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=ca_file)
    return ctx


async def guess_timezone(
    urls: Sequence[str] | None = None,
    *,
    timeout: float = 3.0,
    total_timeout: float = 5.5,
    ca_file: str | None = None,
    allow_http: bool = False,
) -> tuple[str | None, str | None]:
    """Query endpoints in order until one yields a valid IANA zone.

    Returns ``(zone, source_host)`` or ``(None, None)``.  Pass ``urls=[]`` to
    disable network lookups cleanly in tests.
    """
    if urls is not None and len(urls) == 0:
        return None, None
    endpoints = list(urls) if urls is not None else list(DEFAULT_ENDPOINTS)
    try:
        import aiohttp
    except ImportError:
        return None, None

    deadline = time.monotonic() + max(0.5, float(total_timeout))
    ssl_opt = _ssl_context(ca_file)
    headers = {"User-Agent": f"RetroBoot/{__version__}", "Accept": "application/json, text/plain"}

    for url in endpoints:
        remaining = deadline - time.monotonic()
        if remaining <= 0.1:
            break
        try:
            parts = urlsplit(str(url))
        except ValueError:
            continue
        if parts.scheme != "https" and not (allow_http and parts.scheme == "http"):
            continue
        if not parts.hostname:
            continue
        per_req = min(float(timeout), remaining)
        try:
            client_timeout = aiohttp.ClientTimeout(
                total=per_req,
                sock_connect=min(2.5, per_req),
                sock_read=per_req,
            )
            async with aiohttp.ClientSession(
                timeout=client_timeout,
                connector=aiohttp.TCPConnector(ssl=ssl_opt),
                headers=headers,
            ) as session:
                async with session.get(str(url), allow_redirects=False) as resp:
                    if resp.status != 200:
                        continue
                    body = bytearray()
                    async for chunk in resp.content.iter_chunked(16 * 1024):
                        body.extend(chunk)
                        if len(body) > MAX_BODY:
                            break
                    if len(body) > MAX_BODY:
                        continue
                    zone = _parse_response(bytes(body), resp.headers.get("Content-Type", ""))
                    if zone:
                        return zone, parts.hostname
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
    return None, None
