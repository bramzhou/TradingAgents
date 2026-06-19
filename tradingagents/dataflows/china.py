"""China A-share market data: OHLCV + fundamentals via Baostock / Tushare /
AKShare, wired into the vendor-routing layer (``interface.route_to_vendor``) as
fallback sources for 6-digit A-share codes.

Source notes (observed on a restricted network):
  * **Baostock** — keyless, reliable for price; the primary OHLCV source.
  * **Tushare**  — needs ``TUSHARE_TOKEN``; daily price works as a fallback.
  * **AKShare**  — its *price* endpoint is often blocked, but its
    financial-statement endpoint works, so it is the primary *fundamentals*
    source (price is registered last, as a best-effort fallback).

Each function raises :class:`NoMarketDataError` (or ``VendorNotConfiguredError``)
when it has nothing, so the router cleanly falls through to the next vendor.
"""

from __future__ import annotations

import datetime as dt
import os

import pandas as pd

from .errors import NoMarketDataError, VendorNotConfiguredError

_CN_SUFFIXES = (".SH", ".SS", ".SZ", ".BJ")
_OHLCV_COLS = ["Date", "Open", "High", "Low", "Close", "Volume"]


def bare_code(symbol: str) -> str:
    """Strip any exchange suffix, returning the bare 6-digit code."""
    s = symbol.strip().upper()
    for suf in _CN_SUFFIXES:
        if s.endswith(suf):
            return s[: -len(suf)]
    return s


def is_cn_a_share(symbol) -> bool:
    """True for a China A-share: a 6-digit code, optionally with a CN suffix."""
    if not isinstance(symbol, str):
        return False
    code = bare_code(symbol)
    return code.isdigit() and len(code) == 6


def _exchange(code: str) -> str:
    """Baostock/Tushare exchange for a CN code (Shanghai/Shenzhen/Beijing).

    Covers stocks and exchange-traded funds: Shanghai 5/6/9 (incl. 50/51/56/58
    ETF/LOF), Shenzhen 0/1/2/3 (incl. 15/16/18 ETF/LOF), Beijing 4/8.
    """
    head = code[0]
    if head in "569":
        return "sh"
    if head in "0123":
        return "sz"
    return "bj"


def _round_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    for c in ["Open", "High", "Low", "Close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce").round(2)
    df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce")
    return df


# ── Baostock (primary price) ──────────────────────────────────────────────────
def _baostock_df(code: str, start: str, end: str) -> pd.DataFrame:
    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        raise NoMarketDataError(code, code, f"baostock login failed: {lg.error_msg}")
    try:
        rs = bs.query_history_k_data_plus(
            f"{_exchange(code)}.{code}",
            "date,open,high,low,close,volume",
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag="2",  # forward-adjusted (前复权)
        )
        if rs.error_code != "0":
            raise NoMarketDataError(code, code, f"baostock query failed: {rs.error_msg}")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
    finally:
        bs.logout()
    if not rows:
        raise NoMarketDataError(code, code, f"baostock: no rows {start}..{end}")
    return _round_ohlcv(pd.DataFrame(rows, columns=_OHLCV_COLS))


# ── Tushare (fallback price) ──────────────────────────────────────────────────
def _tushare_pro():
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        raise VendorNotConfiguredError("TUSHARE_TOKEN environment variable is not set.")
    import tushare as ts

    return ts.pro_api(token)


def _tushare_df(code: str, start: str, end: str) -> pd.DataFrame:
    pro = _tushare_pro()
    suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}[_exchange(code)]
    d = pro.daily(
        ts_code=f"{code}.{suffix}",
        start_date=start.replace("-", ""),
        end_date=end.replace("-", ""),
    )
    if d is None or d.empty:
        raise NoMarketDataError(code, code, f"tushare: no rows {start}..{end}")
    d = d.sort_values("trade_date")
    out = pd.DataFrame({
        "Date": pd.to_datetime(d["trade_date"], format="%Y%m%d").dt.strftime("%Y-%m-%d"),
        "Open": d["open"], "High": d["high"], "Low": d["low"],
        "Close": d["close"], "Volume": d["vol"],
    })
    return _round_ohlcv(out.reset_index(drop=True))


