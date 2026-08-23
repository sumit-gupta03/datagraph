import textwrap

from datagraph import JsExtractor, NodeType
from datagraph.cli import main


def _project(tmp_path):
    (tmp_path / "db.ts").write_text(textwrap.dedent('''
        export async function loadCustomers(client) {
          return client.query(`SELECT customer_id, email FROM analytics.dim_customer`);
        }

        export const loadOrders = async (client) => {
          return client.query("SELECT * FROM analytics.fact_booking");
        };
    '''), encoding="utf-8")
    (tmp_path / "api.ts").write_text(textwrap.dedent('''
        import { loadCustomers } from './db';

        export async function customersEndpoint(req, res) {
          const rows = await loadCustomers(req.client);
          res.json(rows);
        }
    '''), encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("function ignored() {}", encoding="utf-8")
    return tmp_path


def test_js_functions_imports_calls(tmp_path):
    graph = JsExtractor(_project(tmp_path)).extract()
    funcs = {n.name for n in graph.nodes(NodeType.FUNCTION)}
    assert {"loadCustomers", "loadOrders", "customersEndpoint"} <= funcs
    assert "ignored" not in funcs  # node_modules skipped
    edges = {(e.src, e.dst, e.type.value) for e in graph.edges()}
    assert ("file:api.ts", "file:db.ts", "imports") in edges
    assert ("func:api.ts::customersEndpoint", "func:db.ts::loadCustomers", "calls") in edges
    # callee change reaches the caller; file change reaches the importer
    assert "func:api.ts::customersEndpoint" in graph.impact("func:db.ts::loadCustomers")
    assert "file:api.ts" in graph.impact("file:db.ts")


def test_js_sql_bridge(tmp_path):
    graph = JsExtractor(_project(tmp_path)).extract()
    affected = graph.impact("table:analytics.dim_customer")
    assert "func:db.ts::loadCustomers" in affected
    assert "func:api.ts::customersEndpoint" in affected  # via the call edge
    assert "func:db.ts::loadOrders" not in affected


def test_js_cli(tmp_path, capsys):
    gp = tmp_path / "g.json"
    assert main(["build", "--js", str(_project(tmp_path)), "-o", str(gp)]) == 0
    assert "js/ts:" in capsys.readouterr().out
    assert main(["impact", "table:analytics.fact_booking", "--graph", str(gp)]) == 0
    assert "loadOrders" in capsys.readouterr().out
