"""A read-only local viewer for a graph: ``datagraph serve``.

The one thing a catalog gives you that a CLI does not is a place to *browse*. This is that place,
scaled to a library: a single-user, read-only web page served from the graph file by Python's own
HTTP server - no database, no accounts, no write path, no dependencies.

    datagraph serve --graph datagraph.json           ->  http://127.0.0.1:8765

Routes
    /                       search + asset browser (one self-contained page)
    /api/search?q=&type=&domain=&tag=&term=&owner=   ranked results (JSON)
    /api/node/<id>          context pack, lineage, columns, profile, usage (JSON)
    /api/report             hotspots, deprecated, failing tests, PII, domains, glossary (JSON)
    /api/model              the dimensional model (JSON)
    /lineage/<id>           the interactive lineage view (HTML)
    /graph                  the whole-graph view (HTML)

Safety: binds to 127.0.0.1 unless ``--host`` says otherwise (and then it warns), serves GET only,
never executes anything from the graph, escapes all rendered text, and re-reads the graph file when
it changes on disk so a rebuild in another terminal shows up on refresh.
"""

from __future__ import annotations

import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from .graph import ImpactGraph

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--ground:#f6f7f4;--surface:#fff;--surface2:#eef0ec;--ink:#14181c;--ink2:#3d4650;--muted:#6b7480;
      --rule:#dde1dc;--accent:#0e6f76;--accent-soft:#e2efef;--warn:#a75a17;--warn-soft:#f6ece1}