# ── AKShare (best-effort price; primary fundamentals) ─────────────────────────
def _akshare_df(code: str, start: str, end: str) -> pd.DataFrame:
    import akshare as ak

    d = ak.stock_zh_a_hist(
        symbol=code, period="daily",
        start_date=start.replace("-", ""), end_date=end.replace("-", ""),
        adjust="qfq",
    )
    if d is None or d.empty:
        raise NoMarketDataError(code, code, f"akshare: no rows {start}..{end}")
    out = pd.DataFrame({
        "Date": pd.to_datetime(d["日期"]).dt.strftime("%Y-%m-%d"),
        "Open": d["开盘"], "High": d["最高"], "Low": d["最低"],
        "Close": d["收盘"], "Volume": d["成交量"],
    })
    return _round_ohlcv(out.reset_index(drop=True))


# ── Shared OHLCV loader (price + indicators) ──────────────────────────────────
def cn_ohlcv_df(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """OHLCV DataFrame for an A-share, trying Baostock → Tushare → AKShare."""
    code = bare_code(symbol)
    errors = []
    for fetch in (_baostock_df, _tushare_df, _akshare_df):
        try:
            return fetch(code, start_date, end_date)
        except VendorNotConfiguredError as e:
            errors.append(f"{fetch.__name__}: {e}")
        except NoMarketDataError as e:
            errors.append(f"{fetch.__name__}: {e.detail}")
        except Exception as e:  # noqa: BLE001 — blocked endpoint, network, etc.
            errors.append(f"{fetch.__name__}: {type(e).__name__}: {e}")
    raise NoMarketDataError(symbol, code, "; ".join(errors) or "no CN vendor returned data")


def _stock_data_str(code: str, df: pd.DataFrame, start: str, end: str, source: str) -> str:
    header = (
        f"# Stock data for {code} (China A-share) from {start} to {end}\n"
        f"# Source: {source} (forward-adjusted)\n"
        f"# Total records: {len(df)}\n\n"
    )
    return header + df.to_csv(index=False)


def get_baostock_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    code = bare_code(symbol)
    return _stock_data_str(code, _baostock_df(code, start_date, end_date), start_date, end_date, "Baostock")


def get_tushare_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    code = bare_code(symbol)
    return _stock_data_str(code, _tushare_df(code, start_date, end_date), start_date, end_date, "Tushare")


def get_akshare_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    code = bare_code(symbol)
    return _stock_data_str(code, _akshare_df(code, start_date, end_date), start_date, end_date, "AKShare")


def cn_identity(symbol: str) -> dict:
    """Best-effort A-share identity (name + exchange) via Baostock. Returns ``{}``
    on any failure so the caller falls back to ticker-only context."""
    code = bare_code(symbol)
    try:
        import baostock as bs

        bs.login()
        try:
            rs = bs.query_stock_basic(code=f"{_exchange(code)}.{code}")
            row = rs.get_row_data() if rs.error_code == "0" and rs.next() else None
        finally:
            bs.logout()
    except Exception:  # noqa: BLE001 — never block the run on identity lookup
        return {}
    if not row:
        return {}
    name = (row[1] or "").strip()
    exchanges = {"sh": "Shanghai Stock Exchange", "sz": "Shenzhen Stock Exchange", "bj": "Beijing Stock Exchange"}
    identity = {"exchange": exchanges[_exchange(code)]}
    if name:
        identity["company_name"] = name
    return identity


# ── Fundamentals ──────────────────────────────────────────────────────────────
def get_akshare_fundamentals(ticker: str, curr_date: str | None = None) -> str:
    """Key financial indicators from AKShare's financial-statement endpoint."""
    import akshare as ak

    code = bare_code(ticker)
    d = ak.stock_financial_abstract(symbol=code)
    if d is None or d.empty:
        raise NoMarketDataError(ticker, code, "akshare: no fundamentals")
    # Wide frame: an 指标 (indicator) column plus one column per reporting period
    # (YYYYMMDD), newest first. Keep the indicator + the latest few periods.
    period_cols = [c for c in d.columns if str(c).isdigit() and len(str(c)) == 8]
    period_cols = sorted(period_cols, reverse=True)[:4]
    name_col = "指标" if "指标" in d.columns else d.columns[0]
    cols = [name_col] + period_cols
    table = d[cols].to_markdown(index=False)
    return (
        f"# Fundamentals for {code} (China A-share)\n"
        f"# Source: AKShare financial abstract; latest {len(period_cols)} periods\n\n"
        f"{table}\n"
    )


def _akshare_statement(code: str, sina_symbol: str, title: str, n: int = 4) -> str:
    """A financial statement (latest ``n`` periods) via AKShare's Sina endpoint,
    transposed to line-items × periods."""
    import akshare as ak

    d = ak.stock_financial_report_sina(stock=f"{_exchange(code)}{code}", symbol=sina_symbol)
    if d is None or d.empty or "报告日" not in d.columns:
        raise NoMarketDataError(code, code, f"akshare: no {title}")
    d = d.sort_values("报告日", ascending=False).head(n).set_index("报告日").T
    return (
        f"# {title} for {code} (China A-share)\n"
        f"# Source: AKShare (Sina); latest {len(d.columns)} periods (amounts in RMB)\n\n"
        f"{d.to_markdown()}\n"
    )


def get_akshare_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: str | None = None) -> str:
    return _akshare_statement(bare_code(ticker), "资产负债表", "Balance sheet")


