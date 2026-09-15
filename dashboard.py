#!/usr/bin/env python3
"""
Everything the bot is doing, on one page.

    python dashboard.py             # live at http://127.0.0.1:8787, this machine only
    python dashboard.py --share     # reachable off-machine, password required
    python dashboard.py --snapshot  # writes dashboard.html, self-contained

Read-only. It parses logs and reads the state file; it never sends a transaction
and never touches the RPC. That last part is deliberate: the shared QuickNode
endpoint is already the binding constraint on trading -- three of one day's six
buy failures were detection timeouts with a 429 alongside them -- so a dashboard
that polled it for live prices would cost real trades. Open-position state comes
from the "Tracking $X: Multiplier" line the bot already writes.
"""
import base64
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dashboard_data import collect, bot_process, source_stamp

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8787

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Robinhood Bot Activity</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
:root{
  --ground:#FAF8F2;--surface:#FFFFFF;--sunk:#F3EFE5;--line:#E2DACA;--line-soft:#EDE7DA;
  --text:#1D1A14;--muted:#6F6658;--faint:#948A79;--accent:#8A5512;
  --win:#3D6F42;--loss:#9C3B2B;--warn:#8A6D1B;
  --sans:"Archivo","Segoe UI",system-ui,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ground:#14130F;--surface:#1C1A15;--sunk:#191710;--line:#2E2A22;--line-soft:#252219;
  --text:#EDE8DE;--muted:#968E7E;--faint:#6E6759;--accent:#D9922E;
  --win:#7FB878;--loss:#D4705D;--warn:#D6B15C;
}}
:root[data-theme="dark"]{
  --ground:#14130F;--surface:#1C1A15;--sunk:#191710;--line:#2E2A22;--line-soft:#252219;
  --text:#EDE8DE;--muted:#968E7E;--faint:#6E6759;--accent:#D9922E;
  --win:#7FB878;--loss:#D4705D;--warn:#D6B15C;
}
*{box-sizing:border-box}
body{background:var(--ground);color:var(--text);font-family:var(--sans);font-size:15px;
     line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:78rem;margin:0 auto;padding-inline:20px;padding-block:32px 64px}
h1,h2{margin:0;text-wrap:balance}
h1{font-size:1.65rem;font-weight:700;letter-spacing:-.02em}
h2{font-size:1.05rem;font-weight:600}
.mast{display:flex;flex-wrap:wrap;gap:14px 24px;align-items:flex-end;justify-content:space-between;
      padding-bottom:18px;border-bottom:2px solid var(--text)}
.sub{font-family:var(--mono);font-size:.75rem;color:var(--muted);margin-top:6px}
.pill{display:inline-flex;align-items:center;gap:7px;font-family:var(--mono);font-size:.7rem;
      font-weight:500;text-transform:uppercase;letter-spacing:.09em;border-radius:999px;
      padding:5px 12px;border:1px solid currentColor;white-space:nowrap}
.pill.on{color:var(--win)} .pill.off{color:var(--loss)}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor;flex:none}
@media (prefers-reduced-motion:no-preference){.pill.on .dot{animation:p 2.4s ease-in-out infinite}
@keyframes p{0%,100%{opacity:1}50%{opacity:.3}}}
section{margin-top:38px}
.eyebrow{font-family:var(--mono);font-size:.68rem;font-weight:600;text-transform:uppercase;
         letter-spacing:.13em;color:var(--accent);display:block;margin-bottom:5px}
.note{color:var(--muted);font-size:.86rem;max-width:70ch;margin:7px 0 0}
.tiles{display:grid;grid-template-columns:repeat(6,1fr);gap:1px;
       margin-top:18px;background:var(--line);border:1px solid var(--line);border-radius:6px;overflow:hidden}
@media(max-width:900px){.tiles{grid-template-columns:repeat(4,1fr)}}
@media(max-width:560px){.tiles{grid-template-columns:repeat(2,1fr)}}
.tile{background:var(--surface);padding:13px 15px}
.tile .k{font-family:var(--mono);font-size:.64rem;text-transform:uppercase;letter-spacing:.1em;
         color:var(--faint);display:block}
.tile .v{font-family:var(--mono);font-variant-numeric:tabular-nums;font-size:1.3rem;
         font-weight:600;margin-top:3px;display:block}
