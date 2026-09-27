"""Load stock price files into a local SQLite database and read them back for preprocessing.

Usage:
    python price_db.py                      # ingest every file in the default folder
    python price_db.py "D:\\some\\folder"   # ingest a different folder (or a single file)

In a notebook:
    from price_db import load_prices
    px = load_prices()                              # tickers as rows, one column per date
    px = load_prices(["RELIANCE", "TCS"], start="2020-01-01")
    ts = load_prices(wide=False)                    # long: ticker, date, close, source

Prices are stored long (ticker, date) -> close because SQLite caps a table at 2000 columns
and the daily history alone has 6000+ dates; load_prices() pivots back to the wide view.

Re-running is safe: unchanged files are skipped, new files/dates are added. When two files
disagree on the same ticker+date, the value already stored is kept and the disagreement is
written to the `price_conflicts` table for review.
"""
import hashlib
import os
import re
import sqlite3
import sys

import pandas as pd

DEFAULT_SOURCE_DIR = r"C:\Users\Singh\Desktop\Daily Prices"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prices.db")
FILE_TYPES = (".xlsx", ".xlsm", ".xls", ".csv")
REL_TOLERANCE = 1e-4  # differences smaller than 0.01% are rounding, not conflicts

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingested_files (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL UNIQUE,   -- file name
    sha1        TEXT NOT NULL,
    rows        INTEGER NOT NULL,
    ingested_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS prices (
    ticker    TEXT NOT NULL,
    date      TEXT NOT NULL,            -- ISO yyyy-mm-dd
    close     REAL NOT NULL,
    source_id INTEGER NOT NULL REFERENCES ingested_files(id),  -- file the value came from
    PRIMARY KEY (ticker, date)
) WITHOUT ROWID;
CREATE VIEW IF NOT EXISTS prices_with_source AS
    SELECT p.ticker, p.date, p.close, f.source
    FROM prices p JOIN ingested_files f ON f.id = p.source_id;
CREATE TABLE IF NOT EXISTS price_conflicts (
    ticker          TEXT NOT NULL,
    date            TEXT NOT NULL,
    stored_close    REAL NOT NULL,
    stored_source   TEXT NOT NULL,
    incoming_close  REAL NOT NULL,
    incoming_source TEXT NOT NULL,
    PRIMARY KEY (ticker, date, incoming_source)
);
"""

_SUFFIX = re.compile(r"\s+(IN\s+)?(Equity|Index)\s*$", re.IGNORECASE)


def normalize_ticker(name):
    """'RELIANCE IN Equity ' / 'Reliance' / 'NSEMCAP Equity' -> 'RELIANCE' / 'RELIANCE' / 'NSEMCAP'."""
    if pd.isna(name):
        return None
    t = _SUFFIX.sub("", str(name).strip()).strip().upper()
    return t or None


def _to_dates(values):
    """Parse header/column values to Timestamps; anything that isn't a date becomes NaT."""
    s = pd.Series(list(values), dtype=object)
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    is_dt = s.map(lambda v: isinstance(v, (pd.Timestamp,)) or hasattr(v, "year"))
    out[is_dt] = pd.to_datetime(s[is_dt], errors="coerce")
    # Excel serial day numbers (roughly 1954-2064)
    num = pd.to_numeric(s[~is_dt], errors="coerce")
    serial = num[(num > 20000) & (num < 60000)]
    out[serial.index] = pd.to_datetime(serial, unit="D", origin="1899-12-30")
    # Text dates; Indian files are day-first (03/06/2025 = 3 June)
    txt = s[~is_dt & s.map(lambda v: isinstance(v, str))]
    if len(txt):
        out[txt.index] = pd.to_datetime(txt.str.strip(), errors="coerce", dayfirst=True, format="mixed")
    return out


def _date_share(values):
    vals = [v for v in values if not pd.isna(v)]
    return _to_dates(vals).notna().mean() if vals else 0.0


def parse_sheet(raw):
    """Turn one sheet (read with header=None) into long rows: ticker, date, close.

    Handles three layouts:
      * wide     - tickers down the first column, dates across a header row (title rows above are skipped)
      * transposed - dates down the first column, tickers across a header row
      * long     - columns named like Ticker / Date / Close|Price
    """
    raw = raw.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
    if raw.empty:
        return pd.DataFrame(columns=["ticker", "date", "close"])

    # Long layout: look for a header row containing ticker + date + price column names
    for h in range(min(10, len(raw))):
        names = {str(v).strip().lower(): i for i, v in raw.iloc[h].items() if not pd.isna(v)}
        tcol = next((names[k] for k in names if k in ("ticker", "symbol", "stock", "security", "name")), None)
        dcol = next((names[k] for k in names if k in ("date", "dates", "trade date", "as of")), None)
        pcol = next((names[k] for k in names if k in ("close", "price", "px_last", "last", "adj close", "close price")), None)
        if tcol is not None and dcol is not None and pcol is not None:
            body = raw.iloc[h + 1:]
            long = pd.DataFrame({"ticker": body[tcol], "date": _to_dates(body[dcol]).values, "close": body[pcol].values})
            return _clean(long)

    # Wide / transposed: find the first row (within 10) that is mostly dates after column 0
    for h in range(min(10, len(raw))):
        if _date_share(raw.iloc[h, 1:]) >= 0.8:
            body = raw.iloc[h + 1:]
            dates = _to_dates(raw.iloc[h, 1:]).values
            wide = pd.DataFrame(body.iloc[:, 1:].values, index=body.iloc[:, 0].values, columns=dates)
            wide = wide.loc[:, ~pd.isna(wide.columns)]
            long = wide.rename_axis(index="ticker", columns="date").stack().rename("close").reset_index()
            return _clean(long)
        if h + 1 < len(raw) and _date_share(raw.iloc[h + 1:, 0]) >= 0.8:
            body = raw.iloc[h + 1:]
            wide = pd.DataFrame(body.iloc[:, 1:].values, index=_to_dates(body.iloc[:, 0]).values,
                                columns=raw.iloc[h, 1:].values)
            wide = wide.loc[~pd.isna(wide.index)]
            long = wide.rename_axis(index="date", columns="ticker").stack().rename("close").reset_index()
            return _clean(long)

    raise ValueError("couldn't find a date header row/column or Ticker/Date/Close columns")


def _clean(long):
    long = long.copy()
    long["ticker"] = long["ticker"].map(normalize_ticker)
    long["close"] = pd.to_numeric(long["close"], errors="coerce")
    long["date"] = pd.to_datetime(long["date"], errors="coerce")
    long = long.dropna(subset=["ticker", "date", "close"])
    long = long[long["close"] > 0]  # zeros / negatives are placeholders, not prices
    long["date"] = long["date"].dt.strftime("%Y-%m-%d")
    return long[["ticker", "date", "close"]]


def read_file(path):
    if path.lower().endswith(".csv"):
        sheets = {"csv": pd.read_csv(path, header=None)}
    else:
        sheets = pd.read_excel(path, sheet_name=None, header=None)
    parts, skipped = [], []
    for name, raw in sheets.items():
        try:
            parts.append(parse_sheet(raw))
        except ValueError as e:
            skipped.append(f"{name}: {e}")
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["ticker", "date", "close"])

    # The same ticker can appear twice (or under 'X' and 'X IN Equity'); identical rows collapse,
    # differing ones keep the first and are reported.
    df = df.drop_duplicates()
    dup = df.duplicated(["ticker", "date"], keep=False)
    clashing = sorted(df.loc[dup, "ticker"].unique())
    df = df.drop_duplicates(["ticker", "date"], keep="first")
    return df, skipped, clashing


