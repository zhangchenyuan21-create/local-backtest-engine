from __future__ import annotations

import unittest

import broker


class FakeAccount:
    def to_dict(self):
        return {"available_cash": 1000.0, "total_assets": 1000.0}


class FakeResult:
    def __init__(self, **kw):
        self.data = kw

    def to_dict(self):
        return dict(self.data)


class FakeThsClient:
    def __init__(self):
        self.calls = []

    def get_account(self):
        return FakeAccount()

    def get_positions(self):
        return []

    def get_orders(self):
        return []

    def place_order(self, symbol, side, quantity, **kw):
        self.calls.append(("place", symbol, side, quantity, kw))
        return FakeResult(order_id="x", dry_run=kw.get("dry_run"))

    def cancel(self, order_id, order_date=None, dry_run=True):
        self.calls.append(("cancel", order_id, order_date, dry_run))
        return FakeResult(order_id=order_id, dry_run=dry_run)

    def cancel_all(self, dry_run=True):
        return {"dry_run": dry_run, "cancelled": 0}

    def health(self):
        return {"ok": True}


class PaperBrokerTests(unittest.TestCase):
    def test_buy_then_t1_sell(self):
        b = broker.PaperBroker(initial_cash=100000, volume_limit=1.0)
        b.submit_order("000001.XSHE", "buy", 1000, order_date="2024-01-02")
        fills = b.process_bar("2024-01-03", {
            "000001.XSHE": {"open": 10.0, "close": 10.5, "volume": 1_000_000,
                            "limit_up": 11.0, "limit_down": 9.0},
        })
        self.assertEqual(len(fills), 1)
        pos = b.get_positions()[0]
        self.assertEqual(pos["quantity"], 1000)
        self.assertEqual(pos["available"], 0)
        b.submit_order("000001.XSHE", "sell", 1000, order_date="2024-01-03")
        fills = b.process_bar("2024-01-04", {
            "000001.XSHE": {"open": 10.2, "close": 10.2, "volume": 1_000_000,
                            "limit_up": 11.0, "limit_down": 9.0},
        })
        self.assertEqual(len(fills), 1)
        self.assertEqual(b.get_positions(), [])
        self.assertGreater(b.get_account()["available_cash"], 100000)

    def test_limit_up_reject_and_volume_cap(self):
        b = broker.PaperBroker(initial_cash=100000, volume_limit=0.25)
        b.submit_order("600000.XSHG", "buy", 1000, order_date="2024-01-02")
        fills = b.process_bar("2024-01-03", {
            "600000.XSHG": {"open": 11.0, "close": 11.0, "volume": 1_000_000,
                            "limit_up": 11.0, "limit_down": 9.0},
        })
        self.assertEqual(fills, [])
        self.assertEqual(b.get_orders()[0]["status"], "rejected")

        b2 = broker.PaperBroker(initial_cash=100000, volume_limit=0.25)
        b2.submit_order("600000.XSHG", "buy", 2000, order_date="2024-01-02")
        fills = b2.process_bar("2024-01-03", {
            "600000.XSHG": {"open": 10.0, "close": 10.0, "volume": 4000,
                            "limit_up": 11.0, "limit_down": 9.0},
        })
        self.assertEqual(fills[0]["filled_quantity"], 1000)

    def test_cancel(self):
        b = broker.PaperBroker()
        o = b.submit_order("000001.XSHE", "buy", 100, order_date="2024-01-02")
        self.assertEqual(b.cancel(o["order_id"])["status"], "cancelled")
        self.assertEqual(b.cancel_all(dry_run=True)["cancelled"], 0)


class ThsAdapterTests(unittest.TestCase):
    def test_dry_run_forwarding(self):
        fake = FakeThsClient()
        b = broker.ThsSimBroker(fake, dry_run=True)
        b.submit_order("600000.XSHG", "buy", 100, price=10.0)
        self.assertTrue(fake.calls[0][4]["dry_run"])
        self.assertEqual(b.get_account()["available_cash"], 1000.0)
        b.cancel("x", "20240102")
        self.assertTrue(fake.calls[-1][3])


if __name__ == "__main__":
    unittest.main()