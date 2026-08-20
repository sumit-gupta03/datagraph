import textwrap

import pytest

sqlglot = pytest.importorskip("sqlglot")

from impactgraph.extractors.sql_extractor import SqlExtractor  # noqa: E402


@pytest.fixture
def sql_dir(tmp_path):
    (tmp_path / "dim_customer.sql").write_text(
        textwrap.dedent(
            """
            CREATE TABLE analytics.dim_customer AS
            SELECT c.customer_id AS customer_key, c.email
            FROM raw.customers c;
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "fact_booking.sql").write_text(
        textwrap.dedent(
            """
            CREATE TABLE analytics.fact_booking AS
            SELECT b.booking_id, d.customer_key
            FROM raw.bookings b
            JOIN analytics.dim_customer d ON b.customer_id = d.customer_key;
            """
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_table_lineage(sql_dir):
    graph = SqlExtractor(sql_dir).extract()
    # raw.customers changed -> dim_customer -> fact_booking
    affected = graph.impact("table:raw.customers")
    assert "table:analytics.dim_customer" in affected
    assert "table:analytics.fact_booking" in affected


def test_output_columns(sql_dir):
    graph = SqlExtractor(sql_dir).extract()
    col = graph.get_node("column:analytics.dim_customer.customer_key")
    assert col is not None
    affected = graph.impact("table:analytics.dim_customer")
    assert "column:analytics.dim_customer.customer_key" in affected


def test_downstream_only(sql_dir):
    graph = SqlExtractor(sql_dir).extract()
    affected = graph.impact("table:analytics.fact_booking")
    assert "table:analytics.dim_customer" not in affected
