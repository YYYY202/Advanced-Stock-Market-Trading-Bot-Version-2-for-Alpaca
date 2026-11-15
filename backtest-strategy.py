#!/usr/bin/env python3
"""
Backtest simple daily rules on one stock (default: BE) against buy-and-hold.

Rules tested (signals use the day's close; orders fill at the NEXT day's open,
so there is no look-ahead):

  dip8_hold5/10/20   buy after a close-to-close drop <= -8%, sell after N days
  dip8_vol_hold10    same, but only when volume > 2x its 20-day average
  dip8_rsi80         buy after a -8% drop, sell when RSI(14) > 80 or after 20 days
  dip8_rsi80_stop15  same, plus a -15% stop loss (checked on close)
  momo_rsi70         buy when RSI crosses above 70, sell when RSI > 80 or close < SMA20
  trend_sma200       hold while close > SMA200

Every trade pays --cost (default 0.15%) per side for commission + slippage.

The period can be split with --split YYYY-MM-DD to compare an "in-sample"
period with an "out-of-sample" one. Results are per period, with equity
restarting at 1.0 in each.

Data: --csv FILE (Date,Open,High,Low,Close,Volume), else yfinance.
Not financial advice: past results do not predict future results.
"""

import argparse
import sys

import numpy as np
import pandas as pd


def load(args):
    if args.csv:
        df = pd.read_csv(args.csv, parse_dates=["Date"]).set_index("Date").sort_index()
        return df
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("Pass --csv or `pip install yfinance`.")
    df = yf.download(args.symbol, period="max", interval="1d", auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df[["Open", "High", "Low", "Close", "Volume"]]


def indicators(df):
    df = df.copy()
    c = df["Close"]
    df["ret"] = c.pct_change()
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + g / l)
    df["sma20"] = c.rolling(20).mean()
    df["sma200"] = c.rolling(200).mean()
    df["vratio"] = df["Volume"] / df["Volume"].rolling(20).mean()
    return df


# Each rule: entry(df, i) -> bool, exit(df, i, entry_i, entry_px) -> bool
def make_rules():
    dip = lambda df, i: df["ret"].iat[i] <= -0.08
    rules = {}
    for h in (5, 10, 20):
        rules[f"dip8_hold{h}"] = (dip, lambda df, i, e, p, h=h: i - e >= h - 1)
    rules["dip8_vol_hold10"] = (lambda df, i: dip(df, i) and df["vratio"].iat[i] > 2,
                                lambda df, i, e, p: i - e >= 9)
    rules["dip8_rsi80"] = (dip, lambda df, i, e, p: df["rsi"].iat[i] > 80 or i - e >= 19)
    rules["dip8_rsi80_stop15"] = (dip, lambda df, i, e, p: df["rsi"].iat[i] > 80 or i - e >= 19
                                  or df["Close"].iat[i] / p - 1 <= -0.15)
    rules["momo_rsi70"] = (lambda df, i: i > 0 and df["rsi"].iat[i - 1] <= 70 < df["rsi"].iat[i],
                           lambda df, i, e, p: df["rsi"].iat[i] > 80 or df["Close"].iat[i] < df["sma20"].iat[i])
    rules["trend_sma200"] = (lambda df, i: df["Close"].iat[i] > df["sma200"].iat[i],
                             lambda df, i, e, p: df["Close"].iat[i] < df["sma200"].iat[i])
    return rules


