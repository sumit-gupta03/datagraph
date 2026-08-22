"""impactgraph command-line interface.

  build       Build the unified graph from artifacts (--update skips when inputs unchanged)
  impact      Blast radius of one or more nodes (--html for an interactive view)
  diff        Blast radius of the current git diff
  nodes       List / search graph nodes
  paths       Propagation paths from a changed node to a target
  hotspots    Nodes with the largest blast radius
  graph-diff  Schema / dependency drift between two graph snapshots
  export      GraphML / DOT / Cypher / JSON export
  html        Interactive HTML view of a blast radius
  watch       Rebuild the graph whenever inputs change
  hook-install  Install a git post-commit hook that rebuilds the graph
  explain     AI explanation of an impact analysis (requires [ai] extra)
  mcp         Serve the graph as an MCP server for AI coding assistants ([mcp] extra)
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import List, Optional

from .analysis import analyze_impact
from .extractors import (
    DbtExtractor,
    LineageFileExtractor,
    OpenLineageExtractor,
    PythonExtractor,
    changed_node_ids,
    collect_changes,
)
from .graph import ImpactGraph, diff_graphs
from .report import render_analysis

DEFAULT_GRAPH = "impactgraph.json"


def _add_build_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", help="Path to a Python code repo to scan")
    p.add_argument("--dbt-manifest", help="Path to a dbt manifest.json")
    p.add_argument("--sql", help="Directory of .sql files (requires [sql] extra)")
    p.add_argument("--sql-dialect", default=None, help="sqlglot dialect (e.g. snowflake)")
    p.add_argument("--openlineage", help="OpenLineage events file (JSON array or NDJSON)")
    p.add_argument("--lineage-file", help="DataHub-style lineage file (YAML/JSON)")
    p.add_argument("--no-column-lineage", action="store_true", help="Skip column-level SQL lineage")
    p.add_argument("-o", "--output", default=DEFAULT_GRAPH, help="Output graph JSON path")


def _add_analysis_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--graph", default=DEFAULT_GRAPH)
    p.add_argument("--max-depth", type=int, default=None)
    p.add_argument("--json", action="store_true", help="Emit JSON instead of a report")
    p.add_argument("--no-inferred", action="store_true", help="Exclude heuristic (inferred) edges")
    p.add_argument("--html", metavar="OUT", help="Also write an interactive HTML view")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="impactgraph",
        description="AI-powered Change Impact Graph: if I change this, what can break?",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="Build the unified graph from artifacts")
    _add_build_args(p_build)
    p_build.add_argument("--update", action="store_true", help="Skip the build if inputs are unchanged")

    p_impact = sub.add_parser("impact", help="Blast radius of one or more nodes")
    p_impact.add_argument("nodes", nargs="+", help="Node ids, names, or paths")
    _add_analysis_args(p_impact)

    p_diff = sub.add_parser("diff", help="Blast radius of the current git diff")
    p_diff.add_argument("--repo", default=".", help="Path to the git repo")
    p_diff.add_argument("--base", default="HEAD", help="Base ref (default HEAD)")
    p_diff.add_argument("--head", default=None, help="Optional head ref (base...head)")
    _add_analysis_args(p_diff)

    p_nodes = sub.add_parser("nodes", help="List or search graph nodes")
    p_nodes.add_argument("--graph", default=DEFAULT_GRAPH)
    p_nodes.add_argument("--search", default=None)
    p_nodes.add_argument("--type", dest="node_type", default=None)

    p_paths = sub.add_parser("paths", help="Propagation paths from a changed node to a target")
    p_paths.add_argument("changed")
    p_paths.add_argument("target")
    p_paths.add_argument("--graph", default=DEFAULT_GRAPH)
    p_paths.add_argument("--json", action="store_true")

    p_hot = sub.add_parser("hotspots", help="Nodes with the largest blast radius")
    p_hot.add_argument("--graph", default=DEFAULT_GRAPH)
    p_hot.add_argument("--top", type=int, default=10)
    p_hot.add_argument("--no-inferred", action="store_true")
    p_hot.add_argument("--json", action="store_true")

    p_gd = sub.add_parser("graph-diff", help="Drift between two graph snapshots")
    p_gd.add_argument("old")
    p_gd.add_argument("new")
    p_gd.add_argument("--json", action="store_true")

    p_exp = sub.add_parser("export", help="Export the graph")
    p_exp.add_argument("--graph", default=DEFAULT_GRAPH)
    p_exp.add_argument("--format", choices=["graphml", "dot", "cypher", "json"], default="graphml")
    p_exp.add_argument("-o", "--output", required=True)

    p_html = sub.add_parser("html", help="Interactive HTML view of a blast radius")
    p_html.add_argument("nodes", nargs="+")
    p_html.add_argument("--graph", default=DEFAULT_GRAPH)
    p_html.add_argument("--max-depth", type=int, default=None)
    p_html.add_argument("--no-inferred", action="store_true")
    p_html.add_argument("-o", "--output", default="impact.html")

    p_watch = sub.add_parser("watch", help="Rebuild the graph whenever inputs change")
    _add_build_args(p_watch)
    p_watch.add_argument("--interval", type=float, default=2.0)

    p_hook_install = sub.add_parser("hook-install", help="Install a git post-commit hook that rebuilds the graph")
    p_hook_install.add_argument("--git-repo", default=".", help="Git repository to install the hook into")
    p_hook_install.add_argument("--command", dest="hook_cmd", default=None, help="Command to run (default: impactgraph build ... --update)")
    _add_build_args(p_hook_install)

    p_explain = sub.add_parser("explain", help="AI explanation of an impact analysis")
    p_explain.add_argument("nodes", nargs="+", help="Node ids, names, or paths")
    p_explain.add_argument("--graph", default=DEFAULT_GRAPH)
    p_explain.add_argument("--max-depth", type=int, default=None)
    p_explain.add_argument("--model", default="claude-opus-5")

    p_mcp = sub.add_parser("mcp", help="Serve the graph over MCP (stdio)")
    p_mcp.add_argument("--graph", default=DEFAULT_GRAPH)

    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except ImportError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    return {
        "build": _cmd_build,
        "impact": _cmd_impact,
        "diff": _cmd_diff,
        "nodes": _cmd_nodes,
        "paths": _cmd_paths,
        "hotspots": _cmd_hotspots,
        "graph-diff": _cmd_graph_diff,
        "export": _cmd_export,
        "html": _cmd_html,
        "watch": _cmd_watch,
        "hook-install": _cmd_hook_install,
        "explain": _cmd_explain,
        "mcp": _cmd_mcp,
    }[args.command](args)


# ------------------------------------------------------------------ helpers


def _load_graph(path: str) -> ImpactGraph:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"graph file '{path}' not found — run 'impactgraph build' first")
    return ImpactGraph.load(p)


def _build_inputs(args: argparse.Namespace) -> List[Optional[str]]:
    return [args.repo, args.dbt_manifest, args.sql, args.openlineage, args.lineage_file]


def _build_graph(args: argparse.Namespace, log=print) -> ImpactGraph:
    graph = ImpactGraph()
    if args.repo:
        fragment = PythonExtractor(args.repo).extract()
        log(f"python: {len(fragment)} nodes from {args.repo}")
        graph.merge(fragment)
    if args.dbt_manifest:
        fragment = DbtExtractor(
            args.dbt_manifest, column_lineage=not args.no_column_lineage, dialect=args.sql_dialect
        ).extract()
        log(f"dbt: {len(fragment)} nodes from {args.dbt_manifest}")
        graph.merge(fragment)
    if args.sql:
        from .extractors.sql_extractor import SqlExtractor

        fragment = SqlExtractor(args.sql, dialect=args.sql_dialect).extract()
        log(f"sql: {len(fragment)} nodes from {args.sql}")
        graph.merge(fragment)
    if args.openlineage:
        fragment = OpenLineageExtractor(args.openlineage).extract()
        log(f"openlineage: {len(fragment)} nodes from {args.openlineage}")
        graph.merge(fragment)
    if args.lineage_file:
        fragment = LineageFileExtractor(args.lineage_file).extract()
        log(f"lineage-file: {len(fragment)} nodes from {args.lineage_file}")
        graph.merge(fragment)
    return graph


def _require_inputs(args: argparse.Namespace) -> bool:
    if not any(_build_inputs(args)):
        print(
            "error: provide at least one of --repo, --dbt-manifest, --sql, --openlineage, --lineage-file",
            file=sys.stderr,
        )
        return False
    return True


def _emit(graph, analysis, args) -> None:
    if getattr(args, "html", None):
        from .html_report import render_html

        Path(args.html).write_text(render_html(graph, analysis), encoding="utf-8")
        print(f"html view -> {args.html}", file=sys.stderr)
    if args.json:
        print(json.dumps(analysis.to_dict(), indent=2, sort_keys=True))
    else:
        render_analysis(graph, analysis)


# ----------------------------------------------------------------- commands


def _cmd_build(args: argparse.Namespace) -> int:
    from . import maintenance

    if not _require_inputs(args):
        return 2
    inputs = _build_inputs(args)
    if args.update and maintenance.is_up_to_date(args.output, inputs):
        print(f"{args.output} is up to date (inputs unchanged)")
        return 0
    graph = _build_graph(args)
    graph.save(args.output)
    maintenance.write_cache(args.output, inputs)
    print(f"unified graph: {len(graph)} nodes, {len(graph.edges())} edges -> {args.output}")
    return 0


def _cmd_impact(args: argparse.Namespace) -> int:
    graph = _load_graph(args.graph)
    for ref in args.nodes:
        if graph.resolve(ref) is None:
            matches = graph.find(ref)
            if matches:
                print(f"'{ref}' is ambiguous; candidates:", file=sys.stderr)
                for m in matches[:10]:
                    print(f"  {m.id}", file=sys.stderr)
            else:
                print(f"'{ref}' not found in graph", file=sys.stderr)
    analysis = analyze_impact(graph, args.nodes, max_depth=args.max_depth, include_inferred=not args.no_inferred)
    if not analysis.changed:
        return 2
    _emit(graph, analysis, args)
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
    analysis = analyze_impact(graph, changed_ids, max_depth=args.max_depth, include_inferred=not args.no_inferred)
    _emit(graph, analysis, args)
    return 0


def _cmd_nodes(args: argparse.Namespace) -> int:
    graph = _load_graph(args.graph)
    nodes = graph.find(args.search) if args.search else graph.nodes()
    if args.node_type:
        nodes = [n for n in nodes if n.type.value == args.node_type]
    for node in sorted(nodes, key=lambda n: n.id):
        owner = f"\towner={node.owner}" if node.owner else ""
        print(f"{node.id}\t{node.type.value}\t{node.name}{owner}")
    return 0


def _cmd_paths(args: argparse.Namespace) -> int:
    graph = _load_graph(args.graph)
    src, dst = graph.resolve(args.changed), graph.resolve(args.target)
    if src is None or dst is None:
        print("error: could not resolve changed/target node", file=sys.stderr)
        return 2
    paths = graph.impact_paths(src.id, dst.id)
    if args.json:
        print(json.dumps(paths, indent=2))
        return 0
    if not paths:
        print(f"no propagation path from {src.id} to {dst.id}")
        return 0
    for p in paths:
        print("  " + "  ->  ".join(p))
    return 0


def _cmd_hotspots(args: argparse.Namespace) -> int:
    graph = _load_graph(args.graph)
    rows = graph.hotspots(top=args.top, include_inferred=not args.no_inferred)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"{'blast':>5}  {'in':>3} {'out':>3}  node")
    for r in rows:
        print(f"{r['blast_radius']:>5}  {r['in_degree']:>3} {r['out_degree']:>3}  {r['id']}  ({r['type']})")
    return 0


def _cmd_graph_diff(args: argparse.Namespace) -> int:
    old, new = _load_graph(args.old), _load_graph(args.new)
    d = diff_graphs(old, new)
    if args.json:
        print(json.dumps(d, indent=2, sort_keys=True))
        return 0
    print(f"+{len(d['added_nodes'])} nodes  -{len(d['removed_nodes'])} nodes  "
          f"+{len(d['added_edges'])} edges  -{len(d['removed_edges'])} edges")
    for n in d["removed_nodes"]:
        print(f"  - {n['id']}")
    for n in d["added_nodes"]:
        print(f"  + {n['id']}")
    for e in d["removed_edges"]:
        print(f"  - {e['src']} -[{e['type']}]-> {e['dst']}")
    for e in d["added_edges"]:
        print(f"  + {e['src']} -[{e['type']}]-> {e['dst']}")
    if d["removed_columns"]:
        print("removed columns (schema drift — check downstream):")
        for c in d["removed_columns"]:
            print(f"  ! {c}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    graph = _load_graph(args.graph)
    out = Path(args.output)
    if args.format == "graphml":
        graph.to_graphml(out)
    elif args.format == "dot":
        out.write_text(graph.to_dot(), encoding="utf-8")
    elif args.format == "cypher":
        out.write_text(graph.to_cypher(), encoding="utf-8")
    else:
        graph.save(out)
    print(f"exported {len(graph)} nodes as {args.format} -> {out}")
    return 0


def _cmd_html(args: argparse.Namespace) -> int:
    from .html_report import render_html

    graph = _load_graph(args.graph)
    analysis = analyze_impact(graph, args.nodes, max_depth=args.max_depth, include_inferred=not args.no_inferred)
    if not analysis.changed:
        print("no matching nodes found", file=sys.stderr)
        return 2
    Path(args.output).write_text(render_html(graph, analysis), encoding="utf-8")
    print(f"html view -> {args.output}")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    from . import maintenance

    if not _require_inputs(args):
        return 2
    inputs = _build_inputs(args)

    def build() -> None:
        graph = _build_graph(args, log=lambda *_: None)
        graph.save(args.output)
        maintenance.write_cache(args.output, inputs)
        print(f"rebuilt {args.output}: {len(graph)} nodes")

    try:
        maintenance.watch(
            build, inputs, interval=args.interval,
            exclude=[args.output, str(maintenance.cache_path(args.output))],
        )
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_hook_install(args: argparse.Namespace) -> int:
    from . import maintenance

    if args.hook_cmd:
        command = args.hook_cmd
    else:
        if not _require_inputs(args):
            return 2
        parts = ["impactgraph", "build", "--update", "-o", args.output]
        for flag, value in (("--repo", args.repo), ("--dbt-manifest", args.dbt_manifest), ("--sql", args.sql),
                            ("--openlineage", args.openlineage), ("--lineage-file", args.lineage_file)):
            if value:
                parts += [flag, value]
        command = " ".join(shlex.quote(p) for p in parts)
    path = maintenance.install_hook(args.git_repo, command)
    print(f"installed {path}\n  {command}")
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


def _cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import serve

    serve(args.graph)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
