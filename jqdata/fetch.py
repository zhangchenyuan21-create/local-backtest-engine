"""数据抓取 —— 聚宽 Data API（主）+ 新浪资金流（补缺）。

改法 C 的数据源分配：
  聚宽 Data API：日线 / 估值 / 财务 / 指数成分 / 股票列表 / 交易日历
  新浪资金流：  net_amount_main 的【方向】

═══════════════════════════════════════════════════════════════════════════
为什么走 HTTPS 而不是 jqdatasdk
═══════════════════════════════════════════════════════════════════════════
jqdatasdk 用 Thrift 连 dataapi.joinquant.com:7000，但本机出网白名单只放行
80/443，7000 一律 TimeoutError（已实测 4 个候选地址全部超时）。
聚宽另有 HTTPS 接口：POST https://dataapi.joinquant.com/v2/apis（443，通）。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request

import pandas as pd

from . import store

# ═══════════════════════════════════════════════════════════════════════════
# 聚宽 Data API 客户端（HTTPS v2）
# ═══════════════════════════════════════════════════════════════════════════
JQ_API = "https://dataapi.joinquant.com/v2/apis"
JQ_HDR = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
TOKEN_FILE = os.path.join(store.META, ".jq_v2_token")


class JQError(RuntimeError):
    pass


class JQClient:
    """聚宽 HTTP v2 客户端。token 落盘缓存（约 20 小时有效）。"""

    def __init__(self, user=None, pwd=None, token=None):
        self.user = user or os.environ.get("JQDATA_USERNAME")
        self.pwd = pwd or os.environ.get("JQDATA_PASSWORD")
        self._token = token
        if not self._token and os.path.exists(TOKEN_FILE):
            try:
                cached = open(TOKEN_FILE, encoding="utf-8").read().strip()
                if cached and time.time() - os.path.getmtime(TOKEN_FILE) < 20 * 3600:
                    self._token = cached
            except Exception:
                pass

    def _post(self, body: dict, timeout: int = 90) -> str:
        req = urllib.request.Request(
            JQ_API, data=json.dumps(body).encode("utf-8"),
            headers=JQ_HDR, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", errors="replace").strip()
        if txt.startswith("error"):
            raise JQError(txt)
        return txt

    def call(self, method: str, **kw) -> str:
        if not self._token:
            self.login()
        body = {"method": method, "token": self._token}
        body.update(kw)
        return self._post(body)

    def login(self, force=False) -> str:
        if self._token and not force:
            return self._token
        if not (self.user and self.pwd):
            raise JQError(
                "缺少凭证。请设置环境变量 JQDATA_USERNAME / JQDATA_PASSWORD，"
                "或传入 user=/pwd=")
        tok = self._post({"method": "get_token", "mob": self.user, "pwd": self.pwd})
        self._token = tok
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(tok)
        return tok

    # ---------- 数据接口 ----------
    def query_count(self) -> pd.DataFrame:
        return _csv(self.call("get_query_count"))

    def trade_days(self, start: str, end: str) -> pd.DataFrame:
        return _csv(self.call("get_trade_days", start_date=start, end_date=end))

    def all_securities(self, date: str, stype: str = "stock",
                       fields: str | None = None) -> pd.DataFrame:
        kw = {"date": date, "type": stype}
        if fields:
            kw["fields"] = fields
        return _csv(self.call("get_all_securities", **kw))

    def price(self, code: str, start: str | None = None, end: str | None = None,
              count: int | None = None, unit: str = "1d",
              fields: str | None = None, fq: str | None = None) -> pd.DataFrame:
        """fq 留空 = 不复权（与回测默认一致）。"""
        kw = {"code": code, "unit": unit}
        if count:
            kw["count"] = count
        if start:
            kw["date"] = start
        if end:
            kw["end_date"] = end
        if fields:
            kw["fields"] = fields
        if fq:
            kw["fq"] = fq
        return _csv(self.call("get_price", **kw))

    def fundamentals(self, table: str, columns: str, date: str,
                     count: int | None = None) -> pd.DataFrame:
        kw = {"table": table, "columns": columns, "date": date}
        if count:
            kw["count"] = count
        return _csv(self.call("get_fundamentals", **kw))

    def industry(self, code: str, date: str) -> pd.DataFrame:
        return _csv(self.call("get_industry", code=code, date=date))

    def index_stocks(self, index_code: str, date: str) -> pd.DataFrame:
        """成分股。v2 若支持则直接用；不支持时由上层回退。"""
        return _csv(self.call("get_index_stocks", code=index_code, date=date))


def _csv(txt: str) -> pd.DataFrame:
    """聚宽 Data API 返回 CSV/文本 -> DataFrame。"""
    txt = (txt or "").strip()
    if not txt or txt.startswith("error"):
        return pd.DataFrame()
    from io import StringIO
    try:
        return pd.read_csv(StringIO(txt))
    except Exception:
        # 有些接口返回单列多行
        lines = [l for l in txt.splitlines() if l.strip()]
        if len(lines) == 1 and "," in lines[0]:
            return pd.DataFrame([lines[0].split(",")])
        return pd.DataFrame({"value": lines})


# ═══════════════════════════════════════════════════════════════════════════
# 新浪资金流
# ═══════════════════════════════════════════════════════════════════════════
SINA_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "MoneyFlow.ssl_qsfx_zjlrqs"
            "?page=1&num={num}&sort=opendate&asc=0&daima={code}")
SINA_HDR = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://finance.sina.com.cn/",
}


class SinaError(RuntimeError):
    pass


def fetch_sina_flow(sina_code: str, num: int = 2000,
                    tries: int = 4, timeout: int = 30) -> pd.DataFrame:
    """抓单只股票的资金流全历史。

    ★ 两个已实测的坑：
      1. 返回是【类 JSON】（键无引号），必须补引号后才能 json.loads
      2. 数组顺序【不单调】（num=2000 时 arr[0] 可能是 2020 年），
         必须按 opendate 建字典，不能用位置
    """
    url = SINA_URL.format(num=num, code=sina_code)
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=SINA_HDR)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                txt = r.read().decode("utf-8", errors="replace")
            if not txt or len(txt) < 30:
                raise SinaError(f"响应过短：{len(txt)} 字节")
            arr = json.loads(re.sub(r"([{,])(\w+):", r'\1"\2":', txt))
            if not arr:
                return pd.DataFrame()
            by = {x["opendate"]: x for x in arr if x.get("opendate")}
            rows = []
            for d in sorted(by):
                x = by[d]
                rows.append({
                    "date": d,
                    "netamount": _f(x.get("netamount")),
                    "r0_net": _f(x.get("r0_net")),
                    "ratioamount": _f(x.get("ratioamount")),
                    "r0_ratio": _f(x.get("r0_ratio")),
                    "turnover": _f(x.get("turnover")),
                    "change_pct_sina": _f(x.get("changeratio")) * 100.0,
                    "close_sina": _f(x.get("trade")),
                })
            return pd.DataFrame(rows)
        except Exception as e:
            last = e
            if k < tries - 1:
                time.sleep(1.0 + k * 0.8)
    raise SinaError(f"{sina_code}: {type(last).__name__}: {last}")


def _f(v):
    try:
        return float(v)
    except Exception:
        return float("nan")


# ═══════════════════════════════════════════════════════════════════════════
# 抓取编排（含断点续传）
# ═══════════════════════════════════════════════════════════════════════════
def fetch_calendar(client: JQClient, start: str, end: str) -> int:
    df = client.trade_days(start, end)
    if df.empty:
        return 0
    col = "trade_date" if "trade_date" in df.columns else df.columns[0]
    days = [str(x)[:10] for x in df[col].tolist()]
    store.save_calendar(days)
    return len(days)


def fetch_securities(client: JQClient, date: str) -> int:
    df = client.all_securities(date, "stock",
                               "display_name,name,start_date,end_date,type")
    if df.empty:
        return 0
    codecol = "code" if "code" in df.columns else df.columns[0]
    recs = {}
    for _, r in df.iterrows():
        code = str(r[codecol])
        recs[code] = {k: (None if pd.isna(r[k]) else str(r[k]))
                      for k in df.columns if k != codecol}
    store.save_securities(recs)
    return len(recs)


def fetch_index(client: JQClient, index_code: str, snap_dates: list[str]) -> int:
    snaps = store.load_index(index_code)
    n = 0
    for d in snap_dates:
        if d in snaps:
            continue
        try:
            df = client.index_stocks(index_code, d)
        except JQError:
            continue
        if df.empty:
            continue
        col = "code" if "code" in df.columns else df.columns[0]
        snaps[d] = [str(x) for x in df[col].tolist()]
        n += 1
    if n:
        store.save_index(index_code, snaps)
    return n


DAILY_FIELDS = "date,open,high,low,close,volume,amount,paused,"
FUND_FIELDS = {
    "valuation": "code,pe_ratio,pb_ratio,ps_ratio,pcf_ratio,"
                 "market_cap,circulating_market_cap,turnover_ratio",
    "indicator": "code,roe,inc_revenue_year_on_year,"
                 "inc_net_profit_year_on_year,net_profit_margin,"
                 "gross_profit_margin",
}


def fetch_daily(client: JQClient, code: str, start: str, end: str,
                chunk_days: int = 900) -> int:
    """抓日线 + 估值。分段抓（接口对单次返回行数可能有上限）。"""
    frames = []
    cur = pd.Timestamp(start)
    endt = pd.Timestamp(end)
    while cur <= endt:
        nxt = min(cur + pd.Timedelta(days=chunk_days), endt)
        try:
            df = client.price(code, start=cur.strftime("%Y-%m-%d"),
                              end=nxt.strftime("%Y-%m-%d"), unit="1d")
            if not df.empty:
                frames.append(df)
        except JQError as e:
            print(f"    [日线] {code} {cur.date()}~{nxt.date()} 失败: {str(e)[:60]}")
        cur = nxt + pd.Timedelta(days=1)
    if not frames:
        return 0
    df = pd.concat(frames, ignore_index=True)
    if "time" in df.columns:
        df = df.rename(columns={"time": "date"})
    if "date" in df.columns:
        df["date"] = df["date"].astype(str).str[:10]
    store.save_daily(code, df)
    return len(df)


def fetch_fundamentals(client: JQClient, table: str, codes: list[str],
                       dates: list[str], chunk: int = 800) -> int:
    """抓某个估值/财务表。返回落盘行数。"""
    cols = FUND_FIELDS.get(table)
    if not cols:
        return 0
    got = {}
    for d in dates:
        for i in range(0, len(codes), chunk):
            part = codes[i:i + chunk]
            try:
                df = client.fundamentals(table, cols, d)
            except JQError as e:
                print(f"    [{table}] {d} 失败: {str(e)[:60]}")
                continue
            if df.empty:
                continue
            for _, r in df.iterrows():
                c = str(r.get("code"))
                got.setdefault(c, []).append(r.to_dict())
    n = 0
    for c, rows in got.items():
        out = pd.DataFrame(rows)
        # 合并进已有财务文件（按 pubDate/date 去重）
        old = store.load_financial(c) if table == "indicator" else None
        if old is not None and not old.empty:
            out = pd.concat([old, out], ignore_index=True)
        key = "pubDate" if "pubDate" in out.columns else (
            "date" if "date" in out.columns else None)
        if key:
            out = out.drop_duplicates(key).sort_values(key)
        store.save_financial(c, out)
        n += len(out)
    return n


def fetch_flow(codes: list[str], pause: float = 0.25,
               force: bool = False) -> dict:
    """抓新浪资金流。逐只落盘 + 断点续传。"""
    from . import to_sina_code
    st = store.load_state()
    ok = skip = fail = 0
    errs = []
    t0 = time.time()
    for i, c in enumerate(codes, 1):
        if not force and store.is_done("flow", c, st):
            skip += 1
            continue
        try:
            df = fetch_sina_flow(to_sina_code(c))
            if df.empty:
                fail += 1
                errs.append(f"{c}: 空")
                continue
            store.save_flow(c, df)
            store.mark_done("flow", c, st)
            ok += 1
        except Exception as e:
            fail += 1
            errs.append(f"{c}: {type(e).__name__}")
        if i % 20 == 0:
            print(f"    [资金流] {i}/{len(codes)}  成功 {ok} 跳过 {skip} "
                  f"失败 {fail}  {time.time()-t0:.0f}s")
        time.sleep(pause)
    return {"成功": ok, "跳过": skip, "失败": fail,
            "耗时秒": round(time.time() - t0, 1), "错误样例": errs[:5]}
