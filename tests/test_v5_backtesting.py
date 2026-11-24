"""V5 market-structure, backtest, sizing and mark-price regression tests."""
import unittest
import pandas as pd

from scanner.market_structure import analyze_structure
from scanner.backtesting import run_backtest, walk_forward_splits, run_walk_forward
from scanner.risk_sizing import calculate_position_size
from scanner.v5_analysis import validate_mark_price, reward_risk_at_price
from scanner.binance_client import BinanceClient


class V5BacktestingTests(unittest.TestCase):
    def frame(self, n=60):
        idx = pd.date_range("2026-01-01", periods=n, freq="h")
        return pd.DataFrame({
            "open": [100.0]*n, "high": [101.0]*n,
            "low": [99.0]*n, "close": [100.0]*n,
            "volume": [1000.0]*n,
        }, index=idx)

    def test_backtest_enters_on_next_bar_not_signal_close(self):
        df = self.frame(5)
        df.iloc[1, df.columns.get_loc("open")] = 100
        df.iloc[1, df.columns.get_loc("high")] = 106
        df.iloc[1, df.columns.get_loc("low")] = 99
        seen = []
        def signal(prefix, i):
            seen.append(len(prefix))
            return {"side":"LONG", "stop_loss":95, "take_profit":105} if i == 0 else None
        result = run_backtest(df, signal, fee_rate=0, slippage_rate=0)
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["trades"][0]["entry_index"], 1)
        self.assertEqual(result["trades"][0]["reason"], "TARGET")
        self.assertEqual(seen[0], 1)

    def test_stop_wins_when_stop_and_target_hit_same_bar(self):
        df = self.frame(4)
        df.iloc[1, df.columns.get_loc("high")] = 106
        df.iloc[1, df.columns.get_loc("low")] = 94
        result = run_backtest(
            df, lambda prefix, i: {"side":"LONG","stop_loss":95,"take_profit":105} if i == 0 else None,
            fee_rate=0, slippage_rate=0,
        )
        self.assertEqual(result["trades"][0]["reason"], "STOP")

    def test_backtest_rejects_next_open_rr_degradation(self):
        df = self.frame(4)
        df.iloc[1, df.columns.get_loc("open")] = 101
        fn = lambda prefix, i: {"side":"LONG","stop_loss":95,"take_profit":110} if i == 0 else None
        result = run_backtest(df, fn, fee_rate=0, slippage_rate=0, min_rr=2.0)
        self.assertEqual(result["trade_count"], 0)

    def test_backtest_rejects_large_gap_from_signal_close(self):
        df = self.frame(4)
        df.iloc[1, df.columns.get_loc("open")] = 103
        fn = lambda prefix, i: {"side":"LONG","stop_loss":95,"take_profit":115,
                                "reference_entry":100,"atr":1} if i == 0 else None
        result = run_backtest(
            df, fn, fee_rate=0, slippage_rate=0,
            min_rr=2.0, max_entry_deviation_atr=0.5,
        )
        self.assertEqual(result["trade_count"], 0)

    def test_fees_reduce_net_return(self):
        df = self.frame(4)
        df.iloc[1, df.columns.get_loc("high")] = 106
        fn = lambda prefix, i: {"side":"LONG","stop_loss":95,"take_profit":105} if i == 0 else None
        no_fee = run_backtest(df, fn, fee_rate=0, slippage_rate=0)
        with_fee = run_backtest(df, fn, fee_rate=0.001, slippage_rate=0)
        self.assertLess(with_fee["trades"][0]["net_return"], no_fee["trades"][0]["net_return"])

    def test_walk_forward_folds_are_chronological(self):
        folds = walk_forward_splits(self.frame(100))
        self.assertGreaterEqual(len(folds), 2)
        for fold in folds:
            self.assertLess(fold["train"].index[-1], fold["validation"].index[0])
            self.assertLess(fold["validation"].index[-1], fold["test"].index[0])

    def test_walk_forward_runs_held_out_test_windows(self):
        result = run_walk_forward(
            self.frame(100),
            lambda train, validation: (lambda prefix, i: None),
            backtest_kwargs={"fee_rate":0, "slippage_rate":0},
        )
        self.assertGreaterEqual(result["fold_count"], 2)
        self.assertEqual(result["trade_count"], 0)

    def test_position_size_respects_step_and_risk(self):
        result = calculate_position_size(
            balance=10000, risk_pct=0.5, entry=100, stop_loss=95,
            qty_step=0.01, min_qty=0.01, min_notional=5,
        )
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["quantity"] / 0.01, round(result["quantity"] / 0.01))
        self.assertLessEqual(result["estimated_risk"], 50.0)

    def test_below_exchange_minimum_rejected(self):
        result = calculate_position_size(
            balance=10, risk_pct=0.5, entry=100, stop_loss=99,
            qty_step=0.001, min_qty=0.01, min_notional=6,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "BELOW_EXCHANGE_MINIMUM")

    def test_mark_price_guard_and_rr_recalculation(self):
        ok, reason, _ = validate_mark_price(100, 100.2, 1)
        self.assertTrue(ok)
        self.assertEqual(reason, "MARK_PRICE_CONFIRMED")
        ok, reason, _ = validate_mark_price(100, 102, 1)
        self.assertFalse(ok)
        self.assertEqual(reason, "MARK_PRICE_DEVIATION_TOO_LARGE")
        self.assertEqual(reward_risk_at_price("LONG", 101, 95, 113), 2.0)

    def test_mark_price_never_falls_back_to_last_trade(self):
        class Exchange:
            has = {}
        client = object.__new__(BinanceClient)
        client.rate_limited = False
        client.exchange = Exchange()
        client._tickers_cache = {"BTC/USDT:USDT": {"last": 100.0, "info": {}}}
        self.assertIsNone(client.get_mark_price("BTCUSDT"))
        client._tickers_cache["BTC/USDT:USDT"]["mark"] = 99.5
        self.assertEqual(client.get_mark_price("BTCUSDT"), 99.5)

    def test_exchange_lot_rules_are_extracted(self):
        class Exchange:
            precisionMode = 2
            has = {}
            def market(self, symbol):
                return {
                    "precision":{"amount":3},
                    "limits":{"amount":{"min":0.01,"max":1000},"cost":{"min":5}},
                }
        client = object.__new__(BinanceClient)
        client._markets_loaded = True
        client.futures = True
        client.exchange = Exchange()
        result = client.get_order_constraints("BTCUSDT")
        self.assertEqual(result["qty_step"], 0.001)
        self.assertEqual(result["min_qty"], 0.01)
        self.assertEqual(result["min_notional"], 5.0)

    def test_liquidity_sweep_is_detected(self):
        df = self.frame(30)
        # Prior 20-bar low is 99; final bar sweeps below and closes back above it.
        df.iloc[-1, df.columns.get_loc("open")] = 100
        df.iloc[-1, df.columns.get_loc("low")] = 97
        df.iloc[-1, df.columns.get_loc("high")] = 101
        df.iloc[-1, df.columns.get_loc("close")] = 100.5
        result = analyze_structure(df, lookback=20)
        self.assertTrue(result["bullish_sweep"])
        self.assertGreater(result["long_score"], 0)


if __name__ == "__main__":
    unittest.main()
