"""China open-end fund data via AKShare (Xueqiu profile/fees/allocation,
Eastmoney NAV + holdings).

A fund is analyzed by what it owns and how its NAV behaves — not single-company
fundamentals — so these helpers expose NAV history (with return/risk stats) and a
composition overview (profile, manager, fees, asset allocation, top holdings).
They are driven by ``asset_type`` (fund/etf), not symbol detection, because CN
fund codes are 6 digits and collide with A-share stock codes.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import functools
import math

import pandas as pd

from .errors import NoMarketDataError

# AKShare endpoints occasionally hang indefinitely for some funds (no socket
# timeout of their own), which would stall the whole analyst run. Cap each call.
_AK_TIMEOUT = 30  # seconds


def _t(call, label: str = "akshare call"):
    """Run a blocking akshare call with a hard wall-clock timeout, raising
    ``TimeoutError`` (callers degrade to a sentinel) instead of hanging. The
    worker thread is abandoned, not awaited, so a hung request can't block us."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(call)
    try:
        return fut.result(timeout=_AK_TIMEOUT)
    except concurrent.futures.TimeoutError as e:
        raise TimeoutError(f"{label} timed out after {_AK_TIMEOUT}s") from e
    finally:
        ex.shutdown(wait=False)


def _latest_fiscal_year() -> str:
    # Holdings/allocation are disclosed with a lag; the current or prior year
    # is the safest query.
    return str(dt.date.today().year)


@functools.lru_cache(maxsize=1)
def _fund_list() -> pd.DataFrame:
    import akshare as ak

    return _t(lambda: ak.fund_name_em(), "fund_name_em")


def fund_name_type(symbol: str) -> tuple[str, str]:
    """(short name, category) from the full fund directory — a fallback when the
    per-fund Xueqiu profile endpoint doesn't cover a fund. The list is large, so
    it is fetched once and cached for the process."""
    from .china import bare_code

    try:
        d = _fund_list()
        row = d[d["基金代码"].astype(str) == bare_code(symbol)]
        if not row.empty:
            r = row.iloc[0]
            return str(r.get("基金简称", "") or "").strip(), str(r.get("基金类型", "") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    return "", ""


def fund_identity(symbol: str) -> dict:
    """Best-effort fund identity (name, company, category) so every agent anchors
    to the real fund instead of hallucinating one. Returns ``{}`` on failure."""
    info = {}
    try:
        import akshare as ak

        basic = _t(lambda: ak.fund_individual_basic_info_xq(symbol), "fund_individual_basic_info_xq")
        info = dict(zip(basic["item"], basic["value"]))
    except Exception:  # noqa: BLE001 — never block the run on identity lookup
        info = {}
    # Fall back to the fund directory for name/category when the profile is thin.
    if not str(info.get("基金名称", "") or "").strip():
        name, category = fund_name_type(symbol)
        if name:
            info["基金名称"] = name
        if category and not str(info.get("基金类型", "") or "").strip():
            info["基金类型"] = category
    out = {}
    name = str(info.get("基金名称", "") or "").strip()
    if name:
        out["name"] = name
    company = str(info.get("基金公司", "") or "").strip()
    if company:
        out["company"] = company
    category = str(info.get("基金类型", "") or "").strip()
    if category:
        out["category"] = category
    return out


def _nav_df(symbol: str) -> pd.DataFrame:
    import akshare as ak

    d = _t(lambda: ak.fund_open_fund_info_em(symbol, indicator="单位净值走势"), "fund_open_fund_info_em")
    if d is None or d.empty or "单位净值" not in d.columns:
        raise NoMarketDataError(symbol, symbol, "akshare: no NAV history")
    d = d.rename(columns={"净值日期": "date", "单位净值": "nav"})[["date", "nav"]]
    d["date"] = pd.to_datetime(d["date"])
    d["nav"] = pd.to_numeric(d["nav"], errors="coerce")
    return d.dropna().sort_values("date").reset_index(drop=True)


def _nav_stats(navs: list[float]) -> dict:
    """Total/annualized return, annualized vol and max drawdown from a NAV series."""
    if len(navs) < 2:
        return {}
    total = navs[-1] / navs[0] - 1
    rets = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) if len(rets) > 1 else 0.0
    vol = math.sqrt(var) * math.sqrt(252)
    years = len(navs) / 252
    annualized = (1 + total) ** (1 / years) - 1 if years > 0 else total
    peak = navs[0]
    mdd = 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {
        "total_return": total,
        "annualized_return": annualized,
        "annualized_volatility": vol,
        "max_drawdown": mdd,
    }


