"""Deterministic SQL lineage extractor built on sqlglot (optional dependency).

Parses ``.sql`` files and emits:
  * table/view nodes with DEPENDS_ON edges (created relation -> relations it
    selects from),
  * COLUMN nodes for the output columns of each created relation, and
  * true column-to-column DEPENDS_ON edges via sqlglot's lineage engine
    (``analytics.dim_customer.customer_key`` depends on
    ``raw.customers.customer_id``), so a column change propagates to the exact
    downstream columns that derive from it — including renamed ones.

Install with: ``pip install datagraph[sql]``
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from ..graph import Edge, EdgeType, ImpactGraph, Node, NodeType
from .base import Extractor

try:
    import sqlglot
    from sqlglot import exp
    from sqlglot.lineage import lineage as _sqlglot_lineage

    HAS_SQLGLOT = True
except ImportError:  # pragma: no cover
    HAS_SQLGLOT = False


def _require_sqlglot() -> None:
    if not HAS_SQLGLOT:
        raise ImportError(
            "sqlglot is required for SQL lineage. Install it with: pip install datagraph[sql]"
        )


class SqlExtractor(Extractor):
    name = "sql"

    def __init__(self, root: Union[str, Path], dialect: Optional[str] = None) -> None:
        _require_sqlglot()
        self.root = Path(root).resolve()
        self.dialect = dialect
        # SQL the parser could not handle — candidates for the LLM fallback (ai.suggest_lineage)
        self.unparsed: List[Dict[str, str]] = []

    def extract(self) -> ImpactGraph:
        graph = ImpactGraph()
        for path in sorted(self.root.rglob("*.sql")):
            rel = path.relative_to(self.root).as_posix()
            file_id = f"file:{rel}"
            graph.add_node(Node(id=file_id, type=NodeType.FILE, name=rel, path=rel))
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            try:
                statements = sqlglot.parse(text, read=self.dialect)
            except Exception as e:
                self.unparsed.append({"where": rel, "sql": text, "error": str(e)[:200]})
                continue
            for stmt in statements:
                if stmt is not None:
                    try:
                        self._extract_statement(graph, stmt, file_id)
                    except Exception as e:
                        self.unparsed.append({"where": rel, "sql": stmt.sql(), "error": str(e)[:200]})
        return graph

    def _extract_statement(self, graph: ImpactGraph, stmt, file_id: str) -> None:
        if isinstance(stmt, exp.Create):
            target = stmt.find(exp.Table)
            if target is None:
                return
            kind = (stmt.args.get("kind") or "table").lower()
            target_name = table_name(target)
            target_id = "table:" + target_name.lower()
            graph.add_node(
                Node(id=target_id, type=NodeType.VIEW if kind == "view" else NodeType.TABLE, name=target_name)
            )
            graph.add_edge(Edge(src=file_id, dst=target_id, type=EdgeType.CONTAINS))
            query = stmt.expression
            if query is None or query.find(exp.Select) is None:
                return
            cte_names = {cte.alias_or_name.lower() for cte in stmt.find_all(exp.CTE)}
            for source in source_tables(stmt, exclude=cte_names | {target_name.lower()}):
                graph.add_edge(Edge(src=target_id, dst=source, type=EdgeType.DEPENDS_ON))
            add_column_lineage(graph, target_id, target_name, query, self.dialect)

        elif isinstance(stmt, exp.Insert):
            target = stmt.find(exp.Table)
            if target is None:
                return
            target_name = table_name(target)
            target_id = "table:" + target_name.lower()
            graph.add_node(Node(id=target_id, type=NodeType.TABLE, name=target_name))
            for source in source_tables(stmt, exclude={target_name.lower()}):
                graph.add_edge(Edge(src=target_id, dst=source, type=EdgeType.DEPENDS_ON))
            query = stmt.expression
            if query is not None and query.find(exp.Select) is not None:
                add_column_lineage(graph, target_id, target_name, query, self.dialect)


# ----------------------------------------------------------------- helpers


def table_name(table: "exp.Table") -> str:
    parts = [p.name for p in (table.args.get("catalog"), table.args.get("db")) if p]
    parts.append(table.name)
    return ".".join(parts)


def source_tables(stmt, exclude=frozenset()) -> List[str]:
    out, seen = [], set()
    for table in stmt.find_all(exp.Table):
        name = table_name(table)
        if name.lower() in exclude or not table.name:
            continue
        tid = "table:" + name.lower()
        if tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


def _final_select(query):
    """The SELECT whose projections are the statement's output (the last one in a CTE chain / union)."""
    if isinstance(query, exp.Select):
        return query
    if isinstance(query, exp.Union):
        return _final_select(query.this)
    if isinstance(query, exp.Subquery):
        return _final_select(query.this)
    inner = getattr(query, "expression", None)
    if inner is not None and inner.find(exp.Select) is not None:
        return _final_select(inner)
    return query.find(exp.Select)


