#!/usr/bin/env python3
"""
Trading pattern finder for a single stock (default: BE, Bloom Energy).

Looks for statistically testable patterns in daily bars and reports the
forward returns that followed each one, compared with the stock's
unconditional (baseline) forward return:

  1. Day-of-week effect
  2. Overnight gap vs. intraday behaviour (gap fill / gap continuation)
  3. Big down days (<= -8%) and big up days (>= +8%): bounce or follow-through?
  4. Losing / winning streaks
  5. RSI(14) extremes
  6. Price vs. 50/200-day moving averages (trend regime)
  7. Volatility regime (20-day realised volatility terciles)
  8. Volume spikes (volume > 2x 20-day average)

Data sources (first one that works):
  --csv FILE           a CSV with Date,Open,High,Low,Close,Volume columns
  Alpaca               if APCA_API_KEY_ID / APCA_API_SECRET_KEY are set
  yfinance             pip install yfinance

Usage:
  python3 trading-pattern-finder.py                 # BE, 5 years
  python3 trading-pattern-finder.py --symbol BE --years 3
  python3 trading-pattern-finder.py --csv be.csv

Results describe the past only. Small sample sizes (n < 30) are flagged
and should not be trusted. This is not financial advice.
"""

import argparse
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

HORIZONS = (1, 5, 10, 20)
MIN_SAMPLES = 30


# --------------------------------------------------------------------------- data
def load_csv(path):
    df = pd.read_csv(path)
    df.columns = [c.strip().capitalize() for c in df.columns]
    df["Date"] = pd.to_datetime(df["Date"])
    return df.set_index("Date").sort_index()[["Open", "High", "Low", "Close", "Volume"]]


def load_alpaca(symbol, years):
    key, secret = os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY")
    if not (key and secret):
        return None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import Adjustment
    except ImportError:
        return None
    client = StockHistoricalDataClient(key, secret)
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=datetime.now() - timedelta(days=365 * years),
        adjustment=Adjustment.ALL,
    )
    df = client.get_stock_bars(req).df
    if df.empty:
        return None
    df = df.xs(symbol, level="symbol") if isinstance(df.index, pd.MultiIndex) else df
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    df = df.rename(columns=str.capitalize)
    return df[["Open", "High", "Low", "Close", "Volume"]]


def load_yfinance(symbol, years):
    try:
        import yfinance as yf
    except ImportError:
        return None
    df = yf.download(symbol, period=f"{years}y", interval="1d",
                     auto_adjust=True, progress=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df[["Open", "High", "Low", "Close", "Volume"]]


def load_data(args):
    if args.csv:
        return load_csv(args.csv), f"CSV {args.csv}"
    for name, fn in (("Alpaca", load_alpaca), ("yfinance", load_yfinance)):
        df = fn(args.symbol, args.years)
        if df is not None and len(df) > 0:
            return df, name
    sys.exit("No data source available. Pass --csv, set Alpaca keys, "
             "or `pip install yfinance`.")


# --------------------------------------------------------------------- features
def rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)


def add_features(df):
    df = df.copy()
    c = df["Close"]
    df["ret"] = c.pct_change()
    df["gap"] = df["Open"] / c.shift(1) - 1
    df["intraday"] = c / df["Open"] - 1
    for h in HORIZONS:
        df[f"fwd{h}"] = c.shift(-h) / c - 1
    df["rsi"] = rsi(c)
    df["sma50"] = c.rolling(50).mean()
    df["sma200"] = c.rolling(200).mean()
    df["vol20"] = df["ret"].rolling(20).std() * np.sqrt(252)
    df["volu_ratio"] = df["Volume"] / df["Volume"].rolling(20).mean()
    sign = np.sign(df["ret"]).fillna(0)
    grp = (sign != sign.shift()).cumsum()
    df["streak"] = sign.groupby(grp).cumcount().add(1) * sign
    return df


# ---------------------------------------------------------------------- report
def summarise(df, mask, label):
    sub = df[mask]
    row = {"pattern": label, "n": int(len(sub))}
    for h in HORIZONS:
        s = sub[f"fwd{h}"].dropna()
        row[f"avg{h}d"] = s.mean() * 100 if len(s) else np.nan
        row[f"win{h}d"] = (s > 0).mean() * 100 if len(s) else np.nan
    row["note"] = "" if len(sub) >= MIN_SAMPLES else "small n"
    return row


