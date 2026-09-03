# 由 china-etf-flow-premarket skill 脚本派生，仅放宽「无法估值即抛错」的校验，
# 用于历史数据回填。近期数据仍应使用原 skill 脚本以保证口径一致。
#!/usr/bin/env python3
"""Generate a pre-market ETF subscription/redemption report.

All data is fetched through AkShare. ETF shares come from the Shanghai and
Shenzhen exchanges; the calendar, classification, NAV, and market-price inputs
come from AkShare's public-source adapters. Money-market ETFs are excluded.
Estimated cash flow is share change multiplied by the latest unit NAV, with
the latest market price used only when NAV is unavailable.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import akshare as ak


SHANGHAI = ZoneInfo("Asia/Shanghai")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exchange-share previous-trading-day non-money ETF flow report"
    )
    parser.add_argument(
        "--trade-date",
        help="Completed trading day in YYYYMMDD; defaults to the latest trading day before today",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs/etf_flow",
        help="Directory for CSV, JSON, and Markdown outputs",
    )
    parser.add_argument("--top", type=int, default=10, help="Rows in each ranking")
    return parser.parse_args()


def clean_for_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean_for_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_for_json(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if hasattr(value, "item"):
        return clean_for_json(value.item())
    return value


def fetch_component(health: list[dict[str, Any]], name: str, source: str, call):
    try:
        frame = call()
        health.append(
            {
                "component": name,
                "status": "ok",
                "rows": int(len(frame)),
                "source": source,
                "error": None,
            }
        )
        return frame
    except Exception as exc:
        health.append(
            {
                "component": name,
                "status": "error",
                "rows": 0,
                "source": source,
                "error": f"{type(exc).__name__}: {str(exc)[:500]}",
            }
        )
        raise


def fetch_optional_component(health: list[dict[str, Any]], name: str, source: str, call):
    try:
        return fetch_component(health, name, source, call)
    except Exception:
        return pd.DataFrame()


def resolve_dates(requested: str | None, health: list[dict[str, Any]]) -> tuple[str, str]:
    today = datetime.now(SHANGHAI).date()
    calendar = fetch_component(
        health,
        "trade_calendar",
        "AkShare.tool_trade_date_hist_sina / Sina",
        ak.tool_trade_date_hist_sina,
    )
    if calendar.empty:
        raise RuntimeError("AkShare trade calendar returned no rows")
    if "trade_date" not in calendar.columns:
        raise RuntimeError("AkShare trade calendar is missing trade_date")
    open_dates = sorted(pd.to_datetime(calendar["trade_date"]).dt.strftime("%Y%m%d").unique())
    if requested:
        if requested not in open_dates:
            raise RuntimeError(f"Requested date {requested} is not an A-share trading day")
        eligible = [d for d in open_dates if d <= requested]
    else:
        today_text = today.strftime("%Y%m%d")
        eligible = [d for d in open_dates if d < today_text]
    if len(eligible) < 2:
        raise RuntimeError("Could not resolve two completed AkShare trading days")
    return eligible[-1], eligible[-2]


def normalize_sse(frame: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    required = {"基金代码", "基金简称", "统计日期", "基金份额"}
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(f"SSE ETF response missing columns: {sorted(missing)}")
    result = frame.loc[:, list(required)].copy()
    result["trade_date"] = pd.to_datetime(result["统计日期"]).dt.strftime("%Y%m%d")
    dates = set(result["trade_date"].dropna())
    if dates != {trade_date}:
        raise RuntimeError(f"SSE ETF response date mismatch: expected {trade_date}, got {sorted(dates)}")
    result["ts_code"] = result["基金代码"].astype(str).str.zfill(6) + ".SH"
    result["exchange_name"] = result["基金简称"].astype(str)
    result["share_wan"] = pd.to_numeric(result["基金份额"], errors="coerce") / 10000
    result["exchange"] = "SSE"
    if result["ts_code"].duplicated().any() or result["share_wan"].isna().any():
        raise RuntimeError(f"Invalid or duplicate SSE ETF shares for {trade_date}")
    return result[["ts_code", "exchange_name", "trade_date", "share_wan", "exchange"]]


def normalize_szse(frame: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    required = {"日期", "基金代码", "基金简称", "基金份额"}
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(f"SZSE ETF response missing columns: {sorted(missing)}")
    result = frame.loc[:, list(required)].copy()
    result["trade_date"] = pd.to_datetime(result["日期"]).dt.strftime("%Y%m%d")
    result = result[result["trade_date"] == trade_date].copy()
    if result.empty:
        raise RuntimeError(f"SZSE ETF response has no rows for {trade_date}")
    result["ts_code"] = result["基金代码"].astype(str).str.zfill(6) + ".SZ"
    result["exchange_name"] = result["基金简称"].astype(str)
    result["share_wan"] = pd.to_numeric(result["基金份额"], errors="coerce") / 10000
    result["exchange"] = "SZSE"
    if result["ts_code"].duplicated().any() or result["share_wan"].isna().any():
        raise RuntimeError(f"Invalid or duplicate SZSE ETF shares for {trade_date}")
    return result[["ts_code", "exchange_name", "trade_date", "share_wan", "exchange"]]


def fetch_exchange_shares(
    previous_date: str, target_date: str, health: list[dict[str, Any]]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    sse_previous_raw = fetch_component(
        health,
        f"sse_etf_share_{previous_date}",
        "AkShare.fund_etf_scale_sse / SSE",
        lambda: ak.fund_etf_scale_sse(date=previous_date),
    )
    sse_current_raw = fetch_component(
        health,
        f"sse_etf_share_{target_date}",
        "AkShare.fund_etf_scale_sse / SSE",
        lambda: ak.fund_etf_scale_sse(date=target_date),
    )
    szse_raw = fetch_component(
        health,
        f"szse_etf_share_{previous_date}_{target_date}",
        "AkShare.fund_scale_daily_szse / SZSE",
        lambda: ak.fund_scale_daily_szse(
            start_date=previous_date, end_date=target_date, symbol="ETF"
        ),
    )
    previous = pd.concat(
        [normalize_sse(sse_previous_raw, previous_date), normalize_szse(szse_raw, previous_date)],
        ignore_index=True,
    )
    current = pd.concat(
        [normalize_sse(sse_current_raw, target_date), normalize_szse(szse_raw, target_date)],
        ignore_index=True,
    )
    return previous, current


def numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(
        series.astype(str).str.replace(",", "", regex=False).replace({"---": pd.NA, "nan": pd.NA}),
        errors="coerce",
    )


def add_etf_exchange_suffix(series: pd.Series) -> pd.Series:
    codes = series.astype(str).str.zfill(6)
    return codes + codes.map(lambda code: ".SH" if code.startswith("5") else ".SZ")


def fetch_ths_info(trade_date: str) -> pd.DataFrame:
    frame = ak.fund_etf_spot_ths(date=trade_date)
    required = {"基金代码", "基金名称", "当前-单位净值", "基金类型", "查询日期"}
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(f"THS ETF response missing columns: {sorted(missing)}")
    query_dates = set(pd.to_datetime(frame["查询日期"]).dt.strftime("%Y%m%d").dropna())
    if query_dates != {trade_date}:
        raise RuntimeError(
            f"THS ETF response date mismatch: expected {trade_date}, got {sorted(query_dates)}"
        )
    result = frame.loc[:, list(required)].copy()
    result["ts_code"] = add_etf_exchange_suffix(result["基金代码"])
    result["name_ths"] = result["基金名称"].astype(str)
    result["fund_type_ths"] = result["基金类型"].astype(str)
    result["nav_ths"] = numeric(result["当前-单位净值"])
    return result[["ts_code", "name_ths", "fund_type_ths", "nav_ths"]].drop_duplicates(
        "ts_code"
    )


def fetch_szse_classification() -> pd.DataFrame:
    frame = ak.fund_etf_scale_szse()
    required = {"基金代码", "基金简称", "基金类别", "投资类别", "净值"}
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(f"SZSE ETF classification missing columns: {sorted(missing)}")
    result = frame.loc[:, list(required)].copy()
    result["ts_code"] = add_etf_exchange_suffix(result["基金代码"])
    result["name_szse"] = result["基金简称"].astype(str)
    result["fund_category_szse"] = result["基金类别"].astype(str)
    result["invest_type_szse"] = result["投资类别"].astype(str)
    result["nav_szse"] = numeric(result["净值"])
    return result[
        [
            "ts_code",
            "name_szse",
            "fund_category_szse",
            "invest_type_szse",
            "nav_szse",
        ]
    ].drop_duplicates("ts_code")


def fetch_eastmoney_info(trade_date: str) -> pd.DataFrame:
    frame = ak.fund_etf_fund_daily_em()
    date_text = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
    nav_column = f"{date_text}-单位净值"
    required = {"基金代码", "基金简称", "类型", nav_column, "市价"}
    missing = required - set(frame.columns)
    if missing:
        raise RuntimeError(
            f"Eastmoney ETF response is stale or missing columns for {trade_date}: {sorted(missing)}"
        )
    result = frame.loc[:, list(required)].copy()
    result["ts_code"] = add_etf_exchange_suffix(result["基金代码"])
    result["name_em"] = result["基金简称"].astype(str).str.replace("行情$", "", regex=True)
    result["fund_type_em"] = result["类型"].astype(str)
    result["nav_em"] = numeric(result[nav_column])
    result["market_price_em"] = numeric(result["市价"])
    return result[
        ["ts_code", "name_em", "fund_type_em", "nav_em", "market_price_em"]
    ].drop_duplicates("ts_code")


def records(frame: pd.DataFrame, columns: list[str]) -> list[dict[str, Any]]:
    return clean_for_json(frame.loc[:, columns].to_dict(orient="records"))


def render_markdown(report: dict[str, Any], top: int) -> str:
    s = report["summary"]
    q = report["quality"]
    lines = [
        f"# 全市场非货币 ETF 申购赎回观察｜{report['trade_date']}",
        "",
        "> 口径：沪深交易所 ETF 总份额的交易日变化，排除货币型 ETF。金额为份额变化 × 当日单位净值；单位净值缺失时使用收盘价估算。正数表示净申购，负数表示净赎回。",
        "",
        "## 市场概览",
        "",
        f"- 总量方向：**{s['direction']} {abs(s['net_flow_yi']):.2f} 亿元**",
        f"- 各ETF净申购额合计：**{s['gross_subscription_yi']:.2f} 亿元**",
        f"- 各ETF净赎回额合计：**{s['gross_redemption_yi']:.2f} 亿元**",
        f"- 股票型 ETF 估算净流入：**{s['stock_net_flow_yi']:.2f} 亿元**",
        f"- 净申购 / 净赎回 / 份额不变：**{s['subscription_count']} / {s['redemption_count']} / {s['unchanged_count']} 只**",
        "",
        "## 分类汇总",
        "",
        "| 类型 | ETF数 | 净申购数 | 净赎回数 | 估算净流入（亿元） |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in report["by_fund_type"]:
        lines.append(
            f"| {row['fund_type']} | {row['etf_count']} | {row['subscription_count']} | "
            f"{row['redemption_count']} | {row['net_flow_yi']:.2f} |"
        )

    def ranking(title: str, rows: list[dict[str, Any]]) -> None:
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                "| 代码 | 名称 | 类型 | 份额变化（万份） | 估算流入（亿元） |",
                "|---|---|---|---:|---:|",
            ]
        )
        for row in rows[:top]:
            lines.append(
                f"| {row['ts_code']} | {row['name']} | {row['fund_type']} | "
                f"{row['share_change_wan']:.2f} | {row['estimated_flow_yi']:.2f} |"
            )

    ranking("净申购前十", report["top_subscriptions"])
    ranking("净赎回前十", report["top_redemptions"])
    lines.extend(
        [
            "",
            "## 数据质量",
            "",
            f"- 比较交易日：{report['previous_trade_date']} → {report['trade_date']}",
            f"- 交易所两日共同ETF：{q['exchange_matched_etfs']} 只；排除货币型：{q['excluded_money_etfs']} 只；纳入统计：{q['included_etfs']} 只",
            f"- 金额可估算：{q['valued_etfs']} 只；有份额变化但缺少净值与收盘价：{q['unvalued_changed_etfs']} 只",
            f"- 单位净值定价：{q['nav_priced_etfs']} 只；市价降级定价：{q['market_price_priced_etfs']} 只",
            "- 这里的申购/赎回是ETF份额净变化；金额是研究估算值，不是基金管理人披露的申购和赎回成交总额。",
            "",
            "## 接口状态",
            "",
            "| 组件 | 状态 | 行数 | 来源 |",
            "|---|---|---:|---|",
        ]
    )
    for row in report["components"]:
        lines.append(
            f"| {row['component']} | {row['status']} | {row['rows']} | {row['source']} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    generated_at = datetime.now(SHANGHAI)
    health: list[dict[str, Any]] = []
    target_date, previous_date = resolve_dates(args.trade_date, health)
    previous, current = fetch_exchange_shares(previous_date, target_date, health)
    szse_info = fetch_optional_component(
        health,
        "szse_etf_classification_latest",
        "AkShare.fund_etf_scale_szse / SZSE",
        fetch_szse_classification,
    )
    ths_info = fetch_optional_component(
        health,
        f"etf_nav_type_ths_{target_date}",
        "AkShare.fund_etf_spot_ths / THS",
        lambda: fetch_ths_info(target_date),
    )
    em_info = fetch_optional_component(
        health,
        f"etf_nav_price_em_{target_date}",
        "AkShare.fund_etf_fund_daily_em / Eastmoney",
        lambda: fetch_eastmoney_info(target_date),
    )
    if szse_info.empty and ths_info.empty:
        raise RuntimeError("No AkShare ETF classification source is available")
    if szse_info.empty:
        szse_info = pd.DataFrame(
            columns=[
                "ts_code",
                "name_szse",
                "fund_category_szse",
                "invest_type_szse",
                "nav_szse",
            ]
        )
    if ths_info.empty:
        ths_info = pd.DataFrame(
            columns=["ts_code", "name_ths", "fund_type_ths", "nav_ths"]
        )
    if em_info.empty:
        em_info = pd.DataFrame(
            columns=["ts_code", "name_em", "fund_type_em", "nav_em", "market_price_em"]
        )

    previous_codes = set(previous["ts_code"])
    current_codes = set(current["ts_code"])
    common_codes = previous_codes & current_codes
    unmatched = pd.concat(
        [
            previous.loc[~previous["ts_code"].isin(common_codes)].assign(missing_on=target_date),
            current.loc[~current["ts_code"].isin(common_codes)].assign(missing_on=previous_date),
        ],
        ignore_index=True,
    )
    frame = (
        previous[["ts_code", "exchange_name", "share_wan", "exchange"]]
        .rename(
            columns={
                "exchange_name": "exchange_name_previous",
                "share_wan": "share_previous_wan",
                "exchange": "exchange_previous",
            }
        )
        .merge(
            current[["ts_code", "exchange_name", "share_wan", "exchange"]].rename(
                columns={
                    "exchange_name": "exchange_name_current",
                    "share_wan": "share_current_wan",
                    "exchange": "exchange_current",
                }
            ),
            on="ts_code",
            how="inner",
        )
        .merge(ths_info, on="ts_code", how="left")
        .merge(szse_info, on="ts_code", how="left")
        .merge(em_info, on="ts_code", how="left")
    )
    if frame.empty:
        raise RuntimeError("No ETFs had share records on both trading days")
    if frame["ts_code"].duplicated().any():
        raise RuntimeError("Duplicate ETF codes remained after merging")
    if not frame["exchange_previous"].equals(frame["exchange_current"]):
        raise RuntimeError("ETF exchange changed between the two trading days")

    frame["exchange"] = frame["exchange_current"]
    frame["name"] = (
        frame["name_ths"].astype("string")
        .combine_first(frame["name_em"].astype("string"))
        .combine_first(frame["exchange_name_current"].astype("string"))
    )
    frame["fund_type"] = frame["fund_type_ths"]
    em_type = frame["fund_type_em"].fillna("")
    frame.loc[frame["fund_type"].isna() & em_type.str.contains("股票"), "fund_type"] = "股票型"
    frame.loc[frame["fund_type"].isna() & em_type.str.contains("固收"), "fund_type"] = "债券型"
    frame.loc[
        frame["fund_type"].isna() & em_type.str.contains("其他|海外"), "fund_type"
    ] = "其他"
    frame["fund_type"] = frame["fund_type"].fillna("未分类")
    money_mask = frame["fund_type"].eq("货币型") | frame["invest_type_szse"].eq(
        "货币市场基金"
    )
    excluded_money = frame.loc[money_mask].copy()
    frame = frame.loc[~money_mask].copy()

    for column in (
        "share_previous_wan",
        "share_current_wan",
        "nav_em",
        "nav_ths",
        "nav_szse",
        "market_price_em",
    ):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["share_change_wan"] = frame["share_current_wan"] - frame["share_previous_wan"]
    frame["share_change_pct"] = frame["share_change_wan"] / frame["share_previous_wan"].replace(0, pd.NA)
    frame["unit_nav"] = frame["nav_em"].where(
        frame["nav_em"].gt(0),
        frame["nav_ths"].where(frame["nav_ths"].gt(0), frame["nav_szse"]),
    )
    nav_valid = frame["unit_nav"].gt(0)
    market_price_valid = frame["market_price_em"].gt(0)
    frame["valuation_price"] = frame["unit_nav"].where(nav_valid, frame["market_price_em"])
    frame["price_source"] = "missing"
    frame.loc[frame["nav_em"].gt(0), "price_source"] = "eastmoney_nav"
    frame.loc[
        ~frame["nav_em"].gt(0) & frame["nav_ths"].gt(0), "price_source"
    ] = "ths_nav"
    frame.loc[
        ~frame["nav_em"].gt(0) & ~frame["nav_ths"].gt(0) & frame["nav_szse"].gt(0),
        "price_source",
    ] = "szse_nav"
    frame.loc[
        ~nav_valid & market_price_valid, "price_source"
    ] = "eastmoney_market_price"
    frame["estimated_flow_yi"] = frame["share_change_wan"] * frame["valuation_price"] / 10000
    frame.loc[
        frame["share_change_wan"].eq(0) & frame["estimated_flow_yi"].isna(),
        "estimated_flow_yi",
    ] = 0.0
    unvalued_changed = frame["share_change_wan"].ne(0) & frame["estimated_flow_yi"].isna()
    if unvalued_changed.any():
        # [历史回填降级] 原版 skill 在此直接抛错，导致无法回溯 2 年前的数据。
        # 历史日期上部分 ETF（多为已退市或数据源不再覆盖）取不到净值/价格，
        # 这里改为将其净流记为 0 并标记，保证长历史可生成。
        degraded = int(unvalued_changed.sum())
        frame.loc[unvalued_changed, "estimated_flow_yi"] = 0.0
        frame.loc[unvalued_changed, "price_source"] = "unavailable_historical"
        print(f"[degraded] {degraded} ETF(s) unvalued -> flow set to 0")
    frame["structural_change_flag"] = (
        frame["share_change_pct"].abs().gt(0.5) & frame["estimated_flow_yi"].abs().gt(1)
    )
    frame = frame.sort_values(["estimated_flow_yi", "ts_code"], ascending=[False, True])

    priced = frame[frame["estimated_flow_yi"].notna()].copy()
    by_type = (
        frame.groupby("fund_type", dropna=False)
        .agg(
            etf_count=("ts_code", "count"),
            subscription_count=("share_change_wan", lambda x: int((x > 0).sum())),
            redemption_count=("share_change_wan", lambda x: int((x < 0).sum())),
            net_flow_yi=("estimated_flow_yi", "sum"),
        )
        .reset_index()
        .sort_values("net_flow_yi", ascending=False)
    )
    by_type_rows = clean_for_json(by_type.to_dict(orient="records"))
    stock_flow = float(
        priced.loc[priced["fund_type"] == "股票型", "estimated_flow_yi"].sum()
    )
    net_flow = float(priced["estimated_flow_yi"].sum())
    direction = "净申购" if net_flow > 0 else "净赎回" if net_flow < 0 else "申赎平衡"
    report_columns = [
        "ts_code",
        "name",
        "fund_type",
        "exchange",
        "share_change_wan",
        "share_change_pct",
        "nav_em",
        "nav_ths",
        "nav_szse",
        "unit_nav",
        "market_price_em",
        "valuation_price",
        "price_source",
        "estimated_flow_yi",
        "structural_change_flag",
    ]
    report = {
        "generated_at": generated_at.isoformat(),
        "akshare_version": ak.__version__,
        "source_policy": "AkShare only; SSE/SZSE shares, Sina calendar, THS/Eastmoney classification and NAV; money ETFs excluded",
        "methodology": "Change in exchange-reported ETF outstanding shares; estimated cash flow uses AkShare NAV, then AkShare market price as fallback",
        "trade_date": target_date,
        "previous_trade_date": previous_date,
        "summary": {
            "direction": direction,
            "net_flow_yi": net_flow,
            "gross_subscription_yi": float(
                priced.loc[priced["estimated_flow_yi"] > 0, "estimated_flow_yi"].sum()
            ),
            "gross_redemption_yi": float(
                -priced.loc[priced["estimated_flow_yi"] < 0, "estimated_flow_yi"].sum()
            ),
            "stock_net_flow_yi": stock_flow,
            "subscription_count": int((frame["share_change_wan"] > 0).sum()),
            "redemption_count": int((frame["share_change_wan"] < 0).sum()),
            "unchanged_count": int(frame["share_change_wan"].eq(0).sum()),
        },
        "quality": {
            "exchange_matched_etfs": int(len(common_codes)),
            "exchange_unmatched_etfs": int(len(unmatched)),
            "excluded_money_etfs": int(len(excluded_money)),
            "included_etfs": int(len(frame)),
            "valued_etfs": int(frame["estimated_flow_yi"].notna().sum()),
            "unvalued_changed_etfs": int(
                (frame["valuation_price"].isna() & frame["share_change_wan"].ne(0)).sum()
            ),
            "nav_priced_etfs": int(frame["price_source"].str.endswith("_nav").sum()),
            "market_price_priced_etfs": int(
                frame["price_source"].eq("eastmoney_market_price").sum()
            ),
            "structural_change_flags": int(frame["structural_change_flag"].sum()),
        },
        "by_fund_type": by_type_rows,
        "top_subscriptions": records(
            priced[priced["estimated_flow_yi"] > 0].nlargest(args.top, "estimated_flow_yi"),
            report_columns,
        ),
        "top_redemptions": records(
            priced[priced["estimated_flow_yi"] < 0].nsmallest(args.top, "estimated_flow_yi"),
            report_columns,
        ),
        "structural_change_flags": records(
            frame[frame["structural_change_flag"]].sort_values(
                "estimated_flow_yi", key=lambda x: x.abs(), ascending=False
            ),
            report_columns,
        ),
        "unmatched_exchange_etfs": records(
            unmatched.sort_values(["missing_on", "ts_code"]),
            ["ts_code", "exchange_name", "trade_date", "exchange", "missing_on"],
        ),
        "excluded_money_etfs": records(
            excluded_money.sort_values("ts_code"),
            ["ts_code", "name", "fund_type", "exchange"],
        ),
        "missing_price_etfs": records(
            frame.loc[frame["valuation_price"].isna()],
            ["ts_code", "name", "fund_type", "share_change_wan"],
        ),
        "components": health,
    }
    report = clean_for_json(report)

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = output_dir / f"etf_flow_{target_date}"
    frame.to_csv(stem.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    stem.with_suffix(".json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    stem.with_suffix(".md").write_text(render_markdown(report, args.top), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
