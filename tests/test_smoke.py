"""不依赖行情数据的安装/导入/策略加载冒烟测试。"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
ROOT = TEST_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("JQSHIM_DATA", str(TEST_DIR / "data_test_smoke"))

import jqdata  # noqa: E402
import runner  # noqa: E402
from jqdata.setup import OrderCost  # noqa: E402


class SmokeTests(unittest.TestCase):
    def test_code_normalization(self):
        self.assertEqual(jqdata.normalize_code("sh600000"), "600000.XSHG")
        self.assertEqual(jqdata.normalize_code("000001"), "000001.XSHE")
        self.assertEqual(jqdata.to_baostock_code("600000.XSHG"), "sh.600000")

    def test_demo_strategy_loads(self):
        mod = runner.load_strategy(str(ROOT / "examples" / "demo_strategy.py"))
        self.assertTrue(callable(getattr(mod, "initialize", None)))
        self.assertTrue(callable(getattr(mod, "rebalance", None)))

    def test_order_cost_defaults(self):
        cost = OrderCost()
        self.assertGreaterEqual(cost.close_tax, 0)
        self.assertGreaterEqual(cost.min_commission, 0)

    def test_jqdata_surface(self):
        self.assertTrue(callable(jqdata.get_price))
        self.assertTrue(callable(jqdata.attribute_history))
        self.assertTrue(callable(jqdata.order_target_percent))


if __name__ == "__main__":
    unittest.main()