.scroll{overflow-x:auto;margin-top:16px;border:1px solid var(--line);border-radius:6px}
.tall{max-height:30rem;overflow-y:auto}
table{width:100%;border-collapse:collapse;background:var(--surface)}
th,td{padding:8px 13px;text-align:left;border-bottom:1px solid var(--line-soft);white-space:nowrap}
tr:last-child td{border-bottom:0}
thead th{position:sticky;top:0;font-family:var(--mono);font-size:.64rem;font-weight:600;
         text-transform:uppercase;letter-spacing:.1em;color:var(--faint);background:var(--sunk);
         border-bottom:1px solid var(--line);z-index:1}
td{font-size:.86rem}
.m{font-family:var(--mono)}
.n{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right}
.win{color:var(--win)} .loss{color:var(--loss)} .warn{color:var(--warn)} .dim{color:var(--faint)}
.tag{font-family:var(--mono);font-size:.6rem;text-transform:uppercase;letter-spacing:.07em;
     border:1px solid var(--line);border-radius:3px;padding:1px 5px;color:var(--faint)}
.wrapcell{white-space:normal;min-width:18rem;max-width:34rem}
.alert{margin-top:20px;background:var(--surface);border:1px solid var(--loss);
       border-left:3px solid var(--loss);border-radius:0 6px 6px 0;padding:14px 18px}
.alert b{color:var(--loss)}
.alert p{margin:6px 0 0;font-size:.88rem;color:var(--muted)}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.bar button{font-family:var(--mono);font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;
  background:var(--surface);color:var(--muted);border:1px solid var(--line);border-radius:4px;
  padding:6px 11px;cursor:pointer}
