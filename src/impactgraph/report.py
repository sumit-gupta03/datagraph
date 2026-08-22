"""Terminal rendering of an impact analysis using rich."""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text
from rich.tree import Tree

from .analysis import ImpactAnalysis
from .graph import INFERRED, ImpactGraph

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

    if analysis.owners:
        console.print("[bold]Notify (owners of affected artifacts):[/bold]")
        for owner, names in analysis.owners.items():
            console.print(f"  {escape(str(owner))}: {escape(', '.join(names))}")
        console.print()

    console.print("[bold]Recommended tests:[/bold]")
    for rec in analysis.recommended_tests:
        console.print(f"  {tick} {escape(rec)}")


def _to_rich_tree(entry: dict, icons: dict) -> Tree:
    icon = icons.get(entry.get("type", ""), "- ")
    label = f"{escape(icon)}[cyan]{escape(entry['name'])}[/cyan] [dim]({entry.get('type', '?')})[/dim]"
    via = entry.get("via")
    if via:
        label += f" [dim italic]via {escape(via)}[/dim italic]"
    if entry.get("provenance") == INFERRED:
        label += " [yellow dim](inferred)[/yellow dim]"
    tree = Tree(label)
    for child in entry.get("children", []):
        tree.add(_to_rich_tree(child, icons))
    return tree