def get_akshare_income_statement(ticker: str, freq: str = "quarterly", curr_date: str | None = None) -> str:
    return _akshare_statement(bare_code(ticker), "利润表", "Income statement")


def get_akshare_cashflow(ticker: str, freq: str = "quarterly", curr_date: str | None = None) -> str:
    return _akshare_statement(bare_code(ticker), "现金流量表", "Cash flow statement")


def get_akshare_news(ticker: str, start_date: str | None = None, end_date: str | None = None) -> str:
    """Recent stock-specific CN news via AKShare/Eastmoney. Feeds the news analyst
    and the sentiment analyst's news block (Yahoo/StockTwits/Reddit don't cover
    A-shares)."""
    import akshare as ak

    code = bare_code(ticker)
    try:
        d = ak.stock_news_em(symbol=code)
    except Exception as e:  # noqa: BLE001 — akshare raises internally (e.g. KeyError)
        # when a code has no news (notably fund codes); degrade to "no data".
        raise NoMarketDataError(code, code, f"akshare news unavailable: {e}") from e
    if d is None or d.empty or "新闻标题" not in d.columns:
        raise NoMarketDataError(code, code, "akshare: no news")
    if start_date and "发布时间" in d.columns:
        d = d[d["发布时间"].astype(str) >= start_date]
    if d.empty:
        raise NoMarketDataError(code, code, f"akshare: no news since {start_date}")
    items = []
    for _, r in d.head(15).iterrows():
        when = str(r.get("发布时间", "")).strip()
        title = str(r.get("新闻标题", "")).strip()
        source = str(r.get("文章来源", "")).strip()
        body = " ".join(str(r.get("新闻内容", "")).split())[:280]
        items.append(f"- [{when}｜{source}] {title}\n  {body}")
    return (
        f"# Recent news for {code} (China A-share)\n"
        f"# Source: AKShare / Eastmoney; {len(items)} items\n\n"
        + "\n".join(items)
        + "\n"
    )


def get_tushare_fundamentals(ticker: str, curr_date: str | None = None) -> str:
    """Financial indicators from Tushare (requires a token with fina_indicator)."""
    code = bare_code(ticker)
    pro = _tushare_pro()
    suffix = {"sh": "SH", "sz": "SZ", "bj": "BJ"}[_exchange(code)]
    end = (curr_date or dt.date.today().isoformat()).replace("-", "")
    start = str(int(end[:4]) - 2) + end[4:]
    d = pro.fina_indicator(ts_code=f"{code}.{suffix}", start_date=start, end_date=end)
    if d is None or d.empty:
        raise NoMarketDataError(ticker, code, "tushare: no fundamentals")
    return (
        f"# Fundamentals for {code} (China A-share)\n"
        f"# Source: Tushare fina_indicator\n\n"
        f"{d.head(4).to_markdown(index=False)}\n"
    )
