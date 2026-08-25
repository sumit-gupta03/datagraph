"""Self-contained interactive HTML views (no external assets).

* ``render_html(graph, analysis)``            — blast radius of a change
* ``render_lineage_html(graph, node, ...)``   — upstream + downstream lineage of a node
* ``render_graph_html(graph, ...)``           — the whole graph (or a subset)

All three share one renderer: nodes are laid out in columns by "depth"
(negative = upstream, 0 = focus/changed, positive = downstream), arrows point
in the direction data/impact flows, and the page lets you click a node to
highlight its downstream, search, toggle inferred edges, and see owners/tests.
"""

from __future__ import annotations

import html
import json
from typing import Dict, Iterable, List, Optional

from .analysis import ImpactAnalysis
from .graph import IMPACT_DIRECTION, ImpactGraph, NodeType
from .profiling import profile_summary
from .security import escape_script_json

_COLORS = {
    "file": "#8E8E93",
    "function": "#5AC8FA",
    "class": "#34AADC",
    "dbt_model": "#FF9500",
    "dbt_source": "#FFCC00",
    "dbt_seed": "#FFCC00",
    "dbt_snapshot": "#FF9500",
    "table": "#4CD964",
    "view": "#A4E47A",
    "column": "#C7C7CC",
    "exposure": "#FF3B30",
    "dashboard": "#FF3B30",
    "report": "#FF2D55",
    "api": "#AF52DE",
    "lambda": "#5856D6",
    "dag": "#007AFF",
    "task": "#64B5F6",
}


def render_html(graph: ImpactGraph, analysis: ImpactAnalysis, title: str = "datagraph — change impact") -> str:
    depth: Dict[str, int] = {nid: 0 for nid in analysis.changed}
    depth.update({k: v for k, v in analysis.affected.items() if k not in depth})
    changed_names = ", ".join(graph.get_node(n).name if graph.get_node(n) else n for n in analysis.changed)
    return _render(
        graph, depth, focus=set(analysis.changed), title=title,
        subtitle=f"Changed: {changed_names}",
        risk=analysis.risk, tests=analysis.recommended_tests, owners=analysis.owners,
        count_label=f"{len(analysis.affected)} affected",
    )


def render_lineage_html(
    graph: ImpactGraph,
    node_id: str,
    upstream_depth: Optional[int] = None,
    downstream_depth: Optional[int] = None,
    include_inferred: bool = True,
    title: str = "datagraph — lineage",
) -> str:
    lin = graph.lineage(node_id, upstream_depth, downstream_depth, include_inferred)
    depth: Dict[str, int] = {node_id: 0}
    depth.update({k: -v for k, v in lin["upstream"].items()})
    depth.update({k: v for k, v in lin["downstream"].items() if k not in depth})
    node = graph.get_node(node_id)
    name = node.name if node else node_id
    return _render(
        graph, depth, focus={node_id}, title=title,
        subtitle=f"Lineage of {name}  ·  ← upstream (where it comes from)   downstream (what it feeds) →",
        count_label=f"{len(lin['upstream'])} upstream · {len(lin['downstream'])} downstream",
        hide_columns=False,
    )


def render_graph_html(
    graph: ImpactGraph,
    node_ids: Optional[Iterable[str]] = None,
    hide_columns: bool = True,
    title: str = "datagraph — full graph",
) -> str:
    """Whole-graph view: columns are topological 'layers' (sources left, dashboards right)."""
    ids = set(node_ids) if node_ids is not None else {n.id for n in graph.nodes()}
    if hide_columns:
        ids = {i for i in ids if (graph.get_node(i) and graph.get_node(i).type != NodeType.COLUMN)}
    # layer = longest upstream chain length (roots at 0)
    depth: Dict[str, int] = {}
    order = sorted(ids)
    for nid in order:
        ups = graph.upstream(nid)
        depth[nid] = max([d for k, d in ups.items() if k in ids] or [0])
    return _render(
        graph, depth, focus=set(), title=title,
        subtitle="Every node and edge in the graph (columns hidden)" if hide_columns else "Every node and edge in the graph",
        count_label=f"{len(ids)} nodes",
        hide_columns=hide_columns,
    )


# ----------------------------------------------------------------- renderer