def get_fund_nav(symbol: str, start_date: str | None = None, end_date: str | None = None) -> str:
    """NAV history (recent rows) plus return/risk stats over the available series."""
    df = _nav_df(symbol)
    if start_date:
        df = df[df["date"] >= pd.Timestamp(start_date)]
    if end_date:
        df = df[df["date"] <= pd.Timestamp(end_date)]
    if df.empty:
        raise NoMarketDataError(symbol, symbol, "akshare: no NAV in range")
    navs = df["nav"].tolist()
    s = _nav_stats(navs)
    recent = df.tail(60).copy()
    recent["date"] = recent["date"].dt.strftime("%Y-%m-%d")
    stats = (
        f"# Total return {s['total_return'] * 100:.1f}%, annualized {s['annualized_return'] * 100:.1f}%, "
        f"volatility {s['annualized_volatility'] * 100:.1f}%, max drawdown {s['max_drawdown'] * 100:.1f}% "
        f"(over {len(navs)} NAV points)\n"
        if s else ""
    )
    return (
        f"# NAV history for fund {symbol} (China open-end fund; unit NAV, amounts in CNY)\n"
        f"{stats}"
        f"# Showing the latest {len(recent)} of {len(df)} NAV points\n\n"
        + recent.to_csv(index=False)
    )


def get_etf_overview(symbol: str) -> str:
    """ETF composition: name + tracked index, NAV vs market price (premium/
    discount), and top constituent holdings. ETFs trade intraday, so their price
    technicals come from the stock OHLCV path; this covers the fund-side facts."""
    import akshare as ak

    from .china import _baostock_df, bare_code, cn_identity

    code = bare_code(symbol)
    ident = cn_identity(code)
    name = ident.get("company_name", "")
    lines = [f"# ETF overview for {code} (China exchange-traded fund)\n", "## 基本信息"]
    if name:
        lines.append(f"- 名称: {name}")
        lines.append("- 跟踪标的: 见名称所含指数（如沪深300、中证500、创业板等）")

    # Premium/discount = market close vs unit NAV (both latest available).
    last_nav = last_close = None
    try:
        nav_df = _nav_df(code)
        last_nav = float(nav_df["nav"].iloc[-1])
        nav_date = nav_df["date"].iloc[-1].strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        nav_date = ""
    try:
        end = dt.date.today().isoformat()
        start = (dt.date.today() - dt.timedelta(days=20)).isoformat()
        px = _baostock_df(code, start, end)
        last_close = float(px["Close"].iloc[-1])
    except Exception:  # noqa: BLE001
        pass
    if last_nav is not None:
        lines.append(f"- 最新单位净值: {last_nav:.4f}（{nav_date}）")
    if last_close is not None:
        lines.append(f"- 最新收盘价: {last_close:.4f}")
    if last_nav and last_close:
        prem = (last_close - last_nav) / last_nav * 100
        lines.append(f"- 溢价/折价率: {prem:+.2f}%（正为溢价，负为折价）")

    # Top constituents (the ETF's holdings track its index).
    hold = None
    for year in (_latest_fiscal_year(), str(int(_latest_fiscal_year()) - 1)):
        try:
            h = _t(lambda: ak.fund_portfolio_hold_em(symbol=code, date=year), "fund_portfolio_hold_em")
            if h is not None and not h.empty and "股票代码" in h.columns:
                hold = h
                break
        except Exception:  # noqa: BLE001
            continue
    if hold is not None:
        cols = [c for c in ["股票代码", "股票名称", "占净值比例"] if c in hold.columns]
        lines.append("\n## 前十大成分股")
        lines.append(hold.head(10)[cols].to_markdown(index=False))

    return "\n".join(lines) + "\n"


