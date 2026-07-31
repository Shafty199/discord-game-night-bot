import re
from datetime import datetime, timedelta, timezone

from settings import DISPLAY_TIMEZONE_OFFSET


def _build_display_timezone(value: str):
    cleaned = str(value or "").strip().upper()

    if cleaned in {"", "UTC", "GMT", "Z"}:
        return timezone.utc, "UTC"

    match = re.fullmatch(
        r"(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d{2}))?",
        cleaned,
    )

    if not match:
        raise RuntimeError(
            "DISPLAY_TIMEZONE_OFFSET must look like +10:00, "
            "-05:00, UTC or GMT."
        )

    sign = 1 if match.group(1) == "+" else -1
    hours = int(match.group(2))
    minutes = int(match.group(3) or 0)

    if hours > 23 or minutes > 59:
        raise RuntimeError(
            "DISPLAY_TIMEZONE_OFFSET must be less than 24 hours."
        )

    offset = timedelta(
        hours=hours,
        minutes=minutes,
    ) * sign

    if offset == timedelta(0):
        return timezone.utc, "UTC"

    minute_suffix = f":{minutes:02d}" if minutes else ""
    label = f"GMT{match.group(1)}{hours}{minute_suffix}"

    return timezone(offset, name=label), label


DISPLAY_TIMEZONE, DISPLAY_TIMEZONE_LABEL = (
    _build_display_timezone(DISPLAY_TIMEZONE_OFFSET)
)


def utc_now() -> datetime:
    """Return an aware UTC timestamp for persistent storage."""

    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def display_now() -> datetime:
    """Return the current time in the configured display timezone."""

    return datetime.now(DISPLAY_TIMEZONE)


def parse_stored_datetime(
    value,
) -> datetime | None:
    """Parse a stored timestamp and return it as aware UTC.

    Older bot versions stored naive timestamps in the bot's display
    timezone. Treating those values as the configured timezone keeps
    migrated history on the same visible date. New writes use UTC.
    """

    cleaned = str(value or "").strip()

    if not cleaned:
        return None

    if cleaned.endswith("Z"):
        cleaned = f"{cleaned[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=DISPLAY_TIMEZONE
        )

    return parsed.astimezone(timezone.utc)


def normalise_stored_datetime(
    value,
) -> str | None:
    parsed = parse_stored_datetime(value)
    return parsed.isoformat() if parsed is not None else None


def to_display_datetime(
    value,
) -> datetime | None:
    parsed = parse_stored_datetime(value)

    if parsed is None:
        return None

    return parsed.astimezone(DISPLAY_TIMEZONE)


def format_display_datetime(
    value,
    *,
    date_only: bool = False,
    fallback: str = "Unknown time",
) -> str:
    displayed = to_display_datetime(value)

    if displayed is None:
        return fallback

    if date_only:
        return displayed.strftime("%Y-%m-%d")

    return (
        displayed.strftime("%d %b %Y, %H:%M")
        + f" {DISPLAY_TIMEZONE_LABEL}"
    )
