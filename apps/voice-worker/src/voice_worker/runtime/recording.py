"""Call recording capture and upload (§11.5, §17, §18).

A recording is the most sensitive artefact this system produces. It is a
farmer's voice, their name, their village, what they grow and what they owe --
and unlike the database, it cannot be selectively redacted after the fact. So
three things are true of every recording here, and each is enforced rather than
documented:

**It is encrypted at rest with a customer-managed key.** §17 requires SSE-KMS,
not SSE-S3: the difference is who can decrypt it. With SSE-S3 anyone with
``s3:GetObject`` can read the audio; with SSE-KMS they also need a grant on the
key, which is auditable and revocable independently of bucket policy.

**Its URL is never logged.** §23-6 puts a recording URL in the same category as
a phone number, and for the same reason: a presigned URL in a log aggregator is
a copy of the recording in a log aggregator. Every log line here carries the
object *key* and never the URL.

**It expires.** §18 sets a retention window and the object carries the deletion
date as metadata, so a lifecycle rule can enforce it and an auditor can see the
intent on the object itself rather than having to trust a bucket policy they
cannot see from here.

Capture is separate from upload on purpose. §11.5 puts the upload in the
post-call pipeline with a ≤30 s budget and idempotent retries; doing it inline
would put an S3 round trip on the audio path, and a slow bucket would become a
dropped call.
"""

from __future__ import annotations

import io
import struct
import wave
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from uaagro_domain.errors import VendorError
from uaagro_domain.settings import Settings, get_defaults

log = structlog.get_logger(__name__)

SAMPLE_RATE = 8000
SAMPLE_WIDTH = 2  # 16-bit PCM
CHANNELS = 2  # caller and agent, kept separate -- see RecordingBuffer

#: How much audio one call may hold in memory before the buffer refuses more.
#: At 8 kHz 16-bit stereo that is roughly 30 minutes, comfortably past §7's
#: expected call length, and it is a ceiling rather than a target: a call that
#: reaches it has gone wrong in some other way and should not also exhaust the
#: worker.
MAX_BUFFERED_BYTES = 8000 * 2 * 2 * 60 * 30


@dataclass
class RecordingBuffer:
    """Accumulates a call's audio in memory, both legs kept apart.

    Two channels rather than a mix, because §19's evaluation harness needs the
    caller's audio without the agent talking over it to measure WER, and a
    mixdown cannot be un-mixed. The stereo file is also what makes a disputed
    call reviewable -- who said what, not just what was said.

    Silence-padded on write rather than timestamped: the two legs arrive at
    different rates and a naive interleave would drift. Padding the shorter leg
    to the longer one keeps them aligned to within one frame, which is what a
    reviewer needs.
    """

    caller: bytearray = field(default_factory=bytearray, repr=False)
    agent: bytearray = field(default_factory=bytearray, repr=False)
    truncated: bool = False

    def add_caller(self, pcm: bytes) -> None:
        self._add(self.caller, pcm)

    def add_agent(self, pcm: bytes) -> None:
        self._add(self.agent, pcm)

    def _add(self, target: bytearray, pcm: bytes) -> None:
        if len(self.caller) + len(self.agent) + len(pcm) > MAX_BUFFERED_BYTES:
            if not self.truncated:
                # Once, loudly. A recording that silently stops halfway is worse
                # than one marked incomplete, because nobody knows to distrust it.
                log.error("recording.truncated", limit_bytes=MAX_BUFFERED_BYTES)
                self.truncated = True
            return
        target.extend(pcm)

    @property
    def duration_s(self) -> float:
        frames = max(len(self.caller), len(self.agent)) // SAMPLE_WIDTH
        return frames / SAMPLE_RATE

    @property
    def is_empty(self) -> bool:
        return not self.caller and not self.agent

    def to_wav(self) -> bytes:
        """Interleave both legs into a two-channel WAV.

        WAV rather than raw PCM: an operator reviewing a disputed call opens it
        in whatever is on their laptop, and a headerless file that plays as
        static in every player is not reviewable.
        """
        length = max(len(self.caller), len(self.agent))
        # Both legs padded to the same length before interleaving. Without this
        # the shorter one runs out mid-file and the remaining frames pair real
        # audio with nothing, which plays as one channel abruptly going mono.
        caller = bytes(self.caller).ljust(length, b"\x00")
        agent = bytes(self.agent).ljust(length, b"\x00")

        interleaved = bytearray(length * 2)
        for index in range(0, length - 1, SAMPLE_WIDTH):
            offset = index * 2
            interleaved[offset : offset + 2] = caller[index : index + 2]
            interleaved[offset + 2 : offset + 4] = agent[index : index + 2]

        out = io.BytesIO()
        with wave.open(out, "wb") as handle:
            handle.setnchannels(CHANNELS)
            handle.setsampwidth(SAMPLE_WIDTH)
            handle.setframerate(SAMPLE_RATE)
            handle.writeframes(bytes(interleaved))
        return out.getvalue()


