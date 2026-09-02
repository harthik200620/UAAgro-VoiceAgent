"""JSON for the paths that run per audio frame (§7, §7.6).

A telephony media frame arrives every 20 ms per call, and each one is a JSON
decode plus a base64 decode. Outbound is the same in reverse. At §7.6's
concurrency that is thousands of encode/decode pairs a second on the same event
loop that has to keep audio moving, so it is the one place in this system where
the codec is worth choosing rather than inheriting.

`orjson` is the fastest maintained option and the mainstream choice. Measured on
a real 578-byte Exotel frame: **3.6x faster to decode, 18.8x faster to encode**.

Being straight about the size of that: it is 17.7 ms of CPU per second at 50
concurrent calls, under 2% of one core. It is not why calls are fast. It is
adopted because it is strictly better at zero risk, not because it rescued a
budget.

The *incidental* win is larger and was not the reason for the change. `orjson`
emits raw UTF-8 where the standard library escapes non-ASCII to ``\\uXXXX`` --
six bytes per character instead of three. §5.5 sends the catalogue's spoken
vocabulary to the recogniser at the start of every call, up to 6,000 characters
of Devanagari, and that message is now roughly half the size.

**Two API differences that would be bugs if papered over**, which is why this
is a shim and not an import alias:

``dumps`` returns ``bytes`` in orjson.
    A WebSocket ``send()`` accepts both, and the two mean *different frame
    types*. Handing a control message to a vendor as a binary frame instead of
    a text frame is a protocol violation that most servers reject and some
    silently ignore. :func:`dumps` here returns ``str``, exactly as the
    standard library does.

``JSONDecodeError`` is a different class.
    It subclasses the standard library's, so existing ``except
    json.JSONDecodeError`` handlers keep working -- verified rather than
    assumed. Invalid UTF-8 raises ``JSONDecodeError`` here where the standard
    library raises ``UnicodeDecodeError``; call sites that catch both are
    unaffected, and both are re-exported below so a call site can catch the
    union without importing two modules.
"""

from __future__ import annotations

import json
from typing import Any

try:
    import orjson

    _FAST = True
except ImportError:  # pragma: no cover - orjson is a declared dependency
    _FAST = False


#: Catch this to handle malformed input from any codec, from either backend.
#:
#: ``UnicodeDecodeError`` is in the tuple because the standard library raises it
#: for invalid UTF-8 while orjson folds that into a decode error. A call site
#: that catches this tuple behaves the same whichever backend is in use.
JsonError: tuple[type[Exception], ...] = (json.JSONDecodeError, UnicodeDecodeError)


def loads(data: str | bytes) -> Any:
    """Decode JSON from text or bytes.

    Bytes are preferred where the caller has them: skipping the intermediate
    ``str`` is most of orjson's decode advantage, and a WebSocket frame and an
    HTTP body both arrive as bytes.
    """
    if _FAST:
        return orjson.loads(data)
    return json.loads(data)


def dumps(obj: Any) -> str:
    """Encode JSON as ``str``, compact.

    ``str`` and not ``bytes``: see the module docstring. The extra decode still
    leaves this well ahead of the standard library, and it keeps every existing
    call site's frame semantics exactly as they were.
    """
    if _FAST:
        return orjson.dumps(obj).decode()
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def dumps_bytes(obj: Any) -> bytes:
    """Encode JSON as ``bytes``, for callers that genuinely want bytes.

    An HTTP body, a file, a Redis value. Never a WebSocket control message.
    """
    if _FAST:
        return orjson.dumps(obj)
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()


def backend() -> str:
    """Which codec is in use. Reported at startup and asserted in tests."""
    return "orjson" if _FAST else "stdlib"


__all__ = ("JsonError", "backend", "dumps", "dumps_bytes", "loads")
