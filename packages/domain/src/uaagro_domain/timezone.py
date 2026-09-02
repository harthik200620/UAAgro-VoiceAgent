"""Indian Standard Time, resolved once and loudly.

IST is not a display preference here. §18 makes the 09:00-21:00 calling window a
legal constraint, §12.3 gates a warm transfer on centre working hours, and §13
schedules campaigns against it. Every one of those decisions is wrong by five
and a half hours if the zone silently falls back to UTC.

Windows ships no system zone database, and neither do slim Linux containers, so
``ZoneInfo("Asia/Kolkata")`` is not guaranteed to work anywhere the code has not
already run. ``tzdata`` is therefore a hard dependency of this package rather
than something the host is trusted to provide, and the failure below names it --
§0 rule 4: fail loudly, say exactly what is missing.
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ConfigurationError

IST_KEY = "Asia/Kolkata"


@lru_cache(maxsize=8)
def zone(key: str = IST_KEY) -> tzinfo:
    """Resolve a zone key, or fail with something actionable."""
    try:
        return ZoneInfo(key)
    except ZoneInfoNotFoundError as exc:
        raise ConfigurationError(
            f"Time zone {key!r} is not available on this host.",
            remedy="Install the 'tzdata' package (it is a declared dependency of "
            "uaagro-domain, so this usually means the environment was built "
            "without it). Do not fall back to UTC: the calling-window and "
            "working-hours checks would be wrong by 5.5 hours.",
            context={"zone": key},
        ) from exc


def ist() -> tzinfo:
    """Indian Standard Time."""
    return zone(IST_KEY)


def now_ist() -> datetime:
    """Current time in IST.

    Used wherever a rule is written in local terms -- "before 9 pm", "while the
    centre is open". Anything stored is still UTC (§10); this is for comparing
    against a rule a person wrote.
    """
    from datetime import UTC

    return datetime.now(UTC).astimezone(ist())
