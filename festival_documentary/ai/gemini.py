from __future__ import annotations

import json
import logging
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)
API_BASE = "https://generativelanguage.googleapis.com/v1beta"
MODEL_LIMIT = 8
RETRY_DELAYS = (0, 30, 60, 120)


class GeminiError(RuntimeError):
    pass


class GeminiQuotaError(GeminiError):
    pass


def parse_json_response(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < start:
        raise GeminiError("Gemini response did not contain a JSON object.")
    try:
        value = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise GeminiError(f"Gemini returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise GeminiError("Gemini response must be a JSON object.")
    return value


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        models: tuple[str, ...],
        timeout: int = 60,
        retries: int = 3,
        max_models: int = MODEL_LIMIT,
    ) -> None:
        self.api_key = api_key
        self.models = models
        self.timeout = timeout
        self.retries = retries
        self.max_models = max_models

    def available_models(self) -> tuple[str, ...]:
        """Prefer configured and current stable Flash models, discovered from Gemini API."""
        ordered = [*self.models, "gemini-flash-latest"]
        url = f"{API_BASE}/models?key={quote(self.api_key, safe='')}&pageSize=200"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=min(max(self.timeout, 30), 60)) as response:
                payload = json.loads(response.read().decode("utf-8"))
            discovered = [
                str(item["name"]).rsplit("/", 1)[-1]
                for item in payload.get("models", [])
                if isinstance(item, dict)
                and "generateContent" in item.get("supportedGenerationMethods", [])
                and "flash" in str(item.get("name", "")).lower()
                and not any(
                    term in str(item.get("name", "")).lower()
                    for term in (
                        "image",
                        "tts",
                        "live",
                        "audio",
                        "robotics",
                        "computer",
                        "omni",
                        "exp",
                    )
                )
            ]
            ordered.extend(
                sorted(
                    (name for name in discovered if "preview" not in name and "lite" not in name),
                    reverse=True,
                )
            )
            ordered.extend(
                sorted(
                    (name for name in discovered if "preview" not in name and "lite" in name),
                    reverse=True,
                )
            )
            ordered.extend(
                sorted((name for name in discovered if "preview" in name), reverse=True)
            )
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            LOGGER.warning("Could not discover Gemini models; using configured fallbacks: %s", exc)

        unique: list[str] = []
        for model in ordered:
            if model and model not in unique:
                unique.append(model)
        return tuple(unique[: self.max_models])

    def generate(self, prompt: str) -> tuple[str, str]:
        if not self.api_key:
            raise GeminiError("GEMINI_API_KEY is required to generate the documentary script.")
        models = self.available_models()
        if not models:
            raise GeminiError("Gemini did not report any candidate models.")
        errors: list[str] = []
        quota_errors = 0
        for round_index, wait in enumerate(RETRY_DELAYS[: max(1, self.retries)]):
            if wait:
                LOGGER.info("All available Gemini models are busy; retrying in %s seconds.", wait)
                time.sleep(wait)
            round_quota_errors = 0
            for model in models:
                try:
                    return self._generate_model(model, prompt), model
                except GeminiQuotaError as exc:
                    round_quota_errors += 1
                    quota_errors += 1
                    errors.append(f"{model}: {exc}")
                except (HTTPError, URLError, TimeoutError, GeminiError) as exc:
                    errors.append(f"{model}: {exc}")
            if round_quota_errors == len(models):
                raise GeminiQuotaError(
                    "Every discovered Gemini model reported exhausted quota. Check Google AI "
                    "Studio rate limits and billing before retrying."
                )
            if round_index + 1 == min(max(1, self.retries), len(RETRY_DELAYS)):
                break
        raise GeminiError("All Gemini models failed: " + " | ".join(errors))

    def _generate_model(self, model: str, prompt: str) -> str:
        url = (
            f"{API_BASE}/models/"
            f"{quote(model, safe='')}:generateContent?key={quote(self.api_key, safe='')}"
        )
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.65},
        }
        request = Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=max(self.timeout, 180)) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                message = json.loads(detail).get("error", {}).get("message", detail)
            except json.JSONDecodeError:
                message = detail
            if exc.code == 429 and any(
                marker in message.lower()
                for marker in ("exceeded your current quota", "resource_exhausted", "billing")
            ):
                raise GeminiQuotaError(
                    "Gemini API quota is exhausted. Check the Google AI Studio rate limits "
                    "and billing settings before retrying."
                ) from exc
            raise HTTPError(
                exc.url, exc.code, str(message), exc.headers, None
            ) from exc
        try:
            return payload["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise GeminiError("Gemini response had no generated text.") from exc


def documentary_prompt(
    festival: str,
    language: str,
    duration_minutes: int,
    visual_style: str,
    research_json: str,
) -> str:
    minimum_scenes = max(6, duration_minutes * 6)
    return f"""You are a careful Indian cultural documentary writer. Use ONLY claims supported by
the supplied research; never invent citations, scripture quotations, dates, or historical certainty.
Clearly label religious traditions and mythology as traditions, distinguish them from established
historical evidence, and mention disagreements where sources differ. Cite supplied source URLs
against factual claims where possible. Avoid sectarian claims and respect regional diversity.

Create a polished {language} narration for a {duration_minutes}-minute documentary about
"{festival}". Style: {visual_style}. The narration must plausibly fill the requested runtime
(roughly 125-145 spoken Hindi words/minute), be cinematic and non-repetitive, have an opening hook,
transitions, and a memorable conclusion. Return at least {minimum_scenes} scenes, with individual
scene durations of about 10 seconds and the sum of scene durations at least {duration_minutes * 60}.

Return ONLY valid JSON with this shape:
{{"title":"...","description":"...","language":"{language}",
 "research_claims":[{{"claim":"...","classification":"history|tradition|mythology|modern interpretation","source_urls":["https://..."],"disagreement_note":""}}],
 "scenes":[
  {{"id":1,"chapter":"...","duration_seconds":10,"narration":"...","visual_prompt":"...","animation":"...","source_urls":["https://..."]}}
]}}
Keep scene narration contiguous and natural. Give each scene a distinct, culturally respectful
16:9 image prompt in the same visual style. Do not claim to create verified Sanskrit verses unless
they are present in research. Cite only exact URLs present in the supplied material. Include
research_claims for central historical, religious-tradition, and seasonal claims, and provide more
than one independent source URL for claims when the material supports that; add disagreement_note
when the supplied sources disagree. Scene source_urls should support the narration in that scene.

RESEARCH SOURCES AND EXTRACTS:
{research_json}
"""


def validate_script(script: dict[str, Any], minimum_duration: int) -> list[dict[str, Any]]:
    scenes = script.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise GeminiError("Generated script must contain a non-empty scenes array.")
    normalized: list[dict[str, Any]] = []
    for index, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            raise GeminiError(f"Scene {index} is not an object.")
        narration = str(scene.get("narration", "")).strip()
        visual_prompt = str(scene.get("visual_prompt", "")).strip()
        if not narration or not visual_prompt:
            raise GeminiError(f"Scene {index} is missing narration or an image prompt.")
        try:
            duration = int(scene.get("duration_seconds", 10))
        except (TypeError, ValueError) as exc:
            raise GeminiError(f"Scene {index} has an invalid duration.") from exc
        if duration < 6 or duration > 14:
            raise GeminiError(
                f"Scene {index} duration must be between 6 and 14 seconds to maintain "
                "approximately one image per 10 seconds."
            )
        normalized.append(
            {
                "id": index,
                "chapter": str(scene.get("chapter", "कथा")),
                "duration_seconds": duration,
                "narration": narration,
                "visual_prompt": visual_prompt,
                "animation": str(scene.get("animation", "slow cinematic zoom and subtle light")),
                "source_urls": scene.get("source_urls", []),
            }
        )
    if sum(scene["duration_seconds"] for scene in normalized) < minimum_duration:
        raise GeminiError(
            "Generated script is too short: "
            f"{sum(scene['duration_seconds'] for scene in normalized)}s < {minimum_duration}s."
        )
    return normalized
