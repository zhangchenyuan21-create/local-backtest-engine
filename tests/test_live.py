from __future__ import annotations

import unittest

from live.adapters import LiveBrokerAdapter, VnpyAdapter, _infer_exchange


class FakeClient:
    def __init__(self):
        self.calls = []

    def get_account(self, account_id=None):
        return {"cash": 1000, "account_id": account_id}

    def get_positions(self, account_id=None):
        return []

    def get_orders(self, account_id=None):
        return []

    def submit_order(self, **kwargs):
        self.calls.append(("submit", kwargs))
        return {"accepted": True, "order_id": "L1"}

    def cancel(self, **kwargs):
        self.calls.append(("cancel", kwargs))
        return {"accepted": True, "order_id": kwargs.get("order_id")}

    def health(self):
        return {"connected": True}


class LiveAdapterTests(unittest.TestCase):
    def test_dry_run_does_not_submit(self):
        fake = FakeClient()
        a = LiveBrokerAdapter(fake, dry_run=True)
        result = a.submit_order("600000.XSHG", "buy", 100, price=10.0)
        self.assertFalse(result["accepted"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(fake.calls, [])
        self.assertTrue(a.health()["ok"])

    def test_live_forwards(self):
        fake = FakeClient()
        a = VnpyAdapter(fake, dry_run=False)
        result = a.submit_order("600000.XSHG", "buy", 100, price=10.0)
        self.assertTrue(result["accepted"])
        self.assertEqual(fake.calls[0][0], "submit")
        a.cancel("L1")
        self.assertEqual(fake.calls[-1][0], "cancel")

    def test_exchange_inference(self):
        self.assertEqual(_infer_exchange("600000.XSHG"), "SSE")
        self.assertEqual(_infer_exchange("000001.XSHE"), "SZSE")
        self.assertEqual(_infer_exchange("430001.BJSE"), "BSE")


if __name__ == "__main__":
    unittest.main()