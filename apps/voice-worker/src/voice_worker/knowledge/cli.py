"""``uaagro-kb`` -- ingest, embed and inspect the knowledge corpus (§9).

Lives in the voice worker rather than in ``uaagro-db`` because ingestion needs
the chunker and the embedder, and pointing the database package at the worker
would invert the dependency.

Ingestion never publishes. ``uaagro-kb ingest`` loads and indexes; making a
document servable is ``uaagro-kb publish``, which demands a named approver --
§9 requires an agronomist to confirm the `[VERIFY]` sections of the seed corpus,
and a one-command path from file to caller would remove the only step that
catches a wrong dose before a farmer hears it.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from uaagro_db.engine import dispose_engines, migrator_session
from uaagro_db.models import Crop, KbChunk, KbDocument, Product, User
from uaagro_domain.errors import UAAgroError

from .embeddings import E5Embedder
from .ingest import ingest_file, publish_document


def _out(message: str = "") -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


async def _vocabularies(session: AsyncSession) -> tuple[list[str], list[str]]:
    """Crop and product names, for tagging chunks at ingest time."""
    crops = list((await session.scalars(select(Crop.name_en))).all())
    crops += list((await session.scalars(select(Crop.name_hi))).all())
    products = list((await session.scalars(select(Product.name_hi))).all())
    return [c for c in crops if c], [p for p in products if p]


async def _ingest(args: argparse.Namespace) -> int:
    path = Path(args.path)
    # A stat call from a CLI coroutine is not a latency concern, but the rule
    # that catches blocking I/O in the media path does not know the difference.
    if not await asyncio.to_thread(path.is_file):
        raise UAAgroError(
            f"{path} does not exist.",
            remedy="Give the path to a markdown document.",
        )

    embedder: E5Embedder | None = None
    if not args.no_embed:
        # Ingestion is off the audio path, so it may use the machine. One
        # core to spare keeps the terminal responsive during a long corpus.
        embedder = E5Embedder(threads=max(1, (os.cpu_count() or 2) - 1))
        unavailable = await embedder.warm()
        if unavailable is not None:
            # Not fatal. BM25 still indexes, and the report says how many
            # chunks have no vector -- a state the operator can see and fix,
            # rather than a corpus that is quietly half-indexed.
            _out(f"warning: {unavailable.reason}")
            _out(unavailable.remedy)
            _out()
            embedder = None

    async with migrator_session() as session:
        crops, products = await _vocabularies(session)
        report = await ingest_file(
            session,
            path,
            doc_type=args.doc_type,
            language=args.language,
            embedder=embedder,
            crop_vocabulary=crops,
            product_vocabulary=products,
            publish=False,
        )
        await session.commit()

    _out(report.summary())
    for warning in report.warnings:
        _out(f"warning: {warning}")
    if not report.skipped_unchanged:
        _out()
        _out(
            f"Not published. Review it, then:\n"
            f"  uv run uaagro-kb publish {report.document_id} --approver <email>"
        )
    return 0


async def _publish(args: argparse.Namespace) -> int:
    async with migrator_session() as session:
        approver: User | None = await session.scalar(
            select(User).where(User.email == args.approver)
        )
        if approver is None:
            raise UAAgroError(
                f"No user with email {args.approver}.",
                remedy="Publishing records who approved the content, so the "
                "approver must be a real user.",
            )
        await publish_document(session, uuid.UUID(args.document_id), approved_by=approver.id)
        await session.commit()
    _out(f"Published {args.document_id}, approved by {args.approver}.")
    return 0


async def _status(args: argparse.Namespace) -> int:
    async with migrator_session() as session:
        rows = (
            await session.execute(
                select(
                    KbDocument.id,
                    KbDocument.title,
                    KbDocument.version,
                    KbDocument.is_published,
                    func.count(KbChunk.id).label("chunks"),
                    func.count(KbChunk.embedding).label("embedded"),
                )
                .outerjoin(KbChunk, KbChunk.document_id == KbDocument.id)
                .group_by(KbDocument.id)
                .order_by(KbDocument.title)
            )
        ).all()

    if not rows:
        _out("No documents ingested.")
        return 0

    _out(f"{'document':<44} {'ver':>4} {'chunks':>7} {'vectors':>8}  state")
    _out("-" * 82)
    unpublished = 0
    for row in rows:
        state = "published" if row.is_published else "DRAFT -- not servable"
        unpublished += 0 if row.is_published else 1
        _out(f"{row.title[:44]:<44} {row.version:>4} {row.chunks:>7} {row.embedded:>8}  {state}")
    if unpublished:
        # §9 requires the admin panel to carry this banner until the queue is
        # empty. The CLI says the same thing for anyone working without it.
        _out()
        _out(f"{unpublished} document(s) awaiting agronomist approval.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uaagro-kb", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="chunk, embed and store a markdown document")
    ingest.add_argument("path")
    ingest.add_argument("--doc-type", default="knowledge_base")
    ingest.add_argument("--language", default="hi")
    ingest.add_argument(
        "--no-embed",
        action="store_true",
        help="index for BM25 only, skipping the embedding model",
    )
    ingest.set_defaults(func=_ingest)

    publish = subparsers.add_parser("publish", help="make a reviewed document servable")
    publish.add_argument("document_id")
    publish.add_argument("--approver", required=True, help="email of the approving agronomist")
    publish.set_defaults(func=_publish)

    status = subparsers.add_parser("status", help="documents, chunks and approval state")
    status.set_defaults(func=_status)

    args = parser.parse_args(argv)

    async def run() -> int:
        try:
            result: int = await args.func(args)
            return result
        finally:
            await dispose_engines()

    try:
        return asyncio.run(run())
    except UAAgroError as exc:
        _out(f"error: {exc.message}")
        if exc.remedy:
            _out(exc.remedy)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
