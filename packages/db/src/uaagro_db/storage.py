"""Object storage for the things that are not rows: recordings, uploads.

One bucket, three prefixes (``recordings/``, ``knowledge/``, ``exports/``),
addressed by key and never by URL. §23-6 treats a recording URL like a phone
number, so nothing here produces one: the control plane streams bytes through
its own authenticated endpoint, and the panel never learns where they live.

boto3 is synchronous and blocks; every call is pushed to a thread so the
async services that use this -- the API serving a recording, the worker
reading an upload -- keep their event loops for the work that has a latency
budget.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import structlog

from uaagro_domain.errors import ConfigurationError, VendorError
from uaagro_domain.settings import Settings, get_settings

log = structlog.get_logger(__name__)

KNOWLEDGE_PREFIX = "knowledge"
EXPORT_PREFIX = "exports"

#: Streamed reads hand the caller chunks of this size. Large enough that a
#: three-minute recording is a few dozen reads, small enough to start playing
#: before the object has fully arrived.
STREAM_CHUNK_BYTES = 256 * 1024

#: How long to wait for the store to pick up, and for one read to return.
CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 30
#: A health probe gets less than a real request.
PING_TIMEOUT_S = 4.0


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    size: int
    content_type: str | None


class ObjectStore:
    """The bucket, with the credentials resolved once."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: Any = None

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:  # pragma: no cover - declared dependency
                raise ConfigurationError(
                    "boto3 is not installed.",
                    remedy="It is a declared dependency of uaagro-db; reinstall the package.",
                ) from exc
            self._client = boto3.client(
                "s3",
                endpoint_url=self.settings.s3_endpoint,
                region_name=self.settings.s3_region,
                aws_access_key_id=self.settings.require(
                    "s3_access_key_id", needed_for="object storage"
                ),
                aws_secret_access_key=self.settings.require(
                    "s3_secret_access_key", needed_for="object storage"
                ),
                config=Config(
                    s3={
                        "addressing_style": "path" if self.settings.s3_force_path_style else "auto"
                    },
                    retries={"max_attempts": 3, "mode": "standard"},
                    # A bucket that does not answer must fail in seconds, not
                    # in the minutes boto3 allows by default: the panel's
                    # health row and a recording download both wait on this.
                    connect_timeout=CONNECT_TIMEOUT_S,
                    read_timeout=READ_TIMEOUT_S,
                ),
            )
        return self._client

    @property
    def bucket(self) -> str:
        return self.settings.s3_bucket

    async def put(self, key: str, data: bytes, *, content_type: str) -> None:
        client = self._ensure_client()
        params: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": data,
            "ContentType": content_type,
            "ServerSideEncryption": "aws:kms" if self.settings.kms_key_id else "AES256",
        }
        if self.settings.kms_key_id:
            params["SSEKMSKeyId"] = self.settings.kms_key_id
        try:
            await asyncio.to_thread(client.put_object, **params)
        except Exception as exc:
            raise VendorError(
                "Could not write to object storage.",
                remedy="Check S3_ENDPOINT, the bucket name and the storage credentials.",
                context={"key": key},
            ) from exc
        log.info("storage.put", key=key, bytes=len(data))

    async def get(self, key: str) -> bytes:
        client = self._ensure_client()
        try:
            response = await asyncio.to_thread(client.get_object, Bucket=self.bucket, Key=key)
            body = response["Body"]
            return bytes(await asyncio.to_thread(body.read))
        except Exception as exc:
            raise VendorError(
                "Could not read from object storage.",
                remedy="Check that the object exists and the storage credentials are valid.",
                context={"key": key},
            ) from exc

    async def head(self, key: str) -> ObjectInfo | None:
        """Size and type, or None when the object does not exist."""
        client = self._ensure_client()
        try:
            response = await asyncio.to_thread(client.head_object, Bucket=self.bucket, Key=key)
        except Exception as exc:
            code = getattr(getattr(exc, "response", None), "get", lambda *_: None)("Error")
            if isinstance(code, dict) and code.get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            if type(exc).__name__ in {"NoSuchKey", "ClientError"}:
                return None
            raise VendorError(
                "Could not reach object storage.",
                remedy="Check S3_ENDPOINT and the storage credentials.",
                context={"key": key},
            ) from exc
        return ObjectInfo(
            size=int(response.get("ContentLength") or 0),
            content_type=response.get("ContentType"),
        )

    async def stream(self, key: str) -> AsyncIterator[bytes]:
        """Yield an object in chunks, so a recording starts playing early."""
        client = self._ensure_client()
        try:
            response = await asyncio.to_thread(client.get_object, Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise VendorError(
                "Could not read from object storage.",
                remedy="Check that the object exists and the storage credentials are valid.",
                context={"key": key},
            ) from exc
        body = response["Body"]
        try:
            while True:
                chunk = await asyncio.to_thread(body.read, STREAM_CHUNK_BYTES)
                if not chunk:
                    return
                yield bytes(chunk)
        finally:
            with_close = getattr(body, "close", None)
            if with_close is not None:
                with_close()

    async def delete(self, key: str) -> None:
        client = self._ensure_client()
        try:
            await asyncio.to_thread(client.delete_object, Bucket=self.bucket, Key=key)
        except Exception as exc:
            log.warning("storage.delete_failed", key=key, error=type(exc).__name__)

    async def ping(self) -> bool:
        """Whether the bucket answers, decided within a few seconds.

        For the panel's health row. A slow answer is reported as no answer:
        the page must not hang on a store that is down.
        """
        try:
            client = self._ensure_client()
            await asyncio.wait_for(
                asyncio.to_thread(client.head_bucket, Bucket=self.bucket), PING_TIMEOUT_S
            )
        except Exception:
            return False
        return True


def knowledge_key(document_id: str, filename: str) -> str:
    """Where a panel upload lives until it has been ingested and after."""
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in filename)[-120:]
    return f"{KNOWLEDGE_PREFIX}/{document_id}/{safe or 'upload'}"


__all__ = ("EXPORT_PREFIX", "KNOWLEDGE_PREFIX", "ObjectInfo", "ObjectStore", "knowledge_key")
