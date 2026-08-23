"""Deterministic Python AST extractor.

Emits: file nodes, function/class nodes, CONTAINS edges, IMPORTS edges
(file -> file, resolved within the project), and best-effort CALLS edges
(function -> function, resolved by name within the project).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from ..graph import EXTRACTED, INFERRED, Edge, EdgeType, ImpactGraph, Node, NodeType
from .base import Extractor
from .sql_in_code import looks_like_sql, sql_tables

_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".tox", "dist", "build"}


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


class PythonExtractor(Extractor):
    name = "python"

    def __init__(self, root: Union[str, Path]) -> None:
        self.root = Path(root).resolve()

    def extract(self) -> ImpactGraph:
        graph = ImpactGraph()
        py_files = [
            p
            for p in self.root.rglob("*.py")
            if not any(part in _SKIP_DIRS for part in p.parts)
        ]

        # First pass: parse everything, index modules and function definitions.
        parsed: Dict[str, ast.Module] = {}
        module_index: Dict[str, str] = {}  # dotted module name -> file id
        func_index: Dict[str, List[str]] = {}  # bare function name -> [func node ids]

        for path in py_files:
            rel = _rel(path, self.root)
            try:
                tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
            except SyntaxError:
                continue
            parsed[rel] = tree
            file_id = f"file:{rel}"
            graph.add_node(Node(id=file_id, type=NodeType.FILE, name=rel, path=rel))
            module_index[_module_name(rel)] = file_id

            for qualname, node, kind in _walk_defs(tree):
                if kind == "class":
                    nid = f"class:{rel}::{qualname}"
                    ntype = NodeType.CLASS
                else:
                    nid = f"func:{rel}::{qualname}"
                    ntype = NodeType.FUNCTION
                graph.add_node(
                    Node(
                        id=nid,
                        type=ntype,
                        name=qualname,
                        path=rel,
                        meta={"lineno": node.lineno, "end_lineno": getattr(node, "end_lineno", node.lineno)},
                    )
                )
                graph.add_edge(Edge(src=file_id, dst=nid, type=EdgeType.CONTAINS))
                if ntype == NodeType.FUNCTION:
                    func_index.setdefault(qualname.split(".")[-1], []).append(nid)

        # Second pass: imports and calls.
        for rel, tree in parsed.items():
            file_id = f"file:{rel}"
            for target_module in _imported_modules(tree):
                target_file = _resolve_module(target_module, module_index)
                if target_file and target_file != file_id:
                    graph.add_edge(
                        Edge(src=file_id, dst=target_file, type=EdgeType.IMPORTS)
                    )

            # SQL embedded in code -> automatic code<->data bridge edges
            for owner_qualname, sql_text in _sql_strings(tree):
                owner_id = f"func:{rel}::{owner_qualname}" if owner_qualname else file_id
                if graph.get_node(owner_id) is None:
                    continue
                reads, writes, certain = sql_tables(sql_text)
                prov = EXTRACTED if certain else INFERRED
                for t in reads:
                    graph.add_edge(Edge(src=owner_id, dst=f"table:{t}", type=EdgeType.DEPENDS_ON,
                                        meta={"provenance": prov, "via": "sql-in-code"}))
                for t in writes:
                    graph.add_edge(Edge(src=owner_id, dst=f"table:{t}", type=EdgeType.WRITES_TO,
                                        meta={"provenance": prov, "via": "sql-in-code"}))

            for caller_qualname, callee_name in _calls(tree):
                caller_id = f"func:{rel}::{caller_qualname}"
                if graph.get_node(caller_id) is None:
                    continue
                for callee_id in func_index.get(callee_name, []):
                    if callee_id != caller_id:
                        # name-based resolution: mark as inferred so it can be excluded
                        graph.add_edge(
                            Edge(
                                src=caller_id,
                                dst=callee_id,
                                type=EdgeType.CALLS,
                                meta={"provenance": INFERRED, "reason": "resolved by function name"},
                            )
                        )
        return graph


def _module_name(rel_path: str) -> str:
    mod = rel_path[:-3] if rel_path.endswith(".py") else rel_path
    if mod.endswith("/__init__"):
        mod = mod[: -len("/__init__")]
    return mod.replace("/", ".")


def _resolve_module(module: str, module_index: Dict[str, str]) -> Optional[str]:
    """Resolve a dotted import to a project file id, trying progressively shorter prefixes."""
    parts = module.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in module_index:
            return module_index[candidate]
        # also try matching by suffix (src layouts: 'pkg.mod' defined as 'src.pkg.mod')
        for known, fid in module_index.items():
            if known.endswith("." + candidate) or known == candidate:
                return fid
    return None


def _walk_defs(tree: ast.Module):
    """Yield (qualname, node, kind) for all function and class definitions."""

    def visit(node: ast.AST, prefix: str):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{child.name}"
                yield qual, child, "function"
                yield from visit(child, f"{qual}.")
            elif isinstance(child, ast.ClassDef):
                qual = f"{prefix}{child.name}"
                yield qual, child, "class"
                yield from visit(child, f"{qual}.")

    yield from visit(tree, "")


def _imported_modules(tree: ast.Module) -> List[str]:
    out: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
    return out


def _sql_strings(tree: ast.Module) -> List[Tuple[str, str]]:
    """Yield (owner_qualname or "" for module level, sql_text) for SQL-looking strings.

    f-strings are flattened with their expressions replaced by ``{}`` so the
    table names that ARE literal can still be found (and marked inferred)."""
    results: List[Tuple[str, str]] = []

    def text_of(node) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                else:
                    parts.append("{}")
            return "".join(parts)
        return ""

    def visit(node: ast.AST, prefix: str, owner: str):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{child.name}"
                visit(child, f"{qual}.", qual)
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.", owner)
            elif isinstance(child, (ast.Constant, ast.JoinedStr)):
                s = text_of(child)
                if looks_like_sql(s):
                    results.append((owner, s))
            else:
                visit(child, prefix, owner)

    visit(tree, "", "")
    return results


def _calls(tree: ast.Module) -> List[Tuple[str, str]]:
    """Yield (caller_qualname, callee_bare_name) pairs."""
    results: List[Tuple[str, str]] = []

    def callee_name(call: ast.Call) -> Optional[str]:
        f = call.func
        if isinstance(f, ast.Name):
            return f.id
        if isinstance(f, ast.Attribute):
            return f.attr
        return None

    def visit(node: ast.AST, prefix: str):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{child.name}"
                for sub in ast.walk(child):
                    if isinstance(sub, ast.Call):
                        name = callee_name(sub)
                        if name:
                            results.append((qual, name))
                visit(child, f"{qual}.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return results
