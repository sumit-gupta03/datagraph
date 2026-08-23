"""LLM providers for the optional AI layer.

The graph is never built by an LLM; these providers only *explain* results or *suggest*
relationships (tagged ``llm``). Three backends, one tiny interface:

* ``anthropic`` - Claude via the Anthropic API (``pip install datagraph[ai]``, ``ANTHROPIC_API_KEY``)
* ``bedrock``   - Amazon Bedrock Converse API: Amazon Nova, Claude on Bedrock, Llama, Mistral ...
                  (``pip install datagraph[bedrock]``; standard AWS credentials / ``AWS_REGION``)
* ``openai``    - any OpenAI-compatible chat endpoint (OpenAI, Azure OpenAI, Ollama, vLLM, LM Studio, Groq ...)
                  via plain HTTPS - no extra dependency (``DATAGRAPH_LLM_BASE_URL`` / ``DATAGRAPH_LLM_API_KEY``)

Selection: explicit ``provider=`` argument, else ``DATAGRAPH_LLM_PROVIDER``, else ``anthropic``.
Model: explicit ``model=``, else ``DATAGRAPH_LLM_MODEL``, else the provider default.
Secrets are read from the environment / the cloud SDK's credential chain - never from the graph.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

DEFAULT_MODELS = {
    "anthropic": "claude-opus-5",
    "bedrock": "amazon.nova-pro-v1:0",
    "openai": "gpt-4o-mini",
}

_JSON_INSTRUCTION = (
    "\n\nRespond with ONLY a single JSON object (no prose, no code fences) that conforms to this JSON schema:\n"
)


class LLMProvider:
    """Minimal interface: complete(system, user) -> text. Subclasses set ``name`` and ``model``."""

    name = "base"
    model = ""

    def complete(self, system: str, user: str, *, max_tokens: int = 4096, json_schema: Optional[Dict] = None) -> str:
        raise NotImplementedError

    # shared helper for providers without native structured output
    @staticmethod
    def _with_schema(user: str, json_schema: Optional[Dict]) -> str:
        if not json_schema:
            return user
        return user + _JSON_INSTRUCTION + json.dumps(json_schema)


def extract_json(text: str) -> Optional[Any]:
    """Parse JSON from a model reply, tolerating code fences and surrounding prose."""
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(t[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None, client=None):
        self.model = model or DEFAULT_MODELS["anthropic"]
        if client is not None:
            self.client = client
        else:
            try:
                import anthropic
            except ImportError as e:
                raise ImportError("The Anthropic provider requires the 'anthropic' package: pip install datagraph[ai]") from e
            self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def complete(self, system: str, user: str, *, max_tokens: int = 4096, json_schema: Optional[Dict] = None) -> str:
        kwargs: Dict[str, Any] = dict(model=self.model, max_tokens=max_tokens, system=system,
                                      messages=[{"role": "user", "content": user}])
        if json_schema:
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": json_schema}}
        if max_tokens > 8000 and not json_schema:
            with self.client.messages.stream(**kwargs) as stream:  # long outputs: stream to avoid timeouts
                response = stream.get_final_message()
        else:
            response = self.client.messages.create(**kwargs)
        if getattr(response, "stop_reason", None) == "refusal":
            return ""
        return "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")


class BedrockProvider(LLMProvider):
    """Amazon Bedrock Converse API - works for Amazon Nova, Anthropic Claude on Bedrock, Llama, Mistral, ..."""

    name = "bedrock"

    def __init__(self, model: Optional[str] = None, region: Optional[str] = None, client=None, profile: Optional[str] = None):
        self.model = model or os.environ.get("DATAGRAPH_LLM_MODEL") or DEFAULT_MODELS["bedrock"]
        if client is not None:
            self.client = client
        else:
            try:
                import boto3
            except ImportError as e:
                raise ImportError("The Bedrock provider requires 'boto3': pip install datagraph[bedrock]") from e
            session = boto3.Session(profile_name=profile) if profile else boto3.Session()
            self.client = session.client("bedrock-runtime", region_name=region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")

    # Bedrock models have per-model output caps (Nova: 10k, many others 4k-8k); clamp and retry once on the model's limit.
    DEFAULT_MAX_TOKENS = int(os.environ.get("DATAGRAPH_LLM_MAX_TOKENS", "8000"))

    def complete(self, system: str, user: str, *, max_tokens: int = 4096, json_schema: Optional[Dict] = None) -> str:
        budget = min(int(max_tokens), self.DEFAULT_MAX_TOKENS)
        for attempt in (1, 2):
            try:
                response = self.client.converse(
                    modelId=self.model,
                    system=[{"text": system}],
                    messages=[{"role": "user", "content": [{"text": self._with_schema(user, json_schema)}]}],
                    inferenceConfig={"maxTokens": budget, "temperature": 0.0},
                )
                break
            except Exception as e:  # botocore ValidationException carries the limit in its message
                m = re.search(r"model limit of (\d+)", str(e))
                if attempt == 1 and m and int(m.group(1)) < budget:
                    budget = max(256, int(m.group(1)) - 1)
                    continue
                raise
        content = ((response.get("output") or {}).get("message") or {}).get("content") or []
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))


class OpenAICompatibleProvider(LLMProvider):
    """Any /v1/chat/completions endpoint (OpenAI, Azure OpenAI, Ollama, vLLM, Groq, LM Studio ...). No SDK needed."""

    name = "openai"

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None, base_url: Optional[str] = None, transport=None):
        self.model = model or os.environ.get("DATAGRAPH_LLM_MODEL") or DEFAULT_MODELS["openai"]
        self.api_key = api_key or os.environ.get("DATAGRAPH_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        self.base_url = (base_url or os.environ.get("DATAGRAPH_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self._transport = transport  # callable(url, headers, payload) -> dict, injectable for tests

    def _post(self, url: str, headers: Dict[str, str], payload: Dict) -> Dict:
        if self._transport:
            return self._transport(url, headers, payload)
        import urllib.request

        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=300) as resp:  # nosec - URL comes from configuration, not data
            return json.loads(resp.read().decode("utf-8"))

    def complete(self, system: str, user: str, *, max_tokens: int = 4096, json_schema: Optional[Dict] = None) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: Dict[str, Any] = {
            "model": self.model, "max_tokens": int(max_tokens), "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": self._with_schema(user, json_schema)}],
        }
        if json_schema:
            payload["response_format"] = {"type": "json_object"}
        data = self._post(f"{self.base_url}/chat/completions", headers, payload)
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):  # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return content or ""


def get_provider(provider: Optional[Any] = None, model: Optional[str] = None, api_key: Optional[str] = None, **kwargs) -> LLMProvider:
    """Resolve a provider: an LLMProvider instance is returned as-is; a name ('anthropic' | 'bedrock' | 'openai')
    or None (-> $DATAGRAPH_LLM_PROVIDER, default anthropic) builds one."""
    if isinstance(provider, LLMProvider):
        return provider
    name = (provider or os.environ.get("DATAGRAPH_LLM_PROVIDER") or "anthropic").lower()
    model = model or os.environ.get("DATAGRAPH_LLM_MODEL")
    if name == "anthropic":
        return AnthropicProvider(model=model, api_key=api_key, **kwargs)
    if name in ("bedrock", "aws", "nova"):
        return BedrockProvider(model=model, **kwargs)
    if name in ("openai", "openai-compatible", "ollama", "azure", "vllm", "groq"):
        return OpenAICompatibleProvider(model=model, api_key=api_key, **kwargs)
    raise ValueError(f"unknown LLM provider '{name}' (use anthropic | bedrock | openai)")
