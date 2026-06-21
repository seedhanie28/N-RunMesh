from datetime import datetime, timezone

try:
    # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:
    # Python 3.8
    from backports.zoneinfo import ZoneInfo

from app.config import TIMEZONE


def utcnow():
    """UTC aware now"""
    return datetime.now(timezone.utc)


def to_local(dt):
    """Convert UTC datetime → configured local timezone"""
    if not dt:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(ZoneInfo(TIMEZONE))


def format_local(dt, fmt="%Y-%m-%d %H:%M:%S"):
    """Format datetime in local timezone"""
    dt = to_local(dt)
    return dt.strftime(fmt) if dt else "-"