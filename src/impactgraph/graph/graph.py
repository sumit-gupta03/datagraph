"""The unified impact graph built deterministically from engineering artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple, Union

import networkx as nx

from .model import EXTRACTED, IMPACT_DIRECTION, INFERRED, Edge, EdgeType, Node, NodeType


class ImpactGraph:
    """A directed multigraph of code and data artifacts.

    Nodes are files, functions, dbt models, tables, columns, dashboards, etc.
    Edges carry semantic types (CONTAINS, CALLS, DEPENDS_ON, ...) and the
    impact traversal knows in which direction change propagates for each type.

    Every edge has a provenance in ``edge.meta["provenance"]``: ``"extracted"``
    (read directly from an artifact — manifest DAG, AST containment, SQL
    lineage) or ``"inferred"`` (a heuristic such as name-based call
    resolution). Traversals can exclude inferred edges.
    """

    def __init__(self) -> None:
        self._g = nx.MultiDiGraph()

    # ------------------------------------------------------------------ build

    def add_node(self, node: Node) -> Node:
        existing = self._g.nodes.get(node.id)
        if existing is not None:
            existing["node"].meta.update({k: v for k, v in node.meta.items() if v is not None})
            if existing["node"].path is None and node.path:
                existing["node"].path = node.path
            return existing["node"]
        self._g.add_node(node.id, node=node)
        return node

    def add_edge(self, edge: Edge) -> None:
        for nid in (edge.src, edge.dst):
            if nid not in self._g:
                self._g.add_node(nid, node=_infer_node_from_id(nid))
        edge.meta.setdefault("provenance", EXTRACTED)
        if self._g.has_edge(edge.src, edge.dst):
            for _, data in self._g[edge.src][edge.dst].items():
                if data["edge"].type == edge.type:
                    # keep the strongest provenance
                    if data["edge"].meta.get("provenance") == INFERRED and edge.meta.get("provenance") == EXTRACTED:
                        data["edge"].meta["provenance"] = EXTRACTED
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

    def edges_of(self, node_id: str) -> List[Edge]:
        out = [d["edge"] for _, _, d in self._g.out_edges(node_id, data=True)]
        out += [d["edge"] for _, _, d in self._g.in_edges(node_id, data=True)]
        return out

    def find(self, query: str) -> List[Node]:
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

        Order: exact id, exact name, exact path, the file node on that path,
        unique substring match.
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
        file_nodes = [n for n in by_path if n.type == NodeType.FILE]
        if len(file_nodes) == 1:
            return file_nodes[0]
        matches = self.find(ref)
        if len(matches) == 1:
            return matches[0]
        return None

    # --------------------------------------------------------------- traversal

    def _affected_neighbors(
        self, node_id: str, include_inferred: bool = True
    ) -> Iterable[Tuple[str, Edge]]:
        """Nodes affected when ``node_id`` changes, honoring per-type direction."""
        for _, dst, data in self._g.out_edges(node_id, data=True):
            edge: Edge = data["edge"]
            if not include_inferred and edge.meta.get("provenance") == INFERRED:
                continue
            if IMPACT_DIRECTION[edge.type] == "forward":
                yield dst, edge
        for src, _, data in self._g.in_edges(node_id, data=True):
            edge = data["edge"]
            if not include_inferred and edge.meta.get("provenance") == INFERRED:
                continue
            if IMPACT_DIRECTION[edge.type] == "reverse":
                yield src, edge

    def _column_parent(self, node_id: str) -> Optional[str]:
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
        include_inferred: bool = True,
    ) -> Dict[str, int]:
        depths: Dict[str, int] = {}
        frontier: Dict[str, int] = dict(starts)
        while frontier:
            next_frontier: Dict[str, int] = {}
            for nid, d in frontier.items():
                nd = d + 1
                if max_depth is not None and nd > max_depth:
                    continue
                for affected, edge in self._affected_neighbors(nid, include_inferred):
                    if affected in seen:
                        continue
                    if column_filter is not None and edge.type == EdgeType.CONTAINS:
                        child = self.get_node(affected)
                        if child is not None and child.type == NodeType.COLUMN and child.name.lower() != column_filter.lower():
                            continue
                    seen.add(affected)
                    depths[affected] = nd
                    next_frontier[affected] = nd
            frontier = next_frontier
        return depths

    def impact(
        self,
        changed: Union[str, Iterable[str]],
        max_depth: Optional[int] = None,
        include_inferred: bool = True,
    ) -> Dict[str, int]:
        """Blast radius of one or more changed nodes: affected id -> min depth.

        Column changes follow true column-to-column lineage edges when the
        extractors produced them, and additionally propagate through the owning
        model/table to its downstream, flagging same-named downstream columns
        (a rename heuristic marked as inferred in trees).
        """
        if isinstance(changed, str):
            changed = [changed]
        roots = [c for c in changed if c in self._g]
        result: Dict[str, int] = {}
        for root in roots:
            seen: Set[str] = set(roots)
            local = self._bfs([(root, 0)], seen, max_depth, include_inferred=include_inferred)
            parent = self._column_parent(root)
            if parent is not None:
                col_name = self.get_node(root).name
                if parent not in seen and (max_depth is None or max_depth >= 1):
                    local[parent] = 1
                seen.add(parent)
                local.update(
                    self._bfs([(parent, 1)], seen, max_depth, column_filter=col_name, include_inferred=include_inferred)
                )
            for nid, d in local.items():
                if nid not in result or d < result[nid]:
                    result[nid] = d
        return result

    def impact_paths(self, changed: str, target: str, cutoff: int = 25) -> List[List[str]]:
        h = self._impact_digraph()
        if changed not in h or target not in h:
            return []
        try:
            return [list(p) for p in nx.all_simple_paths(h, changed, target, cutoff=cutoff)]
        except nx.NetworkXNoPath:
            return []

    def impact_tree(
        self, changed: str, max_depth: Optional[int] = None, include_inferred: bool = True
    ) -> Dict:
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
            for affected, edge in self._affected_neighbors(nid, include_inferred):
                if affected in seen:
                    continue
                heuristic = False
                if column_filter is not None and edge.type == EdgeType.CONTAINS:
                    child_node = self.get_node(affected)
                    if child_node is not None and child_node.type == NodeType.COLUMN:
                        if child_node.name.lower() != column_filter.lower():
                            continue
                        heuristic = True
                seen.add(affected)
                child = build(affected, depth + 1)
                child["via"] = edge.type.value
                child["provenance"] = INFERRED if heuristic else edge.meta.get("provenance", EXTRACTED)
                if heuristic:
                    child["via"] = "same-name column"
                entry["children"].append(child)
            return entry

        parent = self._column_parent(changed)
        root = build(changed, 0)  # follows true column-lineage edges if present
        if parent is None:
            return root
        column_filter = self.get_node(changed).name
        if parent not in seen:
            seen.add(parent)
            parent_entry = build(parent, 1)
            parent_entry["via"] = "contains"
            parent_entry["provenance"] = EXTRACTED
            root["children"].append(parent_entry)
        return root

    def _impact_digraph(self, include_inferred: bool = True) -> nx.DiGraph:
        h = nx.DiGraph()
        h.add_nodes_from(self._g.nodes)
        for src, dst, data in self._g.edges(data=True):
            edge: Edge = data["edge"]
            if not include_inferred and edge.meta.get("provenance") == INFERRED:
                continue
            if IMPACT_DIRECTION[edge.type] == "forward":
                h.add_edge(src, dst, type=edge.type.value)
            else:
                h.add_edge(dst, src, type=edge.type.value)
        return h

    # ---------------------------------------------------------------- insight

    def hotspots(self, top: int = 10, include_inferred: bool = True) -> List[Dict]:
        """Nodes with the largest blast radius — the places a change hurts most."""
        rows = []
        for node in self.nodes():
            if node.type == NodeType.COLUMN:
                continue
            affected = self.impact(node.id, include_inferred=include_inferred)
            rows.append(
                {
                    "id": node.id,
                    "name": node.name,
                    "type": node.type.value,
                    "blast_radius": len(affected),
                    "in_degree": self._g.in_degree(node.id),
                    "out_degree": self._g.out_degree(node.id),
                }
            )
        rows.sort(key=lambda r: (-r["blast_radius"], -(r["in_degree"] + r["out_degree"]), r["id"]))
        return rows[:top]

    def subgraph(self, node_ids: Iterable[str]) -> "ImpactGraph":
        ids = set(node_ids)
        sub = ImpactGraph()
        for nid in ids:
            node = self.get_node(nid)
            if node:
                sub.add_node(node)
        for edge in self.edges():
            if edge.src in ids and edge.dst in ids:
                sub.add_edge(edge)
        return sub

    # ---------------------------------------------------------------- exports

    def _plain_digraph(self) -> nx.DiGraph:
        h = nx.DiGraph()
        for node in self.nodes():
            h.add_node(node.id, name=node.name, type=node.type.value, path=node.path or "")
        for edge in self.edges():
            h.add_edge(edge.src, edge.dst, type=edge.type.value, provenance=edge.meta.get("provenance", EXTRACTED))
        return h

    def to_graphml(self, path: Union[str, Path]) -> None:
        nx.write_graphml(self._plain_digraph(), str(path))

    def to_dot(self) -> str:
        lines = ["digraph impactgraph {", "  rankdir=LR;", "  node [shape=box, fontname=Helvetica];"]
        for node in self.nodes():
            lines.append(f'  "{_esc(node.id)}" [label="{_esc(node.name)}\\n({node.type.value})"];')
        for edge in self.edges():
            style = ', style=dashed' if edge.meta.get("provenance") == INFERRED else ""
            lines.append(f'  "{_esc(edge.src)}" -> "{_esc(edge.dst)}" [label="{edge.type.value}"{style}];')
        lines.append("}")
        return "\n".join(lines)

    def to_cypher(self) -> str:
        out = []
        for node in self.nodes():
            label = "".join(p.capitalize() for p in node.type.value.split("_"))
            out.append(
                f"MERGE (n:{label} {{id: '{_esc(node.id)}'}}) SET n.name = '{_esc(node.name)}'"
                + (f", n.path = '{_esc(node.path)}'" if node.path else "")
                + ";"
            )
        for edge in self.edges():
            rel = edge.type.value.upper()
            out.append(
                f"MATCH (a {{id: '{_esc(edge.src)}'}}), (b {{id: '{_esc(edge.dst)}'}}) "
                f"MERGE (a)-[:{rel} {{provenance: '{edge.meta.get('provenance', EXTRACTED)}'}}]->(b);"
            )
        return "\n".join(out)

    # ------------------------------------------------------------ persistence

    def to_dict(self) -> Dict:
        return {
            "version": 2,
            "nodes": [n.to_dict() for n in self.nodes()],
            "edges": [e.to_dict() for e in self.edges()],
        }

    def save(self, path: Union[str, Path]) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")

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
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8-sig")))


def diff_graphs(old: ImpactGraph, new: ImpactGraph) -> Dict:
    """Schema / dependency drift between two graph snapshots."""
    old_nodes = {n.id: n for n in old.nodes()}
    new_nodes = {n.id: n for n in new.nodes()}
    old_edges = {(e.src, e.dst, e.type.value) for e in old.edges()}
    new_edges = {(e.src, e.dst, e.type.value) for e in new.edges()}
    added_nodes = sorted(set(new_nodes) - set(old_nodes))
    removed_nodes = sorted(set(old_nodes) - set(new_nodes))
    return {
        "added_nodes": [new_nodes[i].to_dict() for i in added_nodes],
        "removed_nodes": [old_nodes[i].to_dict() for i in removed_nodes],
        "added_edges": [{"src": s, "dst": d, "type": t} for s, d, t in sorted(new_edges - old_edges)],
        "removed_edges": [{"src": s, "dst": d, "type": t} for s, d, t in sorted(old_edges - new_edges)],
        "removed_columns": [i for i in removed_nodes if old_nodes[i].type == NodeType.COLUMN],
        "added_columns": [i for i in added_nodes if new_nodes[i].type == NodeType.COLUMN],
    }


def _esc(s: Optional[str]) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"').replace("'", "\\'")


def _infer_node_from_id(node_id: str) -> Node:
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
        "job": NodeType.DAG,
        "dag": NodeType.DAG,
    }
    ntype = type_map.get(prefix, NodeType.TABLE)
    name = rest.split("::")[-1] if rest else node_id
    meta = {}
    if ntype == NodeType.COLUMN and "." in rest:
        parent_name, _, _col = rest.rpartition(".")
        meta["parent_hint"] = parent_name
    return Node(id=node_id, type=ntype, name=name or node_id, meta=meta)
