"""Warehouse / database metadata extractor.

Give it a connection (or a DSN string) and it reads the schema and turns it
into graph facts:

  * tables and views (``table:<db>.<schema>.<name>``) and their columns with
    data types (``column:<db>.<schema>.<name>.<col>``),
  * **foreign keys** → ``column:child.col DEPENDS_ON column:parent.col`` and
    ``table:child DEPENDS_ON table:parent`` (meta ``via: foreign_key``) — the
    table/column relationships data analysts want to see,
  * **view lineage** (table- and column-level) from view definitions via sqlglot.

Backends:
  * any DB-API connection whose database exposes ``information_schema``
    (Postgres, Redshift, Snowflake, BigQuery, DuckDB, MySQL, SQL Server ...),
  * **SQLite** files (``sqlite_master`` + PRAGMAs) — zero setup, great for trying it,
  * ``connect(dsn)`` turns ``"mydata.db"``, ``"sqlite:///…"``, ``"duckdb://…"`` or any
    SQLAlchemy URL (if SQLAlchemy is installed) into a connection.

Pair it with ``diff_graphs`` to detect schema drift between two snapshots, and
with ``datagraph html --all`` / ``datagraph relationships`` to display it.
"""

from __future__ import annotations

import sqlite3
from typing import List, Optional, Sequence

from ..security import escape_literal

#: catalogs / schemas owned by the engine itself, excluded unless asked for explicitly
SYSTEM_CATALOGS = ("system", "temp")
SYSTEM_SCHEMAS = (
    "information_schema", "pg_catalog", "pg_toast", "sys", "sysibm", "syscat",
    "mysql", "performance_schema", "innodb", "temp", "pg_temp_1",
)
from ..graph import Edge, EdgeType, ImpactGraph, Node, NodeType
from .base import Extractor


def connect(dsn: str):
    """Turn a DSN / file path into a DB-API connection."""
    dsn = str(dsn)
    lower = dsn.lower()
    if lower.startswith("sqlite:///"):
        return sqlite3.connect(dsn[len("sqlite:///"):])
    if lower.endswith((".db", ".sqlite", ".sqlite3")) or lower == ":memory:":
        return sqlite3.connect(dsn)
    if lower.startswith("duckdb://") or lower.endswith(".duckdb"):
        try:
            import duckdb  # type: ignore
        except ImportError as e:
            raise ImportError("pip install duckdb to connect to DuckDB files") from e
        path = dsn[len("duckdb://"):] if lower.startswith("duckdb://") else dsn
        return duckdb.connect(path)
    try:
        from sqlalchemy import create_engine  # type: ignore
    except ImportError as e:
        raise ImportError(
            f"Cannot open '{dsn}': install SQLAlchemy plus the driver for your database "
            "(e.g. pip install sqlalchemy psycopg2-binary / snowflake-sqlalchemy), "
            "or pass a DB-API connection object to WarehouseExtractor directly."
        ) from e
    return create_engine(dsn).raw_connection()


