"""Fetch the local model weights (§5.2, §9).

Two downloads, both large and neither a credential:

- ``intfloat/multilingual-e5-base`` (~1.1 GB) for §9's dense retrieval. Without
  it, retrieval runs BM25-only, which cannot match a Hindi question against an
  English document -- so most Hindi queries return nothing.
- Pipecat Smart Turn v3.1 (~8 MB) for §5.2's semantic end-of-turn on the Marathi
  and non-Flux paths. Without it those languages fall back to VAD-plus-silence,
  which §5.1 labels a lower quality tier.

Neither is committed. ``models/`` is gitignored: a gigabyte in git history is
permanent, and these are reproducible from a public registry.

Idempotent -- a file that is already present is left alone, so this is safe to
put in a container build and safe to re-run after a partial download.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

E5_REPO = "intfloat/multilingual-e5-base"
#: The upstream repository publishes an ONNX export alongside the weights, so
#: there is no conversion step and no torch in the download path.
E5_FILES = (("onnx/model.onnx", "model.onnx"), ("tokenizer.json", "tokenizer.json"))
E5_DIR = Path("models/multilingual-e5-base")

SMART_TURN_REPO = "pipecat-ai/smart-turn-v3"
#: The **CPU** build, deliberately. §5.2 sizes this path at ~12 ms on CPU and
#: the worker has no GPU; the gpu variant would fail to load rather than fall
#: back.
#:
#: Pinned to **v3.1**, not the newest. As of 31 August 2026 the repository also
#: publishes v3.2, which is newer and which nothing here has evaluated. §19.5
#: makes a model change a thing that needs an eval run rather than a version
#: bump -- the turn detector decides when the agent starts talking, and a
#: regression shows up as interrupting farmers mid-sentence. Bump this once
#: `make eval` has measured false-cut and dead-air rates on v3.2.
SMART_TURN_FILES = (("smart-turn-v3.1-cpu.onnx", "smart-turn-v3.1.onnx"),)
SMART_TURN_DIR = Path("models")


def fetch(repo: str, files: tuple[tuple[str, str], ...], target: Path) -> int:
    from huggingface_hub import hf_hub_download

    target.mkdir(parents=True, exist_ok=True)
    fetched = 0
    for remote, local in files:
        destination = target / local
        if destination.is_file():
            sys.stdout.write(f"  {destination} already present\n")
            continue
        try:
            cached = hf_hub_download(repo, remote)
        except Exception as exc:
            # Reported, not raised. Smart Turn's absence degrades two languages;
            # e5's absence degrades retrieval. Neither should stop the other
            # from downloading, and neither should stop a container build that
            # only needs one of them.
            sys.stdout.write(f"  could not fetch {repo}/{remote}: {exc}\n")
            continue
        shutil.copy(cached, destination)
        size_mb = destination.stat().st_size // 1024 // 1024
        sys.stdout.write(f"  {destination} ({size_mb} MB)\n")
        fetched += 1
    return fetched


def main() -> int:
    sys.stdout.write(f"Embedding model ({E5_REPO}):\n")
    fetch(E5_REPO, E5_FILES, E5_DIR)

    sys.stdout.write(f"\nTurn detector ({SMART_TURN_REPO}):\n")
    fetch(SMART_TURN_REPO, SMART_TURN_FILES, SMART_TURN_DIR)

    missing = [
        str(path)
        for path in (
            E5_DIR / "model.onnx",
            E5_DIR / "tokenizer.json",
            SMART_TURN_DIR / "smart-turn-v3.1.onnx",
        )
        if not path.is_file()
    ]
    if missing:
        # A non-zero exit so a container build fails rather than shipping an
        # image whose degradation only shows up on a live call.
        sys.stdout.write(f"\nStill missing: {', '.join(missing)}\n")
        return 1

    sys.stdout.write("\nAll model weights present.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
