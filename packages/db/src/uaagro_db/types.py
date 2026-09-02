"""Custom column types.

The embedding dimension is fixed at the type level rather than read from
configuration: an HNSW index is built for a specific dimension, and changing
``EMBEDDING_DIM`` without a migration would produce rows the index cannot serve.
Switching models is therefore a migration, which is the honest cost.
"""

from __future__ import annotations

from pgvector.sqlalchemy import Vector

#: ``intfloat/multilingual-e5-base`` (§2). Must match ``EMBEDDING_DIM``.
EMBEDDING_DIM = 768

#: Column type for :attr:`uaagro_db.models.advisory.KbChunk.embedding`.
Vector768 = Vector(EMBEDDING_DIM)

__all__ = ["EMBEDDING_DIM", "Vector768"]
