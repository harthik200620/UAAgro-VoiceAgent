"""Object storage for the things that are not rows: recordings, uploads.

One bucket, three prefixes (``recordings/``, ``knowledge/``, ``exports/``),
addressed by key and never by URL. §23-6 treats a recording URL like a phone
number, so nothing here produces one: the control plane streams bytes through
its own authenticated endpoint, and the panel never learns where they live.

Two backends behind one contract:

* :class:`ObjectStore` -- S3-compatible storage (MinIO in the compose stack,
  S3 in ap-south-1 in production). boto3 is synchronous and blocks; every call
  is pushed to a thread so the async services that use this keep their event
  loops for the work that has a latency budget.
* :class:`LocalObjectStore` -- a directory on disk. For development, tests and
  a single-host pilot with no bucket yet. Same keys, same semantics, so a
  recording written locally today streams through the same panel route it
  will stream through from S3 next month.

:func:`object_store` picks one from ``STORAGE_BACKEND``. Production refuses to
start on the local backend (see ``Settings.verify_production_readiness``): a
recording on a container's disk is a recording that vanishes with the
container.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import structlog

from uaagro_domain.errors import ConfigurationError, VendorError
from uaagro_domain.settings import Settings, get_settings

log = structlog.get_logger(__name__)

KNOWLEDGE_PREFIX = "knowledge"
EXPORT_PREFIX = "exports"
RECORDINGS_PREFIX = "recordings"

#: Streamed reads hand the caller chunks of this size. Large enough that a
#: three-minute recording is a few dozen reads, small enough to start playing
#: before the object has fully arrived.
STREAM_CHUNK_BYTES = 256 * 1024

#: How long to wait for the store to pick up, and for one read to return.
CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 30
#: A health probe gets less than a real request.
PING_TIMEOUT_S = 4.0

#: The sidecar that remembers an object's content type and metadata on disk.
_META_SUFFIX = ".meta.json"


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    size: int
    content_type: str | None


class ObjectStorage(Protocol):
    """What the API, the worker and the recorder need from a store."""

    @property
    def bucket(self) -> str: ...
    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def head(self, key: str) -> ObjectInfo | None: ...
    def stream(self, key: str) -> AsyncIterator[bytes]: ...
    async def delete(self, key: str) -> None: ...
    async def ping(self) -> bool: ...


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

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> None:
        client = self._ensure_client()
        params: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": data,
            "ContentType": content_type,
            # §17: a customer-managed key when there is one. The difference
            # from SSE-S3 is whether s3:GetObject alone is enough to hear a
            # farmer's voice.
            "ServerSideEncryption": "aws:kms" if self.settings.kms_key_id else "AES256",
        }
        if self.settings.kms_key_id:
            params["SSEKMSKeyId"] = self.settings.kms_key_id
        if metadata:
            params["Metadata"] = dict(metadata)
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


class LocalObjectStore:
    """Objects as files under one directory, keys as relative paths.

    Writes are atomic (a temporary file renamed into place) so a reader never
    sees half a recording, and every object carries a small sidecar with its
    content type and metadata so ``head`` answers the same questions the
    bucket would.
    """

    def __init__(self, root: Path, *, bucket: str = "local") -> None:
        self._root = root
        self._bucket = bucket

    @property
    def bucket(self) -> str:
        return self._bucket

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, key: str) -> Path:
        """Where a key lives, refusing anything that would leave the root.

        Keys are built by this codebase from ids and dates, never from user
        input -- but a store that trusted that would be one refactor away from
        serving ``../../.env`` to a recording request.
        """
        parts = [p for p in key.split("/") if p]
        if not parts or any(p in {".", ".."} or Path(p).is_absolute() for p in parts):
            raise VendorError(
                "Invalid object key.",
                remedy="Keys are relative paths built from ids; this one is not.",
                context={"key": key[:80]},
            )
        return self._root.joinpath(*parts)

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> None:
        path = self._path(key)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".part")
            temporary.write_bytes(data)
            temporary.replace(path)
            sidecar = {"content_type": content_type, "metadata": dict(metadata or {})}
            path.with_name(path.name + _META_SUFFIX).write_text(
                json.dumps(sidecar), encoding="utf-8"
            )

        try:
            await asyncio.to_thread(write)
        except OSError as exc:
            raise VendorError(
                "Could not write to local object storage.",
                remedy=f"Check that {self._root} exists and is writable (STORAGE_LOCAL_DIR).",
                context={"key": key},
            ) from exc
        log.info("storage.put", key=key, bytes=len(data), backend="local")

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            raise VendorError(
                "Could not read from local object storage.",
                remedy="Check that the object exists.",
                context={"key": key},
            ) from exc

    async def head(self, key: str) -> ObjectInfo | None:
        path = self._path(key)

        def probe() -> ObjectInfo | None:
            if not path.is_file():
                return None
            content_type: str | None = None
            sidecar = path.with_name(path.name + _META_SUFFIX)
            if sidecar.is_file():
                try:
                    content_type = json.loads(sidecar.read_text(encoding="utf-8")).get(
                        "content_type"
                    )
                except (OSError, ValueError):
                    content_type = None
            return ObjectInfo(size=path.stat().st_size, content_type=content_type)

        return await asyncio.to_thread(probe)

    async def stream(self, key: str) -> AsyncIterator[bytes]:
        path = self._path(key)
        try:
            handle = await asyncio.to_thread(path.open, "rb")
        except OSError as exc:
            raise VendorError(
                "Could not read from local object storage.",
                remedy="Check that the object exists.",
                context={"key": key},
            ) from exc
        try:
            while True:
                chunk = await asyncio.to_thread(handle.read, STREAM_CHUNK_BYTES)
                if not chunk:
                    return
                yield chunk
        finally:
            handle.close()

    async def delete(self, key: str) -> None:
        path = self._path(key)

        def remove() -> None:
            for candidate in (path, path.with_name(path.name + _META_SUFFIX)):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass

        try:
            await asyncio.to_thread(remove)
        except OSError as exc:
            log.warning("storage.delete_failed", key=key, error=type(exc).__name__)

    async def ping(self) -> bool:
        def probe() -> bool:
            self._root.mkdir(parents=True, exist_ok=True)
            return os.access(self._root, os.W_OK)

        try:
            return await asyncio.to_thread(probe)
        except OSError:
            return False


def object_store(settings: Settings | None = None) -> ObjectStorage:
    """The configured store (``STORAGE_BACKEND``: ``local`` or ``s3``)."""
    settings = settings or get_settings()
    if settings.storage_backend == "local":
        return LocalObjectStore(Path(settings.storage_local_dir).resolve())
    if settings.storage_backend == "s3":
        return ObjectStore(settings)
    raise ConfigurationError(
        f"STORAGE_BACKEND={settings.storage_backend!r} is not a backend this service knows.",
        remedy="Use 'local' (a directory, development and pilots) or 's3'.",
        context={"storage_backend": settings.storage_backend},
    )


def knowledge_key(document_id: str, filename: str) -> str:
    """Where a panel upload lives until it has been ingested and after."""
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in filename)[-120:]
    return f"{KNOWLEDGE_PREFIX}/{document_id}/{safe or 'upload'}"


__all__ = (
    "EXPORT_PREFIX",
    "KNOWLEDGE_PREFIX",
    "RECORDINGS_PREFIX",
    "LocalObjectStore",
    "ObjectInfo",
    "ObjectStorage",
    "ObjectStore",
    "knowledge_key",
    "object_store",
)
