"""Optional AI layer: explain a deterministic impact analysis in plain language.

The graph itself is never built by an LLM. The model only receives the
already-computed blast radius and turns it into an explanation, a risk
narrative, and a review checklist.

Install with: ``pip install datagraph[ai]`` and set ``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

import json
from typing import Optional

from ..analysis import ImpactAnalysis
from ..security import UNTRUSTED_NOTICE, wrap_untrusted

SYSTEM_PROMPT = (
    "You are a senior data platform engineer reviewing a proposed change. "
    "You are given a deterministic change-impact analysis computed from real "
    "engineering artifacts (Python AST, dbt manifest, SQL lineage, git diff). "
    "Do not invent nodes or dependencies that are not in the analysis. "
    "Write for the engineer about to merge this change: explain what was "
    "changed, what can break and why (walk the propagation paths), whether the "
    "stated risk level seems right, and what to verify before and after "
    "deploying. Keep it focused and concrete. " + UNTRUSTED_NOTICE
)


def explain_impact(
    analysis: ImpactAnalysis,
    model: str = "claude-opus-5",
    api_key: Optional[str] = None,
    max_tokens: int = 16000,
) -> str:
    """Return a plain-language explanation of an impact analysis.

    Requires the ``anthropic`` package and an API key (argument or the
    ``ANTHROPIC_API_KEY`` environment variable / an ``ant auth login`` profile).
    """
    try:
        import anthropic
    except ImportError as e:
        raise ImportError(
            "The AI explanation layer requires the 'anthropic' package. "
            "Install it with: pip install datagraph[ai]"
        ) from e

    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    payload = json.dumps(analysis.to_dict(), indent=2, sort_keys=True)
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    "Here is the change impact analysis as JSON:\n\n"
                    + wrap_untrusted(f"```json\n{payload}\n```")
                    + "\n\nExplain the impact of this change."
                ),
            }
        ],
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        return "(The model declined to analyze this request.)"
    return "".join(block.text for block in response.content if block.type == "text")
