from __future__ import annotations

import json
import io
import logging
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError

from PIL import Image

from festival_documentary.ai.gemini import (
    GeminiClient,
    GeminiError,
    GeminiQuotaError,
    parse_json_response,
    validate_script,
)
from festival_documentary.audio.tts import EdgeTTS, TTSError
from festival_documentary.images.providers import (
    ImageGenerationError,
    ImageProvider,
    ImageProviderManager,
)
from festival_documentary.config import Settings
from festival_documentary.pipeline import run_pipeline
from festival_documentary.research.collector import ResearchCollector, Source
from festival_documentary.research.fact_checker import CitationError, audit_citations
from festival_documentary.video.renderer import VideoRenderer
from festival_documentary.video.subtitles import generate_srt


class ResearchTests(unittest.TestCase):
    def test_page_extract_includes_publication_date(self) -> None:
        title, text, published_at = ResearchCollector._page_text(
            b'<html><head><title>Festival</title><meta property="article:published_time" '
            b'content="2024-10-15"></head><body>Tradition</body></html>'
        )
        self.assertEqual(title, "Festival")
        self.assertIn("Tradition", text)
        self.assertEqual(published_at, "2024-10-15")

    def test_collection_deduplicates_and_marks_wikipedia_as_background(self) -> None:
        collector = ResearchCollector(max_pages=4)
        collector.search = lambda query, limit=10: [  # type: ignore[method-assign]
            {
                "title": "Official festival page",
                "url": "https://culture.gov.in/festival",
                "snippet": "Government cultural information",
                "text": "",
            },
            {
                "title": "Second page",
                "url": "https://culture.gov.in/festival",
                "snippet": "Duplicate URL",
                "text": "",
            },
        ]
        collector._get = lambda url, accept="text/html": b"<html><title>Culture</title><p>Text</p></html>"  # type: ignore[method-assign]
        collector._wikipedia = lambda festival: [  # type: ignore[method-assign]
            {
                "title": "Diwali",
                "url": "https://en.wikipedia.org/wiki/Diwali",
                "snippet": "Background",
                "text": "Background text",
                "source_type": "background reference (Wikipedia)",
            }
        ]
        sources = collector.collect("Diwali")
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0].source_type, "government")
        self.assertIn("background", sources[1].source_type)


class GeminiTests(unittest.TestCase):
    def test_parses_fenced_json(self) -> None:
        self.assertEqual(parse_json_response('```json\n{"scenes": []}\n```'), {"scenes": []})

    def test_rejects_malformed_and_short_scripts(self) -> None:
        with self.assertRaises(GeminiError):
            parse_json_response("not json")
        script = {"scenes": [{"narration": "कथा", "visual_prompt": "दीप", "duration_seconds": 5}]}
        with self.assertRaises(GeminiError):
            validate_script(script, 60)

    def test_model_fallback_after_rate_limit(self) -> None:
        client = GeminiClient("test-key", ("primary", "fallback"), retries=1)
        with patch.object(
            client,
            "_generate_model",
            side_effect=[HTTPError("url", 429, "rate limited", {}, None), '{"ok": true}'],
        ), patch("festival_documentary.ai.gemini.time.sleep"):
            text, model = client.generate("prompt")
        self.assertEqual((text, model), ('{"ok": true}', "fallback"))

    def test_quota_exhaustion_does_not_spam_fallback_models(self) -> None:
        client = GeminiClient("test-key", ("primary", "fallback"), retries=3)
        with patch.object(
            client,
            "_generate_model",
            side_effect=GeminiQuotaError("quota exhausted"),
        ) as generate, patch("festival_documentary.ai.gemini.time.sleep"):
            with self.assertRaises(GeminiQuotaError):
                client.generate("prompt")
        generate.assert_called_once()

    def test_api_quota_error_includes_actionable_message(self) -> None:
        client = GeminiClient("test-key", ("primary",), retries=1)
        payload = json.dumps(
            {"error": {"message": "You exceeded your current quota.", "status": "RESOURCE_EXHAUSTED"}}
        ).encode()
        http_error = HTTPError("https://example.test", 429, "quota", {}, io.BytesIO(payload))
        with patch("festival_documentary.ai.gemini.urlopen", side_effect=http_error):
            with self.assertRaisesRegex(GeminiQuotaError, "quota is exhausted"):
                client._generate_model("primary", "prompt")


