"""
Simulated "mirror the pinned lawmakers" strategy, run daily against an
Alpaca PAPER trading account (no real money - confirmed via the paper
API base URL below).

Strategy:
  - Only follows the 5 pinned lawmakers from congress-trades.json
    (ignores the 2 random extras - see fetch_congress_trades.py).
  - A BUY signal opens/keeps that symbol in the target portfolio.
  - A SELL signal closes that symbol IF currently held. If not held,
    it's ignored entirely - this strategy never shorts.
  - Every symbol in the target portfolio is kept equal-weighted: each
    time the target set changes, ALL open positions are rebalanced
    toward equity / len(target_set), not just the newly-signaled one.
  - Each (lawmaker, symbol, trade_date, direction) signal is only ever
    acted on once - a state file tracks what's already been processed,
    since the same trade can keep showing up in FMP's lookback window
    across many daily runs.

Writes data/paper-portfolio.json for the site to read (equity history,
current positions, recent trade log). Never touches real money - this
talks to https://paper-api.alpaca.markets exclusively.

Required environment variables (GitHub Actions secrets):
  ALPACA_API_KEY_ID
  ALPACA_API_SECRET_KEY
"""

import os
import json
import datetime
import requests

ALPACA_KEY_ID = os.environ["ALPACA_API_KEY_ID"]
ALPACA_SECRET_KEY = os.environ["ALPACA_API_SECRET_KEY"]
ALPACA_BASE = "https://paper-api.alpaca.markets"  # paper only, never live

TRADES_JSON_PATH = "data/congress-trades.json"
PORTFOLIO_JSON_PATH = "data/paper-portfolio.json"

MIN_REBALANCE_DOLLARS = 1.00   # skip rebalancing noise smaller than this
MAX_TRADE_LOG_ENTRIES = 50
MAX_HISTORY_ENTRIES = 365

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_KEY_ID,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
}


