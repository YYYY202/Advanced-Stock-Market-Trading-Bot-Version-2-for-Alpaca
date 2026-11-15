#!/usr/bin/env python3
"""
Sell-time research for an overnight hold (buy at the close of day d, sell on day d+1).
Default symbol BE, no trading costs unless --cost.

  A. Post-open drift by opening-gap bucket: once you see the gap at 09:30,
     is it better to sell at the open or wait until 10:30 ... 16:00?
  B. Limit take-profit: a limit sell at entry*(1+x) working from the open,
     falling back to a timed exit; optional stop loss.
  C. Post-open drift by regime (momentum state, volatility, weekday/weekend).
  D. First 60 minutes at 5-minute resolution (only ~60 days available).

Every table reports the first and second half of the sample separately; an
effect that flips sign between halves should not be traded.

Data: --hourly-csv, --daily-csv, --fivemin-csv (Date,Open,High,Low,Close,Volume,
New York time) or yfinance. Past results do not predict future results.
Not financial advice.
"""

import argparse
import sys

import numpy as np
import pandas as pd

SLOTS = ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]   # hourly bar start times
EXITS = ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30", "16:00"]  # sell time


def yf_download(symbol, **kw):
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("Pass the CSV options or `pip install yfinance`.")
    df = yf.download(symbol, auto_adjust=True, progress=False, **kw)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    idx = pd.to_datetime(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert("America/New_York").tz_localize(None)
    df.index = idx
    return df[["Open", "High", "Low", "Close", "Volume"]]


def read_csv(path):
    return pd.read_csv(path, parse_dates=["Date"]).set_index("Date").sort_index()


def t_stat(s):
    s = pd.Series(s).dropna()
    return s.mean() / (s.std() / np.sqrt(len(s))) if len(s) > 2 and s.std() > 0 else np.nan


def fmt(df, f="{:8.2f}"):
    with pd.option_context("display.float_format", f.format, "display.width", 240):
        return df.to_string()


# --------------------------------------------------------------- build per-trade table
def build_trades(hourly):
    """One row per overnight trade: entry = close of day d, then day d+1's hourly bars."""
    h = hourly[(hourly.index.time >= pd.Timestamp("09:30").time()) &
               (hourly.index.time < pd.Timestamp("16:00").time())]
    h = h.assign(date=h.index.normalize(), slot=h.index.strftime("%H:%M"))
    full_days = h.groupby("date")["slot"].apply(lambda s: set(SLOTS) <= set(s))
    h = h[h["date"].isin(full_days[full_days].index)]
    piv = {f: h.pivot_table(index="date", columns="slot", values=f, aggfunc="last")[SLOTS]
           for f in ("Open", "High", "Low", "Close")}
    day_close = piv["Close"]["15:30"]
    nxt = {f: v.shift(-1) for f, v in piv.items()}
    t = pd.DataFrame(index=piv["Close"].index)
    t["entry"] = day_close
    t["open"] = nxt["Open"]["09:30"]
    # price at each exit time: 09:30 = open, HH:30 = close of the bar that ends then, 16:00 = close
    t["px_09:30"] = t["open"]
    for start, end in zip(SLOTS[:-1], EXITS[1:-1]):
        t[f"px_{end}"] = nxt["Close"][start]
    t["px_16:00"] = nxt["Close"]["15:30"]
    for s in SLOTS:
        t[f"hi_{s}"] = nxt["High"][s]
        t[f"lo_{s}"] = nxt["Low"][s]
    t["gap"] = t["open"] / t["entry"] - 1
    t["sell_date"] = pd.Series(piv["Close"].index, index=piv["Close"].index).shift(-1)
    return t.dropna(subset=["entry", "open", "px_16:00"])


def halves(t):
    mid = t.index[len(t) // 2]
    return mid, t[t.index < mid], t[t.index >= mid]


# ------------------------------------------------------------------------- part A
def drift_table(t, mask_dict, title):
    """Average move from the open to each exit time (decision made at the open)."""
    mid, a, b = halves(t)
    rows = {}
    for name, m in mask_dict.items():
        sub, sa, sb = t[m(t)], a[m(a)], b[m(b)]
        row = {"n": len(sub)}
        for x in EXITS[1:]:
            d = sub[f"px_{x}"] / sub["open"] - 1
            row[x] = d.mean() * 100
        best = max(EXITS[1:], key=lambda x: row[x])
        row["best"] = best if row[best] > 0 else "open"
        da = (sa[f"px_{best}"] / sa["open"] - 1).mean() * 100 if row["best"] != "open" else 0
        db = (sb[f"px_{best}"] / sb["open"] - 1).mean() * 100 if row["best"] != "open" else 0
        row["t(best)"] = t_stat(sub[f"px_{best}"] / sub["open"] - 1) if row["best"] != "open" else np.nan
        row["half1"], row["half2"] = da, db
        rows[name] = row
    df = pd.DataFrame(rows).T
    print(f"\n=== {title} ===")
    print("Columns 10:30..16:00 = average % move from the 09:30 open to that time "
          "(positive = waiting beat selling at the open).")
    print(f"best = time with the highest average; half1/half2 = that same choice in each half "
          f"(split {mid.date()}).")
    print(fmt(df))
    return df


def part_a(t):
    g = t["gap"]
    buckets = {
        "all": lambda x: pd.Series(True, index=x.index),
        "gap <= -3%": lambda x: x["gap"] <= -0.03,
        "-3% < gap <= -1%": lambda x: (x["gap"] > -0.03) & (x["gap"] <= -0.01),
        "-1% < gap < +1%": lambda x: x["gap"].abs() < 0.01,
        "+1% <= gap < +3%": lambda x: (x["gap"] >= 0.01) & (x["gap"] < 0.03),
        "gap >= +3%": lambda x: x["gap"] >= 0.03,
        "gap >= +5%": lambda x: x["gap"] >= 0.05,
    }
    print(f"\nTrades: {len(t)}, average gap {g.mean()*100:.2f}%, gap up {(g>0).mean()*100:.0f}% of nights")
    return drift_table(t, buckets, "PART A - post-open drift by opening gap")


# ------------------------------------------------------------------------- part B
def limit_exit(t, target, deadline, stop=None):
    """Limit sell at entry*(1+target) from the open; stop at entry*(1-stop); else sell at deadline."""
    out = []
    tgt_mult, stop_mult = 1 + target, (1 - stop) if stop else None
    dl_idx = EXITS.index(deadline)
    for _, r in t.iterrows():
        e = r["entry"]
        if r["open"] >= e * tgt_mult:
            out.append(r["open"] / e - 1); continue
        if stop_mult and r["open"] <= e * stop_mult:
            out.append(r["open"] / e - 1); continue
        done = False
        for s in SLOTS[:dl_idx]:                 # bars fully before the deadline
            if stop_mult and r[f"lo_{s}"] <= e * stop_mult:   # assume stop hit first (conservative)
                out.append(-stop); done = True; break
            if r[f"hi_{s}"] >= e * tgt_mult:
                out.append(target); done = True; break
        if not done:
            out.append(r[f"px_{deadline}"] / e - 1)
    return pd.Series(out, index=t.index)


def part_b(t, cost):
    mid, a, b = halves(t)
    rows = {}
    base = t["gap"]
    def rec(name, r):
        ra, rb = r[r.index < mid], r[r.index >= mid]
        g = lambda s: float(np.prod((1 + s) * (1 - cost) ** 2))
        rows[name] = {"avg%": r.mean() * 100, "win%": (r > 0).mean() * 100, "t": t_stat(r),
                      "$1->": g(r), "half1 $1->": g(ra), "half2 $1->": g(rb)}
    rec("sell at open (baseline)", base)
    for dl in ("10:30", "12:30", "16:00"):
        for x in (0.01, 0.02, 0.03, 0.05):
            rec(f"limit +{x:.0%} until {dl}, else sell {dl}", limit_exit(t, x, dl))
    for dl in ("10:30", "12:30"):
        for x, s in ((0.02, 0.03), (0.03, 0.03), (0.03, 0.05)):
            rec(f"limit +{x:.0%} / stop -{s:.0%} until {dl}", limit_exit(t, x, dl, s))
    df = pd.DataFrame(rows).T
    print("\n=== PART B - limit take-profit exits (entry = previous close) ===")
    print("Fill rules: open beyond target/stop fills at the open; otherwise target or stop "
          "price if the hourly high/low touches it (stop assumed first if both in one bar).")
    print(fmt(df))
    return df


# ------------------------------------------------------------------------- part C
def part_c(t, daily):
    d = daily.copy()
    c = d["Close"]
    delta = c.diff()
    g = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    l = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    d["rsi"] = 100 - 100 / (1 + g / l)
    d["sma20"] = c.rolling(20).mean()
    d["vol20"] = c.pct_change().rolling(20).std() * np.sqrt(252)
    d["vol_med"] = d["vol20"].expanding(60).median()
    state, momo = False, []
    for i in range(len(d)):
        r = d["rsi"].iat[i]; rp = d["rsi"].iat[i - 1] if i else np.nan
        if not state and rp <= 70 < r:
            state = True
        elif state and (r > 80 or c.iat[i] < d["sma20"].iat[i]):
            state = False
        momo.append(state)
    d["momo"] = momo
    t = t.join(d[["momo", "vol20", "vol_med", "rsi"]], how="left")
    t["weekend"] = (t["sell_date"] - t.index).dt.days > 1
    masks = {
        "all": lambda x: pd.Series(True, index=x.index),
        "momentum state ON": lambda x: x["momo"] == True,
        "momentum state OFF": lambda x: x["momo"] == False,
        "high vol": lambda x: x["vol20"] > x["vol_med"],
        "low vol": lambda x: x["vol20"] <= x["vol_med"],
        "after weekend/holiday": lambda x: x["weekend"],
        "normal weeknight": lambda x: ~x["weekend"],
        "RSI > 70 at entry": lambda x: x["rsi"] > 70,
        "RSI < 40 at entry": lambda x: x["rsi"] < 40,
    }
    return drift_table(t, masks, "PART C - post-open drift by regime (known at the close of day d)")


# ------------------------------------------------------------------------- part D
def part_d(five):
    f = five[(five.index.time >= pd.Timestamp("09:30").time()) &
              (five.index.time < pd.Timestamp("16:00").time())]
    f = f.assign(date=f.index.normalize(), slot=f.index.strftime("%H:%M"))
    closes = f.pivot_table(index="date", columns="slot", values="Close", aggfunc="last")
    opens = f.groupby("date")["Open"].first()
    last = f.groupby("date")["Close"].last()
    prev_close = last.shift(1)
    gap = opens / prev_close - 1
    # a 5-min bar starting at HH:MM closes 5 minutes later
    points = {"09:35": "09:30", "09:40": "09:35", "09:45": "09:40", "10:00": "09:55",
              "10:15": "10:10", "10:30": "10:25", "11:00": "10:55"}
    rows = {}
    for lab, m in (("all", gap.notna()), ("gap up", gap > 0), ("gap down", gap < 0),
                   ("gap >= +2%", gap >= 0.02), ("gap <= -2%", gap <= -0.02)):
        row = {"n": int(m.sum())}
        for t_end, bar in points.items():
            d = (closes[bar] / opens - 1)[m]
            row[t_end] = d.mean() * 100
        rows[lab] = row
    print(f"\n=== PART D - first 90 minutes, 5-minute bars ({f['date'].nunique()} sessions "
          f"{f.index[0].date()} -> {f.index[-1].date()}) ===")
    print("Average % move from the 09:30 open to each time. Very small sample: indicative only.")
    print(fmt(pd.DataFrame(rows).T))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BE")
    ap.add_argument("--hourly-csv")
    ap.add_argument("--daily-csv")
    ap.add_argument("--fivemin-csv")
    ap.add_argument("--cost", type=float, default=0.0)
    args = ap.parse_args()
    hourly = read_csv(args.hourly_csv) if args.hourly_csv else yf_download(args.symbol, period="730d", interval="60m")
    daily = read_csv(args.daily_csv) if args.daily_csv else yf_download(args.symbol, period="max", interval="1d")
    five = read_csv(args.fivemin_csv) if args.fivemin_csv else yf_download(args.symbol, period="60d", interval="5m")

    t = build_trades(hourly)
    print(f"Overnight trades from hourly data: {t.index[0].date()} -> {t.index[-1].date()}")
    part_a(t)
    part_b(t, args.cost)
    part_c(t, daily)
    part_d(five)
    print(f"\nCost per side {args.cost*100:.2f}%. Past results do not predict future results. "
          "Not financial advice.")


if __name__ == "__main__":
    main()