def get_fund_holdings_news(symbol: str, top_n: int = 5, per_stock: int = 3) -> str:
    """Recent news for the fund's top holdings — a fund's near-term sentiment is
    driven by what it owns. Yahoo/StockTwits/Reddit don't cover CN funds, so this
    is the meaningful sentiment signal for the sentiment analyst."""
    import akshare as ak

    from .china import bare_code

    hold = None
    for year in (_latest_fiscal_year(), str(int(_latest_fiscal_year()) - 1)):
        try:
            h = _t(lambda: ak.fund_portfolio_hold_em(symbol=symbol, date=year), "fund_portfolio_hold_em")
            if h is not None and not h.empty and "股票代码" in h.columns:
                hold = h
                break
        except Exception:  # noqa: BLE001
            continue
    if hold is None:
        raise NoMarketDataError(symbol, symbol, "akshare: no holdings for fund news")

    blocks = []
    for _, r in hold.head(top_n).iterrows():
        code = bare_code(str(r["股票代码"]))
        name = str(r.get("股票名称", "")).strip()
        weight = r.get("占净值比例", "")
        lines = [f"### {name}（{code}）占净值 {weight}%"]
        try:
            d = _t(lambda: ak.stock_news_em(symbol=code), "stock_news_em")
            if d is not None and not d.empty and "新闻标题" in d.columns:
                for _, n in d.head(per_stock).iterrows():
                    when = str(n.get("发布时间", "")).strip()
                    title = str(n.get("新闻标题", "")).strip()
                    lines.append(f"- [{when}] {title}")
            else:
                lines.append("- （暂无最新新闻）")
        except Exception:  # noqa: BLE001 — akshare can raise for thin coverage
            lines.append("- （暂无最新新闻）")
        blocks.append("\n".join(lines))

    return (
        f"# 重仓股新闻摘要（基金 {symbol} 的前 {len(blocks)} 大重仓股）\n"
        "# 来源：AKShare / 东方财富。基金的短期情绪很大程度由其重仓股驱动。\n\n"
        + "\n\n".join(blocks)
        + "\n"
    )


def _is_etf_feeder(name: str, full_name: str, strategy: str) -> bool:
    """An ETF feeder fund (联接基金) holds the target ETF rather than stocks, so
    its latest disclosure is dominated by the ETF and its direct stock weights are
    tiny — the real exposure is the ETF's constituents."""
    blob = f"{name} {full_name} {strategy}"
    return "联接" in blob or "目标ETF" in blob


def _target_etf_label(name: str) -> str:
    """The target ETF's name, derived from the feeder's own name (everything up to
    and including 'ETF', dropping the 联接 share-class tail). Reliable — no lookup."""
    if "ETF联接" in name:
        return name.split("ETF联接")[0] + "ETF"
    if "ETF" in name and "联接" in name:
        return name.split("ETF")[0] + "ETF"
    return name.split("联接")[0] if "联接" in name else ""


def _lookthrough_holdings(symbol: str):
    """For a feeder fund, the most recent quarter of the fund's own disclosure that
    still shows real look-through weights (top holding ≥ 2%) — before it shifted to
    holding the target ETF and its direct stock weights went tiny. Returns
    (quarter_label, top10_df) or None."""
    import re

    import akshare as ak

    y = int(_latest_fiscal_year())
    for year in (str(y), str(y - 1), str(y - 2)):
        try:
            h = _t(
                lambda yr=year: ak.fund_portfolio_hold_em(symbol=symbol, date=yr),
                "fund_portfolio_hold_em",
            )
        except Exception:  # noqa: BLE001
            continue
        if h is None or h.empty or "占净值比例" not in h.columns:
            continue
        if "季度" not in h.columns:
            if float(h["占净值比例"].max()) >= 2.0:
                return ("", h.head(10))
            continue
        # Quarters with meaningful weights, most recent first.
        cand = []
        for q in h["季度"].unique():
            sub = h[h["季度"] == q]
            if float(sub["占净值比例"].max()) >= 2.0:
                m = re.search(r"(\d)\s*季度", str(q))
                cand.append((int(m.group(1)) if m else 0, str(q), sub))
        if cand:
            cand.sort(key=lambda t: t[0], reverse=True)
            _, q, sub = cand[0]
            return (q, sub.head(10))
    return None