def _render(graph, depth: Dict[str, int], focus: set, title: str, subtitle: str, risk=None, tests=None,
            owners=None, count_label: str = "", hide_columns: bool = False) -> str:
    node_ids: List[str] = list(depth)
    layers: Dict[int, List[str]] = {}
    for nid in node_ids:
        layers.setdefault(depth[nid], []).append(nid)
    min_layer = min(layers) if layers else 0
    max_layer = max(layers) if layers else 0
    col_w, row_h, pad = 260, 48, 40
    positions: Dict[str, tuple] = {}
    height = (max(len(v) for v in layers.values()) * row_h + 2 * pad) if layers else 200
    for d, ids in layers.items():
        ids.sort(key=lambda i: (graph.get_node(i).type.value if graph.get_node(i) else "", i))
        total = len(ids) * row_h
        y0 = (height - total) / 2 + row_h / 2
        for k, nid in enumerate(ids):
            positions[nid] = (pad + 90 + (d - min_layer) * col_w, y0 + k * row_h)
    width = pad * 2 + 180 + (max_layer - min_layer) * col_w + 160

    nodes_js = []
    for nid in node_ids:
        node = graph.get_node(nid)
        x, y = positions[nid]
        nodes_js.append({
            "id": nid, "name": node.name if node else nid, "type": node.type.value if node else "unknown",
            "depth": depth[nid], "owner": (node.owner if node else None) or "",
            "profile": profile_summary(node) if node else "",
            "x": round(x, 1), "y": round(y, 1), "changed": nid in focus,
        })
    ids = set(node_ids)
    edges_js = []
    for edge in graph.edges():
        if edge.src in ids and edge.dst in ids:
            s, t = (edge.src, edge.dst) if IMPACT_DIRECTION[edge.type] == "forward" else (edge.dst, edge.src)
            edges_js.append({"s": s, "t": t, "type": edge.type.value,
                             "inferred": edge.meta.get("provenance", "extracted") != "extracted",
                             "provenance": edge.meta.get("provenance", "extracted")})

    data = json.dumps({"nodes": nodes_js, "edges": edges_js, "risk": risk or {}, "tests": tests or [],
                       "owners": owners or {}, "colors": _COLORS, "width": width, "height": height})
    risk_level = (risk or {}).get("level")
    risk_pill = ""
    if risk_level:
        risk_color = {"LOW": "#34C759", "MEDIUM": "#FFCC00", "HIGH": "#FF9500", "CRITICAL": "#FF3B30"}.get(risk_level, "#999")
        risk_pill = f'<span class="pill" style="background:{risk_color}">Risk {risk_level} · score {risk["score"]}</span>'
    return (_TEMPLATE.replace("__TITLE__", html.escape(title)).replace("__SUBTITLE__", html.escape(subtitle))
            .replace("__RISKPILL__", risk_pill).replace("__DATA__", escape_script_json(data)).replace("__COUNT__", html.escape(count_label)))


