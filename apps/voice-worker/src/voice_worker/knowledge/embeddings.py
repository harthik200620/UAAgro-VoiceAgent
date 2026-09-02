"""Dense embeddings for Tier-2 retrieval (§9).

``intfloat/multilingual-e5-base``: 768 dimensions, 100 languages, and the reason
it is the right choice here is that a Hindi question has to retrieve an English
document. §9 requires exactly that -- Hindi agronomy prose is sparse, the English
corpus is deeper, and a monolingual model would force the fusion step to do work
the embedding should have done.

**The prefixes are not optional.** E5 is trained with ``query:`` on questions and
``passage:`` on documents, and the asymmetry is what makes it work. Embedding a
question as a passage costs a large, silent chunk of retrieval quality -- nothing
errors, results just get worse -- which is why they are applied here rather than
left to each caller to remember.

Run through **ONNX Runtime**, not torch. The voice worker already carries
onnxruntime for Smart Turn (§5.2), and adding torch for one encoder would put a
gigabyte of CUDA-capable tensor library into a container whose real job is
shipping 20 ms audio frames on time.

The model is a download, not a credential, so a missing one fails loudly with
the command that fetches it (§0 rule 4). Retrieval degrades to BM25-only rather
than to nothing -- that is a decision the caller makes explicitly, in
``retrieval.py``, and it is reported rather than hidden.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import structlog

from uaagro_domain.errors import ConfigurationError

log = structlog.get_logger(__name__)

MODEL_NAME = "intfloat/multilingual-e5-base"
#: §10 sizes the pgvector column at 768. Changing the model means a migration,
#: not a config edit.
DIMENSIONS = 768
#: XLM-RoBERTa's window. Longer input is truncated by the tokeniser, which is
#: why chunking targets 500 tokens -- comfortably inside it.
MAX_SEQUENCE = 512

def default_model_dir() -> Path:
    """Where the model lives, honouring ``E5_MODEL_DIR``.

    Read through settings rather than hard-coded: a container mounts the
    weights read-only from a shared volume instead of baking a gigabyte into
    the image, and the download hint below promises this variable works.
    """
    from uaagro_domain.settings import get_settings

    return get_settings().e5_model_dir

#: The upstream repository publishes an ONNX export alongside the weights, so
#: there is no conversion step and no torch in the download path.
DOWNLOAD_HINT = (
    "Fetch the ONNX export and tokeniser (about 1.1 GB) with:\n"
    "  uv run python -c \"from huggingface_hub import hf_hub_download as d; "
    "import shutil, pathlib; "
    "p=pathlib.Path('models/multilingual-e5-base'); p.mkdir(parents=True, exist_ok=True); "
    "[shutil.copy(d('intfloat/multilingual-e5-base', r), p/l) "
    "for r, l in [('onnx/model.onnx','model.onnx'), ('tokenizer.json','tokenizer.json')]]\"\n"
    "or point E5_MODEL_DIR at an existing copy.\n"
    "Retrieval runs BM25-only without it, which cannot match a Hindi question "
    "against an English document -- so most Hindi queries return nothing."
)


@dataclass(frozen=True, slots=True)
class EmbeddingUnavailable:
    """Why dense retrieval is off, in words an operator can act on."""

    reason: str
    remedy: str


def ensure_model(directory: Path | None = None) -> tuple[Path, Path]:
    """Locate the ONNX graph and tokeniser, or fail naming what is missing."""
    directory = directory or default_model_dir()
    model = directory / "model.onnx"
    tokeniser = directory / "tokenizer.json"
    missing = [p.name for p in (model, tokeniser) if not p.is_file()]
    if missing:
        raise ConfigurationError(
            f"The embedding model is incomplete at {directory}: missing {', '.join(missing)}.",
            remedy=DOWNLOAD_HINT,
            context={"directory": str(directory), "missing": missing},
        )
    return model, tokeniser


class E5Embedder:
    """Encodes text to 768-dimensional unit vectors.

    Loading is lazy and one-shot. §7.5 wants everything warm before the first
    call, so :meth:`warm` is called at worker start -- but a worker that cannot
    load the model still serves Tier 1 and BM25, and finding that out at boot is
    better than finding it out mid-call.
    """

    def __init__(self, directory: Path | None = None, *, threads: int = 1) -> None:
        """
        Args:
            threads: ONNX intra-op threads. One by default, because in a worker
                the other job is shipping 20 ms audio frames on time and an
                encoder that grabs every core makes the media loop jitter.
                Ingestion is a batch job off the audio path and should raise
                this -- a 500-token passage costs about two seconds on one CPU
                core, so a corpus of any size is otherwise an overnight run.
        """
        self._directory = directory or default_model_dir()
        self._threads = max(1, threads)
        self._session: Any | None = None
        self._tokeniser: Any | None = None
        self._lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self._session is not None

    def load(self) -> None:
        """Load the graph and tokeniser. Raises if either is missing."""
        if self._session is not None:
            return
        model_path, tokeniser_path = ensure_model(self._directory)

        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ConfigurationError(
                "onnxruntime and tokenizers are required for dense retrieval.",
                remedy="Reinstall the voice-worker dependencies with `uv sync`.",
            ) from exc

        tokeniser = Tokenizer.from_file(str(tokeniser_path))
        tokeniser.enable_truncation(max_length=MAX_SEQUENCE)
        tokeniser.enable_padding()
        self._tokeniser = tokeniser
        options = ort.SessionOptions()
        options.intra_op_num_threads = self._threads
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        log.info(
            "embeddings.loaded",
            model=MODEL_NAME,
            dimensions=DIMENSIONS,
            threads=self._threads,
        )

    async def warm(self) -> EmbeddingUnavailable | None:
        """Load at start-up. Returns why it could not, rather than raising.

        A worker that refuses to boot because a knowledge model is absent
        cannot answer the 70% of calls §9 routes to Tier 1 -- so the failure is
        reported and the process continues, degraded and saying so.
        """
        async with self._lock:
            try:
                await asyncio.to_thread(self.load)
            except ConfigurationError as exc:
                log.warning("embeddings.unavailable", reason=exc.message)
                return EmbeddingUnavailable(reason=exc.message, remedy=exc.remedy)
            await asyncio.to_thread(self._encode, ["passage: warm-up"])
            return None

    async def embed_query(self, text: str) -> list[float]:
        """Embed a question. Applies the ``query:`` prefix."""
        vectors = await asyncio.to_thread(self._encode, [f"query: {text}"])
        return [float(v) for v in vectors[0]]

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed documents. Applies the ``passage:`` prefix."""
        if not texts:
            return []
        prefixed = [f"passage: {t}" for t in texts]
        vectors = await asyncio.to_thread(self._encode, prefixed)
        return [[float(v) for v in row] for row in vectors]

    def _encode(self, texts: Sequence[str]) -> np.ndarray:
        self.load()
        assert self._tokeniser is not None and self._session is not None

        encoded = self._tokeniser.encode_batch(list(texts))
        input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)

        feeds: dict[str, np.ndarray] = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
        }
        # Some exports keep a token_type_ids input; E5 does not use it, but the
        # graph still requires it to be fed.
        names = {i.name for i in self._session.get_inputs()}
        if "token_type_ids" in names:
            feeds["token_type_ids"] = np.zeros_like(input_ids)

        hidden: np.ndarray = self._session.run(None, feeds)[0]
        return _normalise(_mean_pool(hidden, attention_mask))


def _mean_pool(hidden: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Average the token vectors, ignoring padding.

    E5 pools by mean, not by the CLS token. Using CLS here would produce
    vectors that are stable, plausible and wrong -- the model was never trained
    to put sentence meaning there.
    """
    expanded = mask[..., None].astype(np.float32)
    summed = (hidden * expanded).sum(axis=1)
    counts = np.clip(expanded.sum(axis=1), a_min=1e-9, a_max=None)
    return summed / counts


def _normalise(vectors: np.ndarray) -> np.ndarray:
    """Unit-length rows, so cosine distance is a dot product."""
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    unit: np.ndarray = vectors / np.clip(norms, a_min=1e-12, a_max=None)
    return unit


__all__ = (
    "DIMENSIONS",
    "MAX_SEQUENCE",
    "MODEL_NAME",
    "E5Embedder",
    "EmbeddingUnavailable",
    "default_model_dir",
    "ensure_model",
)
