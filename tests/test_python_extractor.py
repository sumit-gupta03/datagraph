from impactgraph import NodeType


def test_files_and_functions_extracted(py_graph):
    files = {n.name for n in py_graph.nodes(NodeType.FILE)}
    assert {"db.py", "api.py"} <= files
    funcs = {n.name for n in py_graph.nodes(NodeType.FUNCTION)}
    assert {"load_customers", "load_orders", "customers_endpoint"} <= funcs


def test_import_edge_reversed_impact(py_graph):
    # db.py changed -> api.py is affected (api imports db)
    affected = py_graph.impact("file:db.py")
    assert "file:api.py" in affected


def test_call_edge_reversed_impact(py_graph):
    # load_customers changed -> customers_endpoint is affected
    affected = py_graph.impact("func:db.py::load_customers")
    assert "func:api.py::customers_endpoint" in affected


def test_file_change_impacts_contained_functions(py_graph):
    affected = py_graph.impact("file:db.py")
    assert "func:db.py::load_customers" in affected
    assert "func:db.py::load_orders" in affected


def test_unrelated_function_not_affected(py_graph):
    affected = py_graph.impact("func:db.py::load_orders")
    assert "func:api.py::customers_endpoint" not in affected