def get_fund_overview(symbol: str) -> str:
    """Fund identity + strategy + fees + asset allocation + top holdings.

    Each section is best-effort: the Xueqiu profile endpoint doesn't cover every
    fund (and can raise internally), so we degrade to whatever is available
    rather than failing — a raised tool would loop the analyst."""
    import akshare as ak

    info = {}
    try:
        basic = _t(lambda: ak.fund_individual_basic_info_xq(symbol), "fund_individual_basic_info_xq")
        if basic is not None and not basic.empty:
            info = dict(zip(basic["item"], basic["value"]))
    except Exception:  # noqa: BLE001 — profile endpoint thin/unavailable for some funds
        info = {}

    lines = [f"# Fund overview for {symbol} (China open-end fund)\n", "## 基本信息"]
    profile_keys = [
        "基金名称", "基金全称", "基金类型", "基金公司", "基金经理", "成立时间",
        "最新规模", "基金评级", "业绩比较基准", "投资目标", "投资策略",
    ]
    has_profile = False
    for k in profile_keys:
        v = info.get(k)
        if v not in (None, "") and not (isinstance(v, float) and pd.isna(v)):
            lines.append(f"- {k}: {str(v).strip()}")
            has_profile = True
    if not has_profile:
        # The Xueqiu profile is thin for some funds; fall back to the directory
        # for at least the name and category.
        name, category = fund_name_type(symbol)
        if name:
            lines.append(f"- 基金名称: {name}")
        if category:
            lines.append(f"- 基金类型: {category}")
        lines.append("- （该基金的详细档案暂不可用，请基于下方持仓与配置分析）")

    # Asset allocation (stocks / bonds / cash / other).
    try:
        alloc = _t(
            lambda: ak.fund_individual_detail_hold_xq(symbol, date=_latest_fiscal_year()),
            "fund_individual_detail_hold_xq",
        )
        if alloc is not None and not alloc.empty:
            lines.append("\n## 资产配置")
            lines.append(alloc.to_markdown(index=False))
    except Exception:  # noqa: BLE001 — composition is best-effort
        pass

    # Top stock holdings. For an ETF feeder fund the latest disclosure is mostly
    # the target ETF (其他), so the direct stock weights are tiny — surface the
    # look-through holdings (the target ETF's constituents) instead.
    name = info.get("基金名称") or fund_name_type(symbol)[0]
    feeder = _is_etf_feeder(name, info.get("基金全称", ""), info.get("投资策略", ""))
    lookthrough = _lookthrough_holdings(symbol) if feeder else None
    if lookthrough is not None:
        quarter, hold = lookthrough
        etf = _target_etf_label(name)
        suffix = f"（{quarter}）" if quarter else ""
        title = f"穿透至目标ETF（{etf}）的前十大重仓股{suffix}" if etf \
            else f"穿透持仓·前十大重仓股{suffix}"
        cols = [c for c in ["股票代码", "股票名称", "占净值比例", "持仓市值"] if c in hold.columns]
        lines.append(f"\n## {title}")
        lines.append(
            "> 本基金为ETF联接基金，主要通过持有目标ETF获得指数敞口；"
            "下表为穿透至成分股的实际持仓权重（非本基金当期直接持股的小额残余）。"
        )
        lines.append(hold.head(10)[cols].to_markdown(index=False))
    else:
        try:
            hold = _t(
                lambda: ak.fund_portfolio_hold_em(symbol, date=_latest_fiscal_year()),
                "fund_portfolio_hold_em",
            )
            if hold is not None and not hold.empty:
                cols = [c for c in ["股票代码", "股票名称", "占净值比例", "持仓市值"] if c in hold.columns]
                lines.append("\n## 前十大重仓股")
                lines.append(hold.head(10)[cols].to_markdown(index=False))
        except Exception:  # noqa: BLE001
            pass

    # Fees.
    try:
        fees = _t(lambda: ak.fund_individual_detail_info_xq(symbol), "fund_individual_detail_info_xq")
        if fees is not None and not fees.empty:
            lines.append("\n## 费用")
            lines.append(fees.to_markdown(index=False))
    except Exception:  # noqa: BLE001
        pass

    return "\n".join(lines) + "\n"
