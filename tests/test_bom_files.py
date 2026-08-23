"""Windows editors and PowerShell often write UTF-8 with a BOM; extractors must cope."""

import json

import pytest

from datagraph import DbtExtractor, NodeType, PythonExtractor

BOM = b"\xef\xbb\xbf"


def test_python_extractor_handles_bom(tmp_path):
    (tmp_path / "db.py").write_bytes(BOM + b"def load_customers():\n    return [1]\n")
    graph = PythonExtractor(tmp_path).extract()
    funcs = {n.name for n in graph.nodes(NodeType.FUNCTION)}
    assert "load_customers" in funcs


def test_dbt_extractor_handles_bom(tmp_path):
    manifest = {
        "nodes": {
            "model.p.m": {
                "resource_type": "model",
                "name": "m",
                "schema": "s",
                "database": "d",
                "original_file_path": "models/m.sql",
                "config": {},
                "columns": {},
                "depends_on": {"nodes": []},
            }
        },
        "sources": {},
        "exposures": {},
    }
    path = tmp_path / "manifest.json"
    path.write_bytes(BOM + json.dumps(manifest).encode("utf-8"))
    graph = DbtExtractor(path).extract()
    assert graph.get_node("dbt:m") is not None


def test_sql_extractor_handles_bom(tmp_path):
    pytest.importorskip("sqlglot")
    from datagraph.extractors.sql_extractor import SqlExtractor

    (tmp_path / "v.sql").write_bytes(
        BOM + b"CREATE VIEW a.v AS SELECT id FROM raw.t;"
    )
    graph = SqlExtractor(tmp_path).extract()
    assert graph.get_node("table:a.v") is not None