def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def connect(db_path=DB_PATH):
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    return con


def ingest_file(con, path, force=False):
    source = os.path.basename(path)
    sha = _sha1(path)
    done = con.execute("SELECT sha1 FROM ingested_files WHERE source = ?", (source,)).fetchone()
    if done and done[0] == sha and not force:
        print(f"  skip (unchanged): {source}")
        return

    print(f"  reading: {source}")
    df, skipped, clashing = read_file(path)
    for s in skipped:
        print(f"    sheet skipped - {s}")
    if clashing:
        print(f"    {len(clashing)} ticker(s) listed twice with different prices, kept first: {', '.join(clashing[:20])}")
    with con:
        con.execute("INSERT OR IGNORE INTO ingested_files (source, sha1, rows) VALUES (?, '', 0)", (source,))
        source_id = con.execute("SELECT id FROM ingested_files WHERE source = ?", (source,)).fetchone()[0]
        con.execute("DROP TABLE IF EXISTS temp.staging")
        con.execute("CREATE TEMP TABLE staging (ticker TEXT, date TEXT, close REAL, PRIMARY KEY (ticker, date))")
        con.executemany("INSERT INTO staging VALUES (?, ?, ?)", df.itertuples(index=False, name=None))

        n_conflicts = con.execute("""
            INSERT OR REPLACE INTO price_conflicts
            SELECT s.ticker, s.date, p.close, f.source, s.close, ?
            FROM staging s JOIN prices p USING (ticker, date) JOIN ingested_files f ON f.id = p.source_id
            WHERE p.source_id <> ? AND abs(p.close - s.close) > ? * abs(p.close)
        """, (source, source_id, REL_TOLERANCE)).rowcount
        before = con.total_changes
        # New ticker/date pairs are added; a pair already stored from *this* file is refreshed
        # (the file was edited); a pair stored from another file is left alone.
        con.execute("""
            INSERT INTO prices SELECT ticker, date, close, ? FROM staging WHERE true
            ON CONFLICT (ticker, date) DO UPDATE SET close = excluded.close
            WHERE prices.source_id = excluded.source_id
        """, (source_id,))
        written = con.total_changes - before
        con.execute("UPDATE ingested_files SET sha1 = ?, rows = ?, ingested_at = datetime('now') WHERE id = ?",
                    (sha, len(df), source_id))

    print(f"    {len(df):,} prices, {df['ticker'].nunique():,} tickers, "
          f"{df['date'].min()} to {df['date'].max()}; {written:,} written, "
          f"{n_conflicts:,} conflicts with other files logged")


