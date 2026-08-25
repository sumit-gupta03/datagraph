"""datagraph command-line interface.

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
from .security import redact_dsn
from .graph import ImpactGraph, diff_graphs, NodeType
from .report import render_analysis

DEFAULT_GRAPH = "datagraph.json"


def _add_build_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", help="Path to a Python code repo to scan")
    p.add_argument("--dbt-manifest", help="Path to a dbt manifest.json")
    p.add_argument("--dbt-run-results", help="dbt run_results.json - test outcomes and model run state (auto-detected next to the manifest)")
    p.add_argument("--dbt-sources", help="dbt sources.json - source freshness results (auto-detected next to the manifest)")
    p.add_argument("--metadata", help="Governance metadata file (YAML/JSON): glossary, domains, deprecations, owners")
    p.add_argument("--dbt-catalog", help="Path to dbt catalog.json (from `dbt docs generate`) — expands SELECT * in column lineage; auto-detected next to the manifest")
    p.add_argument("--sql", help="Directory of .sql files (requires [sql] extra)")
    p.add_argument("--sql-dialect", default=None, help="sqlglot dialect (e.g. snowflake)")
    p.add_argument("--openlineage", help="OpenLineage events file (JSON array or NDJSON)")
    p.add_argument("--lineage-file", help="DataHub-style lineage file (YAML/JSON)")
    p.add_argument("--warehouse", metavar="DSN", help="Database to read schema + foreign keys from: a .db/.sqlite file, sqlite:///…, duckdb://…, or a SQLAlchemy URL")
    p.add_argument("--warehouse-schemas", default=None, help="Comma-separated schemas to include (default: all user schemas)")
    p.add_argument("--warehouse-database", default=None, help="Database/catalog name filter")
    p.add_argument("--airflow", help="Airflow DAGs folder (or a single DAG file)")
    p.add_argument("--lambda", dest="lambda_template", metavar="TEMPLATE", help="serverless.yml or SAM/CloudFormation template")
    p.add_argument("--js", help="Path to a JavaScript/TypeScript code repo to scan")
    p.add_argument("--datahub", metavar="URL", help="Live DataHub server URL (GraphQL); token from --datahub-token or $DATAHUB_TOKEN")
    p.add_argument("--datahub-token", default=None)
    p.add_argument("--datahub-query", default="*", help="DataHub search query for datasets (default: *)")
    p.add_argument("--no-alias-linking", action="store_true", help="Do not link table names at different qualification (schema.t vs db.schema.t)")
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
        prog="datagraph",
        description="AI-powered Change Impact Graph: if I change this, what can break?",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="Build the unified graph from artifacts")
    _add_build_args(p_build)
    p_build.add_argument("--update", action="store_true", help="Skip the build if inputs are unchanged")
    p_build.add_argument("--llm-fallback", action="store_true",
                         help="After building, ask Claude for relationships the parsers could not derive (tagged llm; needs [ai])")
    p_build.add_argument("--llm-model", default=None)
    p_build.add_argument("--llm-provider", default=None, help="LLM provider for --llm-fallback: anthropic | bedrock | openai")
    p_build.add_argument("--llm-min-confidence", type=float, default=0.6)

    p_enrich = sub.add_parser("enrich", help="Add LLM-suggested relationships (llm provenance) to an existing graph")
    p_enrich.add_argument("--graph", default=DEFAULT_GRAPH)
    p_enrich.add_argument("--unparsed", default=None, help="JSON file of unparsed SQL snippets (default: <graph>.unparsed.json if present)")
    p_enrich.add_argument("--model", default=None, help="Model id (default: provider default / $DATAGRAPH_LLM_MODEL)")
    p_enrich.add_argument("--provider", default=None, help="anthropic | bedrock | openai (default: $DATAGRAPH_LLM_PROVIDER or anthropic)")
    p_enrich.add_argument("--min-confidence", type=float, default=0.6)
    p_enrich.add_argument("--dry-run", action="store_true", help="Print suggestions, do not modify the graph")
    p_enrich.add_argument("--json", action="store_true")

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

    p_html = sub.add_parser("html", help="Interactive HTML view: blast radius of nodes, or --all for the whole graph")
    p_html.add_argument("nodes", nargs="*")
    p_html.add_argument("--all", action="store_true", help="Render the whole graph instead of a blast radius")
    p_html.add_argument("--with-columns", action="store_true", help="Include column nodes in --all view")
    p_html.add_argument("--graph", default=DEFAULT_GRAPH)
    p_html.add_argument("--max-depth", type=int, default=None)
    p_html.add_argument("--no-inferred", action="store_true")
    p_html.add_argument("-o", "--output", default="impact.html")

    p_rel = sub.add_parser("relationships", help="All table and column relationships (foreign keys, lineage) — for data analysis")
    p_rel.add_argument("--graph", default=DEFAULT_GRAPH)
    p_rel.add_argument("--search", default=None, help="Only tables whose name contains this text")
    p_rel.add_argument("--tables-only", action="store_true", help="Skip column-level relationships")
    p_rel.add_argument("--json", action="store_true")

    p_prof = sub.add_parser("profile", help="Light data profiling through a database connection (row counts, nulls, distincts, ranges, top values)")
    p_prof.add_argument("--warehouse", metavar="DSN", required=True, help=".db/.sqlite file, sqlite:///…, duckdb://…, or a SQLAlchemy URL")
    p_prof.add_argument("--graph", default=DEFAULT_GRAPH)
    p_prof.add_argument("--tables", default=None, help="Comma-separated table ids/names (default: all warehouse tables in the graph)")
    p_prof.add_argument("--sample", type=int, default=100000, help="Rows sampled per table for column stats")
    p_prof.add_argument("--no-top-values", action="store_true")
    p_prof.add_argument("--json", action="store_true")

    p_wiki = sub.add_parser("wiki", help="Export a Markdown knowledge base (index, GRAPH_REPORT, llms.txt, one page per node) for AI assistants and humans")
    p_wiki.add_argument("--graph", default=DEFAULT_GRAPH)
    p_wiki.add_argument("-o", "--output", default="datagraph-wiki")
    p_wiki.add_argument("--title", default="datagraph knowledge base")
    p_wiki.add_argument("--with-files", action="store_true", help="Also create pages for source files")

    p_ctx = sub.add_parser("context", help="Compact knowledge pack for one node (columns, profile, lineage, relationships, risk, SQL) — paste into an assistant")
    p_ctx.add_argument("node")
    p_ctx.add_argument("--graph", default=DEFAULT_GRAPH)
    p_ctx.add_argument("--depth", type=int, default=2)

    p_model = sub.add_parser("model", help="Dimensional modelling: classify facts/dimensions, star schema, ER diagram, issues; or propose a star from one wide table")
    p_model.add_argument("--graph", default=DEFAULT_GRAPH)
    p_model.add_argument("--from-table", default=None, help="Propose a fact + dimensions from this wide/flat table")
    p_model.add_argument("--no-inferred", action="store_true", help="Use declared foreign keys only (no name-based links)")
    p_model.add_argument("--json", action="store_true")
    p_model.add_argument("--mermaid", default=None, help="Write the ER diagram (Mermaid) here")
    p_model.add_argument("--markdown", default=None, help="Write the Markdown report here")

    p_an = sub.add_parser("analyze", help="One shot for a database: connect -> graph (tables, columns, FKs, views) -> relationships -> profiling -> dimensional model -> lineage HTML -> wiki, into one folder")
    p_an.add_argument("--warehouse", metavar="DSN", required=True, help=".db/.sqlite file, sqlite:///…, duckdb://…, or a SQLAlchemy URL (password is never stored)")
    p_an.add_argument("--schemas", default=None, help="Comma-separated schemas to include")
    p_an.add_argument("--database", default=None, help="Database/catalog filter")
    p_an.add_argument("--dialect", default=None, help="SQL dialect for view definitions (snowflake, postgres, bigquery, ...)")
    p_an.add_argument("-o", "--output", default="datagraph-out", help="Output folder")
    p_an.add_argument("--no-profile", action="store_true", help="Skip data profiling (metadata only)")
    p_an.add_argument("--sample", type=int, default=100000, help="Rows sampled per table for profiling")
    p_an.add_argument("--no-top-values", action="store_true")
    p_an.add_argument("--no-inferred", action="store_true", help="Declared foreign keys only for the model")
    p_an.add_argument("--metadata", default=None, help="Governance metadata file (glossary, domains, deprecations)")
    p_an.add_argument("--title", default="datagraph analysis")
    p_an.add_argument("--json", action="store_true", help="Print the summary as JSON")

    p_search = sub.add_parser("search", help="Search every asset: names, ids, descriptions, columns, owners, tags, glossary terms, domains")
    p_search.add_argument("query", nargs="?", default="", help="Free text (optional when filtering)")
    p_search.add_argument("--graph", default=DEFAULT_GRAPH)
    p_search.add_argument("--type", dest="node_type", default=None, help="Filter by node type (table, dbt_model, dashboard, ...)")
    p_search.add_argument("--domain", default=None)
    p_search.add_argument("--tag", default=None)
    p_search.add_argument("--term", default=None, help="Filter by glossary term")
    p_search.add_argument("--owner", default=None)
    p_search.add_argument("--columns", action="store_true", help="Include column nodes in the results")
    p_search.add_argument("--limit", type=int, default=25)
    p_search.add_argument("--json", action="store_true")

    p_pii = sub.add_parser("pii", help="Sensitive-data report: where personal data lives and what is exposed to it")
    p_pii.add_argument("--graph", default=DEFAULT_GRAPH)
    p_pii.add_argument("--no-inferred", action="store_true")
    p_pii.add_argument("--json", action="store_true")

    p_gloss = sub.add_parser("glossary", help="Business glossary: terms, definitions and the assets that carry them")
    p_gloss.add_argument("--graph", default=DEFAULT_GRAPH)
    p_gloss.add_argument("--json", action="store_true")

    p_plug = sub.add_parser("plugins", help="List installed extractor plugins (datagraph.extractors entry points)")

    p_lin = sub.add_parser("lineage", help="Upstream (where it comes from) and downstream (what it feeds) of a node")
    p_lin.add_argument("node")
    p_lin.add_argument("--graph", default=DEFAULT_GRAPH)
    p_lin.add_argument("--upstream-depth", type=int, default=None)
    p_lin.add_argument("--downstream-depth", type=int, default=None)
    p_lin.add_argument("--no-inferred", action="store_true")
    p_lin.add_argument("--json", action="store_true")
    p_lin.add_argument("--html", metavar="OUT", help="Also write an interactive lineage view")

    p_watch = sub.add_parser("watch", help="Rebuild the graph whenever inputs change")
    _add_build_args(p_watch)
    p_watch.add_argument("--interval", type=float, default=2.0)

    p_hook_install = sub.add_parser("hook-install", help="Install a git post-commit hook that rebuilds the graph")
    p_hook_install.add_argument("--git-repo", default=".", help="Git repository to install the hook into")
    p_hook_install.add_argument("--command", dest="hook_cmd", default=None, help="Command to run (default: datagraph build ... --update)")
    _add_build_args(p_hook_install)

    p_explain = sub.add_parser("explain", help="AI explanation of an impact analysis")
    p_explain.add_argument("nodes", nargs="+", help="Node ids, names, or paths")
    p_explain.add_argument("--graph", default=DEFAULT_GRAPH)
    p_explain.add_argument("--max-depth", type=int, default=None)
    p_explain.add_argument("--model", default=None, help="Model id (default: provider default / $DATAGRAPH_LLM_MODEL)")
    p_explain.add_argument("--provider", default=None, help="anthropic | bedrock | openai (default: $DATAGRAPH_LLM_PROVIDER or anthropic)")

    p_mcp = sub.add_parser("mcp", help="Serve the graph over MCP (stdio)")
    p_mcp.add_argument("--graph", default=DEFAULT_GRAPH)

    # extractor plugins contribute --<name> flags to build / watch / hook-install
    from .extractors.registry import plugins as _plugins

    for plugin in _plugins():
        for p in (p_build, p_watch, p_hook_install):
            try:
                p.add_argument(f"--{plugin.name}", dest=f"plugin_{plugin.name}", metavar=plugin.value_name, help=plugin.help or f"{plugin.name} extractor plugin")
                for opt, help_text in plugin.options.items():
                    p.add_argument(f"--{plugin.name}-{opt}", dest=f"plugin_{plugin.name}_{opt}", default=None, help=help_text)
            except argparse.ArgumentError:
                pass

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
        "lineage": _cmd_lineage,
        "relationships": _cmd_relationships,
        "profile": _cmd_profile,
        "wiki": _cmd_wiki,
        "context": _cmd_context,
        "plugins": _cmd_plugins,
        "model": _cmd_model,
        "search": _cmd_search,
        "pii": _cmd_pii,
        "glossary": _cmd_glossary,
        "analyze": _cmd_analyze,
        "watch": _cmd_watch,
        "hook-install": _cmd_hook_install,
        "explain": _cmd_explain,
        "enrich": _cmd_enrich,
        "mcp": _cmd_mcp,
    }[args.command](args)


# ------------------------------------------------------------------ helpers


def _load_graph(path: str) -> ImpactGraph:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"graph file '{path}' not found — run 'datagraph build' first")
    return ImpactGraph.load(p)


def _build_inputs(args: argparse.Namespace) -> List[Optional[str]]:
    return [args.repo, args.dbt_manifest, args.sql, args.openlineage, args.lineage_file,
            getattr(args, "airflow", None), getattr(args, "lambda_template", None), getattr(args, "js", None)]


_UNPARSED: List[dict] = []  # filled by _build_graph; saved next to the graph for the LLM fallback


def _build_graph(args: argparse.Namespace, log=print) -> ImpactGraph:
    graph = ImpactGraph()
    _UNPARSED.clear()
    if args.repo:
        fragment = PythonExtractor(args.repo).extract()
        log(f"python: {len(fragment)} nodes from {args.repo}")
        graph.merge(fragment)
    if args.dbt_manifest:
        ext = DbtExtractor(args.dbt_manifest, column_lineage=not args.no_column_lineage, dialect=args.sql_dialect,
                           catalog_path=getattr(args, "dbt_catalog", None),
                           run_results_path=getattr(args, "dbt_run_results", None),
                           sources_path=getattr(args, "dbt_sources", None))
        fragment = ext.extract()
        _UNPARSED.extend(ext.unparsed)
        log(f"dbt: {len(fragment)} nodes from {args.dbt_manifest}")
        graph.merge(fragment)
    if args.sql:
        from .extractors.sql_extractor import SqlExtractor

        ext = SqlExtractor(args.sql, dialect=args.sql_dialect)
        fragment = ext.extract()
        _UNPARSED.extend(ext.unparsed)
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
    if getattr(args, "warehouse", None):
        from .extractors import WarehouseExtractor

        schemas = [s.strip() for s in args.warehouse_schemas.split(",")] if args.warehouse_schemas else None
        fragment = WarehouseExtractor(
            args.warehouse, database=args.warehouse_database, schemas=schemas, dialect=args.sql_dialect
        ).extract()
        log(f"warehouse: {len(fragment)} nodes from {redact_dsn(args.warehouse)}")
        graph.merge(fragment)
    if getattr(args, "airflow", None):
        from .extractors import AirflowExtractor

        fragment = AirflowExtractor(args.airflow).extract()
        log(f"airflow: {len(fragment)} nodes from {args.airflow}")
        graph.merge(fragment)
    if getattr(args, "lambda_template", None):
        from .extractors import LambdaExtractor

        fragment = LambdaExtractor(args.lambda_template, code_root=args.repo or None).extract()
        log(f"lambda: {len(fragment)} nodes from {args.lambda_template}")
        graph.merge(fragment)
    if getattr(args, "js", None):
        from .extractors import JsExtractor

        fragment = JsExtractor(args.js).extract()
        log(f"js/ts: {len(fragment)} nodes from {args.js}")
        graph.merge(fragment)
    if getattr(args, "datahub", None):
        import os

        from .extractors import DataHubExtractor

        token = args.datahub_token or os.environ.get("DATAHUB_TOKEN")
        fragment = DataHubExtractor(args.datahub, token=token, query=args.datahub_query).extract()
        log(f"datahub: {len(fragment)} nodes from {args.datahub}")
        graph.merge(fragment)
    from .extractors.registry import plugins as _plugins

    for plugin in _plugins():
        value = getattr(args, f"plugin_{plugin.name}", None)
        if not value:
            continue
        options = {opt: getattr(args, f"plugin_{plugin.name}_{opt}", None) for opt in plugin.options}
        fragment = plugin.extract(value, **options)
        log(f"{plugin.name}: {len(fragment)} nodes from {value}")
        graph.merge(fragment)
    if getattr(args, "metadata", None):
        from .metadata import apply_metadata, load_metadata

        applied = apply_metadata(graph, load_metadata(args.metadata))
        log(f"metadata: {applied['terms']} term link(s), {applied['domains']} domain assignment(s), "
            f"{applied['deprecations']} deprecation(s), {applied['owners']} owner(s)"
            + (f"; {applied['unmatched']} reference(s) matched nothing" if applied["unmatched"] else ""))
        for ref in applied.get("unmatched_refs", []):
            log(f"  unmatched: {ref}")
    if not getattr(args, "no_alias_linking", False):
        linked = graph.link_table_aliases()
        if linked:
            log(f"linked {linked} table alias pair(s) (schema.table <-> db.schema.table)")
    return graph


def _require_inputs(args: argparse.Namespace) -> bool:
    plugin_values = [v for k, v in vars(args).items() if k.startswith("plugin_") and v and "_" not in k[len("plugin_"):]]
    if not any(_build_inputs(args)) and not getattr(args, "warehouse", None) and not getattr(args, "datahub", None) and not plugin_values:
        print(
            "error: provide at least one of --repo, --dbt-manifest, --sql, --openlineage, --lineage-file, "
            "--warehouse, --airflow, --lambda, --js, --datahub",
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
    unparsed_path = Path(str(args.output) + ".unparsed.json")
    if _UNPARSED:
        unparsed_path.write_text(json.dumps(_UNPARSED, indent=2), encoding="utf-8")
        print(f"note: {len(_UNPARSED)} SQL snippet(s) could not be parsed -> {unparsed_path} "
              f"(run 'datagraph enrich' or build with --llm-fallback to let Claude suggest their lineage)")
    elif unparsed_path.exists():
        unparsed_path.unlink()
    if args.llm_fallback:
        from .ai import apply_suggestions, suggest_lineage

        suggestions = suggest_lineage(graph, unparsed_sql=_UNPARSED, model=args.llm_model, provider=getattr(args, "llm_provider", None))
        added = apply_suggestions(graph, suggestions, min_confidence=args.llm_min_confidence)
        print(f"llm fallback: {len(suggestions)} suggestion(s), {added} edge(s) added (provenance=llm)")
    graph.save(args.output)
    maintenance.write_cache(args.output, inputs)
    print(f"unified graph: {len(graph)} nodes, {len(graph.edges())} edges -> {args.output}")
    return 0


def _cmd_enrich(args: argparse.Namespace) -> int:
    from .ai import apply_suggestions, suggest_lineage

    graph = _load_graph(args.graph)
    unparsed_file = Path(args.unparsed) if args.unparsed else Path(str(args.graph) + ".unparsed.json")
    unparsed = json.loads(unparsed_file.read_text(encoding="utf-8")) if unparsed_file.exists() else []
    try:
        suggestions = suggest_lineage(graph, unparsed_sql=unparsed, model=args.model, provider=args.provider)
    except Exception as exc:  # noqa: BLE001 - provider / network errors: clean message, no traceback
        print(f"error: LLM provider failed: {redact_dsn(str(exc))[:400]}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(suggestions, indent=2))
    else:
        for s in suggestions:
            mark = "+" if s["confidence"] >= args.min_confidence else "-"
            print(f"  {mark} [{s['kind']}] {s['source']} -> {s['target']}  ({s['confidence']:.2f}) {s['reason']}")
    if args.dry_run:
        print(f"{len(suggestions)} suggestion(s); dry run, graph unchanged")
        return 0
    added = apply_suggestions(graph, suggestions, min_confidence=args.min_confidence)
    graph.save(args.graph)
    print(f"added {added} llm-provenance edge(s) to {args.graph} (use --no-inferred to exclude them)")
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
    from .html_report import render_graph_html, render_html

    graph = _load_graph(args.graph)
    if args.all:
        Path(args.output).write_text(render_graph_html(graph, hide_columns=not args.with_columns), encoding="utf-8")
        print(f"html view (whole graph) -> {args.output}")
        return 0
    if not args.nodes:
        print("error: give node ids, or --all for the whole graph", file=sys.stderr)
        return 2
    analysis = analyze_impact(graph, args.nodes, max_depth=args.max_depth, include_inferred=not args.no_inferred)
    if not analysis.changed:
        print("no matching nodes found", file=sys.stderr)
        return 2
    Path(args.output).write_text(render_html(graph, analysis), encoding="utf-8")
    print(f"html view -> {args.output}")
    return 0


def _cmd_relationships(args: argparse.Namespace) -> int:
    from .analysis.relationships import relationships

    graph = _load_graph(args.graph)
    rel = relationships(graph, search=args.search, include_columns=not args.tables_only)
    if args.json:
        print(json.dumps(rel, indent=2, sort_keys=True))
        return 0
    if not rel["tables"]:
        print("no tables/views in the graph (build with --warehouse, --dbt-manifest, --sql or --openlineage)")
        return 0
    for t in rel["tables"]:
        cols = ", ".join(c["name"] + ("*" if c.get("primary_key") else "") for c in t["columns"]) or "-"
        print(f"{t['id']}  ({t['type']})")
        print(f"    columns: {cols}")
        for d in t["depends_on"]:
            print(f"    -> depends on {d['target']}  [{d['via']}]")
        for d in t["dependents"]:
            print(f"    <- feeds {d['source']}  [{d['via']}]")
    if rel["column_relationships"]:
        print("\ncolumn relationships:")
        for r in rel["column_relationships"]:
            print(f"    {r['from']}  ->  {r['to']}  [{r['via']}]")
    print(f"\n{len(rel['tables'])} tables/views, {len(rel['table_relationships'])} table relationships, "
          f"{len(rel['column_relationships'])} column relationships  (* = primary key)")
    return 0


def _cmd_profile(args: argparse.Namespace) -> int:
    from .profiling import profile_warehouse

    graph = _load_graph(args.graph)
    tables = [t.strip() for t in args.tables.split(",")] if args.tables else None
    results = profile_warehouse(args.warehouse, graph, tables=tables, sample=args.sample,
                                top_values=not args.no_top_values, log=None if args.json else print)
    graph.save(args.graph)
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True, default=str))
    else:
        print(f"profiled {len(results)} table(s); results stored in {args.graph}")
    return 0


def _cmd_wiki(args: argparse.Namespace) -> int:
    from .knowledge import build_wiki

    graph = _load_graph(args.graph)
    stats = build_wiki(graph, args.output, title=args.title, include_files=args.with_files)
    print(f"wiki: {stats['pages']} pages for {stats['nodes']} nodes -> {args.output}/ (index.md, GRAPH_REPORT.md, llms.txt)")
    return 0


def _cmd_context(args: argparse.Namespace) -> int:
    from .knowledge import context

    graph = _load_graph(args.graph)
    print(context(graph, args.node, depth=args.depth))
    return 0


def _cmd_model(args: argparse.Namespace) -> int:
    from .analysis.modeling import propose_from_table, star_schema, to_markdown, to_mermaid

    graph = _load_graph(args.graph)
    if args.from_table:
        try:
            model = propose_from_table(graph, args.from_table)
        except KeyError:
            print(f"error: no table matches '{args.from_table}'", file=sys.stderr)
            return 2
        title = f"Proposed star schema from {args.from_table}"
    else:
        model = star_schema(graph, include_inferred=not args.no_inferred)
        title = "Dimensional model"
    if args.mermaid:
        Path(args.mermaid).write_text(to_mermaid(model), encoding="utf-8")
        print(f"mermaid -> {args.mermaid}", file=sys.stderr)
    if args.markdown:
        Path(args.markdown).write_text(to_markdown(model, title), encoding="utf-8")
        print(f"markdown -> {args.markdown}", file=sys.stderr)
    if args.json:
        print(json.dumps(model, indent=2, sort_keys=True, default=str))
    elif not (args.mermaid or args.markdown):
        print(to_markdown(model, title))
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    """Standard flow: connection + schema in -> lineage, relationships, profiling, dimensional model, wiki out."""
    from .analysis.modeling import star_schema, to_markdown as model_md, to_mermaid
    from .analysis.relationships import relationships
    from .extractors.warehouse_extractor import WarehouseExtractor, connect
    from .html_report import render_graph_html
    from .knowledge import build_wiki
    from .profiling import profile_warehouse

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    schemas = [x.strip() for x in args.schemas.split(",")] if args.schemas else None
    say = (lambda *a, **k: None) if args.json else print
    say(f"connecting to {redact_dsn(args.warehouse)} ...")
    connection = connect(args.warehouse)
    graph = WarehouseExtractor(connection, database=args.database, schemas=schemas, dialect=args.dialect).extract()
    graph.link_table_aliases()
    n_tables = len([n for n in graph.nodes() if n.type in (NodeType.TABLE, NodeType.VIEW)])
    say(f"schema: {n_tables} tables/views, {len(graph.nodes(NodeType.COLUMN))} columns, {len(graph.edges())} edges")
    if not args.no_profile:
        res = profile_warehouse(connection, graph, sample=args.sample, top_values=not args.no_top_values, log=None)
        say(f"profiling: {len(res)} tables (sensitive-looking columns masked)")
    if args.metadata:
        from .metadata import apply_metadata, load_metadata

        applied = apply_metadata(graph, load_metadata(args.metadata))
        say(f"metadata: {applied['terms']} term link(s), {applied['domains']} domain assignment(s), "
            f"{applied['deprecations']} deprecation(s)")
    graph.save(out / "datagraph.json")
    rel = relationships(graph, include_columns=True)
    (out / "relationships.json").write_text(json.dumps(rel, indent=2, sort_keys=True, default=str), encoding="utf-8")
    model = star_schema(graph, include_inferred=not args.no_inferred)
    (out / "MODEL.md").write_text(model_md(model, f"Dimensional model - {args.title}"), encoding="utf-8")
    (out / "model.json").write_text(json.dumps({k: v for k, v in model.items() if k != "classification"}, indent=2, sort_keys=True, default=str), encoding="utf-8")
    (out / "er-diagram.mmd").write_text(to_mermaid(model), encoding="utf-8")
    (out / "lineage.html").write_text(render_graph_html(graph, title=args.title), encoding="utf-8")
    stats = build_wiki(graph, out / "wiki", title=args.title)
    summary = {
        "output": str(out), "tables": n_tables, "columns": len(graph.nodes(NodeType.COLUMN)), "edges": len(graph.edges()),
        "foreign_keys": len(rel.get("table_relationships", [])), "profiled": not args.no_profile,
        "facts": [f["id"] for f in model["facts"]], "dimensions": [d["id"] for d in model["dimensions"]],
        "conformed_dimensions": model["conformed_dimensions"], "issues": model["issues"][:20], "wiki_pages": stats["pages"],
        "files": ["datagraph.json", "relationships.json", "MODEL.md", "model.json", "er-diagram.mmd", "lineage.html", "wiki/"],
    }
    if args.json:
        print(json.dumps(summary, indent=2, default=str))
        return 0
    say(f"model: {len(model['facts'])} fact(s), {len(model['dimensions'])} dimension(s), {len(model['conformed_dimensions'])} conformed, {len(model['issues'])} issue(s)")
    say(f"wiki: {stats['pages']} pages")
    say(f"-> {out}/  (datagraph.json, relationships.json, MODEL.md, er-diagram.mmd, lineage.html, wiki/)")
    say("next: datagraph lineage <table> --graph " + str(out / "datagraph.json") + "  |  datagraph context <table> --graph ...  |  datagraph mcp --graph ...")
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    from .analysis.discovery import search

    graph = _load_graph(args.graph)
    rows = search(graph, args.query, node_type=args.node_type, domain=args.domain, tag=args.tag,
                  term=args.term, owner=args.owner, include_columns=args.columns, limit=args.limit)
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True, default=str))
        return 0
    if not rows:
        print("no matches")
        return 0
    for r in rows:
        bits = [r["type"]]
        if r["domain"]:
            bits.append(f"domain={r['domain']}")
        if r["owner"]:
            bits.append(f"owner={r['owner']}")
        if r["terms"]:
            bits.append("terms=" + ",".join(str(t) for t in r["terms"]))
        if r["deprecated"]:
            bits.append("DEPRECATED")
        print(f"{r['id']}\t{' | '.join(bits)}\t(matched {r['matched_on']})")
        if r["description"]:
            print(f"    {r['description']}")
    return 0


def _cmd_pii(args: argparse.Namespace) -> int:
    from .analysis.discovery import pii_report

    graph = _load_graph(args.graph)
    report = pii_report(graph, include_inferred=not args.no_inferred)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0
    if not report["tables"]:
        print("no columns look like personal data")
        return 0
    print(f"{report['sensitive_columns']} sensitive-looking column(s) in {len(report['tables'])} table(s)\n")
    for row in report["tables"]:
        owner = f"  owner={row['owner']}" if row["owner"] else "  owner=UNOWNED"
        print(f"{row['id']}{owner}")
        print(f"    columns : {', '.join(row['columns'])}")
        if row["masked_in_profile"]:
            print(f"    masked  : {', '.join(row['masked_in_profile'])} (values withheld from profiles)")
        if row["exposed_to"]:
            print("    exposed : " + ", ".join(f"{e['name']} ({e['type']})" for e in row["exposed_to"]))
        else:
            print(f"    exposed : nothing downstream ({row['downstream']} node(s) affected)")
    if report["unowned"]:
        print(f"\nunowned tables holding personal data: {len(report['unowned'])}")
    print(f"\nnote: {report['note']}")
    return 0


def _cmd_glossary(args: argparse.Namespace) -> int:
    from .metadata import glossary_index

    graph = _load_graph(args.graph)
    index = glossary_index(graph)
    if args.json:
        print(json.dumps(index, indent=2, sort_keys=True, default=str))
        return 0
    if not index:
        print("no glossary terms in this graph (attach them with --metadata datagraph.yml or dbt meta.terms)")
        return 0
    for name, entry in index.items():
        owner = f"  [owner: {entry['owner']}]" if entry.get("owner") else ""
        print(f"{name}{owner}")
        if entry["definition"]:
            print(f"    {entry['definition']}")
        print(f"    assets ({len(entry['assets'])}): {', '.join(entry['assets'][:8])}")
    return 0


def _cmd_plugins(args: argparse.Namespace) -> int:
    from .extractors.registry import plugins

    found = plugins()
    if not found:
        print("no extractor plugins installed (declare a 'datagraph.extractors' entry point to add one)")
        return 0
    for p in found:
        print(f"--{p.name} {p.value_name}\t{p.help}\t[{p.source}]")
    return 0


def _cmd_lineage(args: argparse.Namespace) -> int:
    graph = _load_graph(args.graph)
    node = graph.resolve(args.node)
    if node is None:
        matches = graph.find(args.node)
        print(f"'{args.node}' not found" + (f"; candidates: {', '.join(m.id for m in matches[:8])}" if matches else ""), file=sys.stderr)
        return 2
    include_inferred = not args.no_inferred
    if args.html:
        from .html_report import render_lineage_html

        Path(args.html).write_text(
            render_lineage_html(graph, node.id, args.upstream_depth, args.downstream_depth, include_inferred),
            encoding="utf-8",
        )
        print(f"lineage view -> {args.html}", file=sys.stderr)
    if args.json:
        lin = graph.lineage(node.id, args.upstream_depth, args.downstream_depth, include_inferred)
        print(json.dumps({"node": node.id, **lin,
                          "upstream_tree": graph.upstream_tree(node.id, args.upstream_depth, include_inferred),
                          "downstream_tree": graph.impact_tree(node.id, args.downstream_depth, include_inferred)},
                         indent=2, sort_keys=True))
    else:
        from .report import render_lineage

        render_lineage(graph, node.id, args.upstream_depth, args.downstream_depth, include_inferred)
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
        parts = ["datagraph", "build", "--update", "-o", args.output]
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
    try:
        print(explain_impact(analysis, model=args.model, provider=args.provider))
    except Exception as exc:  # noqa: BLE001
        print(f"error: LLM provider failed: {redact_dsn(str(exc))[:400]}", file=sys.stderr)
        return 2
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import serve

    serve(args.graph)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
