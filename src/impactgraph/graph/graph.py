"""The unified impact graph built deterministically from engineering artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple, Union

import networkx as nx

from .model import IMPACT_DIRECTION, Edge, EdgeType, Node, NodeType


class ImpactGraph:
    """A directed multigraph of code and data artifacts.

    Nodes are files, functions, dbt models, tables, columns, dashboards, etc.
    Edges carry semantic types (CONTAINS, CALLS, DEPENDS_ON, ...) and the
    impact traversal knows in which direction change propagates for each type.
    """

    def __init__(self) -> None:
        self._g = nx.MultiDiGraph()

    # ------------------------------------------------------------------ build

    def add_node(self, node: Node) -> Node:
        existing = self._g.nodes.get(node.id)
        if existing is not None:
            # merge metadata, keep first-seen identity
            existing["node"].meta.update(node.meta)
            return existing["node"]
        self._g.add_node(node.id, node=node)
        return node

    def add_edge(self, edge: Edge) -> None:
        # ensure endpoints exist as placeholder nodes if not added yet
        for nid in (edge.src, edge.dst):
            if nid not in self._g:
                inferred = _infer_node_from_id(nid)
                self._g.add_node(nid, node=inferred)
        # avoid duplicate identical edges
        if self._g.has_edge(edge.src, edge.dst):
            for _, data in self._g[edge.src][edge.dst].items():
                if data["edge"].type == edge.type:
                    return
        self._g.add_edge(edge.src, edge.dst, edge=edge)

    def merge(self, other: "ImpactGraph") -> None:
        for node in other.nodes():
            self.add_node(node)
        for edge in other.edges():
            self.add_edge(edge)

    # ------------------------------------------------------------------ query

    def __contains__(self, node_id: str) -> bool:
        return node_id in self._g

    def __len__(self) -> int:
        return self._g.number_of_nodes()

    def get_node(self, node_id: str) -> Optional[Node]:
        data = self._g.nodes.get(node_id)
        return data["node"] if data else None

    def nodes(self, type: Optional[NodeType] = None) -> List[Node]:
        out = [d["node"] for _, d in self._g.nodes(data=True)]
        if type is not None:
            out = [n for n in out if n.type == type]
        return out

    def edges(self) -> List[Edge]:
        return [d["edge"] for _, _, d in self._g.edges(data=True)]

    def find(self, query: str) -> List[Node]:
        """Find nodes whose id, name, or path contains the query (case-insensitive)."""
        q = query.lower()
        return [
            n
            for n in self.nodes()
            if q in n.id.lower()
            or q in n.name.lower()
            or (n.path and q in n.path.lower())
        ]

    def resolve(self, ref: str) -> Optional[Node]:
        """Resolve a user-supplied reference to a single node.

        Tries exact id, then exact name, then unique substring match.
        """
        node = self.get_node(ref)
        if node:
            return node
        by_name = [n for n in self.nodes() if n.name == ref]
        if len(by_name) == 1:
            return by_name[0]
        by_path = [n for n in self.nodes() if n.path == ref]
        if len(by_path) == 1:
            return by_path[0]
        # a file node whose path matches wins over things defined in that file
        file_nodes = [n for n in by_path if n.type == NodeType.FILE]
        if len(file_nodes) == 1:
            return file_nodes[0]
        matches = self.find(ref)
        if len(matches) == 1:
            return matches[0]
        return None

    # --------------------------------------------------------------- traversal

    def _affected_neighbors(self, node_id: str) -> Iterable[Tuple[str, Edge]]:
        """Nodes affected when ``node_id`` changes, honoring per-type direction."""
        for _, dst, data in self._g.out_edges(node_id, data=True):
            edge: Edge = data["edge"]
            if IMPACT_DIRECTION[edge.type] == "forward":
                yield dst, edge
        for src, _, data in self._g.in_edges(node_id, data=True):
            edge = data["edge"]
            if IMPACT_DIRECTION[edge.type] == "reverse":
                yield src, edge

    def _column_parent(self, node_id: str) -> Optional[str]:
        """Owning model/table of a COLUMN node (via its incoming CONTAINS edge)."""
        node = self.get_node(node_id)
        if node is None or node.type != NodeType.COLUMN:
            return None
        parent = node.meta.get("parent")
        if parent and parent in self._g:
            return parent
        for src, _, data in self._g.in_edges(node_id, data=True):
            if data["edge"].type == EdgeType.CONTAINS:
                return src
        return None

    def _bfs(
        self,
        starts: Iterable[Tuple[str, int]],
        seen: Set[str],
        max_depth: Optional[int],
        column_filter: Optional[str] = None,
    ) -> Dict[str, int]:
        """Generic impact BFS from (node, depth) seeds.

        ``column_filter``: when set, COLUMN children reached via CONTAINS are
        only included if their name matches — used for column-level changes so
        a renamed ``customer_id`` lights up ``customer_id`` downstream, not every
        column of every downstream model.
        """
        depths: Dict[str, int] = {}
        frontier: Dict[str, int] = {}
        for nid, d in starts:
            frontier[nid] = d
        while frontier:
            next_frontier: Dict[str, int] = {}
            for nid, d in frontier.items():
                nd = d + 1
                if max_depth is not None and nd > max_depth:
                    continue
                for affected, edge in self._affected_neighbors(nid):
                    if affected in seen:
                        continue
                    if column_filter is not None and edge.type == EdgeType.CONTAINS:
                        child = self.get_node(affected)
                        if child is not None and child.type == NodeType.COLUMN and child.name != column_filter:
                            continue
                    seen.add(affected)
                    depths[affected] = nd
                    next_frontier[affected] = nd
            frontier = next_frontier
        return depths

    def impact(
        self, changed: Union[str, Iterable[str]], max_depth: Optional[int] = None
    ) -> Dict[str, int]:
        """Blast radius of one or more changed nodes.

        Returns a mapping of affected node id -> minimum propagation depth
        (1 = directly affected). The changed nodes themselves are excluded.

        Column changes propagate through the owning model/table to everything
        downstream of it, and to same-named columns in downstream relations.
        """
        if isinstance(changed, str):
            changed = [changed]
        roots = [c for c in changed if c in self._g]
        result: Dict[str, int] = {}
        for root in roots:
            seen: Set[str] = set(roots)
            parent = self._column_parent(root)
            if parent is not None:
                col_name = self.get_node(root).name
                local: Dict[str, int] = {}
                if parent not in seen and (max_depth is None or max_depth >= 1):
                    local[parent] = 1
                seen.add(parent)
                local.update(self._bfs([(parent, 1)], seen, max_depth, column_filter=col_name))
            else:
                local = self._bfs([(root, 0)], seen, max_depth)
            for nid, d in local.items():
                if nid not in result or d < result[nid]:
                    result[nid] = d
        return result

    def impact_paths(self, changed: str, target: str) -> List[List[str]]:
        """All simple propagation paths from a changed node to a target node."""
        h = self._impact_digraph()
        if changed not in h or target not in h:
            return []
        try:
            return [list(p) for p in nx.all_simple_paths(h, changed, target, cutoff=25)]
        except nx.NetworkXNoPath:
            return []

    def impact_tree(self, changed: str, max_depth: Optional[int] = None) -> Dict:
        """Nested dict tree of the blast radius, suitable for rendering."""
        seen: Set[str] = {changed}
        column_filter: Optional[str] = None

        def entry_for(nid: str) -> Dict:
            node = self.get_node(nid)
            return {
                "id": nid,
                "name": node.name if node else nid,
                "type": node.type.value if node else "unknown",
                "children": [],
            }

        def build(nid: str, depth: int) -> Dict:
            entry = entry_for(nid)
            if max_depth is not None and depth >= max_depth:
                return entry
            for affected, edge in self._affected_neighbors(nid):
                if affected in seen:
                    continue
                if column_filter is not None and edge.type == EdgeType.CONTAINS:
                    child_node = self.get_node(affected)
                    if child_node is not None and child_node.type == NodeType.COLUMN and child_node.name != column_filter:
                        continue
                seen.add(affected)
                child = build(affected, depth + 1)
                child["via"] = edge.type.value
                entry["children"].append(child)
            return entry

        parent = self._column_parent(changed)
        if parent is None:
            return build(changed, 0)

        # Column change: column -> owning model/table -> downstream (same-named columns only)
        column_filter = self.get_node(changed).name
        root = entry_for(changed)
        seen.add(parent)
        parent_entry = build(parent, 1)
        parent_entry["via"] = "contains"
        root["children"].append(parent_entry)
        return root

    def _impact_digraph(self) -> nx.DiGraph:
        """Plain DiGraph whose edges all point in the direction impact flows."""
        h = nx.DiGraph()
        h.add_nodes_from(self._g.nodes)
        for src, dst, data in self._g.edges(data=True):
            edge: Edge = data["edge"]
            if IMPACT_DIRECTION[edge.type] == "forward":
                h.add_edge(src, dst)
            else:
                h.add_edge(dst, src)
        return h

    # ------------------------------------------------------------ persistence

    def to_dict(self) -> Dict:
        return {
            "version": 1,
            "nodes": [n.to_dict() for n in self.nodes()],
            "edges": [e.to_dict() for e in self.edges()],
        }

    def save(self, path: Union[str, Path]) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )

    @classmethod
    def from_dict(cls, d: Dict) -> "ImpactGraph":
        g = cls()
        for nd in d.get("nodes", []):
            g.add_node(Node.from_dict(nd))
        for ed in d.get("edges", []):
            g.add_edge(Edge.from_dict(ed))
        return g

    @classmethod
    def load(cls, path: Union[str, Path]) -> "ImpactGraph":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def _infer_node_from_id(node_id: str) -> Node:
    """Best-effort node for an id referenced by an edge before being declared."""
    prefix, _, rest = node_id.partition(":")
    type_map = {
        "file": NodeType.FILE,
        "func": NodeType.FUNCTION,
        "class": NodeType.CLASS,
        "dbt": NodeType.DBT_MODEL,
        "source": NodeType.DBT_SOURCE,
        "exposure": NodeType.EXPOSURE,
        "table": NodeType.TABLE,
        "view": NodeType.VIEW,
        "column": NodeType.COLUMN,
        "api": NodeType.API,
        "lambda": NodeType.LAMBDA,
        "report": NodeType.REPORT,
    }
    ntype = type_map.get(prefix, NodeType.TABLE)
    name = rest.split("::")[-1] if rest else node_id
    return Node(id=node_id, type=ntype, name=name or node_id)
