"""LLM lineage fallback — tested offline against a stub Anthropic SDK."""

import json
import sqlite3
import sys
import types

import pytest

from impactgraph import LLM, WarehouseExtractor
from impactgraph.ai import apply_suggestions, schema_summary, suggest_lineage
from impactgraph.cli import main


@pytest.fixture
def no_fk_db(tmp_path):
    """Tables that are obviously related by naming but have NO declared foreign keys."""
    path = tmp_path / "nofk.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount REAL);
        """
    )
    con.commit(); con.close()
    return path


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, text, stop_reason="end_turn"):
        self.stop_reason = stop_reason
        self.content = [_Block(text)]


def _install_stub(monkeypatch, captured, suggestions, stop_reason="end_turn"):
    stub = types.ModuleType("anthropic")

    class Anthropic:
        def __init__(self, api_key=None):
            self.messages = self

        def create(self, **kwargs):
            captured["request"] = kwargs
            return _Resp(json.dumps({"relationships": suggestions}), stop_reason)

    stub.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", stub)


def test_schema_summary_is_compact_and_deterministic(no_fk_db):
    graph = WarehouseExtractor(str(no_fk_db)).extract()
    s = schema_summary(graph)
    assert {t["id"] for t in s["tables"]} == {"table:customers", "table:orders"}
    assert s["known_relationships"] == []  # nothing deterministic here — exactly the fallback case
    assert schema_summary(graph) == s


def test_suggest_and_apply_marks_llm_provenance(no_fk_db, monkeypatch):
    graph = WarehouseExtractor(str(no_fk_db)).extract()
    captured = {}
    _install_stub(monkeypatch, captured, [
        {"kind": "column", "source": "column:orders.customer_id", "target": "column:customers.id",
         "confidence": 0.9, "reason": "naming convention"},
        {"kind": "column", "source": "column:orders.amount", "target": "column:customers.email",
         "confidence": 0.2, "reason": "weak guess"},
    ])
    sugg = suggest_lineage(graph, unparsed_sql=[{"where": "x.sql", "sql": "SELECT 1"}])
    req = captured["request"]
    assert req["model"] == "claude-opus-5"
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert "unparsed_sql" in req["messages"][0]["content"]
    assert len(sugg) == 2

    added = apply_suggestions(graph, sugg, min_confidence=0.6)
    assert added == 1  # low-confidence one rejected
    llm_edges = [e for e in graph.edges() if e.provenance == LLM]
    assert any(e.src == "column:orders.customer_id" and e.dst == "column:customers.id" for e in llm_edges)
    assert any(e.src == "table:orders" and e.dst == "table:customers" for e in llm_edges)  # table-level too

    # impact follows llm edges by default, and excludes them with include_inferred=False
    assert "column:orders.customer_id" in graph.impact("column:customers.id")
    assert "column:orders.customer_id" not in graph.impact("column:customers.id", include_inferred=False)
    tree = graph.impact_tree("table:customers")
    assert any(c.get("provenance") == LLM for c in tree["children"])


def test_apply_refuses_unknown_tables(no_fk_db):
    graph = WarehouseExtractor(str(no_fk_db)).extract()
    added = apply_suggestions(graph, [{"kind": "table", "source": "table:ghost", "target": "table:customers",
                                       "confidence": 0.99, "reason": "invented"}])
    assert added == 0 and "table:ghost" not in graph


def test_refusal_gives_no_suggestions(no_fk_db, monkeypatch):
    graph = WarehouseExtractor(str(no_fk_db)).extract()
    _install_stub(monkeypatch, {}, [], stop_reason="refusal")
    assert suggest_lineage(graph) == []


def test_enrich_cli_and_build_llm_fallback(no_fk_db, tmp_path, monkeypatch, capsys):
    gp = tmp_path / "g.json"
    assert main(["build", "--warehouse", str(no_fk_db), "-o", str(gp)]) == 0
    captured = {}
    _install_stub(monkeypatch, captured, [
        {"kind": "column", "source": "column:orders.customer_id", "target": "column:customers.id",
         "confidence": 0.85, "reason": "naming"},
    ])
    capsys.readouterr()
    assert main(["enrich", "--graph", str(gp), "--dry-run"]) == 0
    assert "dry run" in capsys.readouterr().out
    assert main(["enrich", "--graph", str(gp)]) == 0
    assert "added 1" in capsys.readouterr().out
    assert main(["relationships", "--graph", str(gp), "--json"]) == 0
    rel = json.loads(capsys.readouterr().out)
    assert any(r["via"] == "llm" and r["provenance"] == "llm" for r in rel["column_relationships"])
    # build --llm-fallback does it inline
    gp2 = tmp_path / "g2.json"
    assert main(["build", "--warehouse", str(no_fk_db), "-o", str(gp2), "--llm-fallback"]) == 0
    assert "llm fallback: 1 suggestion(s), 1 edge(s) added" in capsys.readouterr().out


def test_missing_anthropic_is_a_clear_error(no_fk_db, monkeypatch):
    import builtins

    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "anthropic":
            raise ImportError("no anthropic")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    graph = WarehouseExtractor(str(no_fk_db)).extract()
    with pytest.raises(ImportError, match=r"impactgraph\[ai\]"):
        suggest_lineage(graph)
