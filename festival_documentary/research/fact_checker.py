from __future__ import annotations

from urllib.parse import urlparse

from festival_documentary.research.collector import Source


class CitationError(ValueError):
    pass


def audit_citations(script: dict[str, object], sources: list[Source]) -> dict[str, object]:
    """Audit citation provenance and source diversity; this does not prove semantic truth."""
    allowed = {source.url.rstrip("/") for source in sources}
    publisher_by_url = {source.url.rstrip("/"): publisher_domain(source) for source in sources}
    scenes = script.get("scenes", [])
    claims = script.get("research_claims", [])
    if not isinstance(scenes, list) or not isinstance(claims, list):
        raise CitationError("Script scenes and research_claims must be arrays.")

    cited_urls: set[str] = set()
    uncited_scenes: list[int] = []
    for index, scene in enumerate(scenes, 1):
        if not isinstance(scene, dict):
            continue
        references = scene.get("source_urls", [])
        if not isinstance(references, list):
            raise CitationError(f"Scene {index} source_urls must be an array.")
        if not references:
            uncited_scenes.append(index)
        for reference in references:
            url = str(reference).rstrip("/")
            if url not in allowed:
                raise CitationError(f"Scene {index} cites a URL that was not collected: {url}")
            cited_urls.add(url)

    claim_audits: list[dict[str, object]] = []
    for index, claim in enumerate(claims, 1):
        if not isinstance(claim, dict):
            raise CitationError(f"Research claim {index} is not an object.")
        references = claim.get("source_urls", [])
        if not isinstance(references, list):
            raise CitationError(f"Research claim {index} source_urls must be an array.")
        unique = {str(reference).rstrip("/") for reference in references}
        unknown = unique - allowed
        if unknown:
            raise CitationError(
                f"Research claim {index} cites URLs that were not collected: {', '.join(sorted(unknown))}"
            )
        publishers = {
            publisher_by_url[url]
            for url in unique
            if url in publisher_by_url
        }
        claim_audits.append(
            {
                "claim": str(claim.get("claim", "")),
                "classification": str(claim.get("classification", "unspecified")),
                "source_urls": sorted(unique),
                "independent_publishers": sorted(publishers),
                "cross_checked": len(publishers) >= 2,
                "disagreement_note": str(claim.get("disagreement_note", "")),
            }
        )

    warnings = []
    if uncited_scenes:
        warnings.append(f"{len(uncited_scenes)} scene(s) have no source URL.")
    if not claims:
        warnings.append("Gemini did not return research_claims for explicit claim-level review.")
    if any(not entry["cross_checked"] for entry in claim_audits):
        warnings.append("One or more claims cite fewer than two distinct publishers.")
    return {
        "cited_source_urls": sorted(cited_urls),
        "uncited_scene_ids": uncited_scenes,
        "claims": claim_audits,
        "warnings": warnings,
        "limitation": (
            "This checks that citations point to collected URLs and counts distinct publishers. "
            "It cannot establish that a source semantically supports a claim."
        ),
    }


def publisher_domain(source: Source) -> str:
    return urlparse(source.url).netloc.lower().removeprefix("www.")
