"""Real-world regression fixture: dbt's public jaffle_shop project, compiled with
dbt-duckdb (manifest slimmed to the fields impactgraph reads)."""

from pathlib import Path

import pytest

from impactgraph import DbtExtractor, NodeType, analyze_impact
from impactgraph.analysis.relationships import relationships

MANIFEST = Path(__file__).resolve().parents[1] / "examples" / "jaffle_shop" / "manifest.json"
CATALOG = MANIFEST.with_name("catalog.json")


@pytest.fixture(scope="module")
def jaffle():
    return DbtExtractor(MANIFEST, catalog_path=CATALOG).extract()


def test_models_and_seeds(jaffle):
    models = {n.name for n in jaffle.nodes(NodeType.DBT_MODEL)}
    assert {"customers", "orders", "stg_customers", "stg_orders", "stg_payments"} <= models
    seeds = {n.name for n in jaffle.nodes(NodeType.DBT_SEED)}
    assert {"raw_customers", "raw_orders", "raw_payments"} <= seeds


def test_lineage_through_staging_to_seeds(jaffle):
    up = jaffle.upstream("dbt:customers")
    assert {"dbt:stg_customers", "dbt:stg_orders", "dbt:stg_payments", "dbt:raw_customers", "dbt:raw_orders", "dbt:raw_payments"} <= set(up)
    down = jaffle.impact("dbt:raw_orders")
    assert {"dbt:stg_orders", "dbt:orders", "dbt:customers"} <= set(down)


def test_real_compiled_sql_gives_column_lineage(jaffle):
    pytest.importorskip("sqlglot")
    col_edges = {(e.src, e.dst) for e in jaffle.edges() if e.type.value == "depends_on" and e.src.startswith("column:")}
    assert col_edges, "expected column-level edges from compiled_code"
    # stg_customers renames id -> customer_id from the raw_customers seed
    assert ("column:stg_customers.customer_id", "column:raw_customers.id") in col_edges
    # customers.customer_id derives (through CTEs) from stg_customers.customer_id
    assert ("column:customers.customer_id", "column:stg_customers.customer_id") in col_edges
    # a change to the seed's id column reaches the renamed column two hops downstream
    affected = jaffle.impact("column:raw_customers.id")
    assert "column:stg_customers.customer_id" in affected
    assert "column:customers.customer_id" in affected


def test_catalog_is_auto_detected_next_to_manifest():
    ext = DbtExtractor(MANIFEST)
    assert ext.catalog_path is not None and ext.catalog_path.name == "catalog.json"


def test_select_star_chain_resolves_through_catalog(jaffle):
    pytest.importorskip("sqlglot")
    # customers ends in `select * from final`; final aggregates stg_* columns
    col_edges = {(e.src, e.dst) for e in jaffle.edges() if e.type.value == "depends_on" and e.src.startswith("column:customers.")}
    assert ("column:customers.first_name", "column:stg_customers.first_name") in col_edges
    assert ("column:customers.number_of_orders", "column:stg_orders.order_id") in col_edges


def test_analysis_and_relationships_on_real_project(jaffle):
    analysis = analyze_impact(jaffle, ["stg_orders"])
    assert analysis.risk["level"] in {"MEDIUM", "HIGH", "CRITICAL"}
    assert any("dbt build" in t for t in analysis.recommended_tests)
    rel = relationships(jaffle)
    ids = {t["id"] for t in rel["tables"]}
    assert "dbt:customers" in ids and "table:jaffle_shop.main.customers" in ids
    customers = next(t for t in rel["tables"] if t["id"] == "dbt:customers")
    assert {d["target"] for d in customers["depends_on"]} >= {"dbt:stg_customers", "dbt:stg_orders", "dbt:stg_payments"}
