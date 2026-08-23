import json

from datagraph import Edge, EdgeType, ImpactGraph, Node, NodeType
from datagraph.cli import main
from datagraph.extractors.registry import ExtractorPlugin, get, plugins, register, unregister


class _LookerLike:
    """A toy third-party extractor: one dashboard that reads a table."""

    def __init__(self, path, token=None):
        self.path, self.token = path, token

    def extract(self):
        g = ImpactGraph()
        g.add_node(Node(id="exposure:sales_dash", type=NodeType.DASHBOARD, name="sales_dash", meta={"token_seen": bool(self.token)}))
        g.add_edge(Edge(src="table:prod.analytics.fact_booking", dst="exposure:sales_dash", type=EdgeType.EXPOSES))
        return g


def test_register_and_cli_flag(tmp_path, dbt_manifest, capsys):
    register(ExtractorPlugin(name="lookerlike", factory=_LookerLike, help="toy BI extractor", options={"token": "API token"}))
    try:
        assert get("lookerlike") is not None and any(p.name == "lookerlike" for p in plugins())
        gp = tmp_path / "g.json"
        rc = main(["build", "--dbt-manifest", str(dbt_manifest), "--lookerlike", "https://bi.example", "--lookerlike-token", "t", "-o", str(gp)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "lookerlike: " in out
        # the plugin's dashboard is reachable from the dbt model through the table node
        assert main(["impact", "dbt:customer", "--graph", str(gp), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert "exposure:sales_dash" in payload["affected"]
        assert main(["plugins"]) == 0
        assert "--lookerlike" in capsys.readouterr().out
    finally:
        unregister("lookerlike")


def test_plugin_only_build_is_allowed(tmp_path, capsys):
    register(ExtractorPlugin(name="toyonly", factory=_LookerLike))
    try:
        gp = tmp_path / "g.json"
        assert main(["build", "--toyonly", "x", "-o", str(gp)]) == 0
    finally:
        unregister("toyonly")


def test_plugin_must_return_graph():
    import pytest

    bad = ExtractorPlugin(name="bad", factory=lambda v, **o: object())
    with pytest.raises(TypeError):
        bad.extract("x")
