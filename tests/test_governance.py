"""Governance metadata: glossary, domains, deprecation, dbt test results, search, PII."""

import json

import pytest

from datagraph import DbtExtractor, ImpactGraph, Node, NodeType, analyze_impact
from datagraph.analysis.discovery import pii_report, search
from datagraph.cli import main
from datagraph.knowledge import build_wiki, context
from datagraph.mcp_server import build_tools
from datagraph.metadata import (
    apply_metadata, dbt_governance, deprecated_assets, domains, glossary_index, load_metadata,
)

METADATA = {
    "version": 1,
    "glossary": [
        {"term": "Customer PII", "definition": "Personal data about a customer.", "owner": "privacy-office",
         "applies_to": ["column:customer.email", "dbt:customer"]},
        {"term": "Net Revenue", "definition": "Gross revenue minus refunds.", "applies_to": ["dbt:fact_booking"]},
        {"term": "Ghost Term", "applies_to": ["dbt:does_not_exist"]},
    ],
    "domains": [
        {"name": "Finance", "owner": "finance", "assets": ["dbt:fact_booking", "exposure:revenue_report"]},
        {"name": "Customer", "assets": ["dbt:*customer*"]},
    ],
    "deprecations": [
        {"asset": "dbt:customer", "reason": "Superseded by dim_customer.", "replacement": "dbt:dim_customer"},
    ],
    "owners": {"source:raw.customers": "ingestion-team"},
}


def test_apply_metadata_attaches_terms_domains_deprecation_owners(dbt_graph):
    applied = apply_metadata(dbt_graph, METADATA)
    assert applied["terms"] >= 2 and applied["deprecations"] == 1 and applied["owners"] == 1
    assert applied["unmatched"] == 1 and "Ghost Term" in applied["unmatched_refs"][0]

    model = dbt_graph.get_node("dbt:customer")
    assert "Customer PII" in model.meta["terms"]
    assert model.meta["deprecated"]["replacement"] == "dbt:dim_customer"
    assert dbt_graph.get_node("source:raw.customers").owner == "ingestion-team"

    # wildcard matching + owner inherited from the domain
    fact = dbt_graph.get_node("dbt:fact_booking")
    assert fact.meta["domain"] == "Finance" and fact.owner == "finance"
    assert dbt_graph.get_node("dbt:dim_customer").meta["domain"] == "Customer"

    index = glossary_index(dbt_graph)
    assert index["Customer PII"]["definition"].startswith("Personal data")
    assert "dbt:customer" in index["Customer PII"]["assets"]
    assert set(domains(dbt_graph)) == {"Finance", "Customer"}


def test_metadata_file_roundtrip_json(tmp_path, dbt_graph):
    path = tmp_path / "datagraph.json"
    path.write_text(json.dumps(METADATA), encoding="utf-8")
    apply_metadata(dbt_graph, load_metadata(path))
    assert dbt_graph.get_node("dbt:fact_booking").meta["domain"] == "Finance"


def test_metadata_file_roundtrip_yaml(tmp_path, dbt_graph):
    yaml = pytest.importorskip("yaml")
    path = tmp_path / "datagraph.yml"
    path.write_text(yaml.safe_dump(METADATA), encoding="utf-8")
    apply_metadata(dbt_graph, load_metadata(path))
    assert "Net Revenue" in dbt_graph.get_node("dbt:fact_booking").meta["terms"]


def test_deprecated_assets_and_impact_warning(dbt_graph):
    apply_metadata(dbt_graph, METADATA)
    rows = deprecated_assets(dbt_graph)
    assert rows[0]["id"] == "dbt:customer" and rows[0]["still_used_by"]

    analysis = analyze_impact(dbt_graph, ["source:raw.customers"])
    assert any("deprecated" in w and "dim_customer" in w for w in analysis.warnings)
    assert "warnings" in analysis.to_dict()


def test_dbt_governance_from_manifest_meta():
    node = {"meta": {"domain": "Finance", "terms": ["Net Revenue"], "deprecated": {"reason": "old", "replacement": "x"}},
            "tags": []}
    gov = dbt_governance(node)
    assert gov["domain"] == "Finance" and gov["terms"] == ["Net Revenue"]
    assert gov["deprecated"]["replacement"] == "x"
    assert dbt_governance({"tags": ["deprecated"]})["deprecated"]["reason"] == ""
    assert dbt_governance({"group": "growth"})["domain"] == "growth"
    assert dbt_governance({}) == {}


