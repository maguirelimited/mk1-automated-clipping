from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .time_utils import parse_iso_datetime, to_utc_iso


def _cadence(profile: dict[str, Any]) -> dict[str, Any]:
    raw = profile.get("cadence")
    return dict(raw) if isinstance(raw, dict) else {}


def _timezone(profile: dict[str, Any]) -> ZoneInfo:
    raw = str(_cadence(profile).get("timezone") or "UTC")
    try:
        return ZoneInfo(raw)
    except Exception:
        return ZoneInfo("UTC")


def _parse_hhmm(value: str, default: time) -> time:
    try:
        hour, minute = value.split(":", 1)
        return time(hour=int(hour), minute=int(minute))
    except Exception:
        return default


def _allowed_windows(profile: dict[str, Any]) -> list[tuple[time, time]]:
    raw = _cadence(profile).get("allowed_windows")
    if not isinstance(raw, list) or not raw:
        return [(time(0, 0), time(23, 59))]
    out: list[tuple[time, time]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(
            (
                _parse_hhmm(str(item.get("start") or "00:00"), time(0, 0)),
                _parse_hhmm(str(item.get("end") or "23:59"), time(23, 59)),
            )
        )
    return out or [(time(0, 0), time(23, 59))]


def _inside_window(local_dt: datetime, windows: list[tuple[time, time]]) -> bool:
    t = local_dt.time().replace(second=0, microsecond=0)
    for start, end in windows:
        if start <= end and start <= t <= end:
            return True
        if start > end and (t >= start or t <= end):
            return True
    return False


def _next_window_start(local_dt: datetime, windows: list[tuple[time, time]]) -> datetime:
    candidates: list[datetime] = []
    for day_offset in range(0, 8):
        day = (local_dt + timedelta(days=day_offset)).date()
        for start, _ in windows:
            candidate = datetime.combine(day, start, tzinfo=local_dt.tzinfo)
            if candidate >= local_dt:
                candidates.append(candidate)
    return min(candidates) if candidates else local_dt


def _count_on_local_day(existing: list[datetime], candidate: datetime, tz: ZoneInfo) -> int:
    cday = candidate.astimezone(tz).date()
    return sum(1 for item in existing if item.astimezone(tz).date() == cday)


def next_scheduled_time(
    profile: dict[str, Any],
    existing_times: list[str],
    *,
    now: datetime | None = None,
) -> str:
    cadence = _cadence(profile)
    tz = _timezone(profile)
    min_gap = timedelta(minutes=int(cadence.get("min_gap_minutes") or 180))
    lead = timedelta(minutes=int(cadence.get("default_lead_minutes") or 180))
    max_per_day = int(cadence.get("max_uploads_per_day") or 3)
    windows = _allowed_windows(profile)

    existing = [dt for raw in existing_times if (dt := parse_iso_datetime(raw)) is not None]
    baseline = (now or datetime.now(UTC)).astimezone(UTC) + lead
    if existing:
        latest = max(existing)
        if baseline < latest + min_gap:
            baseline = latest + min_gap

    candidate = baseline.astimezone(tz).replace(second=0, microsecond=0)
    for _ in range(0, 60 * 24 * 14):
        if not _inside_window(candidate, windows):
            candidate = _next_window_start(candidate, windows)
        if _count_on_local_day(existing, candidate, tz) >= max_per_day:
            next_day = candidate.date() + timedelta(days=1)
            candidate = datetime.combine(next_day, windows[0][0], tzinfo=tz)
            continue
        if existing and candidate.astimezone(UTC) < max(existing) + min_gap:
            candidate = (max(existing) + min_gap).astimezone(tz)
            continue
        return to_utc_iso(candidate.astimezone(UTC))
    raise RuntimeError("Unable to find a schedule slot within 14 days")