class ImageFallbackTests(unittest.TestCase):
    def test_uses_next_provider_and_records_success(self) -> None:
        class FailingProvider(ImageProvider):
            name = "first"

            def generate(self, prompt: str, destination: Path, timeout: int) -> None:
                raise ImageGenerationError("unavailable")

        class WorkingProvider(ImageProvider):
            name = "second"

            def generate(self, prompt: str, destination: Path, timeout: int) -> None:
                Image.new("RGB", (640, 360), "orange").save(destination)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = ImageProviderManager(
                ("local",), "", "", "", True, 1, 1, root / "logs.jsonl"
            )
            manager.providers = [FailingProvider(), WorkingProvider()]
            with patch("festival_documentary.images.providers.time.sleep"):
                result = manager.generate("festival scene", root / "image.png")
            self.assertEqual(result["provider"], "second")
            with Image.open(root / "image.png") as image:
                self.assertEqual(image.size, (1280, 720))
            entries = [json.loads(line) for line in (root / "logs.jsonl").read_text().splitlines()]
            self.assertEqual(entries[-1]["status"], "success")
            self.assertEqual(len(entries), 2)


class AudioAndSubtitleTests(unittest.TestCase):
    def test_tts_retries_and_propagates_failure(self) -> None:
        engine = EdgeTTS("hi-IN-SwaraNeural", "+0%", "+0Hz", retries=1)
        with patch.object(engine, "_synthesize", side_effect=RuntimeError("network down")):
            with tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(TTSError):
                    engine.synthesize("नमस्ते", Path(temporary) / "scene.mp3")

    def test_tts_writes_measured_scene_file(self) -> None:
        engine = EdgeTTS("hi-IN-SwaraNeural", "+0%", "+0Hz", retries=1)

        async def fake_synthesize(text: str, path: Path) -> None:
            path.write_bytes(b"x" * 1500)

        with patch.object(engine, "_synthesize", new=AsyncMock(side_effect=fake_synthesize)):
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "scene.mp3"
                engine.synthesize("नमस्ते", path)
                self.assertGreater(path.stat().st_size, 1000)

    def test_subtitles_follow_scene_audio_durations(self) -> None:
        scenes = [
            {"narration": "पहला दृश्य और उसकी कहानी"},
            {"narration": "दूसरा दृश्य और उसका महत्व"},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "subtitles.srt"
            generate_srt(scenes, [2.5, 3.0], path)
            value = path.read_text(encoding="utf-8-sig")
            self.assertIn("00:00:00,000 --> 00:00:02,500", value)
            self.assertIn("00:00:02,500 --> 00:00:05,500", value)


class CitationAuditTests(unittest.TestCase):
    def test_checks_registered_urls_and_cross_source_support(self) -> None:
        sources = [
            Source("A", "https://culture.gov.in/festival", "culture.gov.in", "", "government", "now"),
            Source("B", "https://example.edu/paper", "example.edu", "", "academic", "now"),
        ]
        script = {
            "scenes": [
                {"source_urls": ["https://culture.gov.in/festival"]},
                {"source_urls": []},
            ],
            "research_claims": [
                {
                    "claim": "A documented claim",
                    "classification": "history",
                    "source_urls": ["https://culture.gov.in/festival", "https://example.edu/paper"],
                }
            ],
        }
        result = audit_citations(script, sources)
        self.assertTrue(result["claims"][0]["cross_checked"])
        self.assertEqual(result["uncited_scene_ids"], [2])
        script["research_claims"][0]["source_urls"].append("https://fake.example/claim")
        with self.assertRaises(CitationError):
            audit_citations(script, sources)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools unavailable")
class FFmpegRenderTests(unittest.TestCase):
    def test_short_video_render_and_integrity_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            images, audios, scenes = [], [], []
            for index in range(2):
                image = root / f"image_{index}.png"
                Image.new("RGB", (640, 360), ("#dc9230" if index == 0 else "#453366")).save(image)
                audio = root / f"audio_{index}.mp3"
                import subprocess

                subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        "sine=frequency=440:duration=1.2",
                        "-q:a",
                        "6",
                        str(audio),
                    ],
                    check=True,
                )
                images.append(image)
                audios.append(audio)
                scenes.append(
                    {
                        "id": index + 1,
                        "chapter": "कथा",
                        "narration": f"यह एक परीक्षण दृश्य संख्या {index + 1} है।",
                    }
                )
            renderer = VideoRenderer()
            durations = [renderer.probe_duration(path) for path in audios]
            video, narration, length = renderer.render(
                scenes, images, audios, durations, root / "output", "Chhath Puja", 2
            )
            self.assertTrue(video.is_file())
            self.assertTrue(narration.is_file())
            self.assertGreaterEqual(length, 2)
            self.assertGreater(video.stat().st_size, 10_000)
            metadata = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_name,width,height",
                    "-of",
                    "json",
                    str(video),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            video_stream = next(
                stream
                for stream in json.loads(metadata.stdout)["streams"]
                if stream.get("width") == 1920
            )
            self.assertEqual(video_stream["codec_name"], "h264")
            self.assertEqual(video_stream["height"], 1080)


