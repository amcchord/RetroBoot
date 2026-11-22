"""Mac OS's time zone from ``system.timezone`` (emulator/reports/tz-20261012.md).

The Mac clock runs on local time: guest-launcher gives QEMU ``TZ`` and the argv
has ``-rtc base=localtime``.  Mac OS also keeps its own idea of the zone, the
MachineLocation in XPRAM (latitude, longitude, gmtDelta), which Date & Time
shows and software uses to convert local time to UTC.  mac99's NVRAM, where
XPRAM lives, is volatile, so every Mac OS start would see "The time zone has
not been specified".  hostd therefore passes the location at each launch as an
Open Firmware option,

    -prom-env retroboot-location=LATITUDE,LONGITUDE,GMTDELTA[,WHEN,NEXTDELTA]

and RetroBoot's virtio tablet driver (classicvirtio ``retroboot-location.patch``,
loaded in every machine) gives it to Mac OS with ``WriteLocation`` while Mac OS
starts.  The three fields are hexadecimal longs exactly as in MachineLocation:

* latitude and longitude: Fracts (2.30 fixed point) of 90 degrees: those of a
  city from the guest's own Date & Time list (the ``loc#`` resource hostd reads
  from the stopped disk with rbguestprep before each launch, ``choose_city``)
  with the zone's standard offset and daylight-saving rule, nearest to the
  zone's principal city in tzdata's zone1970.tab / zone.tab.  Mac OS 9.2.2's
  Time Synchronizer rejects a location that is not one of these cities ("Your
  Macintosh's time zone is not valid", and Restart/Shut Down stall for about
  100 s; emulator/reports/tz2-20261012.md).  Without the list (an older or
  unreadable disk) the tzdata city's own coordinates are used;
* gmtDelta: the low 24 bits are the offset from UTC in seconds *including*
  daylight saving time, the high byte is 0x80 while daylight saving time is in
  effect.  This is what Mac OS 9.2.1's Date & Time writes itself (New York:
  0x00ffb9b0 in winter, 0x80ffc7c0 in summer, verified in the guest).

Daylight saving time is the zone's at launch, like the clock QEMU starts from.
The optional pair is the zone's next change within 400 days (daylight saving
starting or ending): WHEN is the Mac clock's reading at that moment (local time
before the change, seconds since 1904) and NEXTDELTA the gmtDelta after it.  The
tablet driver looks once a minute and, at that time, moves the Mac clock and
gmtDelta the way Date & Time does; every launch computes both again.
"""
from __future__ import annotations

import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

ZONEINFO = Path("/usr/share/zoneinfo")
MAC_EPOCH = 2082844800                                 # 1904-01-01 to 1970-01-01, seconds
GREENWICH = (51 + 28 / 60 + 38 / 3600, -(5 / 3600))     # for UTC and other zero-offset zones without a city
_COORDS_RE = re.compile(r"^([+-]\d{4}(?:\d{2})?)([+-]\d{5}(?:\d{2})?)$")
_cache: dict[str, tuple[float, float]] | None = None


def _dms(text: str) -> float:
    """tzdata's ±DDMM[SS] / ±DDDMM[SS] as degrees."""
    sign = -1.0 if text[0] == "-" else 1.0
    digits = text[1:]
    deg_len = 2 if len(digits) in (4, 6) else 3
    d, m = int(digits[:deg_len]), int(digits[deg_len:deg_len + 2])
    s = int(digits[deg_len + 2:] or 0)
    return sign * (d + m / 60 + s / 3600)


def zone_coordinates(root: Path = ZONEINFO) -> dict[str, tuple[float, float]]:
    """Zone name -> (latitude, longitude) of its principal city, from zone1970.tab and zone.tab."""
    global _cache
    if _cache is not None and root == ZONEINFO:
        return _cache
    out: dict[str, tuple[float, float]] = {}
    for name in ("zone.tab", "zone1970.tab"):       # zone1970.tab wins where both list a zone
        try:
            lines = (root / name).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line or line.startswith("#"):
                continue
            f = line.split("\t")
            if len(f) < 3:
                continue
            m = _COORDS_RE.match(f[1].strip())
            if m:
                out[f[2].strip()] = (_dms(m.group(1)), _dms(m.group(2)))
    if root == ZONEINFO:
        _cache = out
    return out


def _canonical(zone: str, root: Path) -> str:
    """Follow tzdata links (US/Eastern -> America/New_York) when they are symlinks."""
    try:
        real = os.path.realpath(root / zone)
        rel = os.path.relpath(real, os.path.realpath(root))
        if not rel.startswith(".."):
            return rel
    except (OSError, ValueError):
        pass
    return zone


def fract(degrees: float) -> int:
    """Degrees as a Fract of 90 degrees (2.30 fixed point), as an unsigned 32-bit value."""
    return round(degrees / 90 * (1 << 30)) & 0xFFFFFFFF


def gmt_delta(offset_s: int, dst: bool) -> int:
    """MachineLocation.u.gmtDelta: 24-bit signed seconds, 0x80 in the high byte for DST."""
    return (offset_s & 0xFFFFFF) | (0x80000000 if dst else 0)


def _reference_point(zone: str, offset_s: int, root: Path) -> tuple[float, float]:
    """Where the zone is: its tzdata city; without one (UTC, Etc/GMT+5) the zone's meridian,
    Greenwich for offset 0, so the location is never all zeros (which Mac OS and the share
    driver read as "not set")."""
    coords = zone_coordinates(root)
    where = coords.get(zone) or coords.get(_canonical(zone, root))
    if where is None:
        where = GREENWICH if offset_s == 0 else (0.0, max(-180.0, min(180.0, offset_s / 240.0)))
    return where


