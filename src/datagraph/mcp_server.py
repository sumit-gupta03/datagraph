"""MCP server exposing datagraph to AI coding assistants (Claude Code, Cursor, ...).

Run with ``datagraph mcp --graph datagraph.json`` (stdio transport). Requires
the ``mcp`` package: ``pip install datagraph[mcp]``.

The tool implementations live in ``build_tools`` as plain functions so they can
be tested without the MCP runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Dict, List, Optional

from .analysis import analyze_impact
from .extractors import changed_node_ids, collect_changes
from .graph import ImpactGraph


def build_tools(graph_path: str) -> Dict[str, Callable]:
    """Return the MCP tool functions bound to a graph file (reloaded per call)."""

    def _graph() -> ImpactGraph:
        if not Path(graph_path).exists():
            raise FileNotFoundError(f"graph file '{graph_path}' not found — run 'datagraph build' first")
        return ImpactGraph.load(graph_path)

    def impact(nodes: List[str], max_depth: Optional[int] = None, include_inferred: bool = True) -> dict:
        """Blast radius, risk level, owners and test plan for changed nodes (ids, names or paths)."""
        g = _graph()
        analysis = analyze_impact(g, nodes, max_depth=max_depth, include_inferred=include_inferred)
        if not analysis.changed:
            return {"error": "no matching nodes", "candidates": [n.id for ref in nodes for n in g.find(ref)][:20]}
        return analysis.to_dict()

    def diff(repo: str = ".", base: str = "HEAD", head: Optional[str] = None, include_inferred: bool = True) -> dict:
        """Blast radius of the current git diff (or base...head) — what does this change break?"""
        g = _graph()
        changes = collect_changes(repo, base=base, head=head)
        ids = changed_node_ids(g, changes)
        if not ids:
            return {"changed_files": changes.files, "changed": [], "affected": {}, "note": "changed files map to no graph nodes"}
        return {"changed_files": changes.files, **analyze_impact(g, ids, include_inferred=include_inferred).to_dict()}

    def find_nodes(query: str, node_type: Optional[str] = None, limit: int = 50) -> List[dict]:
        """Search graph nodes by id, name or path; optionally filter by type."""
        g = _graph()
        found = [n for n in g.find(query) if node_type is None or n.type.value == node_type]
        return [{"id": n.id, "type": n.type.value, "name": n.name, "path": n.path, "owner": n.owner} for n in found[:limit]]

    def paths(changed: str, target: str) -> List[List[str]]:
        """All propagation paths from a changed node to a target node."""
        g = _graph()
        src, dst = g.resolve(changed), g.resolve(target)
        if not src or not dst:
            return []
        return g.impact_paths(src.id, dst.id)

    def hotspots(top: int = 10) -> List[dict]:
        """Nodes with the largest blast radius — where a change hurts most."""
        return _graph().hotspots(top=top)

    def lineage(node: str, upstream_depth: Optional[int] = None, downstream_depth: Optional[int] = None) -> dict:
        """Where a node comes from (upstream) and what it feeds (downstream), with trees."""
        g = _graph()
        n = g.resolve(node)
        if n is None:
            return {"error": "no matching node", "candidates": [m.id for m in g.find(node)][:20]}
        lin = g.lineage(n.id, upstream_depth, downstream_depth)
        return {"node": n.id, **lin, "upstream_tree": g.upstream_tree(n.id, upstream_depth),
                "downstream_tree": g.impact_tree(n.id, downstream_depth)}

    def relationships(search: Optional[str] = None, include_columns: bool = True) -> dict:
        """All table and column relationships (foreign keys, lineage) — the schema map for data analysis."""
        from .analysis.relationships import relationships as _rel

        return _rel(_graph(), search=search, include_columns=include_columns)

    return {"impact": impact, "diff": diff, "find_nodes": find_nodes, "paths": paths, "hotspots": hotspots,
            "lineage": lineage, "relationships": relationships}


def serve(graph_path: str) -> None:
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:
        raise ImportError("The MCP server requires the 'mcp' package: pip install datagraph[mcp]") from e
    mcp = FastMCP("datagraph")
    for name, fn in build_tools(graph_path).items():
        mcp.tool(name=name)(fn)
    mcp.run()
