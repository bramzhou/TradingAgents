"""LangChain tools for China open-end fund analysis (asset_type fund/etf)."""

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows.cn_fund import (
    get_etf_overview,
    get_fund_nav,
    get_fund_overview,
)


def _safe(label: str, fn) -> str:
    """Run a fund data fetch, returning an instructive sentinel on failure rather
    than raising — a tool that raises makes the analyst retry it forever."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return (
            f"DATA_UNAVAILABLE: {label} could not be retrieved ({type(e).__name__}: {e}). "
            "Treat it as unavailable and continue with whatever other data you have — "
            "do not call this tool again."
        )


@tool
def get_fund_nav_data(
    symbol: Annotated[str, "fund code, e.g. 014328"],
    start_date: Annotated[str, "Start date yyyy-mm-dd"],
    end_date: Annotated[str, "End date yyyy-mm-dd"],
) -> str:
    """Daily NAV history for a China open-end fund, with total/annualized return,
    volatility and max-drawdown stats. Use this instead of stock OHLCV for funds."""
    return _safe("fund NAV history", lambda: get_fund_nav(symbol, start_date, end_date))


@tool
def get_fund_overview_data(
    symbol: Annotated[str, "fund code, e.g. 014328"],
) -> str:
    """Composition and identity of a China open-end fund: name, type/category,
    company, manager, scale, benchmark, strategy, asset allocation, top holdings
    and fees. Use this instead of company fundamentals for funds."""
    return _safe("fund overview", lambda: get_fund_overview(symbol))


@tool
def get_etf_overview_data(
    symbol: Annotated[str, "ETF code, e.g. 510300"],
) -> str:
    """Composition of a China ETF: name + tracked index, unit NAV vs market price
    (premium/discount) and top constituent holdings. Use this for ETF fundamentals
    (its intraday price/technicals come from the stock OHLCV tools)."""
    return _safe("ETF overview", lambda: get_etf_overview(symbol))