_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>
 body{margin:0;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;background:#0f1115;color:#e6e6e6}
 header{padding:12px 20px;border-bottom:1px solid #262a33;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
 header h1{font-size:16px;margin:0;font-weight:600}
 .sub{color:#9aa3b2;font-size:13px}
 .pill{padding:3px 10px;border-radius:999px;font-size:12px;font-weight:700;color:#111}
 input{background:#1a1e26;border:1px solid #2c313c;color:#eee;padding:6px 10px;border-radius:6px;min-width:220px}
 label{font-size:12px;color:#aaa;display:flex;gap:6px;align-items:center}
 main{display:grid;grid-template-columns:1fr 320px;height:calc(100vh - 57px)}
 #canvas{overflow:auto}
 aside{border-left:1px solid #262a33;padding:14px;overflow:auto;font-size:13px}
 aside h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#8a909c;margin:14px 0 6px}
 .legend span{display:inline-flex;align-items:center;gap:5px;margin:2px 8px 2px 0;font-size:12px}
 .dot{width:10px;height:10px;border-radius:50%;display:inline-block}
 .node{cursor:pointer}
 .node text{font-size:11px;pointer-events:none}
 .node.dim{opacity:.15}
 .edge{stroke:#596273;stroke-width:1.3;fill:none;marker-end:url(#arrow)}
 .edge.inferred{stroke-dasharray:4 3;stroke:#b08a2e}
 .edge.dim{opacity:.08}
 .edge.hl{stroke:#ff9f0a;stroke-width:2}
 ul{padding-left:18px;margin:4px 0}
 li{margin:3px 0}
 #tip{position:fixed;pointer-events:none;background:#1a1e26;border:1px solid #2c313c;padding:6px 9px;border-radius:6px;font-size:12px;display:none}
</style></head><body>
<header>
 <h1>__TITLE__</h1>
 <span class="sub">__SUBTITLE__</span>
 __RISKPILL__
 <span class="sub">__COUNT__</span>
 <input id="q" placeholder="search nodes… (click a node to highlight its downstream)">
 <label><input type="checkbox" id="inf" checked style="min-width:0"> show inferred edges</label>
</header>
<main>
 <div id="canvas"><svg id="svg"></svg></div>
 <aside>
  <h2>Legend</h2><div class="legend" id="legend"></div>
  <h2>Owners</h2><div id="owners"></div>
  <h2>Recommended tests</h2><ul id="tests"></ul>
  <h2>Selected</h2><div id="sel">Click a node.</div>
 </aside>
</main>
<div id="tip"></div>
<script>
const D = __DATA__;
const svg = document.getElementById('svg');
svg.setAttribute('width', D.width); svg.setAttribute('height', D.height);
const NS = 'http://www.w3.org/2000/svg';
const el = (t,a)=>{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);return e;};
const defs = el('defs',{}); const m = el('marker',{id:'arrow',viewBox:'0 0 10 10',refX:'10',refY:'5',markerWidth:'7',markerHeight:'7',orient:'auto-start-reverse'});
m.appendChild(el('path',{d:'M0 0L10 5L0 10z',fill:'#596273'})); defs.appendChild(m); svg.appendChild(defs);
const pos = {}; D.nodes.forEach(n=>pos[n.id]=n);
const out = {}; D.edges.forEach(e=>{(out[e.s]=out[e.s]||[]).push(e.t)});
const edgeEls = D.edges.map(e=>{
  const a=pos[e.s], b=pos[e.t]; if(!a||!b) return null;
  const dx=(b.x-a.x)/2;
  const p = el('path',{d:`M${a.x+70} ${a.y} C ${a.x+70+dx} ${a.y}, ${b.x-70-dx} ${b.y}, ${b.x-70} ${b.y}`,class:'edge'+(e.inferred?' inferred':'')});
  p.dataset.s=e.s; p.dataset.t=e.t; p.dataset.inferred=e.inferred; svg.appendChild(p); return p;}).filter(Boolean);
const nodeEls = {};
D.nodes.forEach(n=>{
  const g = el('g',{class:'node',transform:`translate(${n.x},${n.y})`});
  g.appendChild(el('rect',{x:-70,y:-16,width:140,height:32,rx:8,fill:n.changed?'#ffd60a':(D.colors[n.type]||'#888'),stroke:n.changed?'#fff':'#0f1115','stroke-width':n.changed?2:1,opacity:.92}));
  const t = el('text',{'text-anchor':'middle',y:4}); t.textContent = n.name.length>20? n.name.slice(0,19)+'…':n.name;
  t.setAttribute('fill', '#111'); g.appendChild(t);
  g.addEventListener('click',()=>select(n.id));
  g.addEventListener('mousemove',ev=>{tip.style.display='block';tip.style.left=(ev.clientX+12)+'px';tip.style.top=(ev.clientY+12)+'px';tip.textContent=`${n.id} · ${n.type} · depth ${n.depth}${n.owner?' · owner '+n.owner:''}${n.profile?' · '+n.profile:''}`});
  g.addEventListener('mouseleave',()=>tip.style.display='none');
  svg.appendChild(g); nodeEls[n.id]=g;
});
const tip=document.getElementById('tip');
function select(id){
  const reach=new Set([id]); const st=[id];
  while(st.length){const c=st.pop(); (out[c]||[]).forEach(t=>{if(!reach.has(t)){reach.add(t);st.push(t)}})}
  D.nodes.forEach(n=>nodeEls[n.id].classList.toggle('dim',!reach.has(n.id)));
  edgeEls.forEach(p=>{const on=reach.has(p.dataset.s)&&reach.has(p.dataset.t);p.classList.toggle('dim',!on);p.classList.toggle('hl',on)});
  const n=pos[id]; document.getElementById('sel').innerHTML=`<b>${n.name}</b><br>${n.id}<br>type: ${n.type}<br>depth: ${n.depth}<br>${n.owner?'owner: '+n.owner+'<br>':''}${n.profile?'profile: '+n.profile+'<br>':''}downstream in view: ${reach.size-1}`;
}
document.getElementById('q').addEventListener('input',ev=>{const q=ev.target.value.toLowerCase();D.nodes.forEach(n=>nodeEls[n.id].classList.toggle('dim',q&&!(n.id.toLowerCase().includes(q)||n.name.toLowerCase().includes(q))))});
document.getElementById('inf').addEventListener('change',ev=>{edgeEls.forEach(p=>{if(p.dataset.inferred==='true')p.style.display=ev.target.checked?'':'none'})});
const lg=document.getElementById('legend'); Object.entries(D.colors).forEach(([k,v])=>{const s=document.createElement('span');s.innerHTML=`<i class="dot" style="background:${v}"></i>${k}`;lg.appendChild(s)});
const ow=document.getElementById('owners'); const oks=Object.keys(D.owners||{}); ow.innerHTML = oks.length? oks.map(k=>`<div><b>${k}</b>: ${D.owners[k].join(', ')}</div>`).join('') : '<i>no owners recorded</i>';
const ul=document.getElementById('tests'); (D.tests||[]).forEach(t=>{const li=document.createElement('li');li.textContent=t;ul.appendChild(li)});
</script></body></html>
"""
