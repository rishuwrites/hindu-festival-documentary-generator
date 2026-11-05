from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageFont, ImageOps

LOGGER = logging.getLogger(__name__)


class ImageGenerationError(RuntimeError):
    pass


class ImageProvider:
    name = "base"

    def generate(self, prompt: str, destination: Path, timeout: int) -> None:
        raise NotImplementedError


class PollinationsProvider(ImageProvider):
    name = "pollinations"

    def generate(self, prompt: str, destination: Path, timeout: int) -> None:
        url = (
            "https://image.pollinations.ai/prompt/"
            f"{quote(prompt, safe='')}?width=1280&height=720&model=flux&nologo=true"
        )
        request = Request(url, headers={"User-Agent": "FestivalDocumentaryGenerator/1.0"})
        with urlopen(request, timeout=timeout) as response:
            data = response.read(20_000_000)
        if not data.startswith(b"\x89PNG") and not data.startswith(b"\xff\xd8"):
            raise ImageGenerationError("Pollinations returned a non-image response.")
        destination.write_bytes(data)


class HuggingFaceProvider(ImageProvider):
    name = "huggingface"

    def __init__(self, token: str, model: str) -> None:
        self.token = token
        self.model = model

    def generate(self, prompt: str, destination: Path, timeout: int) -> None:
        if not self.token:
            raise ImageGenerationError("HUGGINGFACE_TOKEN is not configured.")
        url = f"https://router.huggingface.co/hf-inference/models/{quote(self.model, safe='/')}"
        request = Request(
            url,
            data=json.dumps({"inputs": prompt}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "image/png",
            },
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:
            data = response.read(20_000_000)
        if not data.startswith(b"\x89PNG") and not data.startswith(b"\xff\xd8"):
            raise ImageGenerationError("Hugging Face returned a non-image response.")
        destination.write_bytes(data)


class LocalIllustrationProvider(ImageProvider):
    """Produces an explicitly logged decorative illustration, not a photorealistic AI image."""

    name = "local_illustration"

    def __init__(self, font_path: str = "") -> None:
        self.font_path = font_path

    def generate(self, prompt: str, destination: Path, timeout: int) -> None:
        del timeout
        width, height = 1280, 720
        seed = int(hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8], 16)
        randomizer = random.Random(seed)
        palette = [
            ((30, 18, 60), (180, 75, 35)),
            ((10, 52, 75), (20, 120, 110)),
            ((68, 20, 50), (198, 102, 25)),
            ((35, 40, 85), (160, 95, 50)),
        ]
        top, bottom = palette[seed % len(palette)]
        image = Image.new("RGB", (width, height))
        pixels = image.load()
        for y in range(height):
            ratio = y / height
            color = tuple(int(top[i] * (1 - ratio) + bottom[i] * ratio) for i in range(3))
            for x in range(width):
                pixels[x, y] = color
        draw = ImageDraw.Draw(image, "RGBA")
        for _ in range(34):
            x = randomizer.randrange(width)
            y = randomizer.randrange(height)
            radius = randomizer.randrange(2, 20)
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=(255, 210, 105, 55))
        center = (width // 2, height // 2)
        for radius, alpha in ((245, 35), (185, 45), (125, 55)):
            draw.ellipse(
                (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius),
                outline=(255, 224, 160, alpha),
                width=4,
            )
        draw.rectangle((0, height - 122, width, height), fill=(8, 8, 16, 170))
        try:
            font = ImageFont.truetype(self.font_path, 22) if self.font_path else ImageFont.load_default()
        except OSError:
            font = ImageFont.load_default()
        label = "Illustrative fallback"
        draw.text((40, height - 82), label, font=font, fill=(255, 244, 220, 245))
        image.save(destination, format="PNG")


class ImageProviderManager:
    def __init__(
        self,
        provider_order: tuple[str, ...],
        huggingface_token: str,
        huggingface_model: str,
        font_path: str,
        allow_local_fallback: bool,
        retries: int,
        timeout: int,
        log_path: Path,
    ) -> None:
        available: dict[str, ImageProvider] = {
            "pollinations": PollinationsProvider(),
            "huggingface": HuggingFaceProvider(huggingface_token, huggingface_model),
            "local": LocalIllustrationProvider(font_path),
        }
        self.providers = [available[name] for name in provider_order if name in available]
        if not allow_local_fallback:
            self.providers = [provider for provider in self.providers if provider.name != "local_illustration"]
        if not self.providers:
            raise ValueError("No enabled image providers are available.")
        self.retries = max(1, retries)
        self.timeout = timeout
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._hashes: set[str] = set()

    def _log(self, entry: dict[str, object]) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    @staticmethod
    def _validate(path: Path) -> str:
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                if image.width < 256 or image.height < 144:
                    raise ImageGenerationError("Image dimensions are too small.")
                normalized = ImageOps.fit(image.convert("RGB"), (1280, 720), method=Image.Resampling.LANCZOS)
            normalized.save(path, format="PNG")
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, ValueError) as exc:
            raise ImageGenerationError(f"Generated image is invalid: {exc}") from exc

    def generate(self, prompt: str, destination: Path) -> dict[str, str]:
        errors: list[str] = []
        for provider in self.providers:
            for attempt in range(self.retries):
                started = time.time()
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    provider.generate(prompt, destination, self.timeout)
                    image_hash = self._validate(destination)
                    if image_hash in self._hashes:
                        raise ImageGenerationError("Duplicate image content detected.")
                    self._hashes.add(image_hash)
                    result = {
                        "provider": provider.name,
                        "sha256": image_hash,
                        "path": str(destination),
                    }
                    self._log(
                        {
                            **result,
                            "prompt": prompt,
                            "status": "success",
                            "attempt": attempt + 1,
                            "elapsed_seconds": round(time.time() - started, 2),
                        }
                    )
                    return result
                except (HTTPError, URLError, TimeoutError, OSError, ImageGenerationError) as exc:
                    errors.append(f"{provider.name}: {exc}")
                    LOGGER.warning("%s image attempt %s failed: %s", provider.name, attempt + 1, exc)
                    self._log(
                        {
                            "provider": provider.name,
                            "prompt": prompt,
                            "status": "failure",
                            "attempt": attempt + 1,
                            "error": str(exc),
                        }
                    )
                    if destination.exists():
                        destination.unlink()
                    if attempt + 1 < self.retries:
                        time.sleep(min(2**attempt, 8))
        raise ImageGenerationError("All image providers failed: " + " | ".join(errors))
