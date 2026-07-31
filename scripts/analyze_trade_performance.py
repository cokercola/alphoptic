"""
One-off analysis: for every symbol currently held in the paper
portfolio, check whether it's "linked" to a tracked bill (per the most
recent congress-trades-all.json run) and compare unrealized P&L between
linked vs. unlinked positions.

This does NOT change the trading strategy - it's purely diagnostic, to
test the hypothesis "trades tied to an active bill perform better than
trades with no bill connection" before committing to a filter change.

Run locally against a checked-out copy of the repo:
  python scripts/analyze_trade_performance.py
"""

import json
from collections import defaultdict

PORTFOLIO_PATH = "data/paper-portfolio.json"
TRADES_ALL_PATH = "data/congress-trades-all.json"
BILLS_PATH = "data/bills.json"
HISTORY_PATH = "data/performance-analysis-history.json"


def load(path):
    with open(path) as f:
        return json.load(f)


def build_symbol_bill_context(bills_data):
    """Maps ticker -> list of {bill_id, direction, impact_score,
    passage_probability} for every bill that lists that ticker in its
    company exposure."""
    context = defaultdict(list)
    for signal in bills_data.get("signals", []):
        for company in signal.get("companies", []):
            ticker = company.get("ticker")
            if not ticker:
                continue
            context[ticker].append({
                "bill_id": signal["bill_id"],
                "bill_direction": signal["direction"],
                "impact_score": signal["impact_score"],
                "passage_probability": signal["passage_probability"],
                "company_effect": company.get("effect"),
                "exposure": company.get("exposure"),
            })
    return context


def build_symbol_trade_context(trades_data):
    """Maps ticker -> list of {lawmaker, direction, trade_date} for
    every disclosed trade seen in the most recent run."""
    context = defaultdict(list)
    for trade in trades_data.get("trades", []):
        context[trade["symbol"]].append({
            "lawmaker": trade["lawmaker"],
            "direction": trade["direction"],
            "trade_date": trade.get("trade_date"),
            "linked": trade.get("linked", False),
        })
    return context


def pct(cost_basis, market_value):
    if not cost_basis:
        return 0.0
    return ((market_value - cost_basis) / cost_basis) * 100


def main():
    portfolio = load(PORTFOLIO_PATH)
    trades_data = load(TRADES_ALL_PATH)
    bills_data = load(BILLS_PATH)

    bill_context = build_symbol_bill_context(bills_data)
    trade_context = build_symbol_trade_context(trades_data)

    rows = []
    for pos in portfolio.get("positions", []):
        symbol = pos["symbol"]
        return_pct = pct(pos["cost_basis"], pos["market_value"])
        bills_for_symbol = bill_context.get(symbol, [])
        trades_for_symbol = trade_context.get(symbol, [])
        is_linked = any(t.get("linked") for t in trades_for_symbol) or bool(bills_for_symbol)

        rows.append({
            "symbol": symbol,
            "unrealized_pl": pos["unrealized_pl"],
            "return_pct": round(return_pct, 2),
            "linked": is_linked,
            "bills": bills_for_symbol,
            "lawmakers": sorted({t["lawmaker"] for t in trades_for_symbol}),
        })

    linked_rows = [r for r in rows if r["linked"]]
    unlinked_rows = [r for r in rows if not r["linked"]]

    def avg_return(subset):
        return round(sum(r["return_pct"] for r in subset) / len(subset), 2) if subset else None

    print("=" * 70)
    print("PAPER PORTFOLIO: LINKED vs UNLINKED PERFORMANCE")
    print("=" * 70)
    print(f"\nTotal positions: {len(rows)}")
    print(f"Linked to a tracked bill: {len(linked_rows)}  |  avg return: {avg_return(linked_rows)}%")
    print(f"Not linked to any tracked bill: {len(unlinked_rows)}  |  avg return: {avg_return(unlinked_rows)}%")

    print("\n" + "-" * 70)
    print("PER-POSITION DETAIL (sorted by return, best first)")
    print("-" * 70)
    for r in sorted(rows, key=lambda x: x["return_pct"], reverse=True):
        tag = "LINKED  " if r["linked"] else "unlinked"
        print(f"\n{r['symbol']:>6}  [{tag}]  return: {r['return_pct']:>7.2f}%  "
              f"unrealized P&L: ${r['unrealized_pl']:.2f}")
        if r["lawmakers"]:
            print(f"        traded by: {', '.join(r['lawmakers'])}")
        for b in r["bills"]:
            print(f"        -> {b['bill_id']} | {b['bill_direction']} | "
                  f"impact {b['impact_score']}/100 | passage {b['passage_probability']}% | "
                  f"company effect: {b['company_effect']}")

    print("\n" + "=" * 70)
    if linked_rows and unlinked_rows:
        diff = avg_return(linked_rows) - avg_return(unlinked_rows)
        print(f"Linked positions outperformed unlinked by {diff:.2f} percentage points "
              f"(on this snapshot - treat as a hypothesis, not a conclusion, "
              f"until you have more trades/history to test against).")
    else:
        print("Not enough of both linked and unlinked positions yet for a "
              "meaningful comparison. Keep collecting data.")

    # Append this run's summary to a running history file, so the
    # linked-vs-unlinked comparison builds into an actual trend over
    # weeks instead of disconnected one-off snapshots.
    try:
        with open(HISTORY_PATH) as f:
            history = json.load(f)
    except FileNotFoundError:
        history = {"runs": []}

    history["runs"].append({
        "date": portfolio.get("updated_at"),
        "total_positions": len(rows),
        "linked_count": len(linked_rows),
        "unlinked_count": len(unlinked_rows),
        "linked_avg_return_pct": avg_return(linked_rows),
        "unlinked_avg_return_pct": avg_return(unlinked_rows),
        "positions": [
            {"symbol": r["symbol"], "linked": r["linked"], "return_pct": r["return_pct"]}
            for r in rows
        ],
    })
    history["runs"] = history["runs"][-180:]  # keep ~6 months of daily runs

    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2)

    print(f"\nAppended this run to {HISTORY_PATH} ({len(history['runs'])} runs recorded so far).")

    if len(history["runs"]) >= 3:
        print("\nTrend across recorded runs (linked avg % - unlinked avg %):")
        for run in history["runs"][-10:]:
            l = run["linked_avg_return_pct"]
            u = run["unlinked_avg_return_pct"]
            if l is not None and u is not None:
                print(f"  {run['date']}: linked {l:>6.2f}%  |  unlinked {u:>6.2f}%  |  diff {l - u:>6.2f}pp")


if __name__ == "__main__":
    main()
