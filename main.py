from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from festival_documentary.ai.gemini import GeminiError
from festival_documentary.audio.tts import TTSError
from festival_documentary.config import Settings
from festival_documentary.images.providers import ImageGenerationError
from festival_documentary.pipeline import PipelineError, run_pipeline
from festival_documentary.research.fact_checker import CitationError
from festival_documentary.video.renderer import RenderError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Research a Hindu festival and generate a Hindi documentary video."
    )
    parser.add_argument("--festival", required=True, help="Festival name, for example Diwali")
    parser.add_argument("--language", default=None, help="Narration language (Hindi by default)")
    parser.add_argument("--duration-minutes", type=int, default=None, help="Target duration (minimum 6)")
    parser.add_argument("--voice", default=None, help="Edge TTS voice, e.g. hi-IN-SwaraNeural")
    parser.add_argument("--visual-style", default=None, help="Image style")
    parser.add_argument("--output-dir", default=None, help="Output directory")
    parser.add_argument("--test-mode", action="store_true", help="Allow a 1-minute test render")
    parser.add_argument("--no-resume", action="store_true", help="Regenerate all pipeline stages")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    overrides: dict[str, object] = {"test_mode": args.test_mode}
    for argument, setting in (
        ("language", "language"),
        ("duration_minutes", "duration_minutes"),
        ("voice", "voice"),
        ("visual_style", "visual_style"),
        ("output_dir", "output_dir"),
    ):
        value = getattr(args, argument)
        if value is not None:
            overrides[setting] = Path(value) if setting == "output_dir" else value
    try:
        settings = Settings.from_env(args.festival, **overrides)
        video = run_pipeline(settings, resume=not args.no_resume)
    except (
        PipelineError,
        GeminiError,
        ImageGenerationError,
        TTSError,
        CitationError,
        RenderError,
        ValueError,
        OSError,
    ) as exc:
        logging.error("%s", exc)
        return 1
    print(f"Video created: {video.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
