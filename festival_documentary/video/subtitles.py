from __future__ import annotations

import re
from pathlib import Path


def _timestamp(seconds: float) -> str:
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    whole_seconds, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02},{millis:03}"


def _wrap(text: str, max_chars: int = 42) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_chars:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    if not lines:
        return [text]
    return ["\n".join(lines[index : index + 2]) for index in range(0, len(lines), 2)]


def generate_srt(scenes: list[dict[str, object]], durations: list[float], destination: Path) -> None:
    if len(scenes) != len(durations):
        raise ValueError("Each scene must have exactly one measured audio duration.")
    entries: list[str] = []
    cursor = 0.0
    cue_number = 1
    for scene, duration in zip(scenes, durations, strict=True):
        if duration <= 0:
            raise ValueError("Audio durations must be positive.")
        text = re.sub(r"\s+", " ", str(scene["narration"])).strip()
        chunks = _wrap(text)
        weights = [max(1, len(chunk.replace("\n", ""))) for chunk in chunks]
        weight_total = sum(weights)
        local_cursor = cursor
        for index, chunk in enumerate(chunks):
            end = cursor + duration * sum(weights[: index + 1]) / weight_total
            entries.append(
                f"{cue_number}\n{_timestamp(local_cursor)} --> {_timestamp(end)}\n{chunk}\n"
            )
            local_cursor = end
            cue_number += 1
        cursor += duration
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(entries), encoding="utf-8-sig")
