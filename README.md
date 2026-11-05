# Hindu Festival Documentary Generator

A modular Python pipeline that researches a Hindu festival, creates a Hindi documentary script,
generates scene illustrations and narration, and renders a subtitled 1080p MP4 with Ken Burns
camera movement, moving festival-colored visual accents, chapter headings, and scene fades. It can run from a
local command line or from the `Generate festival documentary` GitHub Actions workflow.

The reference project, [serial-recap-bot](https://github.com/rishuwrites/serial-recap-bot), informed
the use of separate processing stages, retry handling, resumable state, and downloadable workflow
artifacts. This project is an independent implementation for cultural documentaries.

## What it produces

The `output/` directory contains:

- `final_video.mp4` — H.264, 1920×1080, 30 fps, AAC audio
- `narration.mp3` and scene-level narration under `audio_scenes/`
- `subtitles.srt` — timed from measured scene audio; subtitles are also burned into the MP4
- `script.md` and `script.json`
- `research.json`, `research_sources.md`, and `sources.json`
- `scene_manifest.json` and `generated_images/`
- `generation_logs/` and `pipeline_state.json`

Production jobs reject a script or final video shorter than six minutes. `--test-mode` is an
explicit, one-minute end-to-end render option; it still needs the configured APIs and internet
access.

## Requirements

- Python 3.10 or later
- FFmpeg with `libx264`, AAC, and the `subtitles` filter (libass)
- Internet access for research, Gemini, image providers, and Edge TTS
- A Google Gemini API key
- A Devanagari-capable font for correctly rendered Hindi subtitles (for example Noto Sans
  Devanagari)

Install FFmpeg separately if it is not already on your `PATH`. On Windows, set `FFMPEG`,
`FFPROBE`, and `FONT_PATH` if their executable or font locations are not discoverable by default.

## Local run

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:GEMINI_API_KEY = "your-key"
python main.py --festival "Diwali" --language Hindi --duration-minutes 8 --voice hi-IN-SwaraNeural
```

The application does not load `.env` files automatically. Copy `.env.example` for reference and
export the values in your shell or your local environment manager. To create a short sample:

```powershell
python main.py --festival "Holi" --test-mode --no-resume
```

The default image priority is Pollinations, Hugging Face Inference (if configured), then a local
decorative illustration fallback. Every success and failure is logged with the provider and prompt.
The local fallback is intentionally identified as an illustration and is not represented as a
photographic or AI-generated depiction.

## GitHub Actions

### Add repository secrets

Open the repository's [Actions secrets and variables settings](https://github.com/rishuwrites/hindu-festival-documentary-generator/settings/secrets/actions)
and select **New repository secret** for each value below. These values are credentials: paste
them into GitHub's secret-value field only, never into a workflow file, issue, or commit.

**Required**

1. `GEMINI_API_KEY` — create an API key in [Google AI Studio](https://aistudio.google.com/app/apikey).
   Copy the key and add it as the value of the `GEMINI_API_KEY` repository secret. Gemini API
   availability, quotas, and pricing depend on your Google account and current terms.

**Optional**

2. `HUGGINGFACE_TOKEN` — create a token from your
   [Hugging Face access-token settings](https://huggingface.co/settings/tokens), then add it with
   this exact secret name. Choose only the minimum permissions supported for inference. Some
   models/providers require authorization or have quotas or charges.
3. `GOOGLE_SEARCH_API_KEY` — optional Google Programmable Search credential. Create or select a
   project in [Google Cloud Console](https://console.cloud.google.com/), enable the
   [Custom Search API](https://console.cloud.google.com/apis/library/customsearch.googleapis.com),
   then create an API key under [APIs & Services → Credentials](https://console.cloud.google.com/apis/credentials).
   Google may require billing, and API access and quotas can change.
4. `GOOGLE_SEARCH_ENGINE_ID` — create a search engine in the
   [Programmable Search Engine control panel](https://programmablesearchengine.google.com/controlpanel/all).
   Open its **Overview** or **Setup** page and copy the **Search engine ID** (`cx`); save that value
   under this exact secret name.

Google Search is enabled only when **both** `GOOGLE_SEARCH_API_KEY` and
`GOOGLE_SEARCH_ENGINE_ID` are set. If either is missing, the collector uses DuckDuckGo instead.
You do not need to create `GITHUB_TOKEN` yourself: GitHub Actions supplies it automatically.
Microsoft Edge TTS also needs no API secret.

### Run the workflow

1. Open **Actions → Generate festival documentary → Run workflow**.
2. Enter the festival and choose the language, duration, voice, and visual style.
3. For a resumed run, optionally enter the earlier run ID and attempt number.
4. Download the `festival-documentary-…` artifact from the completed workflow run.

The workflow installs FFmpeg and Noto fonts, caches Python packages, validates the required
Gemini secret, and uploads outputs even on failure to aid diagnosis. Failed stages leave
`pipeline_state.json` and logs in the artifact when those files were created. To resume on a new
workflow run, provide the previous run ID and attempt number (`1` for the first attempt) in the
optional resume inputs; the workflow restores that run's artifact before processing. Artifacts are
retained for 14 days. Locally, re-running the same configuration resumes completed stages; pass
`--no-resume` to start fresh. The runner is CPU-only and can take significant time: image calls are
made per scene and a typical 8-minute documentary has around 48 images.

## Configuration and providers

See [.env.example](.env.example) for supported environment variables. CLI options override the
festival, language, duration, voice, style, output folder, and test mode.

- **Research:** Multiple festival-specific web searches and page extracts, plus Wikipedia as a
  background reference. Government and academic hosts are labeled. Google CSE is optional; the
  default search backend is DuckDuckGo. Search snippets and scraped pages are not authoritative
  fact checks by themselves: review disputed claims and source citations before publication.
- **Script:** Gemini REST API; configured models are tried in order with retries. The prompt asks
  for distinctions between mythology, tradition, and historical evidence, and requires citations
  from collected material.
- **Images:** Pollinations is unauthenticated when available; service availability, terms, and
  throttling may change. Hugging Face requires a token for many models and its inference offering,
  model access, and charges/quotas depend on current account and provider terms. Neither service is
  guaranteed free or continuously available. Local fallback produces labeled decorative art.
- **Narration:** `edge-tts` uses Microsoft's online Edge speech service. It is not an offline voice
  engine or a service-level-guaranteed API. Verify the voice and usage terms for your intended use.
- **Music:** No commercial music is bundled. Set `MUSIC_PATH` only to a file you have rights to use;
  music is mixed quietly with narration side-chain compression. The pipeline does not download
  music or sound effects.

Use API credentials only in environment variables or GitHub Secrets. Do not commit keys or generated
media. `ALLOW_LOCAL_FALLBACK=false` makes image generation fail if remote providers are unavailable.

## Research and publication notes

The system preserves URLs, publisher, retrieval time, source type, and extracts in its research
files. Wikipedia is labeled background-only. The script is prompted to cite supplied sources and
to identify disagreements, but automated web collection and generative writing cannot guarantee
historical, religious, or astronomical correctness. Review citations, regional differences,
ritual details, dates, and licensing before release; do not treat a model-generated script as a
scholarly fact check. `research.json` includes a provenance audit: citations must match collected
URLs, and claim citations are counted across distinct publishers. The audit flags uncited scenes
and single-publisher claims; it does not claim semantic entailment or independently verify facts.

## Tests

The unit tests mock network/TTS operations and exercise provider fallback and output validation.
The FFmpeg render test runs when `ffmpeg` and `ffprobe` are on `PATH`.

```powershell
python -m unittest discover -v
```

## Troubleshooting

- **No research sources:** Check network access; the pipeline fails instead of inventing source
  material.
- **Gemini failure:** Check `GEMINI_API_KEY`, model availability, API quotas, and output logs.
- **Images fail:** Configure Hugging Face or permit the clearly labeled local illustration
  fallback. See `generation_logs/images.jsonl`.
- **Hindi glyphs are missing:** Install Noto Sans Devanagari and set `FONT_PATH`; ensure FFmpeg's
  subtitles filter can access the installed fontconfig fonts.
- **Video validation fails:** Confirm FFmpeg includes `libx264`, AAC, and libass. A production run
  also fails if measured narration or final video is shorter than six minutes.
