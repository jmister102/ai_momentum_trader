"""
dashboard.py — tiny zero-dependency status dashboard for the AI Momentum Trader.

Reads status.json (written by run_trader.py each scan) and serves an
auto-refreshing web page. Runs as its own process so it can never disturb the
trading loop. Python stdlib only.

    py dashboard.py                 # serve on http://127.0.0.1:8787
    py dashboard.py --port 9000
    py dashboard.py --host 0.0.0.0   # expose on the LAN (be careful)

Then open the URL in any browser. The page polls /status.json every few seconds:
  • RUNNING / STALE / STOPPED badge (freshness of last poll)
  • mode (DRY-RUN / LIVE), last poll time + age, session, active scanner
  • top 5 names on the % change list, each flagged if the account holds it
    (and tagged BOT if it's a bot-opened position)
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import status as status_io

STALE_SECONDS = 120  # last poll older than this (while "running") → STALE

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>AI Momentum Trader</title>
<style>
 :root{color-scheme:dark}
 body{background:#0e1116;color:#e6e6e6;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;padding:24px}
 h1{font-size:18px;margin:0 0 16px;font-weight:600}
 .badge{display:inline-block;padding:4px 12px;border-radius:14px;font-weight:700;font-size:13px;letter-spacing:.5px}
 .run{background:#0d3b1e;color:#26a69a;border:1px solid #26a69a}
 .stale{background:#3b2f0d;color:#ffb74d;border:1px solid #ffb74d}
 .stop{background:#3b0d0d;color:#ef5350;border:1px solid #ef5350}
 .grid{display:flex;gap:28px;flex-wrap:wrap;margin:14px 0 22px}
 .kv{font-size:13px}.kv b{color:#9aa4af;font-weight:500;display:block;font-size:11px;text-transform:uppercase;letter-spacing:.5px}
 table{border-collapse:collapse;width:100%;max-width:640px;margin-top:6px}
 th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #20262e}
 th{color:#9aa4af;font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.5px}
 td.num{text-align:right;font-variant-numeric:tabular-nums}
 .up{color:#26a69a}.held{color:#ffb74d;font-weight:700}
 .tag{font-size:10px;background:#1d2a44;color:#7eb6ff;border-radius:4px;padding:1px 5px;margin-left:6px}
 .chips span{display:inline-block;background:#1a2029;border:1px solid #2a3340;border-radius:10px;padding:2px 9px;margin:2px;font-size:12px}
 .muted{color:#6b7682}
 footer{margin-top:26px;color:#6b7682;font-size:12px}
</style></head>
<body>
 <h1>AI Momentum Trader <span id="badge" class="badge stop">…</span></h1>
 <div class="grid">
   <div class="kv"><b>Mode</b><span id="mode">—</span></div>
   <div class="kv"><b>Last poll</b><span id="poll">—</span></div>
   <div class="kv"><b>Session</b><span id="sess">—</span></div>
   <div class="kv"><b>Scanner</b><span id="scan">—</span></div>
   <div class="kv"><b>Trades today</b><span id="trades">—</span></div>
   <div class="kv"><b>Per-trade</b><span id="pertrade">—</span></div>
   <div class="kv"><b>PID</b><span id="pid">—</span></div>
 </div>
 <h1 style="font-size:14px">Top 5 % movers <span class="muted">(✓ = account holds it)</span></h1>
 <table><thead><tr><th>#</th><th>Symbol</th><th class="num">% chg</th><th class="num">Last</th><th class="num">Prior</th><th>Held</th></tr></thead>
 <tbody id="movers"><tr><td colspan="6" class="muted">waiting for data…</td></tr></tbody></table>
 <h1 style="font-size:14px;margin-top:22px">Orders <span class="muted">(bot orders, filled or not)</span></h1>
 <table style="max-width:780px"><thead><tr><th>Symbol</th><th>Side</th><th>Type</th><th class="num">Limit</th><th class="num">Shares</th><th class="num">Filled</th><th class="num">Avg fill</th><th>Status</th></tr></thead>
 <tbody id="orders"><tr><td colspan="8" class="muted">no orders yet</td></tr></tbody></table>
 <h1 style="font-size:14px;margin-top:22px">Triggered today</h1>
 <div id="trig" class="chips muted">—</div>
 <footer id="foot">polling /status.json…</footer>
<script>
function ago(iso){ if(!iso) return [null,"—"]; const s=Math.round((Date.now()-Date.parse(iso))/1000);
  if(isNaN(s)) return [null,"—"]; const t = s<60?s+"s ago":Math.round(s/60)+"m ago"; return [s,t]; }
function fmtPct(p){ return (p>=0?"+":"")+(p*100).toFixed(0)+"%"; }
async function tick(){
  let d=null;
  try{ d=await (await fetch('/status.json',{cache:'no-store'})).json(); }
  catch(e){ document.getElementById('foot').textContent='cannot reach server'; return; }
  const badge=document.getElementById('badge');
  const [agesec,agetxt]=ago(d.last_poll);
  let cls='stop',label='STOPPED';
  if(d.running){ if(agesec!==null && agesec>__STALE__){cls='stale';label='STALE';} else {cls='run';label='RUNNING';} }
  badge.className='badge '+cls; badge.textContent=label;
  document.getElementById('mode').textContent=d.mode||'—';
  document.getElementById('poll').textContent=(d.last_poll? d.last_poll.replace('T',' '):'—')+'  ('+agetxt+')';
  document.getElementById('sess').textContent=d.session||'—';
  document.getElementById('scan').textContent=d.scanner||'—';
  const tt=(d.trades_today!=null?d.trades_today:'—')+' / '+(d.max_trades!=null?d.max_trades:'—');
  const tEl=document.getElementById('trades'); tEl.textContent=tt;
  tEl.className=(d.max_trades!=null && d.trades_today>=d.max_trades)?'held':'';
  document.getElementById('pertrade').textContent=(d.per_trade!=null?('$'+d.per_trade):'—');
  document.getElementById('pid').textContent=d.pid||'—';
  const mv=document.getElementById('movers');
  if(d.movers && d.movers.length){ mv.innerHTML=d.movers.map((m,i)=>{
     const held=m.held?('<span class="held">✓'+(m.held_shares?(' '+m.held_shares):'')+'</span>'+(m.bot?'<span class="tag">BOT</span>':'')):'<span class="muted">—</span>';
     return '<tr><td>'+(m.rank||i+1)+'</td><td><b>'+m.symbol+'</b></td><td class="num up">'+fmtPct(m.pct)+
       '</td><td class="num">'+(m.last!=null?m.last:'—')+'</td><td class="num">'+(m.prior!=null?m.prior:'—')+'</td><td>'+held+'</td></tr>';
   }).join(''); }
  else { mv.innerHTML='<tr><td colspan="6" class="muted">no movers reported this cycle</td></tr>'; }
  const od=document.getElementById('orders');
  if(d.orders && d.orders.length){ od.innerHTML=d.orders.map(o=>{
     const fillcls=(o.filled>0 && o.filled>=o.qty)?'up':(o.filled>0?'held':'muted');
     return '<tr><td><b>'+o.symbol+'</b></td><td>'+(o.action||'')+'</td><td>'+(o.type||'')+
       '</td><td class="num">'+(o.limit!=null?o.limit:'—')+'</td><td class="num">'+(o.qty!=null?o.qty:'—')+
       '</td><td class="num '+fillcls+'">'+(o.filled!=null?o.filled:0)+'</td><td class="num">'+(o.avg_fill!=null?o.avg_fill:'—')+
       '</td><td>'+(o.status||'')+(o.reason?(' <span class="held" title="'+String(o.reason).replace(/"/g,"&quot;")+'">⚠ '+o.reason+'</span>'):'')+'</td></tr>';
   }).join(''); }
  else { od.innerHTML='<tr><td colspan="8" class="muted">no orders yet</td></tr>'; }
  const tg=document.getElementById('trig');
  tg.innerHTML=(d.triggered_today&&d.triggered_today.length)? d.triggered_today.map(s=>'<span>'+s+'</span>').join('') : '<span class="muted">none yet</span>';
  document.getElementById('foot').textContent='updated '+new Date().toLocaleTimeString();
}
tick(); setInterval(tick,4000);
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/status.json"):
            snap = status_io.read_status() or {"running": False}
            self._send(200, json.dumps(snap), "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE.replace("__STALE__", str(STALE_SECONDS)), "text/html")
        else:
            self._send(404, "not found", "text/plain")

    def log_message(self, *a):  # silence per-request console noise
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description="AI Momentum Trader status dashboard")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Dashboard → http://{args.host}:{args.port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
