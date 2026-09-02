"""The outbound script, as the media path speaks it (§13.2).

The definition lives in the domain package, because the control plane edits
the same script the media path speaks and one copy of each Hindi line is the
only way they stay identical. This module keeps the media path's import
paths stable.
"""

from __future__ import annotations

from uaagro_domain.script import (
    DISCLOSURE,
    MAX_MESSAGE_WORDS,
    OPT_OUT,
    WHATSAPP_SENT,
    OutboundScript,
)

__all__ = (
    "DISCLOSURE",
    "MAX_MESSAGE_WORDS",
    "OPT_OUT",
    "WHATSAPP_SENT",
    "OutboundScript",
)
