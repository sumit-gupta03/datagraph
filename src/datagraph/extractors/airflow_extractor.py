"""Airflow DAG extractor (deterministic, AST-based — no Airflow import needed).

Reads DAG files and emits:
  * ``dag:<dag_id>`` nodes (type DAG) contained by their file,
  * ``task:<dag_id>/<task_id>`` nodes (type TASK) contained by the DAG,
  * task DEPENDS_ON upstream task from ``a >> b``, ``a << b``, lists and ``chain()``,
  * task DEPENDS_ON the Python function it runs (``python_callable=fn``, inferred by name),
  * tables read/written by SQL in operator arguments (``sql=...``) — the
    orchestration↔data bridge — with ``via: sql-in-code``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from ..graph import EXTRACTED, INFERRED, Edge, EdgeType, ImpactGraph, Node, NodeType
from .base import Extractor
from .sql_in_code import looks_like_sql, sql_tables

_SKIP = {".git", ".venv", "venv", "__pycache__", "node_modules"}


class AirflowExtractor(Extractor):
    name = "airflow"

    def __init__(self, root: Union[str, Path]) -> None:
        self.root = Path(root).resolve()

    def extract(self) -> ImpactGraph:
        graph = ImpactGraph()
        files = [self.root] if self.root.is_file() else [
            p for p in self.root.rglob("*.py") if not any(s in p.parts for s in _SKIP)
        ]
        for path in files:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
            except SyntaxError:
                continue
            rel = path.relative_to(self.root).as_posix() if self.root.is_dir() else path.name
            self._extract_file(graph, tree, rel)
        return graph

    def _extract_file(self, graph: ImpactGraph, tree: ast.Module, rel: str) -> None:
        dag_ids = _find_dag_ids(tree)
        if not dag_ids:
            return
        file_id = f"file:{rel}"
        graph.add_node(Node(id=file_id, type=NodeType.FILE, name=rel, path=rel))
        dag_id = dag_ids[0]  # one DAG per file is the Airflow convention
        dag_node = f"dag:{dag_id}"
        graph.add_node(Node(id=dag_node, type=NodeType.DAG, name=dag_id, path=rel, meta={"source": "airflow"}))
        graph.add_edge(Edge(src=file_id, dst=dag_node, type=EdgeType.CONTAINS))

        var_to_task: Dict[str, str] = {}
        tasks: List[Tuple[str, ast.Call]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                task_id = _task_id_of(node.value)
                if task_id:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            var_to_task[target.id] = task_id
                    tasks.append((task_id, node.value))
            elif isinstance(node, ast.Call) and _task_id_of(node) and not _is_assigned(node, tree):
                tasks.append((_task_id_of(node), node))

        seen = set()
        for task_id, call in tasks:
            if task_id in seen:
                continue
            seen.add(task_id)
            tid = f"task:{dag_id}/{task_id}"
            op = _callee_name(call)
            graph.add_node(Node(id=tid, type=NodeType.TASK, name=task_id, path=rel,
                                meta={"dag": dag_id, "operator": op, "lineno": call.lineno, "end_lineno": getattr(call, "end_lineno", call.lineno)}))
            graph.add_edge(Edge(src=dag_node, dst=tid, type=EdgeType.CONTAINS))
            for kw in call.keywords:
                if kw.arg == "python_callable":
                    fn = _name_of(kw.value)
                    if fn:
                        graph.add_edge(Edge(src=tid, dst=f"func:{rel}::{fn}", type=EdgeType.DEPENDS_ON,
                                            meta={"provenance": INFERRED, "via": "python_callable"}))
                elif kw.arg in ("sql", "query", "bash_command", "command", "hql", "select", "statement"):
                    for s in _strings(kw.value):
                        if looks_like_sql(s):
                            reads, writes, certain = sql_tables(s)
                            prov = EXTRACTED if certain else INFERRED
                            for t in reads:
                                graph.add_edge(Edge(src=tid, dst=f"table:{t}", type=EdgeType.DEPENDS_ON, meta={"provenance": prov, "via": "sql-in-code"}))
                            for t in writes:
                                graph.add_edge(Edge(src=tid, dst=f"table:{t}", type=EdgeType.WRITES_TO, meta={"provenance": prov, "via": "sql-in-code"}))
                elif kw.arg in ("destination_table", "destination_dataset_table", "table", "table_name", "target_table"):
                    for s in _strings(kw.value):
                        graph.add_edge(Edge(src=tid, dst=f"table:{s.lower()}", type=EdgeType.WRITES_TO, meta={"via": "operator_arg"}))
                elif kw.arg in ("source_table", "source_project_dataset_table"):
                    for s in _strings(kw.value):
                        graph.add_edge(Edge(src=tid, dst=f"table:{s.lower()}", type=EdgeType.DEPENDS_ON, meta={"via": "operator_arg"}))

        # dependencies: a >> b  /  a << b  /  [a, b] >> c  /  chain(a, b, c)
        def resolve(expr) -> List[str]:
            if isinstance(expr, ast.Name):
                return [var_to_task[expr.id]] if expr.id in var_to_task else []
            if isinstance(expr, (ast.List, ast.Tuple)):
                out: List[str] = []
                for e in expr.elts:
                    out += resolve(e)
                return out
            if isinstance(expr, ast.BinOp):
                # the result of (a >> b) is b for chaining purposes; (a << b) is b as well in Airflow semantics? use rightmost/leftmost
                return resolve(expr.right) if isinstance(expr.op, ast.RShift) else resolve(expr.left)
            return []

        def add_dep(upstreams: List[str], downstreams: List[str]) -> None:
            for u in upstreams:
                for d in downstreams:
                    if u != d:
                        graph.add_edge(Edge(src=f"task:{dag_id}/{d}", dst=f"task:{dag_id}/{u}", type=EdgeType.DEPENDS_ON))

        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.RShift, ast.LShift)):
                left_chain = _flatten_chain(node)
                # left_chain is a list of expression groups in order for >> ; reversed for <<
                groups = [resolve(g) for g in left_chain]
                for a, b in zip(groups, groups[1:]):
                    add_dep(a, b)
            elif isinstance(node, ast.Call) and _callee_name(node) in ("chain", "chain_linear"):
                groups = [resolve(a) for a in node.args]
                for a, b in zip(groups, groups[1:]):
                    add_dep(a, b)


def _flatten_chain(node: ast.BinOp) -> List[ast.AST]:
    """a >> b >> c  ->  [a, b, c];   a << b << c  ->  [c, b, a]."""
    op = type(node.op)
    parts: List[ast.AST] = []

    def walk(n):
        if isinstance(n, ast.BinOp) and isinstance(n.op, op):
            walk(n.left)
            walk(n.right)
        else:
            parts.append(n)

    walk(node)
    return parts if op is ast.RShift else list(reversed(parts))


def _find_dag_ids(tree: ast.Module) -> List[str]:
    ids: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee_name(node) == "DAG":
            dag_id = None
            for kw in node.keywords:
                if kw.arg == "dag_id":
                    dag_id = _const(kw.value)
            if dag_id is None and node.args:
                dag_id = _const(node.args[0])
            if dag_id:
                ids.append(dag_id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:  # @dag(...) TaskFlow API
                if (isinstance(dec, ast.Call) and _callee_name(dec) == "dag") or (isinstance(dec, ast.Name) and dec.id == "dag"):
                    dag_id = None
                    if isinstance(dec, ast.Call):
                        for kw in dec.keywords:
                            if kw.arg == "dag_id":
                                dag_id = _const(kw.value)
                    ids.append(dag_id or node.name)
    return ids


def _task_id_of(call: ast.Call) -> Optional[str]:
    name = _callee_name(call) or ""
    if not (name.endswith("Operator") or name.endswith("Sensor") or name in ("task", "TaskGroup")):
        return None
    for kw in call.keywords:
        if kw.arg == "task_id":
            return _const(kw.value)
    return None


def _is_assigned(call: ast.Call, tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and node.value is call:
            return True
    return False


def _callee_name(call: ast.Call) -> Optional[str]:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _name_of(expr) -> Optional[str]:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


def _const(expr) -> Optional[str]:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    return None


def _strings(expr) -> List[str]:
    out: List[str] = []
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        out.append(expr.value)
    elif isinstance(expr, ast.JoinedStr):
        out.append("".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}" for v in expr.values))
    elif isinstance(expr, (ast.List, ast.Tuple)):
        for e in expr.elts:
            out += _strings(e)
    return out
