"""Deterministic SQL lineage extractor built on sqlglot (optional dependency).

Parses ``.sql`` files and emits table/view nodes with DEPENDS_ON edges
(created relation depends on the relations it selects from), plus COLUMN
nodes for the output columns of each created relation.

Install with: ``pip install impactgraph[sql]``
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Union

from ..graph import Edge, EdgeType, ImpactGraph, Node, NodeType
from .base import Extractor

try:
    import sqlglot
    from sqlglot import exp

    HAS_SQLGLOT = True
except ImportError:  # pragma: no cover
    HAS_SQLGLOT = False


class SqlExtractor(Extractor):
    name = "sql"

    def __init__(
        self,
        root: Union[str, Path],
        dialect: Optional[str] = None,
    ) -> None:
        if not HAS_SQLGLOT:
            raise ImportError(
                "sqlglot is required for SQL extraction. "
                "Install it with: pip install impactgraph[sql]"
            )
        self.root = Path(root).resolve()
        self.dialect = dialect

    def extract(self) -> ImpactGraph:
        graph = ImpactGraph()
        sql_files = sorted(self.root.rglob("*.sql"))
        for path in sql_files:
            rel = path.relative_to(self.root).as_posix()
            file_id = f"file:{rel}"
            graph.add_node(Node(id=file_id, type=NodeType.FILE, name=rel, path=rel))
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            try:
                statements = sqlglot.parse(text, read=self.dialect)
            except Exception:
                continue
            for stmt in statements:
                if stmt is None:
                    continue
                self._extract_statement(graph, stmt, file_id)
        return graph

    def _extract_statement(self, graph: ImpactGraph, stmt, file_id: str) -> None:
        if isinstance(stmt, exp.Create):
            target = stmt.find(exp.Table)
            if target is None:
                return
            kind = (stmt.args.get("kind") or "table").lower()
            target_id = _table_id(target)
            graph.add_node(
                Node(
                    id=target_id,
                    type=NodeType.VIEW if kind == "view" else NodeType.TABLE,
                    name=_table_name(target),
                )
            )
            graph.add_edge(Edge(src=file_id, dst=target_id, type=EdgeType.CONTAINS))

            select = stmt.find(exp.Select)
            if select is not None:
                cte_names = {
                    cte.alias_or_name.lower() for cte in stmt.find_all(exp.CTE)
                }
                for source in _source_tables(stmt, exclude=cte_names | {_table_name(target).lower()}):
                    graph.add_edge(
                        Edge(src=target_id, dst=source, type=EdgeType.DEPENDS_ON)
                    )
                for col in _output_columns(select):
                    col_id = f"column:{_table_name(target)}.{col}"
                    graph.add_node(
                        Node(id=col_id, type=NodeType.COLUMN, name=col, meta={"parent": target_id})
                    )
                    graph.add_edge(Edge(src=target_id, dst=col_id, type=EdgeType.CONTAINS))

        elif isinstance(stmt, exp.Insert):
            target = stmt.find(exp.Table)
            if target is None:
                return
            target_id = _table_id(target)
            graph.add_node(
                Node(id=target_id, type=NodeType.TABLE, name=_table_name(target))
            )
            for source in _source_tables(stmt, exclude={_table_name(target).lower()}):
                graph.add_edge(Edge(src=target_id, dst=source, type=EdgeType.DEPENDS_ON))


def _table_name(table: "exp.Table") -> str:
    parts = [p.name for p in (table.args.get("catalog"), table.args.get("db")) if p]
    parts.append(table.name)
    return ".".join(parts)


def _table_id(table: "exp.Table") -> str:
    return "table:" + _table_name(table).lower()


def _source_tables(stmt, exclude=frozenset()) -> List[str]:
    out = []
    seen = set()
    for table in stmt.find_all(exp.Table):
        name = _table_name(table)
        if name.lower() in exclude:
            continue
        tid = "table:" + name.lower()
        if tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


def _output_columns(select: "exp.Select") -> List[str]:
    cols: List[str] = []
    for projection in select.expressions:
        if isinstance(projection, exp.Star):
            continue
        name = projection.alias_or_name
        if name and name != "*":
            cols.append(name)
    return cols
