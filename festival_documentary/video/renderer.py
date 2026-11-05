from __future__ import annotations

import json
import subprocess
from pathlib import Path

from festival_documentary.video.subtitles import generate_srt


class RenderError(RuntimeError):
    pass


class VideoRenderer:
    def __init__(
        self,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        fps: int = 30,
        font_path: str = "",
    ) -> None:
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.fps = fps
        self.font_path = font_path

    @staticmethod
    def _run(command: list[str]) -> str:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise RenderError(
                f"Command failed ({result.returncode}): {' '.join(command)}\n"
                f"{result.stderr[-5000:]}"
            )
        return result.stdout.strip()

    def probe_duration(self, path: Path) -> float:
        output = self._run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ]
        )
        try:
            return float(output)
        except ValueError as exc:
            raise RenderError(f"Could not read media duration for {path}.") from exc

    def validate_video(self, path: Path, minimum_duration: int) -> float:
        if not path.is_file() or path.stat().st_size < 100_000:
            raise RenderError("Final MP4 is missing or unexpectedly small.")
        duration = self.probe_duration(path)
        if duration < minimum_duration:
            raise RenderError(
                f"Final video is only {duration:.1f}s; required {minimum_duration}s."
            )
        self._run([self.ffmpeg, "-v", "error", "-i", str(path), "-f", "null", "-"])
        return duration

    @staticmethod
    def _filter_path(path: Path) -> str:
        return path.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")

    def _render_scene(
        self,
        image: Path,
        duration: float,
        destination: Path,
        motif: str,
        chapter: str,
        work_dir: Path,
    ) -> None:
        frames = max(1, round(duration * self.fps))
        colors = {
            "diwali": ("orange@0.72", "gold@0.62", "yellow@0.55"),
            "holi": ("magenta@0.72", "cyan@0.65", "lime@0.62"),
            "chhath": ("gold@0.65", "deepskyblue@0.55", "orange@0.5"),
            "navratri": ("violet@0.68", "gold@0.6", "magenta@0.52"),
            "default": ("gold@0.55", "white@0.42", "orange@0.45"),
        }
        colors_for_scene = colors.get(motif, colors["default"])
        filters = [
            "scale=2000:1125:force_original_aspect_ratio=increase",
            "crop=2000:1125",
            f"zoompan=z='min(zoom+0.00035,1.08)':x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps={self.fps}",
        ]
        if motif == "chhath":
            filters.extend(
                [
                    f"drawbox=x='mod(t*120,2100)-180':y='mod(t*25+650,850)':w=170:h=3:color={colors_for_scene[0]}:t=fill",
                    f"drawbox=x='mod(t*95+430,2100)-180':y='mod(t*19+720,850)':w=130:h=2:color={colors_for_scene[1]}:t=fill",
                ]
            )
        else:
            filters.extend(
                [
                    f"drawbox=x='mod(t*115,1920)':y='mod(t*37+140,850)':w=12:h=12:color={colors_for_scene[0]}:t=fill",
                    f"drawbox=x='mod(t*83+520,1920)':y='mod(t*29+280,850)':w=9:h=9:color={colors_for_scene[1]}:t=fill",
                    f"drawbox=x='mod(t*67+1080,1920)':y='mod(t*43+90,850)':w=7:h=7:color={colors_for_scene[2]}:t=fill",
                ]
            )
        if self.font_path and Path(self.font_path).is_file() and chapter.strip():
            chapter_file = work_dir / f"chapter_{destination.stem}.txt"
            chapter_file.write_text(chapter.replace("\n", " "), encoding="utf-8")
            filters.append(
                "drawtext="
                f"fontfile='{self._filter_path(Path(self.font_path))}':"
                f"textfile='{self._filter_path(chapter_file)}':"
                "fontcolor=white:fontsize=42:x=60:y=55:box=1:boxcolor=black@0.38:"
                "boxborderw=18:enable='between(t,0,4)'"
            )
        filters.extend(
            [
                "fade=t=in:st=0:d=0.35",
                f"fade=t=out:st={max(0.35, duration - 0.35):.3f}:d=0.35",
                "format=yuv420p",
            ]
        )
        video_filter = ",".join(filters)
        self._run(
            [
                self.ffmpeg,
                "-y",
                "-loop",
                "1",
                "-framerate",
                str(self.fps),
                "-i",
                str(image),
                "-vf",
                video_filter,
                "-frames:v",
                str(frames),
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                str(destination),
            ]
        )

    @staticmethod
    def _subtitle_filter(path: Path) -> str:
        value = path.resolve().as_posix()
        value = value.replace(":", r"\:").replace("'", r"\'")
        return (
            f"subtitles='{value}':force_style='FontName=Noto Sans Devanagari,"
            "FontSize=22,Outline=2,Shadow=1,Alignment=2,MarginV=35'"
        )

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
        if not (len(scenes) == len(image_paths) == len(audio_paths) == len(audio_durations)):
            raise RenderError("Scene, image, audio, and duration counts must match.")
        if sum(audio_durations) < minimum_duration:
            raise RenderError(
                f"Narration is too short ({sum(audio_durations):.1f}s); "
                f"minimum is {minimum_duration}s."
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        work_dir = output_dir / "work" / "scene_video"
        work_dir.mkdir(parents=True, exist_ok=True)
        clips: list[Path] = []
        cursor = 0.0
        for index, (scene, image, audio, duration) in enumerate(
            zip(scenes, image_paths, audio_paths, audio_durations, strict=True), 1
        ):
            if not image.is_file() or not audio.is_file():
                raise RenderError(f"Scene {index} is missing its image or narration audio.")
            actual_duration = self.probe_duration(audio)
            if abs(actual_duration - duration) > 1.5:
                raise RenderError(f"Scene {index} duration changed after audio measurement.")
            clip = work_dir / f"clip_{index:03d}.mp4"
            motif_text = f"{festival_name} {scene.get('chapter', '')}".lower()
            motif = next((name for name in ("diwali", "holi", "chhath", "navratri") if name in motif_text), "default")
            self._render_scene(
                image, duration, clip, motif, str(scene.get("chapter", "")), work_dir
            )
            clips.append(clip)
            scene["start_seconds"] = round(cursor, 3)
            scene["end_seconds"] = round(cursor + duration, 3)
            cursor += duration

        concat_file = output_dir / "work" / "scene_clips.txt"
        concat_file.parent.mkdir(parents=True, exist_ok=True)
        concat_file.write_text(
            "".join(f"file '{path.resolve().as_posix()}'\n" for path in clips),
            encoding="utf-8",
        )
        silent_video = output_dir / "work" / "silent_video.mp4"
        self._run(
            [
                self.ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-c",
                "copy",
                str(silent_video),
            ]
        )

        audio_list = output_dir / "work" / "audio_clips.txt"
        audio_list.write_text(
            "".join(f"file '{path.resolve().as_posix()}'\n" for path in audio_paths),
            encoding="utf-8",
        )
        narration = output_dir / "narration.mp3"
        self._run(
            [
                self.ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(audio_list),
                "-c:a",
                "libmp3lame",
                "-q:a",
                "3",
                str(narration),
            ]
        )

        subtitles = output_dir / "subtitles.srt"
        generate_srt(scenes, audio_durations, subtitles)
        final_video = output_dir / "final_video.mp4"
        command = [
            self.ffmpeg,
            "-y",
            "-i",
            str(silent_video),
            "-i",
            str(narration),
        ]
        if music_path:
            if not Path(music_path).is_file():
                raise RenderError(f"Configured music file does not exist: {music_path}")
            command.extend(["-stream_loop", "-1", "-i", music_path])
            audio_filter = (
                "[2:a]volume=0.12[bg];[bg][1:a]sidechaincompress=threshold=0.03:"
                "ratio=8:attack=20:release=350[ducked];[1:a][ducked]amix=inputs=2:duration=first[a]"
            )
            command.extend(["-filter_complex", audio_filter, "-map", "0:v:0", "-map", "[a]"])
        else:
            command.extend(["-map", "0:v:0", "-map", "1:a:0"])
        command.extend(
            [
                "-vf",
                self._subtitle_filter(subtitles),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(final_video),
            ]
        )
        self._run(command)
        duration = self.validate_video(final_video, minimum_duration)
        (output_dir / "scene_manifest.json").write_text(
            json.dumps(scenes, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return final_video, narration, duration