def ingest(target=DEFAULT_SOURCE_DIR, db_path=DB_PATH, force=False):
    if os.path.isdir(target):
        files = sorted(os.path.join(target, f) for f in os.listdir(target)
                       if f.lower().endswith(FILE_TYPES) and not f.startswith("~$"))
    else:
        files = [target]
    print(f"Database: {db_path}")
    con = connect(db_path)
    try:
        for f in files:
            ingest_file(con, f, force=force)
        n, t, lo, hi = con.execute("SELECT count(*), count(DISTINCT ticker), min(date), max(date) FROM prices").fetchone()
        c = con.execute("SELECT count(*) FROM price_conflicts").fetchone()[0]
        print(f"Total: {n:,} prices, {t:,} tickers, {lo} to {hi}; {c:,} conflicts in price_conflicts")
    finally:
        con.close()


def load_prices(tickers=None, start=None, end=None, sources=None, wide=True, db_path=DB_PATH):
    """Read prices back.

    wide=True  -> DataFrame with tickers as rows and one column per date (like the source Excel);
                  use .T for dates-as-rows time series.
    wide=False -> long DataFrame: ticker, date, close, source.
    `sources` filters by file name, e.g. to use only the daily history.
    """
    sql, args = "SELECT ticker, date, close, source FROM prices_with_source WHERE 1=1", []
    if tickers is not None:
        tickers = [normalize_ticker(t) for t in ([tickers] if isinstance(tickers, str) else tickers)]
        sql += f" AND ticker IN ({','.join('?' * len(tickers))})"
        args += tickers
    if sources is not None:
        sources = [sources] if isinstance(sources, str) else list(sources)
        sql += f" AND source IN ({','.join('?' * len(sources))})"
        args += sources
    if start is not None:
        sql += " AND date >= ?"
        args.append(pd.Timestamp(start).strftime("%Y-%m-%d"))
    if end is not None:
        sql += " AND date <= ?"
        args.append(pd.Timestamp(end).strftime("%Y-%m-%d"))

    with sqlite3.connect(db_path) as con:
        df = pd.read_sql_query(sql, con, params=args, parse_dates=["date"])
    if not wide:
        return df
    return df.pivot(index="ticker", columns="date", values="close").sort_index()


def load_conflicts(db_path=DB_PATH):
    with sqlite3.connect(db_path) as con:
        return pd.read_sql_query("SELECT * FROM price_conflicts ORDER BY ticker, date", con, parse_dates=["date"])


if __name__ == "__main__":
    ingest(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SOURCE_DIR)
