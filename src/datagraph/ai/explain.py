"""Optional AI layer: explain a deterministic impact analysis in plain language.

The graph itself is never built by an LLM. The model only receives the
already-computed blast radius and turns it into an explanation, a risk
narrative, and a review checklist.

Providers: Anthropic (default), Amazon Bedrock (Nova, Claude on Bedrock, ...), or any
OpenAI-compatible endpoint — see ``datagraph.ai.providers``. Install with
``pip install datagraph[ai]`` (Anthropic) or ``datagraph[bedrock]`` and set the
provider's credentials in the environment.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from ..analysis import ImpactAnalysis
from ..security import UNTRUSTED_NOTICE, wrap_untrusted
from .providers import get_provider

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
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    max_tokens: int = 16000,
    provider: Optional[Any] = None,
) -> str:
    """Return a plain-language explanation of an impact analysis.

    ``provider`` is an ``LLMProvider`` instance or a name ('anthropic' | 'bedrock' | 'openai');
    omitted -> ``$DATAGRAPH_LLM_PROVIDER`` or Anthropic. ``model`` overrides the provider default.
    """
    llm = get_provider(provider, model=model, api_key=api_key)
    payload = json.dumps(analysis.to_dict(), indent=2, sort_keys=True)
    user = ("Here is the change impact analysis as JSON:\n\n"
            + wrap_untrusted(f"```json\n{payload}\n```")
            + "\n\nExplain the impact of this change.")
    text = llm.complete(SYSTEM_PROMPT, user, max_tokens=max_tokens)
    return text or "(The model declined to analyze this request.)"
