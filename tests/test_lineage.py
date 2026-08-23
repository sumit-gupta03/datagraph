import io
import json

from rich.console import Console

from impactgraph.cli import main
from impactgraph.html_report import render_graph_html, render_lineage_html
from impactgraph.report import render_lineage


def test_upstream_and_lineage(dbt_graph):
    up = dbt_graph.upstream("dbt:fact_booking")
    assert "dbt:dim_customer" in up and "dbt:customer" in up and "source:raw.customers" in up
    assert "exposure:revenue_report" not in up  # downstream, not upstream
    assert up["dbt:dim_customer"] == 1 and up["dbt:customer"] == 2
    lin = dbt_graph.lineage("dbt:dim_customer")
    assert "dbt:customer" in lin["upstream"] and "dbt:fact_booking" in lin["downstream"]
    # roots have no upstream, leaves no downstream
    assert dbt_graph.upstream("source:raw.customers") == {}
    assert dbt_graph.impact("exposure:revenue_report") == {}


def test_upstream_tree_and_depth_limit(dbt_graph):
    tree = dbt_graph.upstream_tree("dbt:fact_booking", max_depth=1)
    child_ids = {c["id"] for c in tree["children"]}
    assert "dbt:dim_customer" in child_ids
    assert all(c["children"] == [] for c in tree["children"])  # depth-limited
    assert all(c["via"] for c in tree["children"])


def test_render_lineage_terminal(dbt_graph):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="utf-8", newline="")
    render_lineage(dbt_graph, "dbt:dim_customer", console=Console(file=stream, force_terminal=False, width=100, color_system=None))
    stream.flush()
    out = raw.getvalue().decode("utf-8")
    assert "Upstream" in out and "Downstream" in out
    assert "customer" in out and "fact_booking" in out


def test_lineage_html_and_whole_graph_html(dbt_graph):
    html = render_lineage_html(dbt_graph, "dbt:dim_customer")
    assert "Lineage of dim_customer" in html and "source:raw.customers" in html and "exposure:revenue_report" in html
    whole = render_graph_html(dbt_graph)
    assert "dbt:customer" in whole and "column:customer.email" not in whole  # columns hidden by default
    whole_cols = render_graph_html(dbt_graph, hide_columns=False)
    assert "column:customer.email" in whole_cols


def test_lineage_cli(tmp_path, dbt_manifest, capsys):
    gp = tmp_path / "g.json"
    assert main(["build", "--dbt-manifest", str(dbt_manifest), "-o", str(gp)]) == 0
    capsys.readouterr()
    out_html = tmp_path / "lineage.html"
    assert main(["lineage", "dim_customer", "--graph", str(gp), "--json", "--html", str(out_html)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["node"] == "dbt:dim_customer"
    assert "dbt:customer" in payload["upstream"] and "dbt:fact_booking" in payload["downstream"]
    assert out_html.exists()
    whole = tmp_path / "all.html"
    assert main(["html", "--all", "--graph", str(gp), "-o", str(whole)]) == 0
    assert "<svg" in whole.read_text(encoding="utf-8")
    assert main(["lineage", "nope_xyz", "--graph", str(gp)]) == 2
