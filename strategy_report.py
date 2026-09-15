"""Read-only audit and scenario replay of locally observed price paths.

No network, credentials, trading, randomized wins, or parameter optimization.
The recorded paths stop when the original bot stopped observing a position.
"""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3


def audit(project):
    counts, reasons = Counter(), Counter()
    active, paths = {}, []
    logs = sorted((project / "logs").glob("bot.log*"),
                  key=lambda p: int(p.suffix[1:]) if p.suffix[1:].isdigit() else 0, reverse=True)
    for path in logs:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            for label, needle in (("signals", "Incoming Signal:"), ("buy_attempts", "Executing BUY"),
                                  ("opened", "Position opened:"), ("zero_fill_reports", "0 tokens received"),
                                  ("missing_contract", "No contract address identified"), ("rpc_429_lines", "429")):
                if needle in line:
                    counts[label] += 1
            skipped = re.search(r"Skipping trade for \$[^:]+: (.+)", line)
            if skipped:
                reasons[skipped[1]] += 1
            opened = re.search(r"Position opened: (\S+) for \$(\S+) \(", line)
            if opened:
                item = dict(id=opened[1], ticker=opened[2], prices=[], complete=False)
                active[opened[2]] = item
                paths.append(item)
            tick = re.search(r"Tracking \$(.+?): Multiplier: ([\d.]+)x.*Time: ([\d.]+)m", line)
            if tick and tick[1] in active:
                active[tick[1]]["prices"].append((float(tick[3]), float(tick[2])))
            closed = re.search(r"Position closed: \$(\S+) \|", line)
            if closed and closed[1] in active:
                active.pop(closed[1])["complete"] = True
    events_path = project / "logs" / "events.jsonl"
    rows = []
    malformed = 0
    if events_path.exists():
        for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                malformed += 1
    closed = [r for r in rows if r.get("kind") == "position_closed"]
    inbox = {}
    for mode in ("paper", "live"):
        db_path = project / "cache" / f"signals_{mode}.sqlite3"
        if db_path.exists():
            connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
            try:
                inbox[mode] = [dict(status=s, reason=r, count=n) for s, r, n in connection.execute(
                    "SELECT status,reason,count(*) FROM signals GROUP BY status,reason ORDER BY count(*) DESC")]
            finally:
                connection.close()
    return dict(log_counts=dict(counts), skip_reasons=dict(reasons),
                ledger_closed_rows=len(closed),
                recorded_pnl_usd=sum(r.get("pnl_usd") or 0 for r in closed),
                pnl_basis_counts=dict(Counter(r.get("pnl_basis", "legacy_estimate") for r in closed)),
                malformed_ledger_rows=malformed, inbox=inbox, paths=paths)


def replay(prices, stop=0.5, fee=0.03, slippage_pct=2.0, stake=1.0, single_target=None):
    """Scenario only: $1 at the observed entry; supplied costs on every exit."""
    remaining, proceeds, swaps, peak = 1.0, 0.0, 1, 1.0
    tp1 = tp2 = False
    reason = "observation_ended"
    for minutes, multiplier in prices:
        peak = max(peak, multiplier)
        threshold = max(1.20, peak - 0.25) if tp2 else max(0.95, peak - 0.25) if tp1 else stop
        fraction = 0.0
        if multiplier <= threshold:
            fraction, reason = remaining, "stop"
        elif minutes >= (120 if tp1 else 25):
            fraction, reason = remaining, "timeout"
        elif single_target and multiplier >= single_target:
            fraction, reason = remaining, "target"
        elif not single_target and not tp1 and multiplier >= 1.20:
            fraction, tp1 = min(0.4, remaining), True
        elif not single_target and tp1 and not tp2 and multiplier >= 1.40:
            fraction, tp2 = min(0.4, remaining), True
        elif not single_target and tp2 and multiplier >= 5:
            fraction, reason = remaining, "final_target"
        if fraction:
            proceeds += stake * fraction * multiplier * (1 - slippage_pct / 100)
            remaining -= fraction
            swaps += 1
        if remaining <= 1e-9:
            remaining = 0
            break
    # Unsold holdings are censored. Do not pretend their last mark was a fill.
    return dict(net_realized_pnl=proceeds - stake * (1 - remaining) - fee * swaps,
                remaining_fraction=remaining, complete=remaining == 0, reason=reason)