def _manifest_with_results(tmp_path):
    manifest = {
        "metadata": {"project_name": "demo"},
        "nodes": {
            "model.demo.dim_customer": {
                "resource_type": "model", "name": "dim_customer", "original_file_path": "models/dim_customer.sql",
                "database": "prod", "schema": "analytics", "config": {"materialized": "table"},
                "depends_on": {"nodes": ["source.demo.raw.customers"]}, "columns": {"customer_id": {}},
                "meta": {"domain": "Customer", "terms": ["Customer PII"]},
            },
        },
        "sources": {"source.demo.raw.customers": {"resource_type": "source", "name": "customers",
                                                  "source_name": "raw", "schema": "raw", "columns": {}}},
        "exposures": {},
    }
    manifest["nodes"]["test.demo.unique_dim_customer"] = {
        "resource_type": "test", "name": "unique_dim_customer_customer_id",
        "depends_on": {"nodes": ["model.demo.dim_customer"]},
    }
    manifest["nodes"]["test.demo.not_null_dim_customer"] = {
        "resource_type": "test", "name": "not_null_dim_customer_customer_id",
        "depends_on": {"nodes": ["model.demo.dim_customer"]},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (tmp_path / "run_results.json").write_text(json.dumps({
        "metadata": {"generated_at": "2026-08-25T06:00:00Z"},
        "results": [
            {"unique_id": "model.demo.dim_customer", "status": "success", "execution_time": 1.234},
            {"unique_id": "test.demo.unique_dim_customer", "status": "pass"},
            {"unique_id": "test.demo.not_null_dim_customer", "status": "fail"},
        ],
    }), encoding="utf-8")
    (tmp_path / "sources.json").write_text(json.dumps({
        "results": [{"unique_id": "source.demo.raw.customers", "status": "warn",
                     "max_loaded_at": "2026-08-20T00:00:00Z", "criteria": {"warn_after": {"count": 12, "period": "hour"}}}],
    }), encoding="utf-8")
    return tmp_path / "manifest.json"


def test_dbt_run_results_and_source_freshness(tmp_path):
    graph = DbtExtractor(_manifest_with_results(tmp_path)).extract()   # artifacts auto-detected
    status = graph.get_node("dbt:dim_customer").meta["status"]
    assert status["tests_passed"] == 1 and status["tests_failed"] == 1
    assert status["failing_tests"] == ["not_null_dim_customer_customer_id"]
    assert status["state"] == "success" and status["execution_time"] == 1.234
    assert graph.get_node("source:raw.customers").meta["status"]["freshness"] == "warn"

    # governance from dbt meta comes through too
    assert graph.get_node("dbt:dim_customer").meta["domain"] == "Customer"

    analysis = analyze_impact(graph, ["source:raw.customers"])
    assert any("failing dbt test" in w for w in analysis.warnings)
    assert any("freshness is warn" in w for w in analysis.warnings)
    assert "1 failing test(s)" in context(graph, "dim_customer")


def test_search_ranks_and_filters(dbt_graph):
    apply_metadata(dbt_graph, METADATA)
    hits = search(dbt_graph, "customer")
    assert hits[0]["name"] == "customer" and hits[0]["matched_on"] == "name"
    assert any(h["deprecated"] for h in hits)

    assert {h["id"] for h in search(dbt_graph, "", domain="Finance")} == {"dbt:fact_booking", "exposure:revenue_report"}
    assert [h["id"] for h in search(dbt_graph, "", term="Net Revenue")] == ["dbt:fact_booking"]
    assert all(h["type"] == "dashboard" for h in search(dbt_graph, "", node_type="dashboard"))
    assert search(dbt_graph, "", owner="finance")
    # a column name finds its table even though the query is not in the table name
    assert any(h["matched_on"] == "column" for h in search(dbt_graph, "email"))
    assert search(dbt_graph, "zzz-not-here") == []


def test_pii_report_lists_exposures(dbt_graph):
    report = pii_report(dbt_graph)
    assert report["sensitive_columns"] >= 1
    row = next(r for r in report["tables"] if r["id"] == "dbt:customer")
    assert "email" in row["columns"]
    assert {e["name"] for e in row["exposed_to"]} >= {"revenue_report", "customer_dashboard"}
    assert "heuristic" in report["note"]


def test_cli_search_pii_glossary_and_metadata(tmp_path, dbt_manifest, capsys):
    gp = tmp_path / "g.json"
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps(METADATA), encoding="utf-8")
    assert main(["build", "--dbt-manifest", str(dbt_manifest), "--metadata", str(meta), "-o", str(gp)]) == 0
    out = capsys.readouterr().out
    assert "metadata:" in out and "matched nothing" in out

    assert main(["search", "customer", "--graph", str(gp), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows and rows[0]["id"].endswith("customer")

    assert main(["search", "--graph", str(gp), "--domain", "Finance"]) == 0
    assert "fact_booking" in capsys.readouterr().out

    assert main(["glossary", "--graph", str(gp)]) == 0
    gloss = capsys.readouterr().out
    assert "Customer PII" in gloss and "Personal data" in gloss

    assert main(["pii", "--graph", str(gp)]) == 0
    pii_out = capsys.readouterr().out
    assert "email" in pii_out and "exposed" in pii_out

    assert main(["pii", "--graph", str(gp), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["tables"]


def test_wiki_and_mcp_expose_governance(tmp_path, dbt_graph):
    apply_metadata(dbt_graph, METADATA)
    build_wiki(dbt_graph, tmp_path / "kb")
    report = (tmp_path / "kb" / "GRAPH_REPORT.md").read_text(encoding="utf-8")
    assert "## Deprecated assets" in report and "## Glossary" in report
    assert "## Domains" in report and "## Sensitive data" in report
    index = (tmp_path / "kb" / "index.md").read_text(encoding="utf-8")
    assert "## By domain" in index and "Finance" in index

    pack = context(dbt_graph, "dbt:customer")
    assert "DEPRECATED" in pack and "glossary terms: Customer PII" in pack

    gp = tmp_path / "g.json"
    dbt_graph.save(gp)
    tools = build_tools(str(gp))
    assert "search" in tools and "sensitive_data" in tools
    assert tools["search"]("customer", domain="Customer")
    assert tools["sensitive_data"]()["tables"]
