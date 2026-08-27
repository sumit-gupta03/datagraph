
import pytest

from datagraph import maintenance
from datagraph.mcp_server import build_tools


def test_mcp_tools_without_runtime(tmp_path, dbt_graph):
    gp = tmp_path / "g.json"
    dbt_graph.save(gp)
    tools = build_tools(str(gp))
    out = tools["impact"](["dbt:customer"])
    assert "exposure:revenue_report" in out["affected"]
    assert tools["find_nodes"]("customer", node_type="dbt_model")[0]["id"].startswith("dbt:")
    assert tools["paths"]("dbt:customer", "exposure:revenue_report")
    assert tools["hotspots"](top=2)[0]["blast_radius"] > 0
    assert tools["impact"](["nope_xyz"])["error"] == "no matching nodes"


def test_mcp_missing_graph(tmp_path):
    tools = build_tools(str(tmp_path / "missing.json"))
    with pytest.raises(FileNotFoundError):
        tools["hotspots"]()


def test_fingerprint_and_cache(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("x = 1\n", encoding="utf-8")
    gp = tmp_path / "g.json"
    gp.write_text("{}", encoding="utf-8")
    assert not maintenance.is_up_to_date(str(gp), [str(src)])
    maintenance.write_cache(str(gp), [str(src)])
    assert maintenance.is_up_to_date(str(gp), [str(src)])
    (src / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert not maintenance.is_up_to_date(str(gp), [str(src)])


def test_watch_rebuilds_on_change(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("x = 1\n", encoding="utf-8")
    builds = []
    calls = {"n": 0}

    def build():
        builds.append(1)
        calls["n"] += 1
        if calls["n"] == 1:
            (src / "a.py").write_text("x = 2\n", encoding="utf-8")  # change after first build

    maintenance.watch(build, [str(src)], interval=0, max_iterations=3, log=lambda *_: None)
    assert len(builds) == 2  # initial + one rebuild after the change, no rebuild when unchanged


def test_install_hook(tmp_path):
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    path = maintenance.install_hook(str(tmp_path), "datagraph build --repo . --update")
    assert path.exists()
    assert "datagraph build" in path.read_text(encoding="utf-8")


def test_mcp_server_class_resolves_on_any_sdk_version():
    """`datagraph mcp` must work on mcp 1.x and 2.x.

    mcp 2.0 renamed FastMCP to MCPServer; the studio hands users a config that launches this
    server, so a rename in an optional dependency must not turn into a broken command.
    """
    pytest.importorskip("mcp")
    from datagraph.mcp_server import _server_class

    cls = _server_class()
    assert hasattr(cls, "tool")
    assert hasattr(cls, "run")
