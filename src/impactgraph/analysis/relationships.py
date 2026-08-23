"""Table / column relationship summary — the 'schema map' view for data analysis."""

from __future__ import annotations

from typing import Dict, List, Optional

from ..graph import EdgeType, ImpactGraph, NodeType

_TABLE_TYPES = {NodeType.TABLE, NodeType.VIEW, NodeType.DBT_MODEL, NodeType.DBT_SOURCE, NodeType.DBT_SEED, NodeType.DBT_SNAPSHOT}


def relationships(graph: ImpactGraph, search: Optional[str] = None, include_columns: bool = True) -> Dict:
    """Every table-like node with its columns, what it depends on, what depends on it,
    plus all column-to-column relationships (foreign keys, lineage)."""
    q = (search or "").lower()
    tables = [n for n in graph.nodes() if n.type in _TABLE_TYPES and (not q or q in n.id.lower() or q in n.name.lower())]
    table_ids = {n.id for n in tables}
    by_id = {n.id: n for n in graph.nodes()}

    cols_of: Dict[str, List[Dict]] = {}
    depends: Dict[str, List[Dict]] = {}
    dependents: Dict[str, List[Dict]] = {}
    table_rels: List[Dict] = []
    col_rels: List[Dict] = []

    for e in graph.edges():
        s, d = by_id.get(e.src), by_id.get(e.dst)
        if s is None or d is None:
            continue
        via = e.meta.get("via", e.type.value)
        if e.type == EdgeType.CONTAINS and s.type in _TABLE_TYPES and d.type == NodeType.COLUMN:
            cols_of.setdefault(s.id, []).append({"id": d.id, "name": d.name, "data_type": d.meta.get("data_type"),
                                                 "primary_key": bool(d.meta.get("primary_key"))})
        elif e.type in (EdgeType.DEPENDS_ON, EdgeType.WRITES_TO) and s.type in _TABLE_TYPES and d.type in _TABLE_TYPES:
            if e.type == EdgeType.DEPENDS_ON:
                src_t, dst_t = s, d          # s depends on d
            else:
                src_t, dst_t = d, s          # s writes_to d  => d depends on s
            rel = {"source": src_t.id, "target": dst_t.id, "via": via, "provenance": e.provenance}
            table_rels.append(rel)
            depends.setdefault(src_t.id, []).append({"target": dst_t.id, "via": via})
            dependents.setdefault(dst_t.id, []).append({"source": src_t.id, "via": via})
        elif include_columns and e.type == EdgeType.DEPENDS_ON and s.type == NodeType.COLUMN and d.type == NodeType.COLUMN:
            col_rels.append({"from": s.id, "to": d.id, "via": via, "provenance": e.provenance})

    out_tables = []
    for n in sorted(tables, key=lambda x: x.id):
        out_tables.append({
            "id": n.id, "name": n.name, "type": n.type.value, "owner": n.owner,
            "columns": sorted(cols_of.get(n.id, []), key=lambda c: c["name"]) if include_columns else [],
            "depends_on": sorted(depends.get(n.id, []), key=lambda r: r["target"]),
            "dependents": sorted(dependents.get(n.id, []), key=lambda r: r["source"]),
        })
    col_rels = [r for r in col_rels if not q or any(t in r["from"] or t in r["to"] for t in [q])]
    return {
        "tables": out_tables,
        "table_relationships": sorted([r for r in table_rels if r["source"] in table_ids or r["target"] in table_ids],
                                      key=lambda r: (r["source"], r["target"])),
        "column_relationships": sorted(col_rels, key=lambda r: (r["from"], r["to"])),
    }
