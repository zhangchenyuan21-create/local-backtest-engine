"""可选的 vnpy / QMT 实盘适配器骨架。

安全默认：
  - dry_run=True
  - 不 import vnpy / xtquant，直到显式调用 from_vnpy / from_xtquant
  - submit/cancel 在 dry_run 下只返回计划，不触碰客户端

真实接入前必须在模拟环境验证订单状态、撤单、部分成交和断线重连。
"""
from __future__ import annotations

from typing import Any


class LiveBrokerAdapter:
    def __init__(self, client: Any, account_id: str | None = None, dry_run: bool = True):
        self.client = client
        self.account_id = account_id
        self.dry_run = bool(dry_run)

    def get_account(self) -> dict:
        return _as_dict(self.client.get_account(account_id=self.account_id))

    def get_positions(self) -> list[dict]:
        return [_as_dict(x) for x in self.client.get_positions(account_id=self.account_id)]

    def get_orders(self) -> list[dict]:
        return [_as_dict(x) for x in self.client.get_orders(account_id=self.account_id)]

    def submit_order(self, symbol: str, side: str, quantity: int, *,
                     price: float | None = None, **kwargs) -> dict:
        payload = {"symbol": symbol, "side": side, "quantity": int(quantity),
                   "price": price, **kwargs}
        if self.dry_run:
            return {"accepted": False, "dry_run": True,
                    "message": "dry run: live order not sent", "request": payload}
        return _as_dict(self.client.submit_order(account_id=self.account_id, **payload))

    def cancel(self, order_id: str, **kwargs) -> dict:
        if self.dry_run:
            return {"accepted": False, "dry_run": True,
                    "message": "dry run: cancel not sent", "order_id": order_id}
        return _as_dict(self.client.cancel(account_id=self.account_id, order_id=order_id, **kwargs))

    def health(self) -> dict:
        try:
            h = self.client.health()
            return {"ok": True, "dry_run": self.dry_run,
                    "account_id": self.account_id, **(_as_dict(h) or {})}
        except Exception as e:
            return {"ok": False, "dry_run": self.dry_run,
                    "account_id": self.account_id, "error": str(e)}


def _as_dict(value):
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dict__"):
        return {k: _plain(v) for k, v in vars(value).items() if not k.startswith("_")}
    return value


def _plain(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def _infer_exchange(symbol: str):
    s = str(symbol).strip().upper()
    if s.startswith(("SH", "6")):
        return "SSE"
    if s.startswith(("SZ", "0", "3")):
        return "SZSE"
    if s.startswith(("BJ", "4", "8")):
        return "BSE"
    return "SSE"


class VnpyAdapter(LiveBrokerAdapter):
    """VN.PY 的薄适配器。

    直接传入任意实现了 get_account/get_positions/get_orders/submit_order/cancel
    的 client 即可；也可以用 from_vnpy(...) 包装 vnpy MainEngine。
    """

    @classmethod
    def from_vnpy(cls, main_engine, gateway_name: str, dry_run: bool = True,
                  account_id: str | None = None):
        return cls(_VnpyClient(main_engine, gateway_name), account_id=account_id,
                   dry_run=dry_run)


class _VnpyClient:
    def __init__(self, main_engine, gateway_name: str):
        self.main_engine = main_engine
        self.gateway_name = gateway_name

    def get_account(self, account_id=None):
        accounts = self.main_engine.get_all_accounts()
        return accounts[0] if accounts else None

    def get_positions(self, account_id=None):
        return self.main_engine.get_all_positions()

    def get_orders(self, account_id=None):
        return self.main_engine.get_all_orders()

    def submit_order(self, account_id=None, symbol="", side="buy", quantity=0,
                     price=None, exchange=None, order_type="limit",
                     offset="none", **kwargs):
        from vnpy.trader.constant import Direction, Exchange, Offset, OrderType
        from vnpy.trader.object import OrderRequest
        ex_name = exchange or _infer_exchange(symbol)
        ex = getattr(Exchange, ex_name, Exchange.SSE)
        direction = Direction.LONG if str(side).lower() == "buy" else Direction.SHORT
        ot = OrderType.LIMIT if str(order_type).lower() == "limit" else OrderType.MARKET
        of = getattr(Offset, str(offset).upper(), Offset.NONE)
        req = OrderRequest(symbol=str(symbol), exchange=ex, direction=direction,
                           type=ot, volume=int(quantity), price=float(price or 0),
                           offset=of)
        vt_orderid = self.main_engine.send_order(req, self.gateway_name)
        return {"accepted": bool(vt_orderid), "order_id": vt_orderid}

    def cancel(self, account_id=None, order_id="", exchange=None, **kwargs):
        from vnpy.trader.constant import Exchange
        from vnpy.trader.object import CancelRequest
        ex = getattr(Exchange, exchange or "SSE", Exchange.SSE)
        req = CancelRequest(orderid=str(order_id), exchange=ex)
        ok = self.main_engine.cancel_order(req, self.gateway_name)
        return {"accepted": bool(ok), "order_id": order_id}

    def health(self):
        return {"gateway": self.gateway_name}


class QmtAdapter(LiveBrokerAdapter):
    """QMT/xtquant 的薄适配器。

    QMT 版本和券商封装差异较大，因此这里使用 client 协议：
      get_account / get_positions / get_orders / submit_order / cancel / health
    可以用 from_xtquant 包装标准 xtquant trader，也可以传入自己的封装。
    """

    @classmethod
    def from_xtquant(cls, trader, account, dry_run: bool = True,
                     account_id: str | None = None):
        return cls(_QmtClient(trader, account), account_id=account_id, dry_run=dry_run)


class _QmtClient:
    def __init__(self, trader, account):
        self.trader = trader
        self.account = account

    def get_account(self, account_id=None):
        return self.trader.query_stock_asset(self.account)

    def get_positions(self, account_id=None):
        return self.trader.query_stock_positions(self.account)

    def get_orders(self, account_id=None):
        return self.trader.query_stock_orders(self.account)

    def submit_order(self, account_id=None, symbol="", side="buy", quantity=0,
                     price=None, order_type=23, price_type=11, strategy_name="jqbt",
                     remark="", **kwargs):
        # 23/24 and 11 are common xtquant constants; expose overrides for your build.
        side_code = 23 if str(side).lower() == "buy" else 24
        order_type = int(kwargs.pop("order_type", order_type if str(side).lower() == "buy" else 24))
        order_id = self.trader.order_stock(self.account, str(symbol), order_type,
                                           int(quantity), int(price_type),
                                           float(price or 0), str(strategy_name), str(remark))
        return {"accepted": bool(order_id), "order_id": order_id}

    def cancel(self, account_id=None, order_id="", **kwargs):
        return {"accepted": bool(self.trader.cancel_order_stock(self.account, order_id)),
                "order_id": order_id}

    def health(self):
        return {"broker": "qmt"}