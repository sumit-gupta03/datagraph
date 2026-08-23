import json

from datagraph.cli import main


def test_build_and_impact(tmp_path, dbt_manifest, capsys):
    graph_path = tmp_path / "graph.json"
    rc = main(
        ["build", "--dbt-manifest", str(dbt_manifest), "-o", str(graph_path)]
    )
    assert rc == 0
    assert graph_path.exists()

    rc = main(
        ["impact", "dbt:customer", "--graph", str(graph_path), "--json"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])
    assert "exposure:revenue_report" in payload["affected"]
    assert payload["risk"]["level"] in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}


def test_impact_unknown_node(tmp_path, dbt_manifest):
    graph_path = tmp_path / "graph.json"
    main(["build", "--dbt-manifest", str(dbt_manifest), "-o", str(graph_path)])
    rc = main(["impact", "no_such_node_xyz", "--graph", str(graph_path)])
    assert rc == 2


def test_missing_graph_file(tmp_path):
    rc = main(["impact", "x", "--graph", str(tmp_path / "missing.json")])
    assert rc == 2


def test_nodes_listing(tmp_path, dbt_manifest, capsys):
    graph_path = tmp_path / "graph.json"
    main(["build", "--dbt-manifest", str(dbt_manifest), "-o", str(graph_path)])
    rc = main(["nodes", "--graph", str(graph_path), "--type", "dbt_model"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dbt:customer" in out
    assert "dbt:dim_customer" in out