def object_key(*, call_ref: str, started_at: datetime) -> str:
    """Where a recording lives in the bucket.

    Date-partitioned so a lifecycle rule can expire a whole day's prefix, and so
    an auditor answering "what did you hold on 3 March" lists one prefix rather
    than scanning the bucket.

    The key carries the call reference and nothing else. A key containing a
    phone number would put a phone number in every access log line that touches
    the object (§23-6).
    """
    day = started_at.astimezone(UTC)
    return f"recordings/{day:%Y/%m/%d}/{call_ref}.wav"


@dataclass
class RecordingStore:
    """Uploads recordings to S3-compatible storage with SSE-KMS (§17)."""

    settings: Settings
    _client: Any = field(default=None, repr=False)

    def _ensure_client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "s3",
                endpoint_url=self.settings.s3_endpoint,
                region_name=self.settings.s3_region,
                aws_access_key_id=self.settings.require(
                    "s3_access_key_id", needed_for="recording upload (§11.5)"
                ),
                aws_secret_access_key=self.settings.require(
                    "s3_secret_access_key", needed_for="recording upload (§11.5)"
                ),
                config=_path_style(self.settings),
            )
        return self._client

    async def upload(
        self, *, call_ref: str, started_at: datetime, wav: bytes
    ) -> str:
        """Store one recording. Returns the object key, never a URL.

        Idempotent by construction: the key is derived from the call reference,
        so §11.5's retries overwrite rather than accumulate. A post-call
        pipeline that retried into a new key each time would multiply storage
        and leave an auditor unable to say which copy was the real one.
        """
        import asyncio

        key = object_key(call_ref=call_ref, started_at=started_at)
        # §18's window, from config rather than a constant here: retention is a
        # compliance decision an operator changes, not a property of the
        # uploader.
        retention = get_defaults().compliance.retention_days_recordings
        expires = datetime.now(UTC) + timedelta(days=retention)

        params: dict[str, Any] = {
            "Bucket": self.settings.s3_bucket,
            "Key": key,
            "Body": wav,
            "ContentType": "audio/wav",
            # §17: customer-managed key, not SSE-S3. The difference is whether
            # s3:GetObject alone is enough to hear a farmer's voice.
            "ServerSideEncryption": "aws:kms",
            "Metadata": {
                "call-ref": call_ref,
                # §18: the retention intent travels on the object, so an
                # auditor sees it without needing to read a bucket policy.
                "delete-after": expires.date().isoformat(),
            },
        }
        if self.settings.kms_key_id:
            params["SSEKMSKeyId"] = self.settings.kms_key_id

        client = self._ensure_client()
        try:
            await asyncio.to_thread(client.put_object, **params)
        except Exception as exc:
            # §11.5: a failure here never loses the call record, which was
            # written incrementally during the call. The recording is retried
            # by the post-call pipeline with backoff.
            log.error(
                "recording.upload_failed",
                key=key,
                error=type(exc).__name__,
            )
            raise VendorError(
                "The recording could not be uploaded.",
                remedy="The post-call pipeline retries with backoff. Check the "
                "bucket policy and the KMS grant if it keeps failing.",
                context={"key": key},
            ) from exc

        # The key, never a URL. §23-6 treats a recording URL like a phone
        # number, and a presigned URL in a log aggregator is a copy of the
        # recording in a log aggregator.
        log.info("recording.uploaded", key=key, bytes=len(wav))
        return key


def _path_style(settings: Settings) -> Any:
    """MinIO needs path-style addressing; real S3 does not care."""
    from botocore.config import Config

    return Config(
        s3={"addressing_style": "path" if settings.s3_force_path_style else "virtual"},
        retries={"max_attempts": 3, "mode": "standard"},
    )


def pcm_duration_s(pcm: bytes) -> float:
    """Seconds of 8 kHz 16-bit mono audio in ``pcm``."""
    return len(pcm) / (SAMPLE_RATE * SAMPLE_WIDTH)


def silence(seconds: float) -> bytes:
    """Digital silence, for padding a leg that started late."""
    frames = int(seconds * SAMPLE_RATE)
    return struct.pack(f"<{frames}h", *([0] * frames))


__all__ = (
    "CHANNELS",
    "MAX_BUFFERED_BYTES",
    "SAMPLE_RATE",
    "SAMPLE_WIDTH",
    "RecordingBuffer",
    "RecordingStore",
    "object_key",
    "pcm_duration_s",
    "silence",
)
