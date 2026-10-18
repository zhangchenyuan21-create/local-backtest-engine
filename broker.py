"""Broker 抽象、PaperBroker 和同花顺模拟盘适配器。

这个模块是可选层：
  - PaperBroker 只维护现金/持仓/订单，不修改 runner 或 jqdata/trade。
  - ThsSimBroker 惰性导入现有 `ths_sim`，默认 dry_run=True。
  - 没有安装/配置 ths_sim 时，核心回测完全不受影响。

用法示例：
    from broker import PaperBroker
    b = PaperBroker(initial_cash=100000)
    b.submit_order("000001.XSHE", "buy", 1000, order_date="2024-01-02")
    fills = b.process_bar("2024-01-03", {"000001.XSHE": {"open": 10.0, "close": 10.2, "volume": 1000000}})
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PaperConfig:
    initial_cash: float = 1_000_000.0
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_duty_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001
    volume_limit: float = 0.25


@dataclass
class PaperOrder:
    order_id: str
    symbol: str
    side: str
    quantity: int
    order_date: str
    limit_price: float | None = None
    status: str = "pending"
    filled_quantity: int = 0
    fill_price: float = 0.0
    fee: float = 0.0
    fill_date: str = ""
    reason: str = ""


class PaperBroker:
    """内存撮合账户，适合 paper replay 和测试。

    订单在提交后的下一个 `process_bar(date, bars)` 以开盘价成交；
    T+1、整手、涨跌停、成交量参与率、费用都可配置。
    """

    def __init__(self, config: PaperConfig | None = None, **kwargs):
        if config is None:
            config = PaperConfig(**kwargs)
        self.config = config
        self.cash = float(config.initial_cash)
        self.positions: dict[str, dict[str, Any]] = {}
        self.orders: list[PaperOrder] = []
        self._seq = itertools.count(1)
        self.current_date = ""

    def _fee(self, value: float, side: str) -> float:
        comm = max(self.config.min_commission, value * self.config.commission_rate)
        stamp = value * self.config.stamp_duty_rate if side == "sell" else 0.0
        transfer = value * self.config.transfer_fee_rate
        return comm + stamp + transfer

    def _release_t1(self, date: str):
        for pos in self.positions.values():
            if not pos.get("last_buy_date") or pos["last_buy_date"] < date:
                pos["available"] = int(pos["quantity"])

    def _mark(self, symbol: str, price: float):
        pos = self.positions.get(symbol)
        if pos and price > 0:
            pos["last_price"] = float(price)

    def submit_order(self, symbol: str, side: str, quantity: int, *,
                     price: float | None = None, order_date: str = "") -> dict:
        side = str(side).lower()
        if side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        quantity = int(quantity)
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        oid = f"P{next(self._seq):08d}"
        order = PaperOrder(order_id=oid, symbol=str(symbol), side=side,
                           quantity=quantity, order_date=str(order_date or self.current_date),
                           limit_price=float(price) if price is not None else None)
        self.orders.append(order)
        return self._order_dict(order)

    def _order_dict(self, o: PaperOrder) -> dict:
        return {
            "order_id": o.order_id, "symbol": o.symbol, "side": o.side,
            "quantity": o.quantity, "order_date": o.order_date,
            "status": o.status, "filled_quantity": o.filled_quantity,
            "fill_price": o.fill_price, "fee": o.fee,
            "fill_date": o.fill_date, "reason": o.reason,
        }

    def process_bar(self, date: str, bars: dict[str, dict]) -> list[dict]:
        """处理上一交易日提交的订单；bars = {symbol: {open, close, volume, ...}}。"""
        date = str(date)
        self.current_date = date
        self._release_t1(date)
        fills = []
        for order in self.orders:
            if order.status != "pending" or order.order_date >= date:
                continue
            bar = bars.get(order.symbol)
            if not bar:
                order.status = "rejected"
                order.reason = "no bar (suspended/missing)"
                continue
            try:
                px = float(bar.get("open"))
                volume = float(bar.get("volume") or 0)
            except (TypeError, ValueError):
                order.status = "rejected"
                order.reason = "invalid open/volume"
                continue
            if px <= 0:
                order.status = "rejected"
                order.reason = "invalid open"
                continue
            if order.side == "buy":
                up = bar.get("limit_up")
                if up is not None and px >= float(up) - 0.005:
                    order.status = "rejected"
                    order.reason = "open at limit up"
                    continue
            else:
                down = bar.get("limit_down")
                if down is not None and px <= float(down) + 0.005:
                    order.status = "rejected"
                    order.reason = "open at limit down"
                    continue
            cap = int(volume * max(0.0, min(1.0, float(self.config.volume_limit))))
            if cap <= 0:
                order.status = "rejected"
                order.reason = "volume limit is zero"
                continue
            qty = int(order.quantity)
            if order.side == "buy":
                qty = int(qty // 100 * 100)
                qty = min(qty, int(cap // 100 * 100))
                if qty < 100:
                    order.status = "rejected"
                    order.reason = "buy quantity below one lot after volume cap"
                    continue
            else:
                pos = self.positions.get(order.symbol)
                if not pos or pos["available"] <= 0:
                    order.status = "rejected"
                    order.reason = "no sellable position"
                    continue
                qty = min(qty, int(pos["available"]))
                if qty > cap:
                    qty = int(cap // 100 * 100) if cap < int(pos["quantity"]) else int(cap)
                if qty <= 0:
                    order.status = "rejected"
                    order.reason = "sell quantity below one lot after volume cap"
                    continue
            if order.side == "buy":
                while qty >= 100:
                    value = px * qty
                    fee = self._fee(value, "buy")
                    if value + fee <= self.cash + 1e-9:
                        break
                    qty -= 100
                if qty < 100:
                    order.status = "rejected"
                    order.reason = "insufficient cash"
                    continue
                value = px * qty
                fee = self._fee(value, "buy")
                self.cash -= value + fee
                pos = self.positions.setdefault(order.symbol, {
                    "symbol": order.symbol, "quantity": 0, "available": 0,
                    "avg_cost": 0.0, "last_price": px, "last_buy_date": "",
                    "realized_pnl": 0.0,
                })
                old_qty = int(pos["quantity"])
                total_cost = pos["avg_cost"] * old_qty + value + fee
                pos["quantity"] = old_qty + qty
                pos["avg_cost"] = total_cost / pos["quantity"]
                pos["last_price"] = px
                pos["last_buy_date"] = date
                # 新买的数量当日不可卖；available 保持原值。
            else:
                value = px * qty
                fee = self._fee(value, "sell")
                self.cash += value - fee
                pos = self.positions[order.symbol]
                cost = float(pos["avg_cost"]) * qty
                pos["realized_pnl"] = float(pos.get("realized_pnl", 0.0)) + value - fee - cost
                pos["quantity"] = int(pos["quantity"]) - qty
                pos["available"] = int(pos["available"]) - qty
                pos["last_price"] = px
                if pos["quantity"] <= 0:
                    self.positions.pop(order.symbol, None)
            order.status = "filled"
            order.filled_quantity = qty
            order.fill_price = px
            order.fee = fee
            order.fill_date = date
            fills.append({**self._order_dict(order), "value": px * qty})
        for symbol, bar in bars.items():
            self._mark(symbol, float(bar.get("close") or bar.get("open") or 0))
        return fills

    def cancel(self, order_id: str, *, dry_run: bool = False) -> dict:
        for o in self.orders:
            if o.order_id == str(order_id):
                if o.status == "pending":
                    o.status = "cancelled"
                    o.reason = "cancelled by user"
                return self._order_dict(o)
        raise KeyError(f"order not found: {order_id}")

    def cancel_all(self, *, dry_run: bool = False) -> dict:
        pending = [o for o in self.orders if o.status == "pending"]
        if not dry_run:
            for o in pending:
                o.status = "cancelled"
                o.reason = "cancel_all"
        return {"dry_run": bool(dry_run), "orders": [self._order_dict(o) for o in pending],
                "cancelled": 0 if dry_run else len(pending)}

    def get_account(self) -> dict:
        market_value = sum(int(p["quantity"]) * float(p["last_price"])
                           for p in self.positions.values())
        return {"available_cash": self.cash, "market_value": market_value,
                "total_assets": self.cash + market_value,
                "positions_count": len(self.positions)}

    def get_positions(self) -> list[dict]:
        out = []
        for p in self.positions.values():
            qty = int(p["quantity"])
            last = float(p["last_price"])
            out.append({**p, "market_value": qty * last,
                        "pnl": qty * (last - float(p["avg_cost"]))})
        return out

    def get_orders(self) -> list[dict]:
        return [self._order_dict(o) for o in self.orders]

    def health(self) -> dict:
        return {"ok": True, "broker": "paper", "cash": self.cash,
                "positions": len(self.positions), "orders": len(self.orders)}


class ThsSimBroker:
    """现有 `ths_sim` 客户端的薄适配器；默认 dry_run=True。"""

    def __init__(self, client, dry_run: bool = True):
        self.client = client
        self.dry_run = bool(dry_run)

    @classmethod
    def from_config(cls, config_file=None, dry_run: bool = True):
        from ths_sim.client import ThsClient  # 惰性导入
        from ths_sim.config import ThsConfig
        cfg = ThsConfig.load(config_file)
        return cls(ThsClient(cfg), dry_run=dry_run)

    def get_account(self) -> dict:
        return self.client.get_account().to_dict()

    def get_positions(self) -> list[dict]:
        return [p.to_dict() for p in self.client.get_positions()]

    def get_orders(self) -> list[dict]:
        return [o.to_dict() for o in self.client.get_orders()]

    def submit_order(self, symbol: str, side: str, quantity: int, *,
                     price: float | None = None, order_date: str = "") -> dict:
        result = self.client.place_order(symbol, side, quantity, price=price,
                                         dry_run=self.dry_run)
        return result.to_dict()

    def cancel(self, order_id: str, order_date: str | None = None,
               *, dry_run: bool | None = None) -> dict:
        d = self.dry_run if dry_run is None else bool(dry_run)
        return self.client.cancel(order_id, order_date=order_date, dry_run=d).to_dict()

    def cancel_all(self, *, dry_run: bool | None = None) -> dict:
        d = self.dry_run if dry_run is None else bool(dry_run)
        return self.client.cancel_all(dry_run=d)

    def health(self) -> dict:
        try:
            return {"ok": True, "broker": "ths_sim", "dry_run": self.dry_run,
                    **self.client.health()}
        except Exception as e:
            return {"ok": False, "broker": "ths_sim", "dry_run": self.dry_run,
                    "error": str(e)}


def broker_from_name(name: str, **kwargs):
    name = str(name).lower()
    if name in ("paper", "local"):
        return PaperBroker(**kwargs)
    if name in ("ths", "ths_sim", "tonghuashun"):
        return ThsSimBroker.from_config(dry_run=kwargs.get("dry_run", True),
                                        config_file=kwargs.get("config_file"))
    raise ValueError(f"unknown broker: {name}")