"""Google Gemini adapters: LLM extraction + grounded search for a real API key.

Contract notes
- Both classes implement the Step 3/4 protocols; ALL validation happens upstream
  (strict Pydantic schema + repair retry + prompt-injection markers from Step 4,
  canonicalisation + dedupe from Step 3). These adapters only move bytes.
- The Gemini client is injectable for offline tests; in production it is created
  from GEMINI_API_KEY. No key → the actionable LLMProviderUnavailable error, never
  a silent fallback.
- Model default: gemini-3.5-flash-lite (thinking level "minimal" — the model's
  own default, ideal for high-volume extraction).
"""

from __future__ import annotations

from typing import Any

from portfoliopulse.collect import SearchHit
from portfoliopulse.extract import LLMProviderUnavailable, build_extraction_prompt

DEFAULT_MODEL = "gemini-3.5-flash-lite"


def make_client(api_key: str) -> Any:
    """Create the real Gemini client; refuses loudly when no key is configured."""
    if not api_key:
        raise LLMProviderUnavailable(
            "GEMINI_API_KEY is not set. Add it via the /key page, .env, or the "
            "GEMINI_API_KEY environment variable."
        )
    from google import genai  # imported lazily: import cost only when actually used

    return genai.Client(api_key=api_key)


class GeminiExtractor:
    """LLM claim extraction via Gemini JSON mode."""

    provider_name = "gemini"

    def __init__(
        self,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        client: Any | None = None,
    ):
        self.model = model
        self.client = client if client is not None else make_client(api_key)
        self.calls = 0

    def extract(
        self,
        source_text: str,
        *,
        focus: str,
        publisher: str | None,
        repair_of: str | None = None,
        error: str | None = None,
    ) -> str:
        from google.genai import types

        self.calls += 1
        prompt = build_extraction_prompt(source_text, focus=focus, publisher=publisher)
        if repair_of is not None:
            prompt += (
                f"\nRETRY INSTRUCTIONS: Your previous answer failed schema validation: {error}\n"
                "Return corrected JSON that satisfies the schema exactly. Fix nothing else."
            )
        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(responseMimeType="application/json"),
        )
        text = getattr(response, "text", None)
        if not text:
            # An empty completion is a HARD failure: never fabricate claims from silence.
            raise RuntimeError(
                "Gemini returned an empty completion for a source; run failed honestly"
            )
        return text


class GeminiSearchProvider:
    """Source discovery via Gemini grounding (Search grounding → source URLs).

    We persist the GROUNDED SOURCE URIs (evidence trail), not the model's prose.
    """

    provider_name = "gemini"

    def __init__(
        self,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        client: Any | None = None,
    ):
        self.model = model
        self.client = client if client is not None else make_client(api_key)
        self.calls = 0

    def search(self, company: str, focus: str, limit: int) -> list[SearchHit]:
        from google.genai import types

        self.calls += 1
        prompt = (
            f"List public web sources that report on {company}'s situation regarding "
            f"{focus}. Prefer news, press releases, filings and the company's own "
            "pages. One source per item, with its URL and title."
        )
        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(googleSearch=types.GoogleSearch())]
            ),
        )

        hits: list[SearchHit] = []
        seen: set[str] = set()
        for candidate in getattr(response, "candidates", None) or []:
            metadata = getattr(candidate, "grounding_metadata", None)
            for chunk in getattr(metadata, "grounding_chunks", None) or []:
                web = getattr(chunk, "web", None)
                uri = getattr(web, "uri", None)
                if not uri or uri in seen:
                    continue  # chunks without a URI cannot be collected or cited
                seen.add(uri)
                hits.append(
                    SearchHit(
                        url=uri,
                        title=getattr(web, "title", None),
                        source_type="search_grounding",
                    )
                )
        return hits[:limit]
