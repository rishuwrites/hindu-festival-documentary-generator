from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path


class TTSError(RuntimeError):
    pass


class EdgeTTS:
    def __init__(self, voice: str, rate: str, pitch: str, retries: int = 3) -> None:
        self.voice = voice
        self.rate = rate
        self.pitch = pitch
        self.retries = max(1, retries)

    async def _synthesize(self, text: str, path: Path) -> None:
        try:
            import edge_tts
        except ImportError as exc:
            raise TTSError("Install the edge-tts dependency to synthesize narration.") from exc
        communicate = edge_tts.Communicate(text, self.voice, rate=self.rate, pitch=self.pitch)
        await communicate.save(str(path))

    def synthesize(self, text: str, path: Path) -> None:
        if not text.strip():
            raise TTSError("Cannot synthesize empty narration.")
        try:
            from edge_tts.exceptions import EdgeTTSException
        except ImportError as exc:
            raise TTSError("Install the edge-tts dependency to synthesize narration.") from exc
        path.parent.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        for attempt in range(self.retries):
            try:
                asyncio.run(self._synthesize(text, path))
                if not path.is_file() or path.stat().st_size < 1000:
                    raise TTSError("Edge TTS returned an empty or truncated audio file.")
                return
            except (EdgeTTSException, OSError, RuntimeError, asyncio.TimeoutError, TTSError) as exc:
                errors.append(str(exc))
                if path.exists():
                    path.unlink()
                if attempt + 1 < self.retries:
                    time.sleep(min(2**attempt, 8))
        raise TTSError("Edge TTS failed after retries: " + " | ".join(errors))


def synthesize_scenes(scenes: list[dict[str, object]], audio_dir: Path, engine: EdgeTTS) -> list[Path]:
    audio_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, scene in enumerate(scenes, 1):
        destination = audio_dir / f"scene_{index:03d}.mp3"
        engine.synthesize(str(scene["narration"]), destination)
        paths.append(destination)
    return paths


def write_audio_log(paths: list[Path], durations: list[float], destination: Path) -> None:
    destination.write_text(
        json.dumps(
            [
                {"path": str(path), "duration_seconds": round(duration, 3)}
                for path, duration in zip(paths, durations, strict=True)
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
