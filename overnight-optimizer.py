#!/usr/bin/env python3
"""
Overnight-hold optimiser for one stock (default: BE). No trading costs unless --cost.

Part A - entry/exit time grid (hourly data, New York time):
    buy at one of 13:30 / 14:30 / 15:30 / 16:00(close) on day d,
    sell at one of 09:30(open) / 10:30 / 11:30 / 12:30 / 13:30 on day d+1.
    Prices are hourly-bar closes (09:30 = first bar's open).

Part B - which nights to hold (daily data):
    overnight return = next open / today's close, bucketed by information
    known at today's close (day move, close location in range, RSI, trend,
    weekday, volatility, volume). Buckets are measured on a TRAIN period and
    checked on a TEST period (--split) so a filter only counts if it holds
    out of sample.

Part C - conditional exit (hourly): after holding overnight, sell at the
    open if the gap is >= a threshold, otherwise keep until a later hour.

Part D - candidate strategies compared with buy-and-hold, per year.

Data: --csv daily, --hourly-csv hourly (Date,Open,High,Low,Close,Volume), or
yfinance. Past results do not predict future results. Not financial advice.
"""

import argparse
import sys

import numpy as np
import pandas as pd

ENTRY_TIMES = ["13:30", "14:30", "15:30", "16:00"]
EXIT_TIMES = ["09:30", "10:30", "11:30", "12:30", "13:30"]


# ------------------------------------------------------------------ loading
def yf_download(symbol, **kw):
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("Pass --csv/--hourly-csv or `pip install yfinance`.")
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


def hourly_price_points(h):
    """One row per session: price at 09:30 (open), 10:30, ..., 16:00 (close)."""
    h = h[(h.index.time >= pd.Timestamp("09:30").time()) & (h.index.time < pd.Timestamp("16:00").time())]
    h = h.assign(date=h.index.normalize(), slot=h.index.strftime("%H:%M"))
    closes = h.pivot_table(index="date", columns="slot", values="Close", aggfunc="last")
    first_open = h.groupby("date")["Open"].first()
    last_close = h.groupby("date")["Close"].last()
    # a bar starting at HH:30 closes at HH+1:30
    p = pd.DataFrame(index=closes.index)
    p["09:30"] = first_open
    for start, end in (("09:30", "10:30"), ("10:30", "11:30"), ("11:30", "12:30"),
                       ("12:30", "13:30"), ("13:30", "14:30"), ("14:30", "15:30")):
        p[end] = closes.get(start)
    p["15:30"] = p["15:30"]
    p["16:00"] = last_close
    full = closes.get("15:30").notna()          # drop half-day sessions from intraday points
    p.loc[~full, ["13:30", "14:30", "15:30"]] = np.nan
    return p


# ------------------------------------------------------------------- helpers
def t_stat(s):
    s = pd.Series(s).dropna()
    return s.mean() / (s.std() / np.sqrt(len(s))) if len(s) > 2 and s.std() > 0 else np.nan


def growth(s, cost=0.0):
    s = pd.Series(s).dropna()
    return float(np.prod((1 + s) * (1 - cost) ** 2))


def summary(s, cost=0.0):
    s = pd.Series(s).dropna()
    return {"n": len(s), "avg%": s.mean() * 100, "win%": (s > 0).mean() * 100,
            "t": t_stat(s), "$1->": growth(s, cost)}


def fmt(df, f="{:8.2f}"):
    with pd.option_context("display.float_format", f.format, "display.width", 220):
        return df.to_string()


def add_daily_features(d):
    d = d.copy()
    c = d["Close"]
    d["ret"] = c.pct_change()
    d["intraday"] = c / d["Open"] - 1
    d["overnight_next"] = d["Open"].shift(-1) / c - 1           # hold tonight
    d["gap_today"] = d["Open"] / c.shift(1) - 1
    rng = (d["High"] - d["Low"]).replace(0, np.nan)
    d["clv"] = (c - d["Low"]) / rng                              # 0 = closed at low, 1 = at high
    delta = c.diff()
    g = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    l = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    d["rsi"] = 100 - 100 / (1 + g / l)
    d["sma20"] = c.rolling(20).mean()
    d["sma50"] = c.rolling(50).mean()
    d["vol20"] = d["ret"].rolling(20).std() * np.sqrt(252)
    d["vratio"] = d["Volume"] / d["Volume"].rolling(20).mean()
    d["weekday"] = d.index.day_name().str[:3]
    return d


