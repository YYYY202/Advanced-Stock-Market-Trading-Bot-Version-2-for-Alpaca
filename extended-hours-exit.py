#!/usr/bin/env python3
"""
Extended-hours exit study: buy at the regular close (16:00 New York) and sell
in the next morning's PRE-MARKET instead of at the 09:30 open, for example
around the European open (London/Frankfurt open 08:00/09:00 local
= 03:00 New York most of the year; US pre-market data starts at 04:00).

Also splits the overnight return into
    after-hours   16:00 -> last after-hours trade (~20:00)
    dead hours    ~20:00 -> first pre-market trade (~04:00)
    pre-market    04:00 -> 09:30 open
so you can see WHEN the overnight gain actually happens.

Needs hourly bars including extended hours (New York time), e.g. Yahoo
`interval=60m&includePrePost=true`, or --csv with Date,Open,High,Low,Close,Volume.
Past results do not predict future results. Not financial advice.
"""

import argparse
import sys

import numpy as np
import pandas as pd

PRE = ["04:00", "05:00", "06:00", "07:00", "08:00", "09:00"]
REG_LAST = "15:30"


def load(args):
    if args.csv:
        return pd.read_csv(args.csv, parse_dates=["Date"]).set_index("Date").sort_index()
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("Pass --csv or `pip install yfinance`.")
    df = yf.download(args.symbol, period="730d", interval="60m", prepost=True,
                     auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index).tz_convert("America/New_York").tz_localize(None)
    return df[["Open", "High", "Low", "Close", "Volume"]]


def t_stat(s):
    s = s.dropna()
    sd = s.std()
    return s.mean() / (sd / np.sqrt(len(s))) if len(s) > 2 and sd > 0 else np.nan


def build(h):
    h = h.assign(date=h.index.normalize(), slot=h.index.strftime("%H:%M"))
    o = h.pivot_table(index="date", columns="slot", values="Open", aggfunc="first")
    c = h.pivot_table(index="date", columns="slot", values="Close", aggfunc="last")
    v = h.pivot_table(index="date", columns="slot", values="Volume", aggfunc="sum")
    after = h[h.index.time >= pd.Timestamp("16:00").time()]
    ah_close = after.groupby("date")["Close"].last()
    pre = h[h.index.time < pd.Timestamp("09:30").time()]
    pre_first = pre.groupby("date")["Open"].first()

    day = pd.DataFrame(index=c.index)
    day["close"] = c.get(REG_LAST)                     # regular-session close
    day["ah_close"] = ah_close
    day["pre_first"] = pre_first                       # first pre-market trade (~04:00)
    for s in PRE:
        day[f"pre_{s}_close"] = c.get(s)               # close of the bar starting at s
    day["open"] = o.get("09:30")
    day["reg_close_next"] = c.get(REG_LAST)

    t = pd.DataFrame(index=day.index)
    nxt = day.shift(-1)
    t["entry"] = day["close"]
    t["ah"] = day["ah_close"] / day["close"] - 1
    t["dead"] = nxt["pre_first"] / day["ah_close"] - 1
    t["pre"] = nxt["open"] / nxt["pre_first"] - 1
    t["full_overnight"] = nxt["open"] / day["close"] - 1
    exits = {"after-hours end (~20:00 same day)": day["ah_close"],
             "pre-market first trade (~04:00)": nxt["pre_first"]}
    for s in PRE:
        end = f"{int(s[:2]) + 1:02d}:00" if s != "09:00" else "09:30"
        exits[f"pre-market {end}"] = nxt[f"pre_{s}_close"]
    exits["regular open 09:30"] = nxt["open"]
    for k, px in exits.items():
        t[k] = px / day["close"] - 1
    vol_share = (v.sum() / v.sum().sum() * 100)
    return t.dropna(subset=["entry", "full_overnight"]), list(exits), vol_share, v


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BE")
    ap.add_argument("--csv")
    args = ap.parse_args()
    t, exits, vol_share, v = build(load(args))
    mid = t.index[len(t) // 2]
    print(f"{len(t)} overnight trades {t.index[0].date()} -> {t.index[-1].date()}, halves split {mid.date()}")

    print("\n=== Where does the overnight return happen? (entry = regular close) ===")
    for col, lab in (("ah", "after-hours 16:00 -> ~20:00"), ("dead", "dead hours ~20:00 -> ~04:00"),
                     ("pre", "pre-market ~04:00 -> 09:30 open"), ("full_overnight", "total close -> open")):
        s = t[col].dropna()
        print(f"  {lab:34s} n={len(s):4d}  avg {s.mean()*100:6.3f}%  t={t_stat(s):5.2f}  "
              f"$1->{np.prod(1+s):6.2f}  half1 {np.prod(1+s[s.index<mid]):5.2f}  half2 {np.prod(1+s[s.index>=mid]):5.2f}")

    print("\n=== Sell time comparison (buy at regular close) ===")
    rows = {}
    base = t["regular open 09:30"]
    for k in exits:
        s = t[k]
        both = pd.concat([s, base], axis=1).dropna()
        diff = both.iloc[:, 0] - both.iloc[:, 1]
        rows[k] = {"n": s.notna().sum(), "avg%": s.mean() * 100, "win%": (s.dropna() > 0).mean() * 100,
                   "t": t_stat(s), "$1->": np.prod(1 + s.dropna()),
                   "half1 $1->": np.prod(1 + s[s.index < mid].dropna()),
                   "half2 $1->": np.prod(1 + s[s.index >= mid].dropna()),
                   "vs open avg%": diff.mean() * 100, "vs open t": t_stat(diff)}
    with pd.option_context("display.float_format", "{:8.2f}".format, "display.width", 220):
        print(pd.DataFrame(rows).T.to_string())

    print("\n=== Liquidity: share of total volume by hourly bar (New York time) ===")
    pre_ah = vol_share[[s for s in vol_share.index if s < "09:30" or s >= "16:00"]]
    if pre_ah.sum() < 0.05:
        print("  No extended-hours volume in this data (Yahoo reports 0); check liquidity "
              "with your broker's data before trading pre-market.")
    print("  " + "  ".join(f"{k}:{x:.2f}%" for k, x in pre_ah.items()))
    med = v.median()
    print(f"  median shares traded: 04:00 bar {med.get('04:00', np.nan):,.0f}, 08:00 bar {med.get('08:00', np.nan):,.0f}, "
          f"09:30 bar {med.get('09:30', np.nan):,.0f}")
    print("\nPre-market fills need limit orders with extended_hours=True on Alpaca and spreads are wider "
          "than in regular hours.\nPast results do not predict future results. Not financial advice.")


if __name__ == "__main__":
    main()
