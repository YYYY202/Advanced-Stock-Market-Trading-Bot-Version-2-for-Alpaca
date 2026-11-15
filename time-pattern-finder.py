#!/usr/bin/env python3
"""
Calendar / time-of-day pattern finder for one stock (default: BE).

Daily data (--csv, or yfinance max history):
  - month of year
  - turn of month (last trading day + first 3) vs. the rest of the month
  - day of week
  - overnight (prev close -> open) vs. intraday (open -> close): which one
    has carried the stock's return?

Hourly data (--hourly-csv, or yfinance 60m bars, max ~730 days):
  - average return and average absolute move per hour of the session
  - does the first hour predict the rest of the day?
  - does the last hour predict the next day's open?

Every bucket shows a t-stat (mean / standard error). |t| < 2 means the
difference is within normal noise for a stock this volatile.

CSV format: Date,Open,High,Low,Close,Volume (hourly Date in New York time).
Not financial advice.
"""

import argparse
import sys

import numpy as np
import pandas as pd


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


def bucket_table(values, keys, order=None):
    g = pd.DataFrame({"v": values, "k": keys}).dropna().groupby("k")["v"]
    t = pd.DataFrame({
        "n": g.size(),
        "avg%": g.mean() * 100,
        "median%": g.median() * 100,
        "win%": g.apply(lambda s: (s > 0).mean() * 100),
        "t_stat": g.mean() / (g.std() / np.sqrt(g.size())),
    })
    if order is not None:
        t = t.reindex([o for o in order if o in t.index])
    return t


def show(title, t):
    print(f"\n=== {title} ===")
    with pd.option_context("display.float_format", "{:7.2f}".format, "display.width", 200):
        print(t.to_string())


def daily_analysis(df):
    df = df.copy()
    c = df["Close"]
    df["ret"] = c.pct_change()
    df["overnight"] = df["Open"] / c.shift(1) - 1
    df["intraday"] = c / df["Open"] - 1
    df = df.dropna(subset=["ret"])
    print(f"Daily: {len(df)} bars {df.index[0].date()} -> {df.index[-1].date()}")

    # month of year: monthly return per calendar month
    m = c.resample("ME").last().pct_change().dropna()
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    show("Month of year (monthly returns)", bucket_table(m.values, m.index.strftime("%b"), months))

    # turn of month
    ym = df.index.to_period("M")
    pos = df.groupby(ym).cumcount()
    from_end = df.groupby(ym).cumcount(ascending=False)
    tom = np.where((pos < 3) | (from_end == 0), "turn of month (last+first 3)",
                   np.where(pos < 10, "early month (day 4-10)", "mid/late month"))
    show("Turn of month (daily returns)", bucket_table(df["ret"].values, tom))

    show("Day of week (daily returns)",
         bucket_table(df["ret"].values, df.index.day_name().str[:3],
                      ["Mon", "Tue", "Wed", "Thu", "Fri"]))

    # overnight vs intraday
    print("\n=== Overnight vs intraday (growth of $1 holding only that part of each day) ===")
    for col, lab in (("ret", "close->close (buy & hold)"), ("overnight", "overnight only (close->open)"),
                     ("intraday", "intraday only (open->close)")):
        s = df[col].dropna()
        print(f"  {lab:34s} ${np.prod(1 + s):10.2f}   avg/day {s.mean()*100:6.3f}%  "
              f"t={s.mean()/(s.std()/np.sqrt(len(s))):5.2f}")
    yr = df.groupby(df.index.year)[["overnight", "intraday"]].apply(lambda x: (1 + x).prod() - 1) * 100
    show("Overnight vs intraday return % by year", yr)


def hourly_analysis(h):
    h = h.copy()
    h = h[(h.index.time >= pd.Timestamp("09:30").time()) & (h.index.time < pd.Timestamp("16:00").time())]
    h["date"] = h.index.normalize()
    h["slot"] = h.index.strftime("%H:%M")
    # return of each bar: first bar of the day measured from its open, others from prior bar close
    prev_close = h.groupby("date")["Close"].shift(1)
    h["bar_ret"] = np.where(prev_close.isna(), h["Close"] / h["Open"] - 1, h["Close"] / prev_close - 1)
    print(f"\nHourly: {len(h)} bars, {h['date'].nunique()} sessions "
          f"{h.index[0].date()} -> {h.index[-1].date()}")
    t = bucket_table(h["bar_ret"].values, h["slot"].values)
    t["avg_abs%"] = h.groupby("slot")["bar_ret"].apply(lambda s: s.abs().mean() * 100)
    t["avg_vol_share%"] = (h.groupby("slot")["Volume"].sum() / h["Volume"].sum() * 100)
    show("Hour of session (bar start time, New York)", t)

    days = h.groupby("date").agg(open=("Open", "first"), close=("Close", "last"))
    first = h.groupby("date").apply(lambda d: d["Close"].iat[0] / d["Open"].iat[0] - 1)
    rest = h.groupby("date").apply(lambda d: d["Close"].iat[-1] / d["Close"].iat[0] - 1 if len(d) > 1 else np.nan)
    last = h.groupby("date").apply(lambda d: d["Close"].iat[-1] / d["Close"].iat[-2] - 1 if len(d) > 1 else np.nan)
    next_gap = days["open"].shift(-1) / days["close"] - 1
    df = pd.DataFrame({"first": first, "rest": rest, "last": last, "next_gap": next_gap}).dropna()
    print("\n=== Intraday follow-through ===")
    print(f"  corr(first hour, rest of day)     = {df['first'].corr(df['rest']):+.3f}  (n={len(df)})")
    print(f"  corr(last hour, next-day gap)     = {df['last'].corr(df['next_gap']):+.3f}")
    for lab, m in (("first hour > +2%", df["first"] > 0.02), ("first hour < -2%", df["first"] < -0.02)):
        s = df.loc[m, "rest"]
        if len(s):
            print(f"  {lab}: n={len(s)}, rest of day avg {s.mean()*100:+.2f}%, "
                  f"same direction {(np.sign(s) == np.sign(df.loc[m, 'first'])).mean()*100:.0f}%")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BE")
    ap.add_argument("--csv", help="daily CSV")
    ap.add_argument("--hourly-csv", help="hourly CSV (New York time)")
    args = ap.parse_args()

    daily = read_csv(args.csv) if args.csv else yf_download(args.symbol, period="max", interval="1d")
    daily_analysis(daily)
    hourly = (read_csv(args.hourly_csv) if args.hourly_csv
              else yf_download(args.symbol, period="730d", interval="60m"))
    hourly_analysis(hourly)
    print("\n|t| < 2 is usually noise. Past patterns are not guarantees. Not financial advice.")


if __name__ == "__main__":
    main()