FILTERS = {
    "all nights":                lambda d: pd.Series(True, index=d.index),
    "day up":                    lambda d: d["ret"] > 0,
    "day down":                  lambda d: d["ret"] < 0,
    "day <= -3%":                lambda d: d["ret"] <= -0.03,
    "day >= +3%":                lambda d: d["ret"] >= 0.03,
    "intraday up (open->close)": lambda d: d["intraday"] > 0,
    "intraday down":             lambda d: d["intraday"] < 0,
    "close near high (clv>0.7)": lambda d: d["clv"] > 0.7,
    "close near low (clv<0.3)":  lambda d: d["clv"] < 0.3,
    "close > SMA20":             lambda d: d["Close"] > d["sma20"],
    "close < SMA20":             lambda d: d["Close"] < d["sma20"],
    "close > SMA50":             lambda d: d["Close"] > d["sma50"],
    "close < SMA50":             lambda d: d["Close"] < d["sma50"],
    "RSI < 40":                  lambda d: d["rsi"] < 40,
    "RSI 40-60":                 lambda d: d["rsi"].between(40, 60),
    "RSI > 60":                  lambda d: d["rsi"] > 60,
    "high vol (vol20 > median)": lambda d: d["vol20"] > d["vol20"].median(),
    "low vol (vol20 <= median)": lambda d: d["vol20"] <= d["vol20"].median(),
    "volume > 1.5x":             lambda d: d["vratio"] > 1.5,
    "volume < 1x":               lambda d: d["vratio"] < 1,
    "Mon night":                 lambda d: d["weekday"] == "Mon",
    "Tue night":                 lambda d: d["weekday"] == "Tue",
    "Wed night":                 lambda d: d["weekday"] == "Wed",
    "Thu night":                 lambda d: d["weekday"] == "Thu",
    "Fri (weekend) night":       lambda d: d["weekday"] == "Fri",
}