def print_table(title, rows, baseline):
    print(f"\n=== {title} ===")
    t = pd.DataFrame(rows).set_index("pattern")
    for h in HORIZONS:
        t[f"edge{h}d"] = t[f"avg{h}d"] - baseline[f"avg{h}d"]
    cols = ["n"] + [f"avg{h}d" for h in HORIZONS] + ["win5d", "win20d", "edge5d", "edge20d", "note"]
    with pd.option_context("display.float_format", "{:6.2f}".format, "display.width", 200):
        print(t[cols].to_string())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BE")
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--csv")
    ap.add_argument("--big-move", type=float, default=0.08, help="threshold for big up/down day (default 0.08)")
    args = ap.parse_args()

    raw, source = load_data(args)
    df = add_features(raw).dropna(subset=["ret"])
    print(f"{args.symbol}: {len(df)} daily bars {df.index[0].date()} -> {df.index[-1].date()} (source: {source})")
    print("avgNd = mean forward N-day return %, winNd = % of cases positive, "
          "edgeNd = avg minus baseline")

    all_days = pd.Series(True, index=df.index)
    baseline = summarise(df, all_days, "ALL DAYS (baseline)")

    # 1. day of week
    rows = [baseline] + [summarise(df, df.index.dayofweek == i, d)
                         for i, d in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri"])]
    print_table("Day of week", rows, baseline)
    dow = df.groupby(df.index.day_name().str[:3])["ret"].mean() * 100
    print("Same-day avg return % by weekday:", dow.round(2).to_dict())

    # 2. gaps
    g = 0.03
    rows = [baseline,
            summarise(df, df["gap"] >= g, f"gap up >= {g:.0%}"),
            summarise(df, df["gap"] <= -g, f"gap down <= -{g:.0%}")]
    print_table("Overnight gaps (forward from that close)", rows, baseline)
    for lab, m in ((f"gap up >= {g:.0%}", df["gap"] >= g), (f"gap down <= -{g:.0%}", df["gap"] <= -g)):
        s = df.loc[m, "intraday"]
        if len(s):
            cont = (np.sign(s) == np.sign(df.loc[m, "gap"])).mean() * 100
            print(f"  {lab}: n={len(s)}, same-day open->close avg {s.mean()*100:.2f}%, "
                  f"continued in gap direction {cont:.0f}% of the time")

    # 3. big moves
    b = args.big_move
    rows = [baseline,
            summarise(df, df["ret"] <= -b, f"day <= -{b:.0%}"),
            summarise(df, df["ret"] >= b, f"day >= +{b:.0%}")]
    print_table("Big single-day moves", rows, baseline)

    # 4. streaks
    rows = [baseline] + [summarise(df, df["streak"] == k, f"streak {k:+d}")
                         for k in (-5, -4, -3, 3, 4, 5)]
    rows += [summarise(df, df["streak"] <= -6, "streak <= -6"), summarise(df, df["streak"] >= 6, "streak >= +6")]
    print_table("Consecutive down (-) / up (+) closes", rows, baseline)

    # 5. RSI
    rows = [baseline,
            summarise(df, df["rsi"] < 30, "RSI < 30"),
            summarise(df, (df["rsi"] >= 30) & (df["rsi"] <= 70), "RSI 30-70"),
            summarise(df, df["rsi"] > 70, "RSI > 70"),
            summarise(df, df["rsi"] > 80, "RSI > 80")]
    print_table("RSI(14)", rows, baseline)

    # 6. trend regime
    rows = [baseline,
            summarise(df, (df["Close"] > df["sma50"]) & (df["sma50"] > df["sma200"]), "close>SMA50>SMA200"),
            summarise(df, (df["Close"] < df["sma50"]) & (df["sma50"] < df["sma200"]), "close<SMA50<SMA200"),
            summarise(df, (df["Close"] < df["sma50"]) & (df["sma50"] > df["sma200"]), "pullback in uptrend")]
    print_table("Trend regime (moving averages)", rows, baseline)

    # 7. volatility regime
    v = df["vol20"].dropna()
    if len(v) > 60:
        lo, hi = v.quantile([1 / 3, 2 / 3])
        rows = [baseline,
                summarise(df, df["vol20"] <= lo, f"low vol (<= {lo:.0%} ann.)"),
                summarise(df, (df["vol20"] > lo) & (df["vol20"] <= hi), "mid vol"),
                summarise(df, df["vol20"] > hi, f"high vol (> {hi:.0%} ann.)")]
        print_table("20-day realised volatility regime", rows, baseline)

    # 8. volume spikes
    spike = df["volu_ratio"] > 2
    rows = [baseline,
            summarise(df, spike & (df["ret"] > 0), "vol>2x & up day"),
            summarise(df, spike & (df["ret"] < 0), "vol>2x & down day")]
    print_table("Volume spikes", rows, baseline)

    # current state
    last = df.iloc[-1]
    print("\n=== Current state (last bar) ===")
    print(f"date {df.index[-1].date()}  close {last['Close']:.2f}  day {last['ret']*100:+.2f}%  "
          f"RSI {last['rsi']:.1f}  streak {int(last['streak']):+d}  "
          f"20d vol {last['vol20']:.0%}  vol ratio {last['volu_ratio']:.2f}")
    print(f"SMA50 {last['sma50']:.2f}  SMA200 {last['sma200']:.2f}")
    print("\nPast patterns are not guarantees. Patterns with 'small n' are unreliable. "
          "Not financial advice.")


if __name__ == "__main__":
    main()
