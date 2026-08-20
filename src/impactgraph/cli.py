"""impactgraph command-line interface.

Commands:
  impactgraph build   Build the unified graph from a repo / dbt manifest / SQL dir
  impactgraph impact  Show the blast radius of one or more nodes
  impactgraph diff    Show the blast radius of the current git diff
  impactgraph nodes   List / search graph nodes
  impactgraph explain AI explanation of an impact analysis (requires [ai] extra)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .analysis import analyze_impact
from .extractors import (
    DbtExtractor,
    PythonExtractor,
    changed_node_ids,
    collect_changes,
)
from .graph import ImpactGraph
from .report import render_analysis

DEFAULT_GRAPH = "impactgraph.json"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="impactgraph",
        description="AI-powered Change Impact Graph: if I change this, what can break?",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="Build the unified graph from artifacts")
    p_build.add_argument("--repo", help="Path to a Python code repo to scan")
    p_build.add_argument("--dbt-manifest", help="Path to a dbt manifest.json")
    p_build.add_argument("--sql", help="Directory of .sql files (requires [sql] extra)")
    p_build.add_argument("--sql-dialect", default=None, help="sqlglot dialect (e.g. snowflake)")
    p_build.add_argument("-o", "--output", default=DEFAULT_GRAPH, help="Output graph JSON path")

    p_impact = sub.add_parser("impact", help="Blast radius of one or more nodes")
    p_impact.add_argument("nodes", nargs="+", help="Node ids, names, or paths")
    p_impact.add_argument("--graph", default=DEFAULT_GRAPH)
    p_impact.add_argument("--max-depth", type=int, default=None)
    p_impact.add_argument("--json", action="store_true", help="Emit JSON instead of a report")

    p_diff = sub.add_parser("diff", help="Blast radius of the current git diff")
    p_diff.add_argument("--repo", default=".", help="Path to the git repo")
    p_diff.add_argument("--base", default="HEAD", help="Base ref (default HEAD)")
    p_diff.add_argument("--head", default=None, help="Optional head ref (base...head)")
    p_diff.add_argument("--graph", default=DEFAULT_GRAPH)
    p_diff.add_argument("--max-depth", type=int, default=None)
    p_diff.add_argument("--json", action="store_true")

    p_nodes = sub.add_parser("nodes", help="List or search graph nodes")
    p_nodes.add_argument("--graph", default=DEFAULT_GRAPH)
    p_nodes.add_argument("--search", default=None)
    p_nodes.add_argument("--type", dest="node_type", default=None)

    p_explain = sub.add_parser("explain", help="AI explanation of an impact analysis")
    p_explain.add_argument("nodes", nargs="+", help="Node ids, names, or paths")
    p_explain.add_argument("--graph", default=DEFAULT_GRAPH)
    p_explain.add_argument("--max-depth", type=int, default=None)
    p_explain.add_argument("--model", default="claude-opus-5")

    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "build":
        return _cmd_build(args)
    if args.command == "impact":
        return _cmd_impact(args)
    if args.command == "diff":
        return _cmd_diff(args)
    if args.command == "nodes":
        return _cmd_nodes(args)
    if args.command == "explain":
        return _cmd_explain(args)
    return 1


def _load_graph(path: str) -> ImpactGraph:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"graph file '{path}' not found — run 'impactgraph build' first"
        )
    return ImpactGraph.load(p)


def _cmd_build(args: argparse.Namespace) -> int:
    if not (args.repo or args.dbt_manifest or args.sql):
        print("error: provide at least one of --repo, --dbt-manifest, --sql", file=sys.stderr)
        return 2

    graph = ImpactGraph()
    if args.repo:
        fragment = PythonExtractor(args.repo).extract()
        print(f"python: {len(fragment)} nodes from {args.repo}")
        graph.merge(fragment)
    if args.dbt_manifest:
        fragment = DbtExtractor(args.dbt_manifest).extract()
        print(f"dbt: {len(fragment)} nodes from {args.dbt_manifest}")
        graph.merge(fragment)
    if args.sql:
        from .extractors.sql_extractor import SqlExtractor

        fragment = SqlExtractor(args.sql, dialect=args.sql_dialect).extract()
        print(f"sql: {len(fragment)} nodes from {args.sql}")
        graph.merge(fragment)

    graph.save(args.output)
    print(f"unified graph: {len(graph)} nodes, {len(graph.edges())} edges -> {args.output}")
    return 0


def _cmd_impact(args: argparse.Namespace) -> int:
    graph = _load_graph(args.graph)
    unresolved = [ref for ref in args.nodes if graph.resolve(ref) is None]
    for ref in unresolved:
        matches = graph.find(ref)
        if matches:
            print(f"'{ref}' is ambiguous; candidates:", file=sys.stderr)
            for m in matches[:10]:
                print(f"  {m.id}", file=sys.stderr)
        else:
            print(f"'{ref}' not found in graph", file=sys.stderr)
    analysis = analyze_impact(graph, args.nodes, max_depth=args.max_depth)
    if not analysis.changed:
        return 2
    if args.json:
        print(json.dumps(analysis.to_dict(), indent=2, sort_keys=True))
    else:
        render_analysis(graph, analysis)
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    graph = _load_graph(args.graph)
    changes = collect_changes(args.repo, base=args.base, head=args.head)
    if not changes.files:
        print("no changes detected")
        return 0
    changed_ids = changed_node_ids(graph, changes)
    if not changed_ids:
        print("changed files do not map to any graph nodes:")
        for f in changes.files:
            print(f"  {f}")
        return 0
    analysis = analyze_impact(graph, changed_ids, max_depth=args.max_depth)
    if args.json:
        print(json.dumps(analysis.to_dict(), indent=2, sort_keys=True))
    else:
        render_analysis(graph, analysis)
    return 0


def _cmd_nodes(args: argparse.Namespace) -> int:
    graph = _load_graph(args.graph)
    nodes = graph.find(args.search) if args.search else graph.nodes()
    if args.node_type:
        nodes = [n for n in nodes if n.type.value == args.node_type]
    for node in sorted(nodes, key=lambda n: n.id):
        print(f"{node.id}\t{node.type.value}\t{node.name}")
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    from .ai import explain_impact

    graph = _load_graph(args.graph)
    analysis = analyze_impact(graph, args.nodes, max_depth=args.max_depth)
    if not analysis.changed:
        print("no matching nodes found", file=sys.stderr)
        return 2
    render_analysis(graph, analysis)
    print("\n--- AI explanation ---\n")
    print(explain_impact(analysis, model=args.model))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
