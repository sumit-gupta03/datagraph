"""Warehouse metadata extractor (Snowflake / Postgres / Redshift / BigQuery ...).

Reads ``information_schema`` through any DB-API connection you hand it — no
warehouse-specific driver is bundled — and emits real tables, views, columns
(with data types) and, when view definitions are available and sqlglot is
installed, table- and column-level lineage for views.

Example::

    import snowflake.connector
    conn = snowflake.connector.connect(...)
    graph.merge(WarehouseExtractor(conn, database="PROD", schemas=["ANALYTICS"], dialect="snowflake").extract())

Pair it with ``diff_graphs`` to detect schema drift between two snapshots.
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence

from ..graph import Edge, EdgeType, ImpactGraph, Node, NodeType
from .base import Extractor


class WarehouseExtractor(Extractor):
    name = "warehouse"

    def __init__(
        self,
        connection,
        database: Optional[str] = None,
        schemas: Optional[Sequence[str]] = None,
        dialect: Optional[str] = None,
        view_lineage: bool = True,
        info_schema: str = "information_schema",
    ) -> None:
        self.connection = connection
        self.database = database
        self.schemas = [s for s in (schemas or [])]
        self.dialect = dialect
        self.view_lineage = view_lineage
        self.info_schema = info_schema

    # -------------------------------------------------------------- queries
    def _where(self) -> str:
        clauses = []
        if self.database:
            clauses.append(f"lower(table_catalog) = '{self.database.lower()}'")
        if self.schemas:
            quoted = ", ".join(f"'{s.lower()}'" for s in self.schemas)
            clauses.append(f"lower(table_schema) IN ({quoted})")
        else:
            clauses.append("lower(table_schema) NOT IN ('information_schema', 'pg_catalog')")
        return (" WHERE " + " AND ".join(clauses)) if clauses else ""

    def _query(self, sql: str) -> List[tuple]:
        cur = self.connection.cursor()
        try:
            cur.execute(sql)
            return [tuple(r) for r in cur.fetchall()]
        finally:
            close = getattr(cur, "close", None)
            if close:
                close()

    # -------------------------------------------------------------- extract
    def extract(self) -> ImpactGraph:
        graph = ImpactGraph()
        where = self._where()

        tables = self._query(
            f"SELECT table_catalog, table_schema, table_name, table_type FROM {self.info_schema}.tables{where}"
        )
        for catalog, schema, name, ttype in tables:
            tid = _table_id(catalog, schema, name)
            graph.add_node(
                Node(
                    id=tid,
                    type=NodeType.VIEW if str(ttype or "").upper() == "VIEW" else NodeType.TABLE,
                    name=tid.split(":", 1)[1],
                    meta={"source": "warehouse", "table_type": ttype},
                )
            )

        columns = self._query(
            f"SELECT table_catalog, table_schema, table_name, column_name, data_type, ordinal_position "
            f"FROM {self.info_schema}.columns{where} ORDER BY table_catalog, table_schema, table_name, ordinal_position"
        )
        for catalog, schema, name, col, dtype, _pos in columns:
            tid = _table_id(catalog, schema, name)
            if tid not in graph:
                graph.add_node(Node(id=tid, type=NodeType.TABLE, name=tid.split(":", 1)[1], meta={"source": "warehouse"}))
            col_id = f"column:{tid.split(':', 1)[1]}.{str(col).lower()}"
            graph.add_node(Node(id=col_id, type=NodeType.COLUMN, name=str(col).lower(), meta={"parent": tid, "data_type": dtype}))
            graph.add_edge(Edge(src=tid, dst=col_id, type=EdgeType.CONTAINS))

        if self.view_lineage:
            self._add_view_lineage(graph, where)
        return graph

    def _add_view_lineage(self, graph: ImpactGraph, where: str) -> None:
        try:
            import sqlglot
            from .sql_extractor import add_column_lineage, source_tables, table_name
        except ImportError:
            return
        try:
            views = self._query(
                f"SELECT table_catalog, table_schema, table_name, view_definition FROM {self.info_schema}.views{where}"
            )
        except Exception:
            return
        for catalog, schema, name, definition in views:
            if not definition:
                continue
            vid = _table_id(catalog, schema, name)
            try:
                query = sqlglot.parse_one(str(definition), read=self.dialect)
            except Exception:
                continue
            if query is None:
                continue
            # some engines return "CREATE VIEW ... AS SELECT ..."; others the bare SELECT
            select_like = query.expression if isinstance(query, sqlglot.exp.Create) and query.expression is not None else query
            for src in source_tables(select_like, exclude={vid.split(":", 1)[1]}):
                graph.add_edge(Edge(src=vid, dst=src, type=EdgeType.DEPENDS_ON, meta={"via": "view_definition"}))
            add_column_lineage(graph, vid, vid.split(":", 1)[1], select_like, self.dialect)


def _table_id(catalog, schema, name) -> str:
    parts = [str(p).lower() for p in (catalog, schema, name) if p]
    return "table:" + ".".join(parts)
