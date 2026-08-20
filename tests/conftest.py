import json
import textwrap

import pytest

from impactgraph import DbtExtractor, ImpactGraph, PythonExtractor


@pytest.fixture
def py_project(tmp_path):
    """A tiny Python project: api.py imports db.py; handler calls load_customers."""
    (tmp_path / "db.py").write_text(
        textwrap.dedent(
            """
            def load_customers():
                return [1, 2, 3]

            def load_orders():
                return []
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "api.py").write_text(
        textwrap.dedent(
            """
            import db

            def customers_endpoint():
                return db.load_customers()
            """
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def dbt_manifest(tmp_path):
    """A minimal dbt manifest: source -> customer -> dim_customer -> fact_booking
    -> {revenue_report, customer_dashboard} exposures."""
    manifest = {
        "nodes": {
            "model.proj.customer": {
                "resource_type": "model",
                "name": "customer",
                "schema": "analytics",
                "database": "prod",
                "original_file_path": "models/customer.sql",
                "config": {"materialized": "view"},
                "columns": {"customer_id": {}, "email": {}},
                "depends_on": {"nodes": ["source.proj.raw.customers"]},
            },
            "model.proj.dim_customer": {
                "resource_type": "model",
                "name": "dim_customer",
                "schema": "analytics",
                "database": "prod",
                "original_file_path": "models/dim_customer.sql",
                "config": {"materialized": "table"},
                "columns": {},
                "depends_on": {"nodes": ["model.proj.customer"]},
            },
            "model.proj.fact_booking": {
                "resource_type": "model",
                "name": "fact_booking",
                "schema": "analytics",
                "database": "prod",
                "original_file_path": "models/fact_booking.sql",
                "config": {"materialized": "table"},
                "columns": {},
                "depends_on": {"nodes": ["model.proj.dim_customer"]},
            },
        },
        "sources": {
            "source.proj.raw.customers": {
                "source_name": "raw",
                "name": "customers",
            }
        },
        "exposures": {
            "exposure.proj.revenue_report": {
                "name": "revenue_report",
                "type": "dashboard",
                "owner": {"name": "finance"},
                "depends_on": {"nodes": ["model.proj.fact_booking"]},
            },
            "exposure.proj.customer_dashboard": {
                "name": "customer_dashboard",
                "type": "dashboard",
                "owner": {"name": "growth"},
                "depends_on": {"nodes": ["model.proj.fact_booking"]},
            },
        },
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


@pytest.fixture
def dbt_graph(dbt_manifest) -> ImpactGraph:
    return DbtExtractor(dbt_manifest).extract()


@pytest.fixture
def py_graph(py_project) -> ImpactGraph:
    return PythonExtractor(py_project).extract()