def zone_profile(zone: str, now: datetime | None = None) -> dict | None:
    """The zone at ``now``: offset and DST now, standard offset, whether it changes within
    400 days and the months daylight saving starts and ends in."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(zone)
    except Exception:
        return None
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    t = now.astimezone(tz)
    if t.utcoffset() is None:
        return None
    offset_s = int(t.utcoffset().total_seconds())
    dst_s = int((t.dst() or timedelta(0)).total_seconds())
    starts = ends = None
    when = now
    for _ in range(3):
        change = next_change(zone, when)
        if change is None:
            break
        when, _off, dst_after = change
        month = when.astimezone(tz).month
        if dst_after and starts is None:
            starts = month
        elif not dst_after and ends is None:
            ends = month
        when += timedelta(hours=1)
    return {"offset": offset_s, "dst": dst_s != 0, "std": offset_s - dst_s,
            "changes": starts is not None or ends is not None, "dst_starts": starts, "dst_ends": ends}


def _city_rule(city: dict) -> tuple[bool, int | None, int | None]:
    rules = city.get("rules") or []
    starts = next((r["month"] for r in rules if r.get("dst")), None)
    ends = next((r["month"] for r in rules if not r.get("dst")), None)
    return bool(rules), starts, ends


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _degrees(fract_value: int) -> float:
    v = fract_value - (1 << 32) if fract_value & 0x80000000 else fract_value
    return v / (1 << 30) * 90


def choose_city(cities: list[dict], zone: str, now: datetime | None = None,
                root: Path = ZONEINFO) -> dict | None:
    """The Date & Time city to name for ``zone``: same standard offset and daylight-saving
    rule (whether it has one, then the months it starts and ends in), nearest to the zone's
    tzdata city; failing that the same standard offset, then the nearest offset."""
    prof = zone_profile(zone, now)
    if prof is None or not cities:
        return None
    ref = _reference_point(zone, prof["offset"], root)

    def key(c: dict, offset_weight: bool) -> tuple:
        lat, lon = _degrees(c["latitude"] & 0xFFFFFFFF), _degrees(c["longitude"] & 0xFFFFFFFF)
        return (abs(c["std_offset"] - prof["std"]) if offset_weight else 0, _distance(ref, (lat, lon)))

    def same_rule(c: dict, months: bool) -> bool:
        has, starts, ends = _city_rule(c)
        if has != prof["changes"]:
            return False
        return not months or not has or (starts, ends) == (prof["dst_starts"], prof["dst_ends"])

    tiers = [
        [c for c in cities if c["std_offset"] == prof["std"] and same_rule(c, True)],
        [c for c in cities if c["std_offset"] == prof["std"] and same_rule(c, False)],
        [c for c in cities if c["std_offset"] == prof["std"]],
    ]
    for tier in tiers:
        if tier:
            return min(tier, key=lambda c: key(c, False))
    return min(cities, key=lambda c: key(c, True))


def mac_location(zone: str, now: datetime | None = None, root: Path = ZONEINFO,
                 cities: list[dict] | None = None) -> tuple[int, int, int] | None:
    """(latitude, longitude, gmtDelta) for ``zone`` at ``now`` (default: the current time), or None
    for an unknown zone.  With the guest's Date & Time ``cities`` the coordinates are one of
    them (``choose_city``)."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(zone)
    except Exception:
        return None
    t = (now or datetime.now(timezone.utc)).astimezone(tz)
    off = t.utcoffset()
    if off is None:
        return None
    offset_s = int(off.total_seconds())
    dst = bool(t.dst())
    city = choose_city(cities, zone, now, root) if cities else None
    if city is not None:
        return city["latitude"] & 0xFFFFFFFF, city["longitude"] & 0xFFFFFFFF, gmt_delta(offset_s, dst)
    where = _reference_point(zone, offset_s, root)
    return fract(where[0]), fract(where[1]), gmt_delta(offset_s, dst)


def next_change(zone: str, now: datetime | None = None, days: int = 400) -> tuple[datetime, int, bool] | None:
    """The zone's next offset change after ``now`` within ``days``: (instant, offset after it in
    seconds, daylight saving time after it), or None."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(zone)
    except Exception:
        return None
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    before = now.astimezone(tz).utcoffset()
    lo = hi = None
    for day in range(1, days + 1):
        t = now + timedelta(days=day)
        if t.astimezone(tz).utcoffset() != before:
            lo, hi = t - timedelta(days=1), t
            break
    if hi is None:
        return None
    while hi - lo > timedelta(seconds=1):
        mid = lo + (hi - lo) / 2
        if mid.astimezone(tz).utcoffset() == before:
            lo = mid
        else:
            hi = mid
    hi = hi.replace(microsecond=0)
    if hi.astimezone(tz).utcoffset() == before:      # bisection left hi a fraction short
        hi += timedelta(seconds=1)
    after = hi.astimezone(tz)
    return hi, int(after.utcoffset().total_seconds()), bool(after.dst())


def prom_env(zone: str, now: datetime | None = None, root: Path = ZONEINFO,
             cities: list[dict] | None = None) -> str | None:
    """The ``-prom-env`` value for ``zone``, or None."""
    loc = mac_location(zone, now, root, cities)
    if loc is None:
        return None
    value = "retroboot-location=%08x,%08x,%08x" % loc
    change = next_change(zone, now)
    if change is not None:
        when, offset_after, dst_after = change
        before = loc[2] & 0xFFFFFF
        before = before - 0x1000000 if before & 0x800000 else before
        mac_when = int(when.timestamp()) + MAC_EPOCH + before    # what the Mac clock reads then
        if 0 < mac_when <= 0xFFFFFFFF:
            value += ",%08x,%08x" % (mac_when, gmt_delta(offset_after, dst_after))
    return value
