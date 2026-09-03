"""G.711 µ-law codec (§4.3).

Twilio and Plivo carry µ-law; Exotel carries linear16. §4.3 is explicit that the
adapters stay separate rather than being parameterised, because conflating the
two produces white noise on a live farmer's phone -- and white noise that only
appears when a particular provider is configured is the kind of bug that ships.

Written out rather than taken from a library. Python's ``audioop`` was removed in
3.13, so a codec that compiles today would stop compiling on the next
interpreter; the standard is a fixed table and a few lines of bit manipulation,
and the dependency would be larger than the code.

This follows the reference implementation in ITU-T G.711 (Sun's ``g711.c``)
exactly, and the two details that are easy to get wrong are both here:

**The encoder works on 14 bits, not 16.** The sample is shifted right by two
before anything else. A version that skips the shift produces audio that is
recognisably speech and completely wrong -- measured at -5 dB SNR against the
reference's +38 dB, which sounds like a broken model rather than a broken codec.

**The sign is applied as an XOR mask, not an OR-then-complement.** The mask is
``0xFF`` for positive samples and ``0x7F`` for negative ones, which handles the
inversion µ-law transmits with and the sign bit in one operation.

Neither direction is lossless -- µ-law is 8-bit companded audio and discards the
low bits by design. The error is quantisation noise at telephone bandwidth,
which is what the entire PSTN already sounds like.
"""

from __future__ import annotations

#: G.711 constants, at the 14-bit scale the encoder works in.
_CLIP = 8159
_BIAS = 0x84

#: Segment boundaries for the exponent search, from the standard. Not tunable:
#: an off-by-one here is the -5 dB failure described above.
_SEGMENT_END = (0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF)


def _segment(value: int) -> int:
    for index, boundary in enumerate(_SEGMENT_END):
        if value <= boundary:
            return index
    return 8


def _encode_sample(sample: int) -> int:
    """One 16-bit signed sample to one µ-law byte."""
    value = sample >> 2  # G.711 encodes at 14-bit resolution.

    if value < 0:
        value = -value
        mask = 0x7F
    else:
        mask = 0xFF

    value = min(value, _CLIP) + (_BIAS >> 2)
    segment = _segment(value)
    if segment >= 8:
        return 0x7F ^ mask
    return ((segment << 4) | ((value >> (segment + 1)) & 0x0F)) ^ mask


#: Precomputed because encoding runs on every outbound 20 ms frame of every
#: concurrent call. 65,536 entries is 64 KB and buys a table lookup instead of
#: a segment search per sample.
_ENCODE_TABLE = bytes(_encode_sample(s if s < 32768 else s - 65536) for s in range(65536))


def _decode_sample(byte: int) -> int:
    value = ~byte & 0xFF
    magnitude = ((value & 0x0F) << 3) + _BIAS
    magnitude <<= (value & 0x70) >> 4
    return _BIAS - magnitude if value & 0x80 else magnitude - _BIAS


_DECODE_TABLE = tuple(_decode_sample(b) for b in range(256))


def pcm_to_mulaw(pcm: bytes) -> bytes:
    """16-bit little-endian PCM to µ-law.

    An odd trailing byte is dropped rather than padded: half a sample is a
    framing error upstream, and inventing the missing byte would turn a
    detectable bug into a click on the line.
    """
    usable = len(pcm) - (len(pcm) % 2)
    out = bytearray(usable // 2)
    table = _ENCODE_TABLE
    for index in range(0, usable, 2):
        out[index >> 1] = table[pcm[index] | (pcm[index + 1] << 8)]
    return bytes(out)


def mulaw_to_pcm(mulaw: bytes) -> bytes:
    """µ-law to 16-bit little-endian PCM."""
    out = bytearray(len(mulaw) * 2)
    table = _DECODE_TABLE
    for index, byte in enumerate(mulaw):
        sample = table[byte] & 0xFFFF
        out[index * 2] = sample & 0xFF
        out[index * 2 + 1] = (sample >> 8) & 0xFF
    return bytes(out)


__all__ = ("mulaw_to_pcm", "pcm_to_mulaw")