class WarehouseExtractor(Extractor):
    name = "warehouse"

    def __init__(
        self,
        connection,
        database: Optional[str] = None,
        schemas: Optional[Sequence[str]] = None,
        dialect: Optional[str] = None,
        view_lineage: bool = True,
        foreign_keys: bool = True,
        info_schema: str = "information_schema",
    ) -> None:
        self.connection = connect(connection) if isinstance(connection, str) else connection
        self.database = database
        self.schemas = [s for s in (schemas or [])]
        self.dialect = dialect
        self.view_lineage = view_lineage
        self.foreign_keys = foreign_keys
        self.info_schema = info_schema

    # -------------------------------------------------------------- helpers
    def _where(self) -> str:
        clauses = []
        if self.database:
            clauses.append(f"lower(table_catalog) = {escape_literal(self.database.lower())}")
        else:
            # DuckDB keeps its own objects in the `system` / `temp` catalogs
            quoted = ", ".join(escape_literal(c) for c in SYSTEM_CATALOGS)
            clauses.append(f"(table_catalog IS NULL OR lower(table_catalog) NOT IN ({quoted}))")
        if self.schemas:
            quoted = ", ".join(escape_literal(s.lower()) for s in self.schemas)
            clauses.append(f"lower(table_schema) IN ({quoted})")
        else:
            # engine-owned schemas: MySQL (mysql, sys, performance_schema), Postgres (pg_*),
            # SQL Server (sys), DB2 (sysibm) - never part of a user's data model
            quoted = ", ".join(escape_literal(s) for s in SYSTEM_SCHEMAS)
            clauses.append(f"lower(table_schema) NOT IN ({quoted})")
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

    def _is_sqlite(self) -> bool:
        return isinstance(self.connection, sqlite3.Connection)

    # -------------------------------------------------------------- extract
    def extract(self) -> ImpactGraph:
        if self._is_sqlite():
            return self._extract_sqlite()
        graph = ImpactGraph()
        where = self._where()
        tables = self._query(f"SELECT table_catalog, table_schema, table_name, table_type FROM {self.info_schema}.tables{where}")
        for catalog, schema, name, ttype in tables:
            tid = _table_id(catalog, schema, name)
            graph.add_node(Node(
                id=tid, type=NodeType.VIEW if str(ttype or "").upper() == "VIEW" else NodeType.TABLE,
                name=tid.split(":", 1)[1], meta={"source": "warehouse", "table_type": ttype},
            ))
        columns = self._query(
            f"SELECT table_catalog, table_schema, table_name, column_name, data_type, ordinal_position "
            f"FROM {self.info_schema}.columns{where} ORDER BY table_catalog, table_schema, table_name, ordinal_position"
        )
        for catalog, schema, name, col, dtype, _pos in columns:
            tid = _table_id(catalog, schema, name)
            _add_column(graph, tid, col, dtype)
        if self.foreign_keys:
            self._add_foreign_keys(graph, where)
        if self.view_lineage:
            schema: dict = {}
            for catalog, sch, name, col, dtype, _pos in columns:
                schema.setdefault(str(catalog or ""), {}).setdefault(str(sch), {}).setdefault(str(name), {})[str(col)] = dtype or "UNKNOWN"
                if catalog:
                    schema.setdefault("", {}).setdefault(str(sch), {}).setdefault(str(name), {})[str(col)] = dtype or "UNKNOWN"
            self._add_view_lineage(graph, where, schema)
        return graph

    def _add_foreign_keys(self, graph: ImpactGraph, where: str) -> None:
        sql = (
            "SELECT kcu.table_catalog, kcu.table_schema, kcu.table_name, kcu.column_name, "
            "pk.table_catalog, pk.table_schema, pk.table_name, pk.column_name, rc.constraint_name "
            f"FROM {self.info_schema}.referential_constraints rc "
            f"JOIN {self.info_schema}.key_column_usage kcu "
            "  ON kcu.constraint_name = rc.constraint_name AND kcu.constraint_schema = rc.constraint_schema "
            f"JOIN {self.info_schema}.key_column_usage pk "
            "  ON pk.constraint_name = rc.unique_constraint_name AND pk.constraint_schema = rc.unique_constraint_schema "
            "  AND pk.ordinal_position = kcu.ordinal_position"
        )
        try:
            rows = self._query(sql)
        except Exception:
            return  # engine without FK metadata (e.g. Snowflake, BigQuery)
        for c_cat, c_sch, c_tab, c_col, p_cat, p_sch, p_tab, p_col, cname in rows:
            _add_fk(graph, _table_id(c_cat, c_sch, c_tab), c_col, _table_id(p_cat, p_sch, p_tab), p_col, cname)

    def _add_view_lineage(self, graph: ImpactGraph, where: str, schema_map: Optional[dict] = None) -> None:
        try:
            views = self._query(f"SELECT table_catalog, table_schema, table_name, view_definition FROM {self.info_schema}.views{where}")
        except Exception:
            return
        for catalog, schema, name, definition in views:
            _view_lineage(graph, _table_id(catalog, schema, name), definition, self.dialect, schema_map)

    # --------------------------------------------------------------- sqlite
    def _extract_sqlite(self) -> ImpactGraph:
        graph = ImpactGraph()
        rows = self._query("SELECT type, name, sql FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'")
        for ttype, name, sql in rows:
            tid = "table:" + str(name).lower()
            graph.add_node(Node(id=tid, type=NodeType.VIEW if ttype == "view" else NodeType.TABLE, name=str(name).lower(),
                                meta={"source": "sqlite", "table_type": ttype.upper()}))
            for _cid, col, dtype, _notnull, _dflt, pk in self._query(f'PRAGMA table_info("{name}")'):
                _add_column(graph, tid, col, dtype, primary_key=bool(pk))
        if self.foreign_keys:
            for ttype, name, _sql in rows:
                if ttype != "table":
                    continue
                for _id, _seq, ref_table, from_col, to_col, *_rest in self._query(f'PRAGMA foreign_key_list("{name}")'):
                    # SQLite may omit the referenced column (defaults to the parent's primary key)
                    if not to_col:
                        pk_cols = [r[1] for r in self._query(f'PRAGMA table_info("{ref_table}")') if r[5]]
                        to_col = pk_cols[0] if pk_cols else "id"
                    _add_fk(graph, "table:" + str(name).lower(), from_col, "table:" + str(ref_table).lower(), to_col, f"fk_{name}_{from_col}")
        if self.view_lineage:
            schema_map: dict = {}
            for ttype, name, _sql in rows:
                cols = {str(r[1]): (r[2] or "UNKNOWN") for r in self._query(f'PRAGMA table_info("{name}")')}
                if cols:
                    schema_map[str(name)] = cols
            for ttype, name, sql in rows:
                if ttype == "view" and sql:
                    _view_lineage(graph, "table:" + str(name).lower(), sql, self.dialect or "sqlite", schema_map)
        return graph


