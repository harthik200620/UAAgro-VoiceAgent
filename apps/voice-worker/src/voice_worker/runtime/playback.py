"""Outbound playback and barge-in (§5.4).

Farmers interrupt constantly, and an agent that talks over them reads as
disrespectful. §5.4 lists four things that must happen on speech-start, and the
third is the one that is usually missed:

1. cancel the TTS stream;
2. send the provider's buffer-clear so queued audio is dropped;
3. **truncate the assistant message in LLM context to what was actually
   spoken**, by played-byte count rather than by what was generated;
4. suppress barge-in only during the mandatory disclosure (§13.2).

Step 3 exists because without it the model believes it said things the farmer
never heard, and the conversation desynchronises: the agent references a price
it never got to, or repeats itself because it thinks it was interrupted earlier
than it was.

**Pacing is what makes step 3 honest.** If the worker pushes audio at the
provider as fast as the socket accepts it, "bytes sent" runs seconds ahead of
"bytes heard" and the truncation point is fiction. :class:`PacedSender` emits
one 20 ms frame every 20 ms, so sent and played differ only by the provider's
small jitter buffer -- which is exactly what the clear message discards.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

from ..text.script import words as script_words
from . import audio as audio_utils

log = structlog.get_logger(__name__)

#: §5.4: barge-in must cut audio within 200 ms.
CANCEL_DEADLINE_MS = 200

#: §13.2: the automated-call disclosure is a legal requirement, so it alone
#: resists interruption -- and only for long enough to be heard.
DISCLOSURE_SUPPRESSION_MS = 600

#: Provider-side jitter buffer assumed when converting sent bytes to heard
#: bytes. Deliberately conservative: over-estimating what the farmer heard
#: leaves the model believing it said something it did not, which is the
#: failure this module exists to prevent.
ASSUMED_JITTER_MS = 60


@dataclass(slots=True)
class Segment:
    """One synthesised sentence and the audio it produced."""

    text: str
    audio_bytes: int
    #: Cumulative bytes at the end of this segment.
    end_offset: int


@dataclass
class PlaybackTracker:
    """Maps played audio back to the words the caller actually heard.

    Sentence-granular by design. §5.3 already chunks synthesis on sentence
    boundaries, and truncating mid-word would put a fragment into the
    transcript that nobody said.
    """

    segments: list[Segment] = field(default_factory=list)
    #: Bytes handed to the telephony transport.
    bytes_sent: int = 0

    def register(self, text: str, audio: bytes) -> None:
        """Record a synthesised segment before it is sent."""
        if not audio:
            return
        end = (self.segments[-1].end_offset if self.segments else 0) + len(audio)
        self.segments.append(Segment(text=text, audio_bytes=len(audio), end_offset=end))

    def open_segment(self, text: str) -> None:
        """Start a segment whose audio has not arrived yet.

        The streaming counterpart of :meth:`register`. A streamed sentence is
        the same unit as a collected one -- §5.4 truncates on sentence
        boundaries and proportionally within the sentence that was playing --
        so it stays one segment that grows, rather than one segment per audio
        chunk. Chunk-sized segments would make the truncation land on whatever
        arbitrary boundary the vendor happened to flush at.
        """
        end = self.segments[-1].end_offset if self.segments else 0
        self.segments.append(Segment(text=text, audio_bytes=0, end_offset=end))

    def grow(self, byte_count: int) -> None:
        """Extend the open segment as its audio arrives."""
        if not self.segments or byte_count <= 0:
            return
        segment = self.segments[-1]
        segment.audio_bytes += byte_count
        segment.end_offset += byte_count

    def mark_sent(self, byte_count: int) -> None:
        self.bytes_sent += byte_count

    @property
    def total_bytes(self) -> int:
        return self.segments[-1].end_offset if self.segments else 0

    @property
    def full_text(self) -> str:
        return " ".join(segment.text for segment in self.segments if segment.text)

    def heard_bytes(self, *, jitter_ms: int = ASSUMED_JITTER_MS) -> int:
        """Bytes the caller plausibly heard.

        Discounts the provider's jitter buffer from what was sent. Rounds
        *down*: crediting the agent with audio the farmer may not have heard is
        the error that desynchronises the conversation.
        """
        jitter_bytes = audio_utils.SAMPLE_RATE * audio_utils.SAMPLE_WIDTH * jitter_ms // 1000
        return max(0, min(self.bytes_sent, self.total_bytes) - jitter_bytes)

    def spoken_text(self, *, jitter_ms: int = ASSUMED_JITTER_MS) -> str:
        """The prefix of the utterance the caller actually heard.

        Complete segments are included whole. The segment that was playing when
        the interruption landed is truncated to a whole-word boundary, in
        proportion to how much of its audio had gone out.
        """
        heard = self.heard_bytes(jitter_ms=jitter_ms)
        if heard <= 0 or not self.segments:
            return ""

        spoken: list[str] = []
        for segment in self.segments:
            if segment.end_offset <= heard:
                spoken.append(segment.text)
                continue

            start = segment.end_offset - segment.audio_bytes
            if heard <= start:
                break
            fraction = (heard - start) / segment.audio_bytes
            partial = _truncate_to_words(segment.text, fraction)
            if partial:
                spoken.append(partial)
            break

        return " ".join(part for part in spoken if part).strip()

    def reset(self) -> None:
        self.segments.clear()
        self.bytes_sent = 0


def _truncate_to_words(text: str, fraction: float) -> str:
    """Keep the leading ``fraction`` of ``text``, on a word boundary.

    Word-proportional rather than character-proportional: speech time tracks
    words far better than characters, and Devanagari words vary wildly in
    character count because of combining marks.
    """
    tokens = script_words(text)
    if not tokens:
        return ""
    keep = int(len(tokens) * max(0.0, min(1.0, fraction)))
    return " ".join(tokens[:keep])


class PacedSender:
    """Sends audio at real time so barge-in can be honest and prompt.

    Blasting frames at the provider would be faster to *deliver* and worse in
    every way that matters: the buffer-clear would discard seconds of already-
    committed audio, and the played-byte count would be meaningless.
    """

    def __init__(
        self,
        send_frame: Callable[[bytes], Awaitable[None]],
        *,
        tracker: PlaybackTracker | None = None,
        realtime: bool = True,
    ) -> None:
        self._send_frame = send_frame
        self.tracker = tracker or PlaybackTracker()
        self._realtime = realtime
        self._cancelled = asyncio.Event()
        #: Bytes of a streamed utterance not yet forming a whole frame.
        self._residue = bytearray()
        #: When the next frame is due. Spans an utterance, not a chunk.
        self._next_slot: float | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        """Stop sending. Idempotent, and safe from any task."""
        self._cancelled.set()

    def resume(self) -> None:
        self._cancelled.clear()
        # A new utterance starts with an empty tail and a fresh clock. Carrying
        # either across a barge-in would splice the interrupted answer's last
        # milliseconds onto the front of the next one.
        self._residue.clear()
        self._next_slot = None

    async def play(self, text: str, audio: bytes) -> bool:
        """Send one complete segment, frame by frame.

        Returns True if the whole segment went out, False if it was cut short
        by :meth:`cancel`.
        """
        if not audio:
            return True

        self.tracker.register(text, audio)
        self._next_slot = None
        sent = await self._send_paced(audio)
        if sent:
            await self.flush()
        return sent

    async def play_chunk(self, audio: bytes) -> bool:
        """Send part of a segment that is still arriving.

        The streaming counterpart of :meth:`play`. Two things it must not do,
        both of which a naive per-chunk `play` would:

        **Re-frame at every chunk boundary.** `iter_frames` zero-pads a short
        final frame, so framing each chunk on its own would splice up to 20 ms
        of silence into the middle of a word every time the vendor flushed.
        The remainder is carried in `_residue` and only padded by :meth:`flush`.

        **Restart the pacing clock.** `play` anchors its clock at the first
        frame; doing that per chunk would send each chunk's audio immediately
        and then wait, which is a stutter rather than real-time playback.
        `_next_slot` persists across chunks for exactly one utterance.
        """
        if not audio:
            return True
        self.tracker.grow(len(audio))
        self._residue.extend(audio)

        whole = len(self._residue) // audio_utils.FRAME_BYTES * audio_utils.FRAME_BYTES
        if whole == 0:
            return not self._cancelled.is_set()

        payload = bytes(self._residue[:whole])
        del self._residue[:whole]
        return await self._send_paced(payload)

    async def flush(self) -> bool:
        """Send whatever is left of the current utterance, padded.

        Called at the end of a sentence so the tail is not held back waiting
        for a chunk that will never come.
        """
        if not self._residue:
            self._next_slot = None
            return True
        payload = bytes(self._residue)
        self._residue.clear()
        sent = await self._send_paced(payload)
        self._next_slot = None
        return sent

    async def _send_paced(self, audio: bytes) -> bool:
        """Frame and send at real time, keeping the clock across calls."""
        if self._next_slot is None:
            self._next_slot = time.perf_counter()

        for frame in audio_utils.iter_frames(audio):
            if self._cancelled.is_set():
                return False
            await self._send_frame(frame)
            self.tracker.mark_sent(len(frame))

            if self._realtime:
                # Sleep to the next slot rather than a flat 20 ms, so send
                # latency does not accumulate into drift over a long answer.
                self._next_slot += audio_utils.FRAME_MS / 1000
                delay = self._next_slot - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
        return True


@dataclass
class BargeInPolicy:
    """Decides whether a detected speech-start may interrupt (§5.4).

    Only the mandatory disclosure is protected, and only briefly. §13.2 requires
    the caller to be told the call is automated; everything else the agent says
    can and should be interruptible.
    """

    suppression_ms: int = DISCLOSURE_SUPPRESSION_MS
    #: Set while the disclosure is playing.
    protecting_disclosure: bool = False
    _protected_since: float | None = None

    def begin_disclosure(self) -> None:
        self.protecting_disclosure = True
        self._protected_since = time.perf_counter()

    def end_disclosure(self) -> None:
        self.protecting_disclosure = False
        self._protected_since = None

    def may_interrupt(self, *, now: float | None = None) -> bool:
        if not self.protecting_disclosure or self._protected_since is None:
            return True
        elapsed_ms = ((now or time.perf_counter()) - self._protected_since) * 1000
        return elapsed_ms >= self.suppression_ms


@dataclass(frozen=True, slots=True)
class BargeInResult:
    """What happened when the caller interrupted."""

    interrupted: bool
    #: The prefix the caller actually heard, for the LLM context.
    spoken_text: str = ""
    #: Milliseconds from speech-start to audio stopping. §5.4 caps this at 200.
    cut_latency_ms: float = 0.0
    reason: str = ""


async def handle_barge_in(
    sender: PacedSender,
    *,
    policy: BargeInPolicy,
    clear_playback: Callable[[], Awaitable[None]],
    cancel_synthesis: Callable[[], Awaitable[None]] | None = None,
) -> BargeInResult:
    """Execute the §5.4 sequence.

    Both halves are required. Cancelling generation without clearing the
    provider's buffer leaves the agent talking for another second, which is the
    failure everyone ships; clearing without cancelling means the synthesiser
    keeps producing audio for an utterance nobody will hear, and keeps billing
    for it.
    """
    started = time.perf_counter()

    if not policy.may_interrupt():
        return BargeInResult(
            interrupted=False,
            reason="disclosure is protected for its first moments (spec 13.2)",
        )

    sender.cancel()
    if cancel_synthesis is not None:
        await cancel_synthesis()
    await clear_playback()

    spoken = sender.tracker.spoken_text()
    elapsed_ms = (time.perf_counter() - started) * 1000

    if elapsed_ms > CANCEL_DEADLINE_MS:
        # Surfaced rather than swallowed: §5.4 gives this 200 ms and a breach
        # is audible as the agent talking over the farmer.
        log.warning("bargein.slow_cut", cut_latency_ms=round(elapsed_ms, 1))

    log.info(
        "bargein.cut",
        cut_latency_ms=round(elapsed_ms, 1),
        heard_bytes=sender.tracker.heard_bytes(),
        total_bytes=sender.tracker.total_bytes,
    )
    return BargeInResult(
        interrupted=True,
        spoken_text=spoken,
        cut_latency_ms=elapsed_ms,
        reason="caller started speaking",
    )
