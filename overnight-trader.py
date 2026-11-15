#!/usr/bin/env python3
"""
Small-size overnight trader for one stock (default BE) on Alpaca.

Strategy (from the research scripts in this repo):
  * Buy at the regular close with a market-on-close order.
  * Sell next morning in the PRE-MARKET (04:00-09:30 New York) with an
    extended-hours limit order; whatever is still held at 09:25 is sold with
    a market-on-open order.
  * While the "momentum" state is on (RSI(14) crossed above 70; ends when
    RSI > 80 or close < SMA20) the position is kept through the day instead.

Commands (New York time, Mon-Fri):
  auto                        run every 5 minutes from cron; picks the step below
                              from the current New York time (safe to repeat)
  status                      account, clock, position, open orders, signal
  buy        ~15:40           market-on-close buy (must be sent before 15:50)
  sell-pre   ~04:05 .. 09:00  extended-hours limit sell: bid - --slip if a fresh quote
                              exists, never below prev close - --floor (default 2%);
                              without a fresh quote the limit IS that floor, which
                              still fills at the best bid above it
  sell-open  ~09:20           cancel unfilled pre-market sell, market-on-open sell
  cancel                      cancel all open orders for the symbol

Safety:
  * PAPER trading unless --live is given AND APCA_API_BASE_URL points at the
    live endpoint. Both must agree or the script refuses to trade.
  * --dry-run prints the order instead of sending it.
  * --max-usd (default 300) caps the position value; whole shares only.
  * Never buys if a position or an open order already exists.
  * Every decision is appended to overnight-trader-log.jsonl.

Environment: APCA_API_KEY_ID, APCA_API_SECRET_KEY, APCA_API_BASE_URL
(https://paper-api.alpaca.markets or https://api.alpaca.markets).

This is an experiment, not financial advice. You can lose money.
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
NY = ZoneInfo("America/New_York")
MAX_QUOTE_AGE_MIN = 15
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overnight-trader-log.jsonl")


# ------------------------------------------------------------------ helpers
def log(event, **kw):
    rec = {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **kw}
    print(json.dumps(rec, default=str))
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


class Alpaca:
    def __init__(self, live):
        key, secret = os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY")
        base = (os.getenv("APCA_API_BASE_URL") or PAPER_URL).rstrip("/").removesuffix("/v2")
        if not key or not secret:
            sys.exit("Set APCA_API_KEY_ID and APCA_API_SECRET_KEY.")
        is_live_url = base == LIVE_URL
        if live != is_live_url:
            sys.exit(f"Refusing: --live={live} but APCA_API_BASE_URL={base}. "
                     f"Use {PAPER_URL} for paper, or pass --live with {LIVE_URL}.")
        self.base, self.live = base, live
        self.h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    def _req(self, method, url, **kw):
        r = requests.request(method, url, headers=self.h, timeout=20, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {url} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else None

    def get(self, path, **params):
        return self._req("GET", self.base + path, params=params)

    def post(self, path, body):
        return self._req("POST", self.base + path, json=body)

    def delete(self, path):
        return self._req("DELETE", self.base + path)

    def data(self, path, **params):
        return self._req("GET", DATA_URL + path, params=params)

    # convenience
    def position(self, symbol):
        try:
            return self.get(f"/v2/positions/{symbol}")
        except RuntimeError as e:
            if "404" in str(e):
                return None
            raise

    def open_orders(self, symbol):
        return self.get("/v2/orders", status="open", symbols=symbol)

    def latest_quote(self, symbol, feed):
        q = self.data(f"/v2/stocks/{symbol}/quotes/latest", feed=feed)["quote"]
        return float(q["bp"]), float(q["ap"]), q["t"]

    def latest_trade(self, symbol, feed):
        t = self.data(f"/v2/stocks/{symbol}/trades/latest", feed=feed)["trade"]
        return float(t["p"]), t["t"]


# ------------------------------------------------------------------ signal
def _bars(api, symbol, feed):
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=200)).strftime("%Y-%m-%d")
    end = (now - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")   # free plan: SIP >15 min old
    bars, token = [], None
    while True:
        params = dict(timeframe="1Day", start=start, end=end, adjustment="all", feed=feed, limit=1000)
        if token:
            params["page_token"] = token
        r = api.data(f"/v2/stocks/{symbol}/bars", **params)
        bars += r.get("bars") or []
        token = r.get("next_page_token")
        if not token:
            break
    return bars


def daily_closes(api, symbol, feed):
    """Completed daily closes (consolidated SIP if allowed, else the given feed), excluding today."""
    try:
        bars = _bars(api, symbol, "sip")
    except RuntimeError:
        bars = _bars(api, symbol, feed)
    today = datetime.now(NY).date()
    bars = [b for b in bars if parse_ts(b["t"]).astimezone(NY).date() < today]
    return [b["c"] for b in bars]


def momentum_state(closes):
    """Replays the momentum rule over history; returns (state_on, rsi, sma20)."""
    if len(closes) < 40:
        return False, float("nan"), float("nan")
    alpha = 1 / 14
    avg_g = avg_l = None
    rsis = []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g, l = max(d, 0), max(-d, 0)
        avg_g = g if avg_g is None else avg_g + alpha * (g - avg_g)
        avg_l = l if avg_l is None else avg_l + alpha * (l - avg_l)
        rsis.append(100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l))
    rsis = [float("nan")] + rsis
    state = False
    for i in range(20, len(closes)):
        sma20 = sum(closes[i - 19:i + 1]) / 20
        if not state and rsis[i - 1] <= 70 < rsis[i]:
            state = True
        elif state and (rsis[i] > 80 or closes[i] < sma20):
            state = False
    return state, rsis[-1], sum(closes[-20:]) / 20


# ------------------------------------------------------------------ commands
def cmd_status(api, a):
    acct = api.get("/v2/account")
    clock = api.get("/v2/clock")
    pos = api.position(a.symbol)
    orders = api.open_orders(a.symbol)
    state, rsi, sma = momentum_state(daily_closes(api, a.symbol, a.feed))
    log("status", mode="LIVE" if api.live else "PAPER", equity=acct.get("equity"),
        buying_power=acct.get("buying_power"), market_open=clock.get("is_open"),
        next_open=clock.get("next_open"), next_close=clock.get("next_close"),
        position_qty=pos and pos.get("qty"), avg_entry=pos and pos.get("avg_entry_price"),
        unrealized_pl=pos and pos.get("unrealized_pl"), open_orders=len(orders),
        momentum_on=state, rsi=round(rsi, 1), sma20=round(sma, 2))


def send(api, a, order, why):
    if a.dry_run:
        log("dry_run_order", why=why, order=order)
        return None
    res = api.post("/v2/orders", order)
    log("order_sent", why=why, order=order, id=res.get("id"), status=res.get("status"))
    return res


def cmd_buy(api, a):
    clock = api.get("/v2/clock")
    if not clock.get("is_open"):
        return log("skip_buy", reason="market closed (holiday/weekend?)")
    if api.position(a.symbol):
        return log("skip_buy", reason="already holding")
    if api.open_orders(a.symbol):
        return log("skip_buy", reason="open order exists")
    price, _ = api.latest_trade(a.symbol, a.feed)
    qty = math.floor(a.max_usd / price)
    if qty < 1:
        return log("skip_buy", reason=f"max_usd {a.max_usd} < price {price}")
    order = {"symbol": a.symbol, "qty": str(qty), "side": "buy", "type": "market",
             "time_in_force": "cls", "client_order_id": f"on-buy-{datetime.now():%Y%m%d}"}
    send(api, a, order, f"overnight entry, last {price}, qty {qty} (~${qty*price:.0f})")


def holding_through_day(api, a):
    state, rsi, sma = momentum_state(daily_closes(api, a.symbol, a.feed))
    if state:
        log("hold", reason="momentum state ON - keep position through the day",
            rsi=round(rsi, 1), sma20=round(sma, 2))
    return state


def trading_day_today(api):
    """True if the regular session opens today (New York date)."""
    nxt = api.get("/v2/clock").get("next_open", "")
    try:
        return datetime.fromisoformat(nxt).astimezone(NY).date() == datetime.now(NY).date()
    except ValueError:
        return False


def cmd_sell_pre(api, a):
    pos = api.position(a.symbol)
    if not pos:
        return log("skip_sell", reason="no position")
    if not trading_day_today(api):
        return log("skip_sell", reason="no regular session today (holiday?)")
    closes = daily_closes(api, a.symbol, a.feed)
    state, rsi, sma = momentum_state(closes)
    if state:
        return log("hold", reason="momentum state ON - keep position through the day",
                   rsi=round(rsi, 1), sma20=round(sma, 2))
    if api.open_orders(a.symbol):
        return log("skip_sell", reason="open order already working")
    qty = int(float(pos["qty_available"] if "qty_available" in pos else pos["qty"]))
    prev_close = closes[-1]
    floor_px = prev_close * (1 - a.floor)
    bid, ask, ts = api.latest_quote(a.symbol, a.feed)
    age_min = (datetime.now(timezone.utc) - parse_ts(ts)).total_seconds() / 60
    if age_min <= MAX_QUOTE_AGE_MIN and bid > 0:
        limit = max(bid * (1 - a.slip), floor_px)
        why = f"pre-market exit at fresh bid {bid}/{ask} ({age_min:.0f} min old)"
    else:
        # No fresh quote (free IEX feed starts at 08:00). A sell limit fills at the best bid
        # at or above the limit, so a floor below the last close still sells near market.
        limit = floor_px
        why = (f"pre-market exit with floor {a.floor:.1%} below prev close {prev_close} "
               f"(quote on '{a.feed}' is {age_min:.0f} min old)")
    limit = round(limit, 2)
    order = {"symbol": a.symbol, "qty": str(qty), "side": "sell", "type": "limit",
             "limit_price": f"{limit:.2f}", "time_in_force": "day", "extended_hours": True,
             "client_order_id": f"on-sellpre-{datetime.now():%Y%m%d%H%M}"}
    send(api, a, order, why + f", entry {pos.get('avg_entry_price')}")


def cmd_sell_open(api, a):
    pos = api.position(a.symbol)
    if not pos:
        return log("skip_sell_open", reason="no position (pre-market sell filled?)")
    if holding_through_day(api, a):
        return
    orders = api.open_orders(a.symbol)
    if any(o["side"] == "sell" and o["time_in_force"] == "opg" for o in orders):
        return log("skip_sell_open", reason="opening-auction sell already queued")
    for o in orders:
        if o["side"] == "sell":
            if a.dry_run:
                log("dry_run_cancel", id=o["id"])
            else:
                api.delete(f"/v2/orders/{o['id']}")
                log("cancelled", id=o["id"], reason="unfilled pre-market sell")
    pos = api.position(a.symbol) if not a.dry_run else pos
    if not pos:
        return log("skip_sell_open", reason="filled while cancelling")
    qty = int(float(pos["qty"]))
    order = {"symbol": a.symbol, "qty": str(qty), "side": "sell", "type": "market",
             "time_in_force": "opg", "client_order_id": f"on-sellopg-{datetime.now():%Y%m%d}"}
    send(api, a, order, "fallback exit at the opening auction")


def cmd_cancel(api, a):
    for o in api.open_orders(a.symbol):
        if a.dry_run:
            log("dry_run_cancel", id=o["id"])
        else:
            api.delete(f"/v2/orders/{o['id']}")
            log("cancelled", id=o["id"])


def parse_ts(ts):
    ts = ts.rstrip("Z")
    if "." in ts:
        head, frac = ts.split(".", 1)
        ts = f"{head}.{frac[:6]}"
    return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)


def cmd_auto(api, a):
    """Dispatch by New York time. Every step is idempotent, so cron can call this often."""
    now = datetime.now(NY)
    if now.weekday() >= 5:
        return
    t = now.time()
    if dtime(15, 35) <= t < dtime(15, 49):
        cmd_buy(api, a)
    elif dtime(4, 5) <= t < dtime(9, 10):
        cmd_sell_pre(api, a)
    elif dtime(9, 15) <= t < dtime(9, 27):
        cmd_sell_open(api, a)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["auto", "status", "buy", "sell-pre", "sell-open", "cancel"])
    ap.add_argument("--symbol", default="BE")
    ap.add_argument("--max-usd", type=float, default=300.0, help="max position value (default 300)")
    ap.add_argument("--slip", type=float, default=0.003,
                    help="pre-market sell limit = bid * (1 - slip), default 0.3%%")
    ap.add_argument("--floor", type=float, default=0.02,
                    help="pre-market sell never below prev close * (1 - floor), default 2%%")
    ap.add_argument("--feed", default="iex", help="market data feed: iex (free) or sip")
    ap.add_argument("--live", action="store_true", help="trade real money (needs live base URL too)")
    ap.add_argument("--dry-run", action="store_true", help="print orders, do not send")
    a = ap.parse_args()
    api = Alpaca(a.live)
    {"auto": cmd_auto, "status": cmd_status, "buy": cmd_buy, "sell-pre": cmd_sell_pre,
     "sell-open": cmd_sell_open, "cancel": cmd_cancel}[a.command](api, a)


if __name__ == "__main__":
    main()
