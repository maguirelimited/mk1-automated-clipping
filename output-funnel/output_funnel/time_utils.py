from __future__ import annotations

from datetime import UTC, datetime


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_iso() -> str:
    return to_utc_iso(now_utc())


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value or not str(value).strip():
        return None
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_utc_iso(value: datetime) -> str:
    dt = value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
