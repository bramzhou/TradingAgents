"""China A-share detection + vendor-chain routing (offline, mocked)."""

import unittest
from unittest import mock

from tradingagents.dataflows import interface
from tradingagents.dataflows.china import _exchange, bare_code, is_cn_a_share
from tradingagents.dataflows.config import set_config


class CnDetectionTests(unittest.TestCase):
    def test_is_cn_a_share(self):
        for sym in ("600519", "000001", "300750", "688981", "600519.SH", "000001.SZ"):
            self.assertTrue(is_cn_a_share(sym), sym)
        for sym in ("AAPL", "BRK.B", "GC=F", "12345", "1234567", "", "BTC-USD"):
            self.assertFalse(is_cn_a_share(sym), sym)

    def test_bare_code_strips_suffix(self):
        self.assertEqual(bare_code("600519.SH"), "600519")
        self.assertEqual(bare_code("000001.sz"), "000001")
        self.assertEqual(bare_code("600519"), "600519")

    def test_exchange_covers_stocks_and_etfs(self):
        # Stocks
        self.assertEqual(_exchange("600519"), "sh")
        self.assertEqual(_exchange("000001"), "sz")
        self.assertEqual(_exchange("300750"), "sz")
        # ETFs/LOFs: Shanghai 5x, Shenzhen 15x/16x
        self.assertEqual(_exchange("510300"), "sh")
        self.assertEqual(_exchange("588000"), "sh")
        self.assertEqual(_exchange("159915"), "sz")


class CnRoutingTests(unittest.TestCase):
    def test_a_share_uses_cn_chain_over_configured_vendor(self):
        # Even with the US vendor configured, a 6-digit A-share routes to the CN
        # chain (Baostock first).
        set_config({"data_vendors": {"core_stock_apis": "yfinance"}})
        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_stock_data": {
                "yfinance": lambda *a, **k: "YF",
                "baostock": lambda *a, **k: "BAOSTOCK",
                "tushare": lambda *a, **k: "TUSHARE",
                "akshare": lambda *a, **k: "AKSHARE",
            }},
            clear=False,
        ):
            self.assertEqual(
                interface.route_to_vendor("get_stock_data", "600519", "2026-01-01", "2026-01-10"),
                "BAOSTOCK",
            )
            # A US symbol still honors the configured vendor.
            self.assertEqual(
                interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10"),
                "YF",
            )

    def test_cn_chain_falls_through_to_next_vendor(self):
        from tradingagents.dataflows.errors import NoMarketDataError

        def _no_data(*a, **k):
            raise NoMarketDataError("600519", "600519", "blocked")

        set_config({"data_vendors": {"core_stock_apis": "yfinance"}})
        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_stock_data": {
                "yfinance": lambda *a, **k: "YF",
                "baostock": _no_data,
                "tushare": lambda *a, **k: "TUSHARE",
                "akshare": lambda *a, **k: "AKSHARE",
            }},
            clear=False,
        ):
            # Baostock fails → Tushare serves the call.
            self.assertEqual(
                interface.route_to_vendor("get_stock_data", "600519", "2026-01-01", "2026-01-10"),
                "TUSHARE",
            )


if __name__ == "__main__":
    unittest.main()