def alpaca_get(path):
    resp = requests.get(f"{ALPACA_BASE}{path}", headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def alpaca_post(path, payload):
    resp = requests.post(f"{ALPACA_BASE}{path}", headers=HEADERS, json=payload, timeout=30)
    if not resp.ok:
        print(f"WARNING: order failed for {payload}: {resp.status_code} {resp.text}")
        return None
    return resp.json()


def alpaca_delete(path):
    resp = requests.delete(f"{ALPACA_BASE}{path}", headers=HEADERS, timeout=30)
    if not resp.ok:
        print(f"WARNING: close failed for {path}: {resp.status_code} {resp.text}")
        return None
    return resp.json() if resp.text else None


def load_portfolio_state():
    """Loads the existing portfolio file, or starts a fresh one. This
    file holds both the public-facing data AND the internal
    processed_signals dedup list - simplest to keep in one place."""
    try:
        with open(PORTFOLIO_JSON_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {
            "updated_at": None,
            "starting_balance": 1000,
            "equity": None,
            "cash": None,
            "positions": [],
            "history": [],
            "trade_log": [],
            "processed_signals": [],
        }


def load_pinned_signals():
    """Every (lawmaker, symbol, trade_date, direction) tuple from the 5
    pinned lawmakers in congress-trades.json - explicitly excludes the
    2 random extras, per the strategy definition."""
    try:
        with open(TRADES_JSON_PATH) as f:
            trades_data = json.load(f)
    except FileNotFoundError:
        print(f"WARNING: {TRADES_JSON_PATH} not found - nothing to act on.")
        return []

    signals = []
    for person in trades_data.get("lawmakers", []):
        if person.get("pinned") is False:
            continue  # skip random extras
        for trade in person.get("trades", []):
            key = f"{person['name']}|{trade['symbol']}|{trade['trade_date']}|{trade['direction']}"
            signals.append({
                "key": key,
                "lawmaker": person["name"],
                "symbol": trade["symbol"],
                "direction": trade["direction"],
                "trade_date": trade["trade_date"],
            })
    return signals


def main():
    state = load_portfolio_state()
    processed = set(state.get("processed_signals", []))

    account = alpaca_get("/v2/account")
    equity = float(account["equity"])

    positions_raw = alpaca_get("/v2/positions")
    current_positions = {p["symbol"]: float(p["market_value"]) for p in positions_raw}

    signals = load_pinned_signals()
    new_signals = [s for s in signals if s["key"] not in processed]

    to_open = set()
    to_close = set()
    trade_log_additions = []

    for signal in new_signals:
        symbol = signal["symbol"]
        if signal["direction"] == "buy":
            to_open.add(symbol)
            trade_log_additions.append({
                "date": datetime.date.today().isoformat(),
                "symbol": symbol,
                "action": "signal_buy",
                "reason": f"{signal['lawmaker']} bought {symbol}",
            })
        elif signal["direction"] == "sell":
            if symbol in current_positions or symbol in to_open:
                to_close.add(symbol)
                trade_log_additions.append({
                    "date": datetime.date.today().isoformat(),
                    "symbol": symbol,
                    "action": "signal_sell",
                    "reason": f"{signal['lawmaker']} sold {symbol} (held - closing)",
                })
            else:
                trade_log_additions.append({
                    "date": datetime.date.today().isoformat(),
                    "symbol": symbol,
                    "action": "signal_ignored",
                    "reason": f"{signal['lawmaker']} sold {symbol} (not held - no short, ignored)",
                })
        processed.add(signal["key"])

    target_set = (set(current_positions.keys()) | to_open) - to_close

    # Fully close anything leaving the portfolio.
    for symbol in to_close:
        if symbol in current_positions:
            alpaca_delete(f"/v2/positions/{symbol}")
            trade_log_additions.append({
                "date": datetime.date.today().isoformat(),
                "symbol": symbol,
                "action": "close",
                "reason": "Fully closed - left target portfolio",
            })

    # Equal-weight rebalance everything still in (or newly entering) the
    # target set. Re-fetch equity isn't needed mid-run since closes free
    # cash that buys below will use; Alpaca's paper account settles this
    # correctly order-by-order.
    if target_set:
        target_value_each = equity / len(target_set)
        for symbol in target_set:
            current_value = current_positions.get(symbol, 0.0)
            delta = target_value_each - current_value

            if abs(delta) < MIN_REBALANCE_DOLLARS:
                continue

            side = "buy" if delta > 0 else "sell"
            order = alpaca_post("/v2/orders", {
                "symbol": symbol,
                "notional": round(abs(delta), 2),
                "side": side,
                "type": "market",
                "time_in_force": "day",
            })
            if order:
                trade_log_additions.append({
                    "date": datetime.date.today().isoformat(),
                    "symbol": symbol,
                    "action": f"rebalance_{side}",
                    "reason": f"Equal-weight rebalance toward ${target_value_each:.2f}",
                })

    # Re-fetch final state for the public JSON.
    account_after = alpaca_get("/v2/account")
    positions_after = alpaca_get("/v2/positions")

    state["updated_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    state["equity"] = float(account_after["equity"])
    state["cash"] = float(account_after["cash"])
    state["positions"] = [
        {
            "symbol": p["symbol"],
            "qty": float(p["qty"]),
            "market_value": float(p["market_value"]),
            "cost_basis": float(p["cost_basis"]),
            "unrealized_pl": float(p["unrealized_pl"]),
        }
        for p in positions_after
    ]

    history = state.get("history", [])
    today_str = datetime.date.today().isoformat()
    history = [h for h in history if h["date"] != today_str]  # replace if re-run same day
    history.append({"date": today_str, "equity": state["equity"]})
    state["history"] = history[-MAX_HISTORY_ENTRIES:]

    trade_log = trade_log_additions + state.get("trade_log", [])
    state["trade_log"] = trade_log[:MAX_TRADE_LOG_ENTRIES]

    state["processed_signals"] = list(processed)

    os.makedirs("data", exist_ok=True)
    with open(PORTFOLIO_JSON_PATH, "w") as f:
        json.dump(state, f, indent=2)

    print(f"Processed {len(new_signals)} new signals. "
          f"Equity: ${state['equity']:.2f} across {len(state['positions'])} positions.")


if __name__ == "__main__":
    main()
