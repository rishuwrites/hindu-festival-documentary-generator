from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from festival_documentary.ai.gemini import (
    GeminiClient,
    documentary_prompt,
    parse_json_response,
    validate_script,
)
from festival_documentary.audio.tts import EdgeTTS, synthesize_scenes, write_audio_log
from festival_documentary.config import Settings
from festival_documentary.images.providers import ImageProviderManager
from festival_documentary.research.collector import ResearchCollector, Source
from festival_documentary.research.fact_checker import audit_citations
from festival_documentary.video.renderer import VideoRenderer

LOGGER = logging.getLogger(__name__)


class PipelineError(RuntimeError):
    pass


class StateManager:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.state: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.state = {"completed": {}}
        except json.JSONDecodeError as exc:
            raise PipelineError(f"Pipeline state is corrupt: {path}") from exc

    def completed(self, stage: str) -> bool:
        return bool(self.state.get("completed", {}).get(stage))

    def mark(self, stage: str, **details: object) -> None:
        self.state.setdefault("completed", {})[stage] = details or True
        self.path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_script(script: dict[str, Any], scenes: list[dict[str, Any]], path: Path) -> None:
    lines = [
        f"# {script.get('title', 'Festival Documentary')}",
        "",
        str(script.get("description", "")),
        "",
        f"**Language:** {script.get('language', 'Hindi')}",
        "",
        "## Narration and scenes",
        "",
    ]
    for scene in scenes:
        lines.extend(
            [
                f"### Scene {scene['id']} — {scene['chapter']} ({scene['duration_seconds']}s)",
                "",
                str(scene["narration"]),
                "",
                f"**Visual:** {scene['visual_prompt']}",
                f"**Motion:** {scene['animation']}",
                f"**Sources:** {', '.join(scene['source_urls']) or 'No source URL supplied'}",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def _read_sources(path: Path) -> list[Source]:
    records = json.loads(path.read_text(encoding="utf-8"))
    return [Source(**record) for record in records]


def _probe_audio_duration(ffprobe: str, path: Path) -> float:
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise PipelineError(f"Unable to validate audio {path}: {result.stderr[-2000:]}")
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise PipelineError(f"Invalid audio duration for {path}.") from exc
    if duration <= 0 or path.stat().st_size < 1000:
        raise PipelineError(f"Audio file is empty or invalid: {path}")
    return duration


def run_pipeline(settings: Settings, resume: bool = True) -> Path:
    output = settings.output_dir
    output.mkdir(parents=True, exist_ok=True)
    (output / "generated_images").mkdir(exist_ok=True)
    (output / "generation_logs").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(output / "generation_logs" / "pipeline.log", encoding="utf-8"),
        ],
        force=True,
    )
    state = StateManager(output / "pipeline_state.json")
    signature_data = {
        "festival_name": settings.festival_name,
        "language": settings.language,
        "duration_minutes": settings.duration_minutes,
        "voice": settings.voice,
        "visual_style": settings.visual_style,
        "gemini_models": settings.gemini_models,
        "google_search_enabled": bool(
            settings.google_search_api_key and settings.google_search_engine_id
        ),
        "image_provider_order": settings.image_provider_order,
        "huggingface_model": settings.huggingface_model,
        "allow_local_fallback": settings.allow_local_fallback,
        "font_path": settings.font_path,
        "speech_rate": settings.speech_rate,
        "speech_pitch": settings.speech_pitch,
        "ffmpeg": settings.ffmpeg,
        "ffprobe": settings.ffprobe,
        "music_path": settings.music_path,
        "test_mode": settings.test_mode,
    }
    signature = hashlib.sha256(
        json.dumps(signature_data, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if state.state.get("signature") != signature:
        state.state = {"signature": signature, "completed": {}}
        state.path.write_text(json.dumps(state.state, indent=2), encoding="utf-8")
    if not resume:
        state.state = {"signature": signature, "completed": {}}
        state.path.write_text(json.dumps(state.state), encoding="utf-8")

    research_json_path = output / "research.json"
    sources_path = output / "sources.json"
    if not (
        resume
        and state.completed("research")
        and research_json_path.exists()
        and sources_path.exists()
    ):
        LOGGER.info("Collecting research for %s", settings.festival_name)
        sources = ResearchCollector(
            timeout=settings.request_timeout,
            google_api_key=settings.google_search_api_key,
            google_engine_id=settings.google_search_engine_id,
        ).collect(settings.festival_name)
        if not sources:
            raise PipelineError(
                "Research returned no sources. Check network access and try again; "
                "the pipeline will not invent research."
            )
        ResearchCollector.save(sources, str(sources_path), str(output / "research_sources.md"))
        research_data = {
            "festival": settings.festival_name,
            "retrieved_source_count": len(sources),
            "independent_non_wikipedia_sources": len(
                {source.publisher for source in sources if "Wikipedia" not in source.source_type}
            ),
            "source_records": [asdict(source) for source in sources],
            "research_caution": (
                "Search extracts are starting points, not fact verification. "
                "Traditions, mythology, historical evidence, and disputed claims must remain distinct."
            ),
        }
        research_json_path.write_text(
            json.dumps(research_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if research_data["independent_non_wikipedia_sources"] < 5:
            LOGGER.warning(
                "Collected fewer than five independent non-Wikipedia publishers; "
                "availability varies by search results."
            )
        state.mark("research", sources=len(sources))
    sources = _read_sources(sources_path)

    script_path = output / "script.json"
    if not (resume and state.completed("script") and script_path.exists()):
        if not settings.gemini_api_key:
            raise PipelineError("GEMINI_API_KEY is required for script generation.")
        duration = min(settings.duration_minutes, 1) if settings.test_mode else settings.duration_minutes
        research_context = json.dumps(
            [
                {
                    "title": source.title,
                    "publisher": source.publisher,
                    "url": source.url,
                    "type": source.source_type,
                    "extract": source.text[:2500] or source.snippet,
                }
                for source in sources
            ],
            ensure_ascii=False,
        )
        prompt = documentary_prompt(
            settings.festival_name,
            settings.language,
            duration,
            settings.visual_style,
            research_context,
        )
        raw, model = GeminiClient(
            settings.gemini_api_key,
            settings.gemini_models,
            timeout=settings.request_timeout,
            retries=settings.max_retries,
        ).generate(prompt)
        script = parse_json_response(raw)
        minimum = duration * 60
        scenes = validate_script(script, minimum)
        script["scenes"] = scenes
        script["citation_audit"] = audit_citations(script, sources)
        script["model"] = model
        script_path.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
        _write_script(script, scenes, output / "script.md")
        research_data = json.loads(research_json_path.read_text(encoding="utf-8"))
        research_data["script_citation_audit"] = script["citation_audit"]
        research_json_path.write_text(
            json.dumps(research_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        for warning in script["citation_audit"]["warnings"]:
            LOGGER.warning("Citation audit: %s", warning)
        state.mark("script", model=model, scenes=len(scenes))
    script = json.loads(script_path.read_text(encoding="utf-8"))
    scenes = validate_script(
        script, (min(settings.duration_minutes, 1) if settings.test_mode else settings.duration_minutes) * 60
    )
    script["scenes"] = scenes
    script["citation_audit"] = audit_citations(script, sources)
    if not (output / "script.md").exists():
        _write_script(script, scenes, output / "script.md")

    image_paths: list[Path] = []
    image_manifest_path = output / "generation_logs" / "image_manifest.json"
    if resume and state.completed("images") and image_manifest_path.exists():
        image_records = json.loads(image_manifest_path.read_text(encoding="utf-8"))
        image_paths = [Path(record["path"]) for record in image_records]
        if len(image_paths) != len(scenes) or not all(path.exists() for path in image_paths):
            image_paths = []
    if not image_paths:
        LOGGER.info("Generating %s scene images", len(scenes))
        manager = ImageProviderManager(
            settings.image_provider_order,
            settings.huggingface_token,
            settings.huggingface_model,
            settings.font_path,
            settings.allow_local_fallback,
            settings.max_retries,
            settings.request_timeout,
            output / "generation_logs" / "images.jsonl",
        )
        image_records = []
        for index, scene in enumerate(scenes, 1):
            prompt = (
                f"{settings.visual_style}, culturally respectful documentary illustration, "
                f"festival {settings.festival_name}; {scene['visual_prompt']}; "
                "wide cinematic 16:9 composition, no text, no watermark."
            )
            path = output / "generated_images" / f"scene_{index:03d}.png"
            record = manager.generate(prompt, path)
            image_records.append(record)
            image_paths.append(path)
        image_manifest_path.write_text(
            json.dumps(image_records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        state.mark("images", images=len(image_paths))

    audio_dir = output / "audio_scenes"
    audio_paths = [audio_dir / f"scene_{index:03d}.mp3" for index in range(1, len(scenes) + 1)]
    audio_log_path = output / "generation_logs" / "audio_durations.json"
    if not (
        resume
        and state.completed("audio")
        and all(path.exists() for path in audio_paths)
        and audio_log_path.exists()
    ):
        LOGGER.info("Synthesizing Hindi narration scene by scene")
        synthesize_scenes(
            scenes,
            audio_dir,
            EdgeTTS(settings.voice, settings.speech_rate, settings.speech_pitch, settings.max_retries),
        )
        durations = [_probe_audio_duration(settings.ffprobe, path) for path in audio_paths]
        write_audio_log(audio_paths, durations, audio_log_path)
        state.mark("audio", clips=len(audio_paths), duration_seconds=round(sum(durations), 3))
    durations = [_probe_audio_duration(settings.ffprobe, path) for path in audio_paths]
    minimum_duration = (
        min(settings.duration_minutes, 1) * 60 if settings.test_mode else settings.duration_minutes * 60
    )
    if sum(durations) < minimum_duration:
        raise PipelineError(
            f"Generated narration is {sum(durations):.1f}s, below the required "
            f"{minimum_duration}s. Regenerate a longer script."
        )

    final_video = output / "final_video.mp4"
    cached_outputs = (
        final_video,
        output / "narration.mp3",
        output / "subtitles.srt",
        output / "scene_manifest.json",
    )
    if not (
        resume
        and state.completed("render")
        and all(path.exists() for path in cached_outputs)
    ):
        LOGGER.info("Rendering final documentary video")
        renderer = VideoRenderer(settings.ffmpeg, settings.ffprobe, font_path=settings.font_path)
        rendered, narration, video_duration = renderer.render(
            scenes,
            image_paths,
            audio_paths,
            durations,
            output,
            settings.festival_name,
            minimum_duration,
            settings.music_path,
        )
        if not rendered.exists() or rendered.stat().st_size < 100_000:
            raise PipelineError("Rendered MP4 is missing or unexpectedly small.")
        state.mark("render", duration_seconds=round(video_duration, 3))
    else:
        renderer = VideoRenderer(
            settings.ffmpeg, settings.ffprobe, font_path=settings.font_path
        )
        renderer.validate_video(final_video, minimum_duration)
    if not (output / "subtitles.srt").exists():
        raise PipelineError("Subtitle output is missing.")
    if not (output / "narration.mp3").exists():
        raise PipelineError("Narration output is missing.")
    required_outputs = (
        output / "script.md",
        output / "research.json",
        output / "sources.json",
        output / "scene_manifest.json",
        output / "subtitles.srt",
        output / "narration.mp3",
    )
    missing = [str(path) for path in required_outputs if not path.is_file()]
    if missing:
        raise PipelineError("Required documentary deliverables are missing: " + ", ".join(missing))
    if len(image_paths) != len(scenes) or not all(path.is_file() for path in image_paths):
        raise PipelineError("The generated image set is incomplete.")
    work_dir = output / "work"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    LOGGER.info("Documentary validated: %s", final_video)
    return final_video
