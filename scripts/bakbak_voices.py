"""List the Bakbak voices this account can use (§5.3).

Voice ids are account-scoped UUIDs, so there is no list to check into the repo
and no sensible default -- which is why ``BAKBAK_VOICE_HI`` has none and an
unset one fails loudly instead of picking a stranger's voice.

Run this once the key is in ``.env``, then put the chosen ids back into
``BAKBAK_VOICE_HI`` and ``BAKBAK_VOICE_EN``:

.. code-block:: console

    uv run python scripts/bakbak_voices.py

§5.3 asks for the choice to be made in a bake-off **over a real phone line**,
not from a name in a table. A voice that sounds warm on laptop speakers can be
unintelligible through 8 kHz GSM, which is the only channel that matters here.
So this prints the shortlist; ``--sample`` renders one line of Hindi per voice
at telephony bandwidth so there is something to actually listen to.

Nothing here prints the API key, and the WAV files land outside the repo.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

from uaagro_domain.settings import Settings
from voice_worker.adapters.tts.bakbak import BakbakTTS
from voice_worker.adapters.tts.base import TtsConfig
from voice_worker.runtime.audio import write_wav

#: One utterance from §5.3's bake-off set: a greeting, a product name and a
#: price, which is what most of the helpline's speech actually is.
SAMPLE_HI = "नमस्ते जी, डीएपी पचास किलो का बैग तेरह सौ पचास रुपये का है।"


async def run(sample_dir: pathlib.Path | None) -> int:
    settings = Settings()
    if not settings.bakbak_api_key:
        sys.stderr.write("BAKBAK_API_KEY is not set in .env.\n")
        return 1

    tts = BakbakTTS(settings)
    try:
        voices = await tts.voices()
        if not voices:
            sys.stderr.write("The account has no voices. Ask Raya to enable some.\n")
            return 1

        width = max(len(str(v.get("name", ""))) for v in voices)
        print(f"{len(voices)} voices, model {settings.bakbak_model!r} in use\n")
        for voice in sorted(voices, key=lambda v: (str(v.get("language")), str(v.get("name")))):
            marker = " " if voice.get("model") == settings.bakbak_model else "!"
            print(
                f"{marker} {voice.get('name', '')!s:{width}}  "
                f"{voice.get('language', '')!s:6}  "
                f"{voice.get('model', ''):8}  {voice.get('id', '')}"
            )
        if any(v.get("model") != settings.bakbak_model for v in voices):
            print(
                "\n!  belongs to another model. A voice id from one model is rejected\n"
                "   by the other, so change BAKBAK_MODEL too if you pick one of these."
            )

        if sample_dir is None:
            print("\nPass --sample to render one line of Hindi per voice at 8 kHz.")
            return 0

        for voice in voices:
            language = str(voice.get("language", ""))
            if not language.startswith("hi"):
                continue
            config = TtsConfig(
                language="hi-IN",
                model=str(voice.get("model") or settings.bakbak_model),
                speaker=str(voice.get("id")),
                sample_rate=settings.tts_output_sample_rate,
            )
            audio = await tts.synthesise_all(SAMPLE_HI, config)
            path = sample_dir / f"{voice.get('name', voice.get('id'))}.wav"
            path.write_bytes(write_wav(audio))
            print(f"wrote {path}")
        print("\nListen on a phone, not on laptop speakers (§5.3).")
        return 0
    finally:
        await tts.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sample",
        nargs="?",
        const="bakbak-samples",
        default=None,
        metavar="DIR",
        help="render one Hindi line per voice into DIR (default: ./bakbak-samples)",
    )
    args = parser.parse_args()
    sample_dir = pathlib.Path(args.sample) if args.sample else None
    if sample_dir is not None:
        # Before the event loop and before any vendor call: an unwritable
        # directory should fail here, not after paying for eleven renders.
        sample_dir.mkdir(parents=True, exist_ok=True)
    return asyncio.run(run(sample_dir))


if __name__ == "__main__":
    raise SystemExit(main())
