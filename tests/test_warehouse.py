import pytest

from impactgraph import WarehouseExtractor, NodeType


class FakeCursor:
    def __init__(self, tables):
        self.tables = tables
        self.rows = []

    def execute(self, sql):
        s = sql.lower()
        if ".tables" in s:
            self.rows = [(t["db"], t["schema"], t["name"], t["type"]) for t in self.tables]
        elif ".columns" in s:
            self.rows = [
                (t["db"], t["schema"], t["name"], c, "VARCHAR", i + 1)
                for t in self.tables
                for i, c in enumerate(t["columns"])
            ]
        elif ".views" in s:
            self.rows = [(t["db"], t["schema"], t["name"], t.get("definition")) for t in self.tables if t["type"] == "VIEW"]
        else:
            raise AssertionError(sql)

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class FakeConnection:
    def __init__(self, tables):
        self.tables = tables

    def cursor(self):
        return FakeCursor(self.tables)


TABLES = [
    {"db": "PROD", "schema": "RAW", "name": "CUSTOMERS", "type": "BASE TABLE", "columns": ["CUSTOMER_ID", "EMAIL"]},
    {"db": "PROD", "schema": "ANALYTICS", "name": "DIM_CUSTOMER", "type": "VIEW", "columns": ["CUSTOMER_KEY", "EMAIL"],
     "definition": "SELECT customer_id AS customer_key, email FROM prod.raw.customers"},
]


def test_tables_and_columns():
    graph = WarehouseExtractor(FakeConnection(TABLES), database="PROD", schemas=["RAW", "ANALYTICS"]).extract()
    assert graph.get_node("table:prod.raw.customers").type == NodeType.TABLE
    assert graph.get_node("table:prod.analytics.dim_customer").type == NodeType.VIEW
    assert graph.get_node("column:prod.raw.customers.customer_id") is not None
    assert graph.get_node("column:prod.raw.customers.customer_id").meta["data_type"] == "VARCHAR"


def test_view_definition_lineage():
    pytest.importorskip("sqlglot")
    graph = WarehouseExtractor(FakeConnection(TABLES), dialect="snowflake").extract()
    affected = graph.impact("column:prod.raw.customers.customer_id")
    assert "column:prod.analytics.dim_customer.customer_key" in affected
    assert "table:prod.analytics.dim_customer" in graph.impact("table:prod.raw.customers")