@media (prefers-color-scheme:dark){:root{--ground:#0e1216;--surface:#141a1f;--surface2:#1b2229;--ink:#e7ece9;
      --ink2:#c0c9c6;--muted:#8b968f;--rule:#262f36;--accent:#5bc6cb;--accent-soft:#122b2d;--warn:#e0a35f;--warn-soft:#2c2317}}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);font:15px/1.55 "Segoe UI",system-ui,sans-serif}
header{position:sticky;top:0;background:var(--surface);border-bottom:1px solid var(--rule);padding:.7rem 1.1rem;
       display:flex;gap:.8rem;align-items:center;z-index:5}
header b{font-size:1rem;letter-spacing:-.01em}
header .sub{color:var(--muted);font-size:.78rem;font-family:ui-monospace,Consolas,monospace}
input,select{font:inherit;padding:.42rem .6rem;border:1px solid var(--rule);border-radius:6px;
       background:var(--ground);color:var(--ink)}
input:focus,select:focus{outline:2px solid var(--accent);outline-offset:1px}
#q{flex:1;min-width:12rem}
main{display:grid;grid-template-columns:22rem minmax(0,1fr);gap:0;height:calc(100vh - 3.4rem)}
#list{overflow:auto;border-right:1px solid var(--rule);background:var(--surface)}
#detail{overflow:auto;padding:1.3rem 1.6rem}
.row{padding:.55rem .9rem;border-bottom:1px solid var(--rule);cursor:pointer}
.row:hover{background:var(--surface2)}
.row.sel{background:var(--accent-soft);border-left:3px solid var(--accent);padding-left:calc(.9rem - 3px)}
.row .n{font-weight:600}
.row .m{color:var(--muted);font-size:.76rem;font-family:ui-monospace,Consolas,monospace;
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip{display:inline-block;font-size:.68rem;font-family:ui-monospace,Consolas,monospace;padding:.1rem .4rem;
      border-radius:999px;border:1px solid var(--rule);color:var(--ink2);background:var(--surface2);margin-right:.25rem}
.chip.a{color:var(--accent);border-color:var(--accent);background:var(--accent-soft)}
.chip.w{color:var(--warn);border-color:var(--warn);background:var(--warn-soft)}
pre{background:var(--surface);border:1px solid var(--rule);border-left:3px solid var(--accent);border-radius:6px;
    padding:.8rem 1rem;overflow:auto;font:12.5px/1.5 ui-monospace,Consolas,monospace;white-space:pre-wrap}
h2{margin:0 0 .2rem;font-size:1.3rem}
.links a{color:var(--accent);margin-right:.9rem;font-size:.85rem}
.empty{color:var(--muted);padding:2rem 1rem;text-align:center}
</style></head><body>
<header>
  <b>datagraph</b><span class="sub" id="stats"></span>
  <input id="q" placeholder="Search tables, models, columns, owners, terms, domains&hellip;" autofocus>
  <select id="type"><option value="">any type</option></select>
  <select id="domain"><option value="">any domain</option></select>
  <a class="chip a" href="/graph" target="_blank">whole graph</a>
  <a class="chip" href="/api/report" target="_blank">report JSON</a>
</header>
<main>
  <div id="list"><div class="empty">Type to search, or press Enter for everything.</div></div>
  <div id="detail"><div class="empty">Select an asset.</div></div>
</main>
<script>
const $=s=>document.querySelector(s);
let rows=[],sel=null;
async function boot(){
  const r=await (await fetch('/api/report')).json();
  $('#stats').textContent=r.nodes+' nodes \\u00b7 '+r.edges+' edges';
  for(const t of r.types){const o=document.createElement('option');o.value=t;o.textContent=t;$('#type').append(o);}
  for(const d of r.domains){const o=document.createElement('option');o.value=d;o.textContent=d;$('#domain').append(o);}
  search();
}
async function search(){
  const p=new URLSearchParams({q:$('#q').value,type:$('#type').value,domain:$('#domain').value,limit:200});
  rows=await (await fetch('/api/search?'+p)).json();
  const list=$('#list');
  if(!rows.length){list.innerHTML='<div class="empty">No matches.</div>';return;}
  list.innerHTML='';
  rows.forEach((row,i)=>{
    const el=document.createElement('div');el.className='row';el.tabIndex=0;
    el.innerHTML='<div class="n"></div><div class="m"></div>';
    el.querySelector('.n').textContent=row.name;
    el.querySelector('.m').textContent=row.type+(row.domain?' \\u00b7 '+row.domain:'')+(row.owner?' \\u00b7 '+row.owner:'')
      +(row.deprecated?' \\u00b7 DEPRECATED':'');
    el.onclick=()=>open_(i);el.onkeydown=e=>{if(e.key==='Enter')open_(i)};
    list.append(el);
  });
}
async function open_(i){
  document.querySelectorAll('.row').forEach((e,j)=>e.classList.toggle('sel',i===j));
  sel=rows[i];
  const d=await (await fetch('/api/node/'+encodeURIComponent(sel.id))).json();
  const chips=[];
  if(d.domain)chips.push('<span class="chip a">'+esc(d.domain)+'</span>');
  (d.terms||[]).forEach(t=>chips.push('<span class="chip a">'+esc(t)+'</span>'));
  (d.tags||[]).forEach(t=>chips.push('<span class="chip">'+esc(t)+'</span>'));
  if(d.deprecated)chips.push('<span class="chip w">deprecated</span>');
  if(d.usage)chips.push('<span class="chip">'+esc(d.usage)+'</span>');
  $('#detail').innerHTML='<h2>'+esc(d.name)+'</h2>'
    +'<div class="m" style="color:var(--muted);font-family:ui-monospace,monospace;font-size:.8rem">'+esc(d.id)+'</div>'
    +'<p>'+chips.join(' ')+'</p>'
    +'<p class="links"><a href="/lineage/'+encodeURIComponent(d.id)+'" target="_blank">interactive lineage</a>'
    +'<a href="/api/node/'+encodeURIComponent(d.id)+'" target="_blank">JSON</a></p>'
    +'<pre>'+esc(d.context)+'</pre>';
}
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
$('#q').oninput=debounce(search,180);$('#type').onchange=search;$('#domain').onchange=search;
function debounce(f,ms){let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>f(...a),ms)}}
boot();
</script></body></html>
"""


class _GraphCache:
    """Reload the graph file whenever it changes on disk."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._graph: Optional[ImpactGraph] = None
        self._mtime: float = -1.0
        self._lock = threading.Lock()

    def get(self) -> ImpactGraph:
        with self._lock:
            if not self.path.exists():
                raise FileNotFoundError(f"graph file '{self.path}' not found - run 'datagraph build' first")
            mtime = self.path.stat().st_mtime
            if self._graph is None or mtime != self._mtime:
                self._graph = ImpactGraph.load(self.path)
                self._mtime = mtime
            return self._graph


def _handler(cache: _GraphCache, title: str):
    from .analysis.discovery import pii_report, search as _search
    from .analysis.modeling import star_schema, to_mermaid
    from .html_report import render_graph_html, render_lineage_html
    from .knowledge import context as _context
    from .metadata import deprecated_assets, domains as _domains, glossary_index
    from .profiling import profile_summary
    from .usage import unused_tables, usage_summary

    class Handler(BaseHTTPRequestHandler):
        server_version = "datagraph"

        def log_message(self, fmt, *args):  # quiet by default; the CLI prints what matters
            pass

        # ---------------------------------------------------------- helpers
        def _send(self, body: str, content_type: str = "text/html; charset=utf-8", status: int = 200) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, payload, status: int = 200) -> None:
            self._send(json.dumps(payload, default=str), "application/json; charset=utf-8", status)

        # ------------------------------------------------------------ routes
        def do_GET(self) -> None:  # noqa: N802 - required name
            parsed = urlparse(self.path)
            route = parsed.path
            query = parse_qs(parsed.query)
            try:
                graph = cache.get()
            except FileNotFoundError as exc:
                self._json({"error": str(exc)}, 404)
                return

            if route == "/":
                self._send(_PAGE.replace("__TITLE__", html.escape(title)))
            elif route == "/api/search":
                rows = _search(
                    graph,
                    query.get("q", [""])[0],
                    node_type=query.get("type", [""])[0] or None,
                    domain=query.get("domain", [""])[0] or None,
                    tag=query.get("tag", [""])[0] or None,
                    term=query.get("term", [""])[0] or None,
                    owner=query.get("owner", [""])[0] or None,
                    include_columns=query.get("columns", ["0"])[0] in ("1", "true"),
                    limit=int(query.get("limit", ["50"])[0]),
                )
                self._json(rows)
            elif route.startswith("/api/node/"):
                node_id = unquote(route[len("/api/node/"):])
                node = graph.get_node(node_id) or graph.resolve(node_id)
                if node is None:
                    self._json({"error": f"no node matches '{node_id}'"}, 404)
                    return
                self._json({
                    "id": node.id, "name": node.name, "type": node.type.value, "owner": node.owner,
                    "domain": node.meta.get("domain"), "terms": node.meta.get("terms") or [],
                    "tags": node.meta.get("tags") or [], "deprecated": bool(node.meta.get("deprecated")),
                    "profile": profile_summary(node), "usage": usage_summary(node),
                    "context": _context(graph, node.id),
                })
            elif route == "/api/report":
                self._json({
                    "nodes": len(graph), "edges": len(graph.edges()),
                    "types": sorted({n.type.value for n in graph.nodes()}),
                    "domains": sorted(_domains(graph)),
                    "hotspots": graph.hotspots(top=15),
                    "deprecated": deprecated_assets(graph)[:20],
                    "unused_tables": unused_tables(graph)[:20],
                    "glossary": glossary_index(graph),
                    "sensitive_data": pii_report(graph),
                })
            elif route == "/api/model":
                model = star_schema(graph)
                model["mermaid"] = to_mermaid(model)
                model.pop("classification", None)
                self._json(model)
            elif route.startswith("/lineage/"):
                node_id = unquote(route[len("/lineage/"):])
                node = graph.get_node(node_id) or graph.resolve(node_id)
                if node is None:
                    self._json({"error": f"no node matches '{node_id}'"}, 404)
                    return
                self._send(render_lineage_html(graph, node.id, title=f"Lineage of {node.name}"))
            elif route == "/graph":
                self._send(render_graph_html(graph, title=title))
            else:
                self._json({"error": "not found", "routes": ["/", "/api/search", "/api/node/<id>",
                                                             "/api/report", "/api/model", "/lineage/<id>", "/graph"]}, 404)

    return Handler


def create_server(graph_path: str, host: str = "127.0.0.1", port: int = 8765,
                  title: str = "datagraph") -> Tuple[ThreadingHTTPServer, str]:
    """Build (but do not start) the viewer. Returns (server, url)."""
    cache = _GraphCache(graph_path)
    server = ThreadingHTTPServer((host, port), _handler(cache, title))
    shown = host if host not in ("0.0.0.0", "::") else "127.0.0.1"
    return server, f"http://{shown}:{server.server_address[1]}"


def serve(graph_path: str, host: str = "127.0.0.1", port: int = 8765, title: str = "datagraph",
          open_browser: bool = False, log=print) -> None:
    """Run the viewer until interrupted."""
    server, url = create_server(graph_path, host, port, title)
    if host not in ("127.0.0.1", "localhost", "::1"):
        log(f"warning: binding to {host} exposes this graph to your network - it has no authentication")
    log(f"datagraph viewer on {url}   (read-only, Ctrl+C to stop)")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopped")
    finally:
        server.server_close()