def report_text(result, fee, slippage):
    lines = ["# Bot audit and historical scenarios", "", f"Generated {datetime.now():%Y-%m-%d %H:%M}",
             "", "## Recorded activity", "", "| Log event | Occurrences |", "|---|---:|"]
    lines += [f"| {label} | {count} |" for label, count in result["log_counts"].items()]
    lines += ["", "These are log occurrences; retries and restarts may overlap.", "",
              "## Why entries were skipped", "", "| Reason | Occurrences |", "|---|---:|"]
    lines += [f"| {reason} | {count} |" for reason, count in sorted(result["skip_reasons"].items(), key=lambda x: -x[1])]
    lines += ["", f"Ledger: {result['ledger_closed_rows']} closes, ${result['recorded_pnl_usd']:+.2f} recorded P&L.",
              f"Measurement types: `{json.dumps(result['pnl_basis_counts'])}`.",
              "Legacy estimates omit actual fills and fees and may differ from saved state and the wallet."]
    if result["inbox"]:
        lines += ["", "## Current durable inbox", "", "| Mode | Outcome | Reason | Count |", "|---|---|---|---:|"]
        for mode, rows in result["inbox"].items():
            lines += [f"| {mode} | {r['status']} | {r['reason']} | {r['count']} |" for r in rows]
    paths = [p for p in result["paths"] if p["prices"]]
    lines += ["", "## Cost-sensitive scenarios (not a validated backtest)", "",
              f"{len(paths)} observed entry paths. Assumptions: $1 stake, ${fee:.3f} fee per buy/sell, "
              f"{slippage:.2f}% adverse slippage on exits; entry costs are already reflected in recorded entry multipliers.",
              "", "| Scenario | Full exits | Censored paths | Realized P&L incl. fees, all paths |", "|---|---:|---:|---:|"]
    for name, options in (("Existing 40/40/20 ladder, 0.50x stop", {}),
                          ("Same ladder, hypothetical 0.75x stop", {"stop": 0.75}),
                          ("Hypothetical full exit at 1.20x / stop 0.75x", {"stop": 0.75, "single_target": 1.20})):
        scenarios = [replay(p["prices"], fee=fee, slippage_pct=slippage, **options) for p in paths]
        complete = sum(s["complete"] for s in scenarios)
        pnl = sum(s["net_realized_pnl"] for s in scenarios)
        lines.append(f"| {name} | {complete} | {len(paths)-complete} | ${pnl:+.3f} |")
    lines += ["", "### Interpretation", "",
              "A censored path still owns unsold tokens; its realized P&L excludes their unobserved future outcome. "
              "Different completion counts prevent a fair profit ranking. The sample excludes missed calls, "
              "logs round prices and times, and prices may be stale. Pool impact, failed-transaction fees, "
              "sandwiching, and sellability are not reconstructed. These scenarios cannot establish an edge "
              "or justify changing live stop/target parameters.", "",
              "Collect fresh paper signals and executable quotes with the upgraded bot, including all rejected calls. "
              "Evaluate net results on later, unseen data before making a live strategy change. "
              "No live strategy parameters were optimized or changed by this report."]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--fee-per-swap-usd", type=float, default=0.03, help="Scenario assumption, not a measured fee")
    parser.add_argument("--slippage-pct", type=float, default=2.0, help="Scenario assumption, not a measured fill")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 0 <= args.fee_per_swap_usd < float("inf") or not 0 <= args.slippage_pct < 100:
        parser.error("fee must be finite and nonnegative; slippage must be in [0,100)")
    result = audit(args.project.resolve())
    content = report_text(result, args.fee_per_swap_usd, args.slippage_pct)
    if args.output:
        args.output.write_text(content, encoding="utf-8")
    print(content)


if __name__ == "__main__":
    main()
