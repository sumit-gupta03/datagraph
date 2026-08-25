"""Terminal rendering of an impact analysis using rich."""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text
from rich.tree import Tree

from .analysis import ImpactAnalysis
from .graph import ImpactGraph

_RISK_STYLES = {
    "LOW": "green",
    "MEDIUM": "yellow",
    "HIGH": "red",
    "CRITICAL": "bold white on red",
}

_ASCII_ICONS = {
    "file": "[file] ",
    "function": "f() ",
    "class": "[cls] ",
    "dbt_model": "[dbt] ",
    "dbt_source": "[src] ",
    "dbt_seed": "[seed] ",
    "dbt_snapshot": "[snap] ",
    "table": "[tbl] ",
    "view": "[view] ",
    "column": "[col] ",
    "exposure": "[dash] ",
    "dashboard": "[dash] ",
    "report": "[rpt] ",
    "api": "[api] ",
    "lambda": "[fn] ",
    "dag": "[job] ",
    "task": "[task] ",
}

_TYPE_ICONS = {
    "file": "📄",
    "function": "ƒ ",
    "class": "◆ ",
    "dbt_model": "⬢ ",
    "dbt_source": "⬡ ",
    "dbt_seed": "⬡ ",
    "dbt_snapshot": "⬢ ",
    "table": "▦ ",
    "view": "▤ ",
    "column": "│ ",
    "exposure": "📊",
    "dashboard": "📊",
    "report": "📈",
    "api": "⇄ ",
    "lambda": "λ ",
    "dag": "⛓ ",
    "task": "▸ ",
}


def render_analysis(graph: ImpactGraph, analysis: ImpactAnalysis, console: Console | None = None) -> None:
    console = console or Console()
    # Legacy / non-UTF-8 terminals (e.g. Windows cp1252) cannot print the
    # Unicode glyphs; fall back to ASCII so the CLI never crashes on output.
    unicode_ok = (console.encoding or "").lower().replace("-", "").startswith("utf")
    icons = _TYPE_ICONS if unicode_ok else _ASCII_ICONS
    warn = "⚠" if unicode_ok else "!"
    tick = "✓" if unicode_ok else "+"
    risk_level = analysis.risk["level"]
    style = _RISK_STYLES.get(risk_level, "white")

    header = Text()
    header.append(f"{warn} Change Impact\n\n", style="bold yellow")
    header.append("Changed:\n", style="bold")
    for nid in analysis.changed:
        node = graph.get_node(nid)
        label = node.name if node else nid
        header.append(f"  {label}\n", style="cyan")
    header.append("\nRisk: ", style="bold")
    header.append(f"{risk_level}", style=style)
    header.append(f"  (score {analysis.risk['score']})")
    if not analysis.include_inferred:
        header.append("\n(inferred edges excluded)", style="dim")
    console.print(Panel(header, expand=False))

    for tree_dict in analysis.trees:
        console.print(_to_rich_tree(tree_dict, icons))
        console.print()

    counts = analysis.summary_by_type()
    if counts:
        console.print("[bold]Affected:[/bold]")
        for type_name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            console.print(f"  {count} {type_name.replace('_', ' ')}(s)")
        console.print()

    if getattr(analysis, "warnings", None):
        console.print()
        for warning in analysis.warnings[:10]:
            console.print(f"[yellow]![/yellow] {escape(warning)}")
    if analysis.owners:
        console.print("[bold]Notify (owners of affected artifacts):[/bold]")
        for owner, names in analysis.owners.items():
            console.print(f"  {escape(str(owner))}: {escape(', '.join(names))}")
        console.print()

    console.print("[bold]Recommended tests:[/bold]")
    for rec in analysis.recommended_tests:
        console.print(f"  {tick} {escape(rec)}")


def render_lineage(
    graph: ImpactGraph,
    node_id: str,
    upstream_depth=None,
    downstream_depth=None,
    include_inferred: bool = True,
    console: Console | None = None,
) -> None:
    """Print where a node comes from (upstream) and what it feeds (downstream)."""
    console = console or Console()
    unicode_ok = (console.encoding or "").lower().replace("-", "").startswith("utf")
    icons = _TYPE_ICONS if unicode_ok else _ASCII_ICONS
    node = graph.get_node(node_id)
    name = node.name if node else node_id
    lin = graph.lineage(node_id, upstream_depth, downstream_depth, include_inferred)

    header = Text()
    header.append("Lineage\n\n", style="bold yellow")
    header.append("Node: ", style="bold")
    header.append(f"{name}  ", style="cyan")
    header.append(f"({node.type.value})" if node else "", style="dim")
    header.append(f"\n{len(lin['upstream'])} upstream · {len(lin['downstream'])} downstream")
    if node and node.owner:
        header.append(f"\nOwner: {node.owner}")
    console.print(Panel(header, expand=False))

    console.print("[bold]Upstream — where it comes from:[/bold]")
    up_tree = graph.upstream_tree(node_id, max_depth=upstream_depth, include_inferred=include_inferred)
    if up_tree["children"]:
        console.print(_to_rich_tree(up_tree, icons))
    else:
        console.print("  (nothing upstream — this is a root / source)")
    console.print()
    console.print("[bold]Downstream — what it feeds:[/bold]")
    down_tree = graph.impact_tree(node_id, max_depth=downstream_depth, include_inferred=include_inferred)
    if down_tree["children"]:
        console.print(_to_rich_tree(down_tree, icons))
    else:
        console.print("  (nothing downstream — this is a leaf)")


def _to_rich_tree(entry: dict, icons: dict) -> Tree:
    icon = icons.get(entry.get("type", ""), "- ")
    label = f"{escape(icon)}[cyan]{escape(entry['name'])}[/cyan] [dim]({entry.get('type', '?')})[/dim]"
    via = entry.get("via")
    if via:
        label += f" [dim italic]via {escape(via)}[/dim italic]"
    prov = entry.get("provenance", "extracted")
    if prov != "extracted":
        label += f" [yellow dim]({escape(str(prov))})[/yellow dim]"
    tree = Tree(label)
    for child in entry.get("children", []):
        tree.add(_to_rich_tree(child, icons))
    return tree