def output_columns(query) -> List[str]:
    select = _final_select(query)
    if select is None:
        return []
    cols: List[str] = []
    for projection in select.expressions:
        if isinstance(projection, exp.Star):
            continue
        name = projection.alias_or_name
        if name and name != "*":
            cols.append(name)
    return cols


def qualify_with_schema(query, dialect: Optional[str] = None, schema=None):
    """Return (qualified_query, output_columns) — expanding ``SELECT *`` through CTEs and,
    when a ``schema`` mapping ({db: {schema: {table: {col: type}}}}) is given, through
    base tables. Falls back to the unqualified query when qualification fails."""
    _require_sqlglot()
    try:
        from sqlglot.optimizer.qualify import qualify
        from sqlglot.schema import MappingSchema

        ms = MappingSchema(schema, dialect=dialect) if isinstance(schema, dict) else schema
        qualified = qualify(query.copy(), schema=ms, dialect=dialect, expand_stars=True, validate_qualify_columns=False)
        cols = output_columns(qualified)
        if cols:
            return qualified, cols
    except Exception:
        pass
    return query, output_columns(query)


def column_lineage(query, dialect: Optional[str] = None, schema=None) -> Dict[str, List[Tuple[str, str]]]:
    """Map each output column of ``query`` to its source (table_name, column) leaves.

    Uses sqlglot's lineage engine; CTEs and aliases are resolved, and ``SELECT *``
    is expanded through CTEs and (with ``schema``) through base tables.
    """
    _require_sqlglot()
    result: Dict[str, List[Tuple[str, str]]] = {}
    qualified, cols = qualify_with_schema(query, dialect, schema)
    ms = None
    if isinstance(schema, dict):
        try:
            from sqlglot.schema import MappingSchema

            ms = MappingSchema(schema, dialect=dialect)
        except Exception:
            ms = None
    for col in cols:
        try:
            node = _sqlglot_lineage(col, qualified, schema=ms if ms is not None else schema, dialect=dialect)
        except Exception:
            continue
        sources: List[Tuple[str, str]] = []
        for leaf in node.walk():
            if leaf.downstream:
                continue
            src = leaf.source
            if isinstance(src, exp.Table) and src.name:
                src_col = leaf.name.split(".")[-1].strip('"')
                if src_col and src_col != "*":
                    pair = (table_name(src), src_col)
                    if pair not in sources:
                        sources.append(pair)
        if sources:
            result[col] = sources
    return result


def add_column_lineage(
    graph: ImpactGraph,
    target_id: str,
    target_name: str,
    query,
    dialect: Optional[str] = None,
    resolve_relation=None,
    schema=None,
) -> int:
    """Emit COLUMN nodes and column->column DEPENDS_ON edges for ``query``.

    ``resolve_relation(table_name_lower) -> (node_id, display_name)`` lets the
    dbt extractor map relation names in compiled SQL back to model/source
    nodes; by default relations map to ``table:<name>`` nodes. ``schema`` is a
    nested mapping of known columns used to expand ``SELECT *``.
    Returns the number of column edges added.
    """
    added = 0
    # Column ids are lower-cased: SQL identifiers are case-insensitive in most
    # warehouses and sqlglot normalizes them per dialect (Snowflake -> UPPER).
    _qualified, out_cols = qualify_with_schema(query, dialect, schema)
    for out_col in out_cols:
        col_id = f"column:{_bare(target_id)}.{out_col.lower()}"
        graph.add_node(Node(id=col_id, type=NodeType.COLUMN, name=out_col.lower(), meta={"parent": target_id}))
        graph.add_edge(Edge(src=target_id, dst=col_id, type=EdgeType.CONTAINS))
    for out_col, sources in column_lineage(query, dialect, schema).items():
        col_id = f"column:{_bare(target_id)}.{out_col.lower()}"
        for src_table, src_col in sources:
            if resolve_relation is not None:
                resolved = resolve_relation(src_table.lower())
                if resolved is None:
                    continue
                parent_id, _display = resolved
            else:
                parent_id = "table:" + src_table.lower()
            src_col_id = f"column:{_bare(parent_id)}.{src_col.lower()}"
            graph.add_node(Node(id=src_col_id, type=NodeType.COLUMN, name=src_col.lower(), meta={"parent": parent_id}))
            graph.add_edge(Edge(src=parent_id, dst=src_col_id, type=EdgeType.CONTAINS))
            graph.add_edge(Edge(src=col_id, dst=src_col_id, type=EdgeType.DEPENDS_ON, meta={"via": "sql"}))
            added += 1
    return added


def _bare(node_id: str) -> str:
    """'table:prod.analytics.x' -> 'prod.analytics.x'; 'dbt:customer' -> 'customer'."""
    return node_id.split(":", 1)[1] if ":" in node_id else node_id
