"""Build the demo graph: dbt lineage + Python code, bridged by one edge.

Run from this directory:
    python build_demo.py
    datagraph impact models/customer.sql --graph datagraph.json
"""

from pathlib import Path

from datagraph import DbtExtractor, Edge, EdgeType, ImpactGraph, PythonExtractor

HERE = Path(__file__).parent

graph = ImpactGraph()
graph.merge(DbtExtractor(HERE / "manifest.json").extract())
graph.merge(PythonExtractor(HERE / "app").extract())

# Bridge code and data: the API function depends on the fact_booking table,
# so a change anywhere upstream of that table reaches the Python API.
graph.add_edge(
    Edge(
        src="func:booking_api.py::fetch_bookings",
        dst="table:prod.analytics.fact_booking",
        type=EdgeType.DEPENDS_ON,
    )
)

graph.save(HERE / "datagraph.json")
print(f"built {len(graph)} nodes, {len(graph.edges())} edges -> {HERE / 'datagraph.json'}")
