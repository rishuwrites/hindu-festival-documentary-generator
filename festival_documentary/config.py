from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    festival_name: str
    output_dir: Path = Path("output")
    language: str = "Hindi"
    duration_minutes: int = 8
    voice: str = "hi-IN-SwaraNeural"
    visual_style: str = "Cinematic Documentary"
    gemini_api_key: str = ""
    gemini_models: tuple[str, ...] = (
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-flash-latest",
    )
    google_search_api_key: str = ""
    google_search_engine_id: str = ""
    huggingface_token: str = ""
    huggingface_model: str = "black-forest-labs/FLUX.1-schnell"
    image_provider_order: tuple[str, ...] = ("pollinations", "huggingface", "local")
    allow_local_fallback: bool = True
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    font_path: str = ""
    music_path: str = ""
    speech_rate: str = "+0%"
    speech_pitch: str = "+0Hz"
    test_mode: bool = False
    max_retries: int = 3
    request_timeout: int = 45

    @classmethod
    def from_env(cls, festival_name: str, **overrides: object) -> "Settings":
        def csv_tuple(value: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
            return tuple(part.strip() for part in value.split(",") if part.strip()) or fallback

        values: dict[str, object] = {
            "festival_name": festival_name,
            "output_dir": Path(os.getenv("OUTPUT_DIR", "output")),
            "language": os.getenv("LANGUAGE", "Hindi"),
            "duration_minutes": int(os.getenv("DURATION_MINUTES", "8")),
            "voice": os.getenv("VOICE", "hi-IN-SwaraNeural"),
            "visual_style": os.getenv("VISUAL_STYLE", "Cinematic Documentary"),
            "gemini_api_key": os.getenv("GEMINI_API_KEY", ""),
            "gemini_models": csv_tuple(
                os.getenv(
                    "GEMINI_MODELS",
                    "gemini-3.8-flash,gemini-3.7-flash,gemini-flash-latest",
                ),
                ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-flash-latest"),
            ),
            "google_search_api_key": os.getenv("GOOGLE_SEARCH_API_KEY", ""),
            "google_search_engine_id": os.getenv("GOOGLE_SEARCH_ENGINE_ID", ""),
            "huggingface_token": os.getenv("HUGGINGFACE_TOKEN", ""),
            "huggingface_model": os.getenv(
                "HUGGINGFACE_MODEL", "black-forest-labs/FLUX.1-schnell"
            ),
            "image_provider_order": csv_tuple(
                os.getenv("IMAGE_PROVIDER_ORDER", "pollinations,huggingface,local"),
                ("pollinations", "huggingface", "local"),
            ),
            "allow_local_fallback": os.getenv("ALLOW_LOCAL_FALLBACK", "true").lower()
            in {"1", "true", "yes"},
            "ffmpeg": os.getenv("FFMPEG", "ffmpeg"),
            "ffprobe": os.getenv("FFPROBE", "ffprobe"),
            "font_path": os.getenv("FONT_PATH", ""),
            "music_path": os.getenv("MUSIC_PATH", ""),
            "speech_rate": os.getenv("SPEECH_RATE", "+0%"),
            "speech_pitch": os.getenv("SPEECH_PITCH", "+0Hz"),
            "test_mode": os.getenv("TEST_MODE", "false").lower() in {"1", "true", "yes"},
            "max_retries": int(os.getenv("MAX_RETRIES", "3")),
            "request_timeout": int(os.getenv("REQUEST_TIMEOUT", "45")),
        }
        values.update(overrides)
        settings = cls(**values)  # type: ignore[arg-type]
        if not settings.festival_name.strip():
            raise ValueError("Festival name must not be empty.")
        if settings.duration_minutes < (1 if settings.test_mode else 6):
            raise ValueError("Production duration must be at least 6 minutes.")
        if not settings.image_provider_order:
            raise ValueError("At least one image provider must be configured.")
        if settings.max_retries < 1 or settings.request_timeout < 1:
            raise ValueError("Retry count and request timeout must be positive.")
        unknown_providers = set(settings.image_provider_order) - {
            "pollinations",
            "huggingface",
            "local",
        }
        if unknown_providers:
            raise ValueError(
                "Unknown image provider(s): " + ", ".join(sorted(unknown_providers))
            )
        return settings