.bar button[aria-pressed="true"]{background:var(--accent);color:var(--ground);border-color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.foot{margin-top:48px;padding-top:16px;border-top:1px solid var(--line);
      font-family:var(--mono);font-size:.72rem;color:var(--faint);line-height:1.75}
@media(max-width:560px){th,td{padding:7px 10px}.wrap{padding-block:24px 48px}}
</style></head><body>
<div class="wrap">
  <header class="mast">
    <div><h1>Robinhood Bot Activity</h1><div class="sub" id="sub">loading…</div></div>
    <span class="pill off" id="status"><span class="dot"></span>—</span>
  </header>
  <div id="app"></div>
  <p class="foot" id="foot"></p>
</div>
<script>
const INLINE = __DATA__;
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const usd = (n) => (n >= 0 ? '+$' : '-$') + Math.abs(Number(n || 0)).toFixed(2);
const cls = (n) => Number(n) >= 0 ? 'win' : 'loss';
let FEED_FILTER = 'all';
let LAST_SIG = null;

// The bot writes to bot.log constantly, so "the file changed" is not a reason to
// rebuild the DOM -- doing that every 5s threw away the reader's scroll position
// mid-table. Re-render only when something that is actually ON the page moved.
const signature = (d) => JSON.stringify([
  (d.generated_at || '').slice(0, 10),
  d.days, d.totals, d.bot, d.open_positions, d.entry_budget, d.activity,
  (d.trades || []).length, (d.swaps || []).length,
  (d.signals || []).length, (d.rejections || []).length, (d.errors || []).length,
]);

function table(head, rows, tall) {
  if (!rows.length) return '<p class="note">Nothing recorded.</p>';
  return `<div class="scroll${tall ? ' tall' : ''}"><table><thead><tr>${
    head.map(h => `<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${
    rows.join('')}</tbody></table></div>`;
}

function render(d) {
  document.getElementById('sub').textContent =
    `generated ${d.generated_at} · ${d.ledger_events} ledger events · code ${d.code_version || 'unknown'}`;
  const st = document.getElementById('status');
  const on = d.bot?.running;
  st.className = 'pill ' + (on === true ? 'on' : on === false ? 'off' : '');
  st.innerHTML = `<span class="dot" aria-hidden="true"></span>${on === true ? 'Bot process running' : on === false ? 'Bot not running' : 'Bot status unavailable'}`;

  const label = (d.generated_at || '').slice(0, 10);
  const days = Object.keys(d.days || {});
  const today = (d.days || {})[label] || {};
  const t = d.totals || {};
  const out = [];

  out.push(`<section><span class="eyebrow">${esc(label)}</span><h2>Today</h2>
    <div class="tiles">
      ${[['channel msgs', today.channel_messages], ['signals', today.signals],
         ['buys tried', today.buys_attempted], ['opened', today.opened],
         ['closed', today.closed], ['failed', today.buys_failed],
         ['skipped', today.skipped], ['TP1 / TP2', `${today.tp1 || 0}/${today.tp2 || 0}`],
         ['rate limits', today.rpc_limited], ['errors', today.errors]]
        .map(([k, v]) => `<div class="tile"><span class="k">${k}</span><span class="v">${v ?? 0}</span></div>`).join('')}
      <div class="tile"><span class="k">realised</span>
        <span class="v ${cls(today.pnl_usd)}">${usd(today.pnl_usd)}</span></div>
      <div class="tile"><span class="k">W / L</span><span class="v">${today.wins || 0}/${today.losses || 0}</span></div>
    </div>
    <p class="note">Lifetime, from the bot's own state file:
      <b>${t.total_trades ?? 0}</b> trades, <b>${t.wins ?? 0}W / ${t.losses ?? 0}L</b>,
      <b class="${cls(t.total_pnl_usd)}">${usd(t.total_pnl_usd)}</b>,
      saved strategy balance <b>$${Number(t.balance_usd || 0).toFixed(2)}</b> · state ${esc(t.state || '—')}.
      This is bookkeeping, not a live wallet valuation. Historical P&amp;L includes estimates;
      newer trades record receipt gas and their cost basis.</p>
  </section>`);

  const budget = d.entry_budget || {};
  const activity = d.activity || {};
  if (budget.known && (budget.below_requirement || budget.halted)) {
    out.push(`<div class="alert" role="status"><b>${budget.halted ? 'Strategy is halted.' : 'Entry budget is below the configured requirement.'}</b>
      <p>Based on saved strategy state and configured limits:
      $${budget.safety_floor_usd.toFixed(2)} floor + $${budget.next_stake_usd.toFixed(2)} next stake +
      $${budget.gas_reserve_usd.toFixed(2)} gas reserve + $${budget.reserved_stake_usd.toFixed(2)} reserved for open positions
      = <b>$${budget.required_balance_usd.toFixed(2)} required</b>.
      Saved strategy balance: <b>$${budget.balance_usd.toFixed(2)}</b>${budget.below_requirement ?
        `; shortfall: <b>$${budget.shortfall_usd.toFixed(2)}</b>` : ''}.
      Raising the floor increases the required balance. Stock reuse does not bypass this budget check.
      ${budget.halted ? 'The halted state also prevents new entries.' : ''}</p></div>`);
  }
  out.push(`<section><span class="eyebrow">Process and recorded activity</span><h2>Current session</h2>
    <p class="note">Last recorded activity: <b>${esc(activity.last_activity_at || 'unavailable')}</b>.
      ${activity.started_at ? `Started ${esc(activity.started_at)} · ${activity.session_signal_evaluations} signal evaluations ·
        ${activity.session_floor_refusals} floor refusals since startup.` : 'Startup time unavailable.'}</p>
    <p class="note">${activity.native_wallet_usd != null ?
      `Last logged native ETH balance: <b>$${Number(activity.native_wallet_usd).toFixed(2)}</b> at ${esc(activity.native_wallet_at)}.
       This excludes stock tokens and has not been refreshed on-chain by the dashboard.` :
      'No native wallet balance recorded.'}</p>
    ${activity.last_floor_refusal_at ? `<p class="note">Most recent recorded floor refusal:
      ${esc(activity.last_floor_refusal_at)}. Historical refusals do not establish that a new call was rejected.</p>` : ''}
  </section>`);

  const open = d.open_positions || [];
  out.push(`<section><span class="eyebrow">Live</span><h2>Open positions</h2>
    ${table(['Ticker', 'Contract', 'Stake', 'Tokens', 'Multiplier', 'Peak', 'Held', 'Buy tx'],
      open.map(p => { const l = p.live || {}; return `<tr>
        <td class="m">$${esc(p.ticker)}</td>
        <td class="m dim">${esc((p.contract_address || '').slice(0, 12))}…</td>
        <td class="n">$${Number(p.stake_usd || 0).toFixed(2)}</td>
        <td class="n">${Number(p.tokens_bought || 0).toLocaleString()}</td>
        <td class="n ${l.mult >= 1 ? 'win' : 'loss'}">${l.mult ? l.mult + '×' : '—'}</td>
        <td class="n">${l.peak ? l.peak + '×' : '—'}</td>
        <td class="n">${l.minutes ? l.minutes + 'm' : '—'}</td>
        <td class="m dim">${esc((p.tx_hash_buy || '—').slice(0, 12))}</td></tr>`; }))}
    <p class="note">Multiplier and hold time are read from the bot's own tracking line,
      not from a price lookup — the dashboard makes no RPC calls.</p>
  </section>`);

  const trades = (d.trades || []).filter(x => x.outcome === 'closed').reverse();
  out.push(`<section><span class="eyebrow">Ledger</span><h2>Closed trades — ${trades.length}</h2>
    ${table(['Closed', 'Ticker', 'Result', 'Stake', 'Tokens', 'Exits', 'P&L', 'Balance', 'Source'],
      trades.map(x => `<tr>
        <td class="m dim">${esc(x.closed_day || '')} ${esc(x.closed_at || '')}</td>
        <td class="m">$${esc(x.ticker)}</td>
        <td class="${x.is_win ? 'win' : 'loss'}">${x.is_win ? 'Win' : 'Loss'}</td>
        <td class="n">$${Number(x.stake_usd || 0).toFixed(2)}</td>
        <td class="n">${Number(x.tokens_bought || 0).toLocaleString()}</td>
        <td class="m dim">${(x.tranches || []).map(r => r.rung).join(' ') || '—'}</td>
        <td class="n ${cls(x.pnl_usd)}">${usd(x.pnl_usd)}</td>
        <td class="n dim">$${Number(x.balance_after || 0).toFixed(2)}</td>
        <td>${x.source === 'ledger' ? '<span class="tag">full</span>'
              : '<span class="tag">rebuilt</span>'}</td></tr>`), true)}
    <p class="note"><b>rebuilt</b> = reconstructed from text logs before the event ledger
      existed. Those rows have no entry price and no transaction hashes, because the bot
      never wrote them down. Rows marked <b>full</b> are complete.</p>
  </section>`);

  const fails = (d.failures || []).reverse();
  out.push(`<section><span class="eyebrow">Rejections</span><h2>Buys that failed — ${fails.length}</h2>
    ${table(['When', 'Ticker', 'Stake', 'Reason', 'Verdict'],
      fails.map(x => `<tr>
        <td class="m dim">${esc(x.day || '')} ${esc(x.opened_at || '')}</td>
        <td class="m">$${esc(x.ticker)}</td>
        <td class="n">$${Number(x.stake_usd || 0).toFixed(2)}</td>
        <td class="wrapcell">${esc(x.failure_reason || 'unknown')}</td>
        <td class="${x.correct_refusal ? 'win' : 'warn'}">${
          x.correct_refusal ? 'correct refusal' : 'investigate'}</td></tr>`), true)}
    <p class="note">A refusal is the bot declining to trade something it could not price or
      route — that is the slippage guard working, not a defect.</p>
  </section>`);

  const rej = (d.rejections || []).reverse();
  out.push(`<section><span class="eyebrow">Filtered</span><h2>Calls not traded — ${rej.length}</h2>
    ${table(['When', 'Ticker', 'Reason'],
      rej.map(x => `<tr><td class="m dim">${esc(x.day)} ${esc(x.time)}</td>
        <td class="m">$${esc(x.ticker)}</td>
        <td class="wrapcell">${esc(x.reason || 'No contract address identified')}</td></tr>`), true)}
  </section>`);

  const swaps = (d.swaps || []).reverse();
  out.push(`<section><span class="eyebrow">On-chain</span><h2>Swaps — ${swaps.length}</h2>
    ${table(['When', 'Venue', 'Token', 'Token Δ', 'ETH Δ', 'Latency', 'Status', 'Tx'],
      swaps.map(x => `<tr>
        <td class="m dim">${esc(x.day)} ${esc(x.time)}</td>
        <td class="m">${esc((x.venue || '').trim())}</td>
        <td class="m dim">${esc((x.address || '').slice(0, 12))}…</td>
        <td class="n ${String(x.token_delta).startsWith('-') ? 'loss' : 'win'}">${esc(x.token_delta)}</td>
        <td class="n">${esc(x.quote_delta)}</td>
        <td class="n dim">${Number(x.ms || 0).toLocaleString()}ms</td>
        <td class="${x.status === '1' ? 'win' : 'loss'}">${esc(x.status)}</td>
        <td class="m dim">${esc(String(x.tx || '').slice(0, 14))}</td></tr>`), true)}
    <td></td><p class="note">Every broadcast the bot made, buy and sell. A sell shows a
      negative token delta. Status 1 is a mined, successful transaction.</p>
  </section>`);

  const feed = buildFeed(d);
  out.push(`<section><span class="eyebrow">Everything</span><h2>Activity feed</h2>
    <div class="bar">${['all', 'signal', 'buy', 'swap', 'close', 'error']
      .map(f => `<button data-f="${f}" aria-pressed="${FEED_FILTER === f}">${f}</button>`).join('')}</div>
    ${table(['When', 'Kind', 'Detail'], feed
      .filter(r => FEED_FILTER === 'all' || r.group === FEED_FILTER)
      .slice(0, 400)
      .map(r => `<tr><td class="m dim">${esc(r.day)} ${esc(r.time)}</td>
        <td class="m ${r.group === 'error' ? 'loss' : ''}">${esc(r.kind)}</td>
        <td class="wrapcell">${esc(r.detail)}</td></tr>`), true)}
    <p class="note">Newest first, capped at 400 rows.</p>
  </section>`);

  const dayRows = days.slice().reverse().map(k => { const c = d.days[k]; return `<tr>
    <td class="m">${esc(k)}</td>
    <td class="n">${c.channel_messages || 0}</td><td class="n">${c.signals || 0}</td>
    <td class="n">${c.buys_attempted || 0}</td><td class="n">${c.opened || 0}</td>
    <td class="n">${c.closed || 0}</td><td class="n">${c.buys_failed || 0}</td>
    <td class="n">${c.tp1 || 0}/${c.tp2 || 0}</td>
    <td class="n dim">${c.rpc_limited || 0}</td>
    <td class="n ${cls(c.pnl_usd)}">${usd(c.pnl_usd)}</td></tr>`; });
  out.push(`<section><span class="eyebrow">History</span><h2>By day</h2>
    ${table(['Day', 'Msgs', 'Signals', 'Buys', 'Opened', 'Closed', 'Failed', 'TP1/TP2', '429s', 'P&L'], dayRows)}
  </section>`);

  const y = window.scrollY;
  document.getElementById('app').innerHTML = out.join('');
  window.scrollTo(0, y);
  document.querySelectorAll('.bar button').forEach(b =>
    b.addEventListener('click', () => { FEED_FILTER = b.dataset.f; LAST_SIG = null; render(d); }));
  document.getElementById('foot').innerHTML =
    `Read-only. Parses logs/bot.log* and logs/events.jsonl; no RPC calls, no transactions.<br>` +
    `Historical returns include estimates; newer entries record receipt gas and their cost basis.`;
}

function buildFeed(d) {
  const rows = [];
  const push = (r, group, kind, detail) =>
    rows.push({ day: r.day, time: r.time, group, kind, detail });
  (d.signals || []).forEach(r => push(r, 'signal', 'signal', `$${r.ticker} (${r.dex || ''})`));
  (d.rejections || []).forEach(r => push(r, 'signal', 'skipped', `$${r.ticker} — ${r.reason || 'no contract address'}`));
  (d.detections || []).forEach(r => push(r, 'buy', 'detect', `${r.venue} fee=${r.fee} in ${r.seconds}s`));
  (d.swaps || []).forEach(r => push(r, 'swap', 'swap',
    `${(r.venue || '').trim()} ${r.token_delta} tokens / ${r.quote_delta} ETH · tx ${String(r.tx).slice(0, 14)} · status ${r.status}`));
  (d.trades || []).forEach(x => {
    if (x.outcome === 'closed')
      rows.push({ day: x.closed_day, time: x.closed_at, group: 'close', kind: 'closed',
                  detail: `$${x.ticker} ${x.is_win ? 'WIN' : 'LOSS'} ${usd(x.pnl_usd)}` });
    if (x.outcome === 'buy_failed')
      rows.push({ day: x.day, time: x.opened_at, group: 'buy', kind: 'buy failed',
                  detail: `$${x.ticker} — ${x.failure_reason || 'unknown'}` });
  });
  (d.errors || []).forEach(r => push(r, 'error', r.level.toLowerCase(), r.message || ''));
  return rows.sort((a, b) => `${b.day} ${b.time}`.localeCompare(`${a.day} ${a.time}`));
}

async function boot() {
  if (INLINE) return render(INLINE);
  const tick = async () => {
    try {
      const d = await (await fetch('/data.json')).json();
      const sig = signature(d);
      if (sig === LAST_SIG) return;
      LAST_SIG = sig;
      render(d);
    }
    catch (e) {
      document.getElementById('sub').textContent = 'Dashboard refresh failed';
      console.error('Dashboard refresh failed', e);
    }
  };
  await tick();
  setInterval(tick, 5000);
}
boot();
</script></body></html>
"""


# Set only by --share. None means loopback-only, where a password buys nothing.
AUTH = None


class Handler(BaseHTTPRequestHandler):
    _cache = {"stamp": None, "data": None}
    _process_cache = {"checked": None, "data": None}
    _cache_lock = threading.Lock()

    @classmethod
    def current_data(cls):
        with cls._cache_lock:
            stamp = source_stamp()
            if stamp != cls._cache["stamp"]:
                cls._cache = {"stamp": stamp, "data": collect(check_process=False)}
            now = time.monotonic()
            if cls._process_cache["checked"] is None or now - cls._process_cache["checked"] >= 10:
                cls._process_cache = {"checked": now, "data": bot_process()}
            # A stopped bot cannot change its log mtime. Refresh process status
            # independently, without re-reading all historical logs every poll.
            return {**cls._cache["data"], "bot": cls._process_cache["data"],
                    "generated_at": datetime.now().isoformat(timespec="seconds")}

    def _authorised(self) -> bool:
        """HTTP Basic, compared in constant time."""
        if AUTH is None:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                supplied = base64.b64decode(header[6:]).decode("utf-8")
            except Exception:
                supplied = ""
            if hmac.compare_digest(supplied, AUTH):
                return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="bot dashboard"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _send(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authorised():
            return
        if self.path.startswith("/data.json"):
            # Re-parse only when a log file actually changed. bot.log.1 alone is
            # 5MB; re-reading it every 5 seconds would burn CPU for nothing.
            body = json.dumps(self.current_data(), default=str, ensure_ascii=False)
            return self._send(body.encode("utf-8"), "application/json; charset=utf-8")
        self._send(PAGE.replace("__DATA__", "null").encode("utf-8"),
                   "text/html; charset=utf-8")

    def log_message(self, *args):
        pass  # the bot's own log is the interesting one


def snapshot(for_artifact: bool = False) -> str:
    """
    Self-contained HTML with the data baked in.

    The artifact variant drops the document wrapper: a published Artifact supplies
    its own <!doctype>/<head>/<body>, so shipping ours would nest a second document
    inside it. The local file keeps them, because it has to open from disk.
    """
    data = json.dumps(collect(), default=str, ensure_ascii=False)
    page = PAGE.replace("__DATA__", data)
    name = "dashboard.html"
    if for_artifact:
        # Drop only the wrapper tags. Everything between them -- title, font links,
        # style, markup, script -- stays in its original order, which is exactly
        # what the Artifact skeleton expects to receive.
        page = re.sub(r"</?(?:!doctype|html|head|body)\b[^>]*>", "",
                      page, flags=re.IGNORECASE).strip()
        name = "dashboard_artifact.html"
    path = os.path.join(HERE, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)
    print(f"wrote {path}  ({os.path.getsize(path) / 1024:.0f} KB)")
    return path


if __name__ == "__main__":
    if "--snapshot" in sys.argv:
        snapshot()
        snapshot(for_artifact=True)
    else:
        # Loopback by default. --share is an explicit, password-gated opt-in: this
        # page shows a wallet address, balances, open positions and P&L, so it must
        # never be reachable off-machine without a password.
        share = "--share" in sys.argv
        if share:
            password = next((a.split("=", 1)[1] for a in sys.argv
                             if a.startswith("--password=")), "") or secrets.token_urlsafe(9)
            globals()["AUTH"] = f"watch:{password}"
            print("SHARE MODE - reachable from the network, password required\n")
            print(f"   username  watch")
            print(f"   password  {password}\n")
            print("   Send the password separately from the link, and stop the")
            print("   server (ctrl+c) once your friend is done looking.\n")
        url = f"http://127.0.0.1:{PORT}"
        print(f"dashboard on {url}   (ctrl+c to stop)")
        if "--no-open" not in sys.argv and not share:
            webbrowser.open(url)
        try:
            ThreadingHTTPServer(("0.0.0.0" if share else "127.0.0.1", PORT),
                                Handler).serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
