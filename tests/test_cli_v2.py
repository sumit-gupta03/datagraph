import json

from datagraph.cli import main


def _build(tmp_path, dbt_manifest, py_project, extra=()):
    gp = tmp_path / "graph.json"
    rc = main(["build", "--dbt-manifest", str(dbt_manifest), "--repo", str(py_project), "-o", str(gp), *extra])
    assert rc == 0
    return gp


def test_build_update_skips_when_unchanged(tmp_path, dbt_manifest, py_project, capsys):
    gp = _build(tmp_path, dbt_manifest, py_project, ["--update"])
    rc = main(["build", "--dbt-manifest", str(dbt_manifest), "--repo", str(py_project), "-o", str(gp), "--update"])
    assert rc == 0
    assert "up to date" in capsys.readouterr().out


def test_paths_hotspots_export_html(tmp_path, dbt_manifest, py_project, capsys):
    gp = _build(tmp_path, dbt_manifest, py_project)
    assert main(["paths", "dbt:customer", "exposure:revenue_report", "--graph", str(gp)]) == 0
    assert "dbt:dim_customer" in capsys.readouterr().out

    assert main(["hotspots", "--graph", str(gp), "--top", "3", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 3 and rows[0]["blast_radius"] >= rows[-1]["blast_radius"]

    for fmt in ("graphml", "dot", "cypher", "json"):
        out = tmp_path / f"g.{fmt}"
        assert main(["export", "--graph", str(gp), "--format", fmt, "-o", str(out)]) == 0
        assert out.exists() and out.stat().st_size > 0
    capsys.readouterr()

    html = tmp_path / "impact.html"
    assert main(["html", "dbt:customer", "--graph", str(gp), "-o", str(html)]) == 0
    assert "<svg" in html.read_text(encoding="utf-8")
    capsys.readouterr()

    html2 = tmp_path / "impact2.html"
    assert main(["impact", "dbt:customer", "--graph", str(gp), "--json", "--html", str(html2), "--no-inferred"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["include_inferred"] is False and html2.exists()


def test_graph_diff_cli(tmp_path, dbt_manifest, py_project, capsys):
    gp = _build(tmp_path, dbt_manifest, py_project)
    gp2 = tmp_path / "graph2.json"
    # a second build with only dbt (python nodes "removed")
    assert main(["build", "--dbt-manifest", str(dbt_manifest), "-o", str(gp2)]) == 0
    capsys.readouterr()
    assert main(["graph-diff", str(gp), str(gp2), "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert any(n["id"] == "file:db.py" for n in d["removed_nodes"])


def test_hook_install_cli(tmp_path, dbt_manifest, capsys):
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    rc = main(["hook-install", "--git-repo", str(tmp_path), "--dbt-manifest", str(dbt_manifest), "-o", str(tmp_path / "g.json")])
    assert rc == 0
    assert (tmp_path / ".git" / "hooks" / "post-commit").exists()


def test_build_with_openlineage_and_lineage_file(tmp_path, capsys):
    ol = tmp_path / "events.json"
    ol.write_text(json.dumps([{"job": {"namespace": "af", "name": "j"},
                               "inputs": [{"namespace": "x", "name": "raw.a"}],
                               "outputs": [{"namespace": "x", "name": "stg.b"}]}]), encoding="utf-8")
    lf = tmp_path / "lineage.json"
    lf.write_text(json.dumps({"lineage": [{"entity": {"name": "mart.c"}, "upstream": [{"entity": {"name": "stg.b"}}]}]}), encoding="utf-8")
    gp = tmp_path / "g.json"
    assert main(["build", "--openlineage", str(ol), "--lineage-file", str(lf), "-o", str(gp)]) == 0
    capsys.readouterr()
    assert main(["impact", "table:raw.a", "--graph", str(gp), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "table:mart.c" in payload["affected"]   # lineage stitched across both sources