def run(df, entry, exit_, cost):
    """Return daily equity curve (start 1.0) and list of trade returns."""
    o, c = df["Open"].to_numpy(), df["Close"].to_numpy()
    n = len(df)
    eq = np.ones(n)
    cash, shares = 1.0, 0.0
    pending = None          # "buy" / "sell" to execute at next open
    entry_i, entry_px, trades = None, None, []
    for i in range(n):
        if pending == "buy":
            entry_px = o[i] * (1 + cost)
            shares, cash, entry_i = cash / entry_px, 0.0, i
        elif pending == "sell":
            px = o[i] * (1 - cost)
            cash, shares = shares * px, 0.0
            trades.append(px / entry_px - 1)
        pending = None
        eq[i] = cash + shares * c[i]
        if i == n - 1:
            break
        if shares == 0 and entry(df, i):
            pending = "buy"
        elif shares > 0 and exit_(df, i, entry_i, entry_px):
            pending = "sell"
    if shares > 0:  # mark open trade at last close
        trades.append(c[-1] * (1 - cost) / entry_px - 1)
    return pd.Series(eq, index=df.index), trades


def stats(eq, trades, in_mkt_days=None):
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    total = eq.iat[-1] / eq.iat[0] - 1
    cagr = (1 + total) ** (1 / yrs) - 1 if yrs > 0 and total > -1 else np.nan
    dr = eq.pct_change().dropna()
    sharpe = dr.mean() / dr.std() * np.sqrt(252) if dr.std() > 0 else np.nan
    mdd = (eq / eq.cummax() - 1).min()
    t = np.array(trades) if trades else np.array([np.nan])
    return {
        "total%": total * 100, "CAGR%": cagr * 100, "maxDD%": mdd * 100, "sharpe": sharpe,
        "trades": len(trades), "win%": np.nanmean(t > 0) * 100 if trades else np.nan,
        "avg_trade%": np.nanmean(t) * 100, "exposure%": in_mkt_days,
    }


def evaluate(df, cost, label):
    rows = {}
    bh = df["Close"] / df["Close"].iat[0]
    rows["buy_and_hold"] = stats(bh, [bh.iat[-1] - 1], 100.0)
    for name, (en, ex) in make_rules().items():
        eq, trades = run(df, en, ex, cost)
        exposure = (eq.pct_change().fillna(0) != 0).mean() * 100
        rows[name] = stats(eq, trades, exposure)
    t = pd.DataFrame(rows).T
    print(f"\n=== {label}: {df.index[0].date()} -> {df.index[-1].date()} "
          f"({len(df)} days, close {df['Close'].iat[0]:.2f} -> {df['Close'].iat[-1]:.2f}) ===")
    with pd.option_context("display.float_format", "{:8.2f}".format, "display.width", 200):
        print(t.to_string())
    return t


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BE")
    ap.add_argument("--csv")
    ap.add_argument("--cost", type=float, default=0.0015, help="cost per side (default 0.0015 = 0.15%%)")
    ap.add_argument("--split", help="YYYY-MM-DD: also report before/after this date")
    ap.add_argument("--yearly", action="store_true", help="also print per-calendar-year returns")
    args = ap.parse_args()

    df = indicators(load(args))
    # indicators need warm-up; trade only once SMA200 exists
    df = df[df["sma200"].notna()]
    evaluate(df, args.cost, "FULL PERIOD")
    if args.split:
        s = pd.Timestamp(args.split)
        evaluate(df[df.index < s], args.cost, "BEFORE split")
        evaluate(df[df.index >= s], args.cost, "AFTER split")
    if args.yearly:
        out = {}
        for y, part in df.groupby(df.index.year):
            if len(part) < 50:
                continue
            row = {"buy_and_hold": (part["Close"].iat[-1] / part["Close"].iat[0] - 1) * 100}
            for name, (en, ex) in make_rules().items():
                eq, _ = run(part, en, ex, args.cost)
                row[name] = (eq.iat[-1] - 1) * 100
            out[y] = row
        print("\n=== Return % by calendar year (each year traded independently) ===")
        with pd.option_context("display.float_format", "{:8.1f}".format, "display.width", 200):
            print(pd.DataFrame(out).to_string())
    print(f"\nFills at next open, cost per side {args.cost*100:.2f}%. Past results do not predict "
          "future results. Not financial advice.")


if __name__ == "__main__":
    main()
