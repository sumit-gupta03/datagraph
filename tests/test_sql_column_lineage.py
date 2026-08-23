import textwrap

import pytest

pytest.importorskip("sqlglot")

from datagraph.extractors.sql_extractor import SqlExtractor  # noqa: E402


@pytest.fixture
def sql_dir(tmp_path):
    (tmp_path / "dim_customer.sql").write_text(
        textwrap.dedent(
            """
            CREATE TABLE analytics.dim_customer AS
            WITH c AS (SELECT customer_id, email FROM raw.customers)
            SELECT c.customer_id AS customer_key, lower(c.email) AS email
            FROM c;
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "fact_booking.sql").write_text(
        textwrap.dedent(
            """
            CREATE TABLE analytics.fact_booking AS
            SELECT b.booking_id, d.customer_key, b.amount
            FROM raw.bookings b
            JOIN analytics.dim_customer d ON b.customer_id = d.customer_key;
            """
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_column_to_column_edges_through_cte_and_alias(sql_dir):
    graph = SqlExtractor(sql_dir).extract()
    edges = {(e.src, e.dst) for e in graph.edges() if e.type.value == "depends_on"}
    # renamed column traced through the CTE and alias
    assert ("column:analytics.dim_customer.customer_key", "column:raw.customers.customer_id") in edges
    assert ("column:analytics.dim_customer.email", "column:raw.customers.email") in edges
    assert ("column:analytics.fact_booking.customer_key", "column:analytics.dim_customer.customer_key") in edges
    assert ("column:analytics.fact_booking.amount", "column:raw.bookings.amount") in edges


def test_renamed_column_change_propagates_by_lineage_not_name(sql_dir):
    graph = SqlExtractor(sql_dir).extract()
    affected = graph.impact("column:raw.customers.customer_id")
    # true lineage: customer_id -> customer_key (renamed) -> fact_booking.customer_key
    assert "column:analytics.dim_customer.customer_key" in affected
    assert "column:analytics.fact_booking.customer_key" in affected
    # an unrelated column in the same table is not flagged
    assert "column:analytics.dim_customer.email" not in affected
    # tables downstream are flagged too (via the owning-table path)
    assert "table:analytics.dim_customer" in affected


def test_tree_marks_sql_lineage_as_extracted(sql_dir):
    graph = SqlExtractor(sql_dir).extract()
    tree = graph.impact_tree("column:raw.customers.customer_id")
    child_ids = {c["id"]: c for c in tree["children"]}
    assert "column:analytics.dim_customer.customer_key" in child_ids
    assert child_ids["column:analytics.dim_customer.customer_key"]["provenance"] == "extracted"
