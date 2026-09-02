"""``uaagro-sim`` -- place a simulated call at the voice worker.

    uaagro-sim --wav fixtures/audio/synthetic_speech_8k.wav
    uaagro-sim --dtmf 1200=1 --out reply.wav
    uaagro-sim --drop-after 900          # simulate a dropped GSM line

Uses ``sys.stdout`` rather than ``print`` (§22) -- this is an operator tool whose
output is read from a terminal, not a log stream.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from ..runtime import audio as audio_utils
from .client import TelephonySimulator

DEFAULT_URL = "ws://localhost:8080/ws/voice"
DEFAULT_FIXTURE = Path("fixtures/audio/synthetic_speech_8k.wav")


def _out(message: str = "") -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def _parse_dtmf(values: list[str]) -> dict[int, str]:
    """``--dtmf 1200=1`` -> ``{1200: "1"}``."""
    schedule: dict[int, str] = {}
    for raw in values:
        offset, _, digit = raw.partition("=")
        if not digit or digit not in "0123456789*#":
            raise argparse.ArgumentTypeError(
                f"--dtmf expects OFFSET_MS=DIGIT with a digit in 0-9 * #, got {raw!r}"
            )
        schedule[int(offset)] = digit
    return schedule


def _load_or_make_audio(path: Path, *, duration_ms: int) -> bytes:
    """Read the fixture, generating a synthetic placeholder if it is absent.

    The generated file is a two-tone pattern, not speech, and is named to say
    so. Real recorded audio -- noisy Hindi, elderly speech, code-mixing -- is
    the Phase 2 evaluation corpus (§19); pretending a tone is speech would make
    the Phase 1 gate meaningless.
    """
    if path.is_file():
        return audio_utils.read_wav(path.read_bytes())

    _out(f"fixture {path} not found -- generating a synthetic placeholder")
    segment = duration_ms // 4
    pcm = (
        audio_utils.tone(300, segment)
        + audio_utils.silence(segment // 2)
        + audio_utils.tone(520, segment)
        + audio_utils.silence(segment // 2)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(audio_utils.write_wav(pcm))
    return pcm


def _write_wav(path: Path, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(audio_utils.write_wav(pcm))


async def _run(args: argparse.Namespace) -> int:
    pcm = await asyncio.to_thread(_load_or_make_audio, Path(args.wav), duration_ms=args.generate_ms)
    simulator = TelephonySimulator(args.url, realtime=not args.fast)

    _out(f"calling {args.url}")
    _out(f"  call_sid   {simulator.call_sid}")
    _out(
        f"  audio in   {audio_utils.duration_ms(pcm)} ms "
        f"({audio_utils.frame_count(pcm)} frames of {audio_utils.FRAME_MS} ms)"
    )

    result = await simulator.place_call(
        pcm,
        dtmf_after_ms=_parse_dtmf(args.dtmf),
        drop_after_ms=args.drop_after,
        listen_tail_ms=args.tail_ms,
    )

    _out()
    _out("result")
    _out(f"  frames sent      {result.frames_sent}")
    _out(f"  frames received  {result.frames_received}")
    _out(f"  audio received   {result.audio_duration_ms} ms")
    if result.first_audio_ms is not None:
        _out(f"  first audio in   {result.first_audio_ms:.0f} ms  (spec 11.1 target: ~50 ms)")
    _out(f"  clear messages   {result.clears_received}")
    if result.errors:
        for error in result.errors:
            _out(f"  ERROR            {error}")

    if result.audio_received and args.out:
        out_path = Path(args.out)
        await asyncio.to_thread(_write_wav, out_path, result.audio_received)
        _out(f"  wrote            {out_path}")

    if args.drop_after is not None:
        _out()
        _out("  socket was dropped mid-call; the worker should have persisted a")
        _out("  partial record with status=abandoned (spec 11.4).")
        return 0

    if not result.frames_received:
        _out()
        _out("  FAIL: the worker returned no audio.")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uaagro-sim", description=__doc__)
    parser.add_argument(
        "--url", default=DEFAULT_URL, help=f"worker WebSocket (default {DEFAULT_URL})"
    )
    parser.add_argument("--wav", default=str(DEFAULT_FIXTURE), help="8 kHz mono 16-bit WAV to send")
    parser.add_argument("--out", default=None, help="write the worker's audio to this WAV")
    parser.add_argument(
        "--dtmf",
        action="append",
        default=[],
        metavar="OFFSET_MS=DIGIT",
        help="inject a keypress, e.g. --dtmf 1200=1",
    )
    parser.add_argument(
        "--drop-after",
        type=int,
        default=None,
        metavar="MS",
        help="close the socket abruptly, simulating a dropped GSM line",
    )
    parser.add_argument(
        "--tail-ms", type=int, default=1500, help="listen this long after the audio ends"
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="send as fast as the socket accepts (hides timing bugs; tests only)",
    )
    parser.add_argument(
        "--generate-ms",
        type=int,
        default=2400,
        help="length of the synthetic placeholder when the fixture is absent",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except ConnectionRefusedError:
        _out(f"error: nothing is listening on {args.url}.")
        _out("  Start the stack with `make dev`, or point --url at a running worker.")
        return 2
    except OSError as exc:
        _out(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