class WorkflowConfigurationTests(unittest.TestCase):
    def test_workflow_has_manual_inputs_and_artifact_upload(self) -> None:
        workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "generate-video.yml").read_text(
            encoding="utf-8"
        )
        for expected in (
            "workflow_dispatch:",
            "festival_name:",
            "duration_minutes:",
            "voice:",
            "visual_style:",
            "resume_run_id:",
            "actions/download-artifact@v4",
            "GEMINI_API_KEY",
            "GOOGLE_SEARCH_API_KEY",
            "timeout-minutes: 360",
            "actions/upload-artifact@v4",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, workflow)


class PipelineIntegrationTests(unittest.TestCase):
    def test_mocked_pipeline_produces_artifacts_and_resumes(self) -> None:
        source_urls = ["https://culture.gov.in/festival", "https://example.edu/festival"]
        sources = [
            Source("Government source", source_urls[0], "culture.gov.in", "Official context", "government", "now"),
            Source("Academic source", source_urls[1], "example.edu", "Research context", "academic", "now"),
        ]
        scenes = [
            {
                "id": index,
                "chapter": "परिचय",
                "duration_seconds": 10,
                "narration": f"यह परीक्षण दृश्य संख्या {index} है।",
                "visual_prompt": f"दीपों का परीक्षण दृश्य {index}",
                "animation": "धीमा ज़ूम",
                "source_urls": source_urls,
            }
            for index in range(1, 7)
        ]
        model_response = json.dumps(
            {
                "title": "दीपावली का इतिहास",
                "description": "Mock documentary",
                "language": "Hindi",
                "research_claims": [
                    {
                        "claim": "A sourced claim",
                        "classification": "history",
                        "source_urls": source_urls,
                    }
                ],
                "scenes": scenes,
            },
            ensure_ascii=False,
        )

        class FakeCollector(ResearchCollector):
            collect_calls = 0

            def collect(self, festival: str) -> list[Source]:
                type(self).collect_calls += 1
                return sources

        class FakeGemini:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def generate(self, prompt: str) -> tuple[str, str]:
                return model_response, "mock-model"

        class FakeImageManager:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def generate(self, prompt: str, destination: Path) -> dict[str, str]:
                Image.new("RGB", (1280, 720), (80, 60, 35)).save(destination)
                return {"provider": "mock", "sha256": str(destination.stat().st_size), "path": str(destination)}

        class FakeTTS:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def synthesize(self, text: str, path: Path) -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"mock audio" * 200)

        class FakeRenderer:
            render_calls = 0
            validate_calls = 0

            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def render(
                self,
                scenes: list[dict[str, object]],
                image_paths: list[Path],
                audio_paths: list[Path],
                audio_durations: list[float],
                output_dir: Path,
                festival_name: str,
                minimum_duration: int,
                music_path: str = "",
            ) -> tuple[Path, Path, float]:
                type(self).render_calls += 1
                video = output_dir / "final_video.mp4"
                video.write_bytes(b"x" * 110_000)
                narration = output_dir / "narration.mp3"
                narration.write_bytes(b"x" * 2000)
                (output_dir / "subtitles.srt").write_text(
                    "1\n00:00:00,000 --> 00:00:10,000\nनमस्ते\n", encoding="utf-8"
                )
                (output_dir / "scene_manifest.json").write_text(
                    json.dumps(scenes, ensure_ascii=False), encoding="utf-8"
                )
                return video, narration, float(minimum_duration)

            def validate_video(self, path: Path, minimum_duration: int) -> float:
                type(self).validate_calls += 1
                return float(minimum_duration)

        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings.from_env(
                "Diwali",
                output_dir=Path(temporary) / "output",
                gemini_api_key="mock",
                test_mode=True,
                duration_minutes=1,
                image_provider_order=("local",),
                max_retries=1,
            )
            patches = (
                patch("festival_documentary.pipeline.ResearchCollector", FakeCollector),
                patch("festival_documentary.pipeline.GeminiClient", FakeGemini),
                patch("festival_documentary.pipeline.ImageProviderManager", FakeImageManager),
                patch("festival_documentary.pipeline.EdgeTTS", FakeTTS),
                patch("festival_documentary.pipeline.VideoRenderer", FakeRenderer),
                patch("festival_documentary.pipeline._probe_audio_duration", return_value=10.0),
                patch(
                    "festival_documentary.pipeline.logging.FileHandler",
                    return_value=logging.NullHandler(),
                ),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
                video = run_pipeline(settings)
                self.assertTrue(video.is_file())
                self.assertTrue((settings.output_dir / "sources.json").is_file())
                self.assertTrue((settings.output_dir / "research.json").is_file())
                self.assertEqual(len(list((settings.output_dir / "generated_images").glob("*.png"))), 6)
                self.assertFalse((settings.output_dir / "work").exists())
                run_pipeline(settings)
            self.assertEqual(FakeCollector.collect_calls, 1)
            self.assertEqual(FakeRenderer.render_calls, 1)
            self.assertEqual(FakeRenderer.validate_calls, 1)


if __name__ == "__main__":
    unittest.main()
