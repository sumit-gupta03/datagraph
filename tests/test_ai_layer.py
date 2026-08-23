"""AI layer tests that run offline: prompt payload construction and error paths.

The live API call is exercised manually (needs ANTHROPIC_API_KEY); these tests
pin everything around it: the ImportError message, the payload the model would
receive, and the refusal/text handling — using a stub anthropic module.
"""

import builtins
import json
import sys
import types

import pytest

from datagraph.ai.explain import explain_impact
from datagraph.analysis import analyze_impact


@pytest.fixture
def analysis(dbt_graph):
    return analyze_impact(dbt_graph, ["dbt:customer"])


def test_missing_anthropic_raises_helpful_error(analysis, monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("No module named 'anthropic'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match=r"pip install datagraph\[ai\]"):
        explain_impact(analysis)


class _Block:
    def __init__(self, type_, text=""):
        self.type = type_
        self.text = text


class _Response:
    def __init__(self, stop_reason, blocks):
        self.stop_reason = stop_reason
        self.content = blocks


class _Stream:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._response


def _install_stub(monkeypatch, response, captured):
    stub = types.ModuleType("anthropic")

    class Anthropic:
        def __init__(self, api_key=None):
            captured["api_key"] = api_key
            self.messages = self

        def stream(self, **kwargs):
            captured["request"] = kwargs
            return _Stream(response)

    stub.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", stub)


def test_payload_contains_deterministic_analysis(analysis, monkeypatch):
    captured = {}
    response = _Response("end_turn", [_Block("text", "Looks risky.")])
    _install_stub(monkeypatch, response, captured)

    out = explain_impact(analysis, api_key="k")
    assert out == "Looks risky."

    request = captured["request"]
    assert request["model"] == "claude-opus-5"
    user_text = request["messages"][0]["content"]
    payload = json.loads(user_text.split("```json\n", 1)[1].split("```")[0])
    assert "exposure:revenue_report" in payload["affected"]
    assert payload["risk"]["level"] in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
    assert "Do not invent nodes" in request["system"]


def test_refusal_handled(analysis, monkeypatch):
    captured = {}
    response = _Response("refusal", [])
    _install_stub(monkeypatch, response, captured)
    out = explain_impact(analysis, api_key="k")
    assert "declined" in out