# ------------------------------------------------------------------- parts
def part_a(p, cost):
    print("\n" + "=" * 100)
    print("PART A - entry time (day d) x exit time (day d+1), hourly data "
          f"{p.index[0].date()} -> {p.index[-1].date()}")
    nxt = p.shift(-1)
    grow, avg, tstat = {}, {}, {}
    for e in ENTRY_TIMES:
        grow[e], avg[e], tstat[e] = {}, {}, {}
        for x in EXIT_TIMES:
            r = nxt[x] / p[e] - 1
            s = summary(r, cost)
            grow[e][x], avg[e][x], tstat[e][x] = s["$1->"], s["avg%"], s["t"]
    print("\n$1 grows to (rows = buy time, cols = sell time next day):")
    print(fmt(pd.DataFrame(grow).T))
    print("\nAverage return per trade %:")
    print(fmt(pd.DataFrame(avg).T, "{:8.3f}"))
    print("\nt-stat:")
    print(fmt(pd.DataFrame(tstat).T))
    half = p.index[len(p) // 2]
    print(f"\nStability: same grid, first half vs second half (split {half.date()}), $1 grows to")
    for lab, part in (("first half", p[p.index < half]), ("second half", p[p.index >= half])):
        n2 = part.shift(-1)
        g = {e: {x: growth(n2[x] / part[e] - 1, cost) for x in EXIT_TIMES} for e in ENTRY_TIMES}
        print(f"-- {lab}\n" + fmt(pd.DataFrame(g).T))


def part_b(d, split, cost):
    print("\n" + "=" * 100)
    print(f"PART B - which nights to hold (daily data, overnight = next open / close). "
          f"TRAIN < {split.date()} <= TEST")
    rows = {}
    train, test = d[d.index < split], d[d.index >= split]
    for name, f in FILTERS.items():
        a = summary(train.loc[f(train).fillna(False), "overnight_next"], cost)
        b = summary(test.loc[f(test).fillna(False), "overnight_next"], cost)
        rows[name] = {"train_n": a["n"], "train_avg%": a["avg%"], "train_t": a["t"],
                      "test_n": b["n"], "test_avg%": b["avg%"], "test_t": b["t"],
                      "test_$1->": b["$1->"]}
    t = pd.DataFrame(rows).T
    base_tr, base_te = t.loc["all nights", "train_avg%"], t.loc["all nights", "test_avg%"]
    t["holds_out_of_sample"] = np.where(
        ((t["train_avg%"] > base_tr) & (t["test_avg%"] > base_te)) |
        ((t["train_avg%"] < base_tr) & (t["test_avg%"] < base_te)), "yes", "no")
    print(fmt(t))
    return t


def part_c(p, cost):
    print("\n" + "=" * 100)
    print("PART C - buy at 16:00 close, conditional exit next day based on the opening gap")
    nxt = p.shift(-1)
    gap = nxt["09:30"] / p["16:00"] - 1
    rows = {}
    rows["always sell 09:30"] = summary(gap, cost)
    for later in ("10:30", "11:30"):
        rows[f"always sell {later}"] = summary(nxt[later] / p["16:00"] - 1, cost)
        for thr in (-0.02, 0.0, 0.01, 0.02, 0.03):
            r = np.where(gap >= thr, gap, nxt[later] / p["16:00"] - 1)
            rows[f"gap >= {thr:+.0%}: sell open, else {later}"] = summary(pd.Series(r, index=p.index), cost)
        for thr in (0.0, -0.02):
            r = np.where(gap <= thr, gap, nxt[later] / p["16:00"] - 1)
            rows[f"gap <= {thr:+.0%}: sell open, else {later}"] = summary(pd.Series(r, index=p.index), cost)
    print(fmt(pd.DataFrame(rows).T))


def part_d(d, split, cost):
    print("\n" + "=" * 100)
    print("PART D - candidate daily-data strategies (no look-ahead: decided at today's close, "
          "exit at next open)")
    on = d["overnight_next"]
    cands = {
        "buy_and_hold (close->close)": d["Close"].pct_change().shift(-1),
        "overnight every night": on,
        "overnight, skip nights after day >= +3%": on.where(~(d["ret"] >= 0.03), 0.0),
        "overnight only if close < SMA20": on.where(d["Close"] < d["sma20"], 0.0),
        "overnight only if day down": on.where(d["ret"] < 0, 0.0),
        "overnight only if high vol": on.where(d["vol20"] > d["vol20"].expanding().median(), 0.0),
    }
    # hybrid: hold full day while in a momentum trade (RSI crossed > 70, until RSI > 80 or close < SMA20),
    # otherwise overnight only
    in_momo = pd.Series(False, index=d.index)
    state = False
    for i in range(1, len(d)):
        r, r_prev = d["rsi"].iat[i], d["rsi"].iat[i - 1]
        if not state and r_prev <= 70 < r:
            state = True
        elif state and (r > 80 or d["Close"].iat[i] < d["sma20"].iat[i]):
            state = False
        in_momo.iat[i] = state
    cc_next = d["Close"].shift(-1) / d["Close"] - 1
    cands["hybrid: momentum -> hold, else overnight"] = cc_next.where(in_momo, on)

    rows, yearly = {}, {}
    for name, r in cands.items():
        r = r.dropna()
        eq = (1 + r).cumprod()
        mdd = (eq / eq.cummax() - 1).min() * 100
        sharpe = r.mean() / r.std() * np.sqrt(252)
        tr, te = r[r.index < split], r[r.index >= split]
        rows[name] = {"$1->": growth(r, cost), "maxDD%": mdd, "sharpe": sharpe,
                      "train $1->": growth(tr, cost), "test $1->": growth(te, cost),
                      "nights_in%": (r != 0).mean() * 100}
        yearly[name] = (r.groupby(r.index.year).apply(lambda s: np.prod(1 + s) - 1) * 100)
    print(fmt(pd.DataFrame(rows).T))
    print("\nReturn % by year:")
    print(fmt(pd.DataFrame(yearly).T, "{:8.1f}"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BE")
    ap.add_argument("--csv")
    ap.add_argument("--hourly-csv")
    ap.add_argument("--split", default="2023-01-01", help="train/test split for daily filters")
    ap.add_argument("--cost", type=float, default=0.0, help="cost per side, default 0")
    args = ap.parse_args()

    daily = read_csv(args.csv) if args.csv else yf_download(args.symbol, period="max", interval="1d")
    hourly = (read_csv(args.hourly_csv) if args.hourly_csv
              else yf_download(args.symbol, period="730d", interval="60m"))
    d = add_daily_features(daily)
    p = hourly_price_points(hourly)
    split = pd.Timestamp(args.split)

    part_a(p, args.cost)
    part_b(d, split, args.cost)
    part_c(p, args.cost)
    part_d(d, split, args.cost)
    print(f"\nCost per side {args.cost*100:.2f}%. Past results do not predict future results. "
          "Not financial advice.")


if __name__ == "__main__":
    main()