# ----------------------------------------------------------------- helpers


#: MySQL puts the literal string 'def' in table_catalog for every row - it is not a real catalog
_PLACEHOLDER_CATALOGS = {"def", "", "none", "null"}


def _table_id(catalog, schema, name) -> str:
    if catalog is not None and str(catalog).strip().lower() in _PLACEHOLDER_CATALOGS:
        catalog = None
    parts = [str(p).lower() for p in (catalog, schema, name) if p]
    return "table:" + ".".join(parts)


def _add_column(graph: ImpactGraph, tid: str, col, dtype, primary_key: bool = False) -> str:
    if tid not in graph:
        graph.add_node(Node(id=tid, type=NodeType.TABLE, name=tid.split(":", 1)[1], meta={"source": "warehouse"}))
    col_id = f"column:{tid.split(':', 1)[1]}.{str(col).lower()}"
    meta = {"parent": tid, "data_type": dtype}
    if primary_key:
        meta["primary_key"] = True
    graph.add_node(Node(id=col_id, type=NodeType.COLUMN, name=str(col).lower(), meta=meta))
    graph.add_edge(Edge(src=tid, dst=col_id, type=EdgeType.CONTAINS))
    return col_id


def _add_fk(graph: ImpactGraph, child_tid: str, child_col, parent_tid: str, parent_col, constraint) -> None:
    c = _add_column(graph, child_tid, child_col, None)
    p = _add_column(graph, parent_tid, parent_col, None)
    graph.add_edge(Edge(src=c, dst=p, type=EdgeType.DEPENDS_ON, meta={"via": "foreign_key", "constraint": constraint}))
    if child_tid != parent_tid:
        graph.add_edge(Edge(src=child_tid, dst=parent_tid, type=EdgeType.DEPENDS_ON, meta={"via": "foreign_key", "constraint": constraint}))


def _view_lineage(graph: ImpactGraph, vid: str, definition, dialect, schema_map: Optional[dict] = None) -> None:
    if not definition:
        return
    try:
        import sqlglot
        from .sql_extractor import add_column_lineage, source_tables
    except ImportError:
        return
    try:
        query = sqlglot.parse_one(str(definition), read=dialect)
    except Exception:
        return
    if query is None:
        return
    select_like = query.expression if isinstance(query, sqlglot.exp.Create) and query.expression is not None else query
    for src in source_tables(select_like, exclude={vid.split(":", 1)[1]}):
        graph.add_edge(Edge(src=vid, dst=src, type=EdgeType.DEPENDS_ON, meta={"via": "view_definition"}))
    add_column_lineage(graph, vid, vid.split(":", 1)[1], select_like, dialect, schema=schema_map or None)
