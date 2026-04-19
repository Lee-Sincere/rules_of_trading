#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
A股复盘回测脚本
验证 T-1 日核心观察池预测在 T 日的实际表现，挖掘赚钱效应 / 规避亏钱效应

用法：
    python backtest.py --t1 20260327 --t 20260330     # 单次回测
    python backtest.py --batch                        # 批量：扫描所有相邻日期对
"""

import pandas as pd
import numpy as np
import json
import os
import glob
import argparse
from datetime import datetime
import warnings

warnings.filterwarnings("ignore")

# ══════════════════════════════════════════════════
# 配置常量
# ══════════════════════════════════════════════════
STOP_LOSS_PCT   = 0.07   # 止损：买入价 × (1 - 0.07)
TAKE_PROFIT_PCT = 0.07   # 动态止盈：从当日最高点回落 7%（报告中标注，不影响估算逻辑）
MIN_PROFIT_PCT  = 0.05   # 💰赚钱效应下限：涨幅≥5% 视为有效盈利机会
SENTIMENT_DROP_THRESHOLD = 0.75   # 情绪衰减率警戒线（T日上涨家数/T-1 < 0.75 → 空仓警告）
VOLUME_DROP_THRESHOLD    = 0.70   # 成交额骤降警戒线（T日/T-1 < 0.70 → 缩量警告）
SEAL_TIME_DELAY_MINUTES  = 30     # 封板时间后移警戒（T日首封比T-1晚 > 30分钟 → 动能减弱）


# ══════════════════════════════════════════════════
# 1. 数据加载
# ══════════════════════════════════════════════════

def load_pool(date: str) -> dict:
    """读取 T-1 日观察池 JSON 快照"""
    path = f"observation_pool_{date}.json"
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"找不到 {path}。请先运行 analysis_20260316.py 生成当日快照。"
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_stocks_xls(date: str) -> pd.DataFrame:
    """读取 T 日全部A股.xls，复用 analysis 脚本的清洗逻辑"""
    path = f"全部Ａ股{date}.xls"
    if not os.path.exists(path):
        raise FileNotFoundError(f"找不到 {path}，请确认文件存在于当前目录。")

    df = pd.read_csv(path, sep="\t", encoding="gbk")
    numeric_cols = [
        "涨幅%", "现价", "涨跌", "总量", "现量", "换手%", "今开", "最高", "最低",
        "昨收", "总金额", "量比", "振幅%", "均价", "内盘", "外盘", "内外比",
        "委比%", "涨速%", "主力净比%", "主力净额", "开盘金额", "开盘抢筹%",
        "短换手%", "2分钟金额",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace("--", "").str.strip(),
                errors="coerce",
            )
    df["代码"] = df["代码"].astype(str).str.extract(r"(\d+)")[0].str.zfill(6)
    df = df[~df["名称"].str.contains("ST|退", na=False)].copy()
    return df


def load_sector_xls(date: str) -> pd.DataFrame:
    """读取 T 日板块指数.xls（可选，用于板块级验证）"""
    candidates = [f"板块指数{date}.xls", f"板块指数-概念{date}.xls"]
    for path in candidates:
        if os.path.exists(path):
            try:
                df = pd.read_csv(path, sep="\t", encoding="gbk")
                df.columns = df.columns.str.strip()
                df["代码"] = df["代码"].astype(str).str.extract(r"(\d+)")[0]
                for col in ["涨幅%", "涨停数", "强弱度%", "换手Z", "量比"]:
                    if col in df.columns:
                        df[col] = pd.to_numeric(
                            df[col].astype(str).str.replace("--", "").str.strip(),
                            errors="coerce",
                        )
                return df
            except Exception:
                pass
    return pd.DataFrame()


# ══════════════════════════════════════════════════
# 2. 辅助函数
# ══════════════════════════════════════════════════

def classify_limit_pct(code: str) -> float:
    c = str(code).zfill(6)
    if c.startswith("688") or c.startswith("300") or c.startswith("301"):
        return 20.0
    if c.startswith("8") or c.startswith("4"):
        return 30.0
    return 10.0


def classify_stock_status(row: pd.Series) -> dict:
    """判断涨停/炸板状态（复用 analysis 脚本逻辑）"""
    code = str(row.get("代码", ""))
    limit_pct = classify_limit_pct(code)
    yesterday_close = row.get("昨收", np.nan)
    high = row.get("最高", np.nan)
    gain = row.get("涨幅%", np.nan)
    theoretical_limit = round(yesterday_close * (1 + limit_pct / 100), 2) if pd.notna(yesterday_close) else np.nan
    is_limit_up = (gain >= (limit_pct - 0.3)) if pd.notna(gain) else False
    is_explode = (
        (pd.notna(high) and pd.notna(theoretical_limit) and high >= theoretical_limit - 0.01)
        and not is_limit_up
    )
    return {
        "limit_pct": limit_pct,
        "theoretical_limit": theoretical_limit,
        "is_limit_up": bool(is_limit_up),
        "is_explode": bool(is_explode),
    }


def seal_time_to_minutes(seal_str: str) -> int:
    """将封板时间字符串（如 '093027'）转化为分钟数（相对 09:30）"""
    try:
        s = str(seal_str).replace(":", "").zfill(6)
        h, m = int(s[:2]), int(s[2:4])
        return h * 60 + m
    except Exception:
        return 999


def estimate_pnl(open_price: float, gain_pct: float, is_limit_up: bool,
                 theoretical_limit: float, stop_loss_pct: float = STOP_LOSS_PCT) -> dict:
    """
    基于T日开盘价估算买入场景的P&L
    假设：集合竞价结束后按今开买入
    - 若当日涨停：理论卖出 = 涨停价，收益 = 涨停价/今开 - 1
    - 若当日跌幅触及止损：收益 = -stop_loss_pct
    - 其余：收益 ≈ 收盘涨幅%（相对昨收），需折算为相对今开的收益
    """
    if pd.isna(open_price) or open_price <= 0:
        return {"estimated_pnl_pct": None, "pnl_basis": "数据缺失"}

    if is_limit_up and pd.notna(theoretical_limit):
        pnl = (theoretical_limit / open_price - 1) * 100
        return {"estimated_pnl_pct": round(pnl, 2), "pnl_basis": "涨停价/今开"}

    if pd.notna(gain_pct):
        # gain_pct 是相对昨收的涨幅，今开与昨收可能有差异，但近似处理
        if gain_pct < -stop_loss_pct * 100:
            return {"estimated_pnl_pct": round(-stop_loss_pct * 100, 2), "pnl_basis": "触发止损"}
        # 用涨幅%近似（保守估算，实际上相对今开的P&L可能不同）
        return {"estimated_pnl_pct": round(gain_pct, 2), "pnl_basis": "收盘涨幅近似"}

    return {"estimated_pnl_pct": None, "pnl_basis": "数据缺失"}


def money_effect_rating(gain_pct, is_limit_up, is_explode):
    """赚钱效应评级"""
    if is_limit_up or (pd.notna(gain_pct) and gain_pct >= MIN_PROFIT_PCT * 100):
        return "💰盈利机会"
    if is_explode or (pd.notna(gain_pct) and gain_pct < -STOP_LOSS_PCT * 100):
        return "⚠️陷阱"
    return "➖中性"


def verify_constitution(sentiment: str, sector_identified: bool,
                        is_sector_core: bool, seal_time_str: str,
                        stock_role: str = "--") -> dict:
    """
    对照《交易系统宪法》验证买入四条件
    条件4（量比>3/分时均线信号）无法从XLS完全验证
    """
    c1 = sentiment in ("强", "混沌")
    c2 = sector_identified
    c3 = is_sector_core  # 连板最高/封板最早 → 近似判断
    # 条件4需盘中数据，此处标注为"需盘中确认"
    compliant = c1 and c2 and c3

    # 构建违规原因列表
    violations = []
    if not c1:
        violations.append(f"情绪={sentiment}(需强/混沌)")
    if not c2:
        violations.append("无明确主线板块")
    if not c3:
        violations.append(f"非板块人气核心(角色={stock_role})")

    return {
        "condition1_emotion": "✅" if c1 else "❌",
        "condition2_sector":  "✅" if c2 else "❌",
        "condition3_core":    "✅" if c3 else "❌",
        "condition4_signal":  "ℹ️需盘中确认",
        "compliant":          "✅宪法合规" if compliant else "❌违规",
        "violations":         violations,
    }


# ══════════════════════════════════════════════════
# 3. 核心回测函数
# ══════════════════════════════════════════════════

def backtest_stock(stock_dict: dict, df_t: pd.DataFrame,
                   t1_pool_codes: set) -> dict:
    """
    单股回测：在 df_t（T日全部A股）中查找该股，计算实际表现
    """
    code = str(stock_dict.get("code", "")).zfill(6)
    name = str(stock_dict.get("name", ""))

    matched = df_t[df_t["代码"] == code]
    if matched.empty:
        return {
            "code": code, "name": name,
            "sector": stock_dict.get("sector", "--"),
            "t1_boards": stock_dict.get("boards", 0),
            "t1_seal_time": stock_dict.get("seal_time", "--"),
            "t1_volume_billion": stock_dict.get("volume_billion", 0),
            "t1_explode_count": stock_dict.get("explode_count", 0),
            "role": stock_dict.get("role", "--"),
            "t_gain_pct": None,
            "t_open": None,
            "t_high": None,
            "t_close": None,
            "t_volume_billion": None,
            "t_vol_ratio": None,
            "is_limit_up": False,
            "is_explode": False,
            "volume_change_ratio": None,
            "estimated_pnl_pct": None,
            "pnl_basis": "T日数据缺失",
            "money_effect": "➖中性",
            "warning_flags": ["T日无数据"],
        }

    row = matched.iloc[0]
    status = classify_stock_status(row)
    gain_pct = row.get("涨幅%", np.nan)
    open_price = row.get("今开", np.nan)
    high_price = row.get("最高", np.nan)
    t_volume = row.get("总金额", np.nan)
    t_vol_ratio = row.get("量比", np.nan)

    t1_volume_bn = stock_dict.get("volume_billion", 0)
    t_vol_bn = t_volume / 1e4 if pd.notna(t_volume) else 0
    volume_change_ratio = (t_vol_bn / t1_volume_bn) if (t1_volume_bn > 0 and pd.notna(t_volume)) else None

    pnl_result = estimate_pnl(
        open_price, gain_pct, status["is_limit_up"], status["theoretical_limit"]
    )

    rating = money_effect_rating(gain_pct, status["is_limit_up"], status["is_explode"])

    # 预警标志
    warning_flags = []
    t1_vol_bn = t1_volume_bn
    if volume_change_ratio is not None and volume_change_ratio < VOLUME_DROP_THRESHOLD:
        warning_flags.append(f"🔻成交额骤降({t1_vol_bn:.1f}→{t_vol_bn:.1f}亿,{volume_change_ratio:.0%})")
    if status["is_explode"]:
        warning_flags.append("💥炸板警告")
    t1_seal_min = seal_time_to_minutes(stock_dict.get("seal_time", "150000"))
    t1_explode = stock_dict.get("explode_count", 0)
    t1_boards  = stock_dict.get("boards", 0)
    if t1_boards > 0 and (t1_explode / t1_boards) > 0.5:
        warning_flags.append(f"⚠️危险指数高(炸板{t1_explode}次/{t1_boards}连板)")

    return {
        "code": code,
        "name": name,
        "sector": stock_dict.get("sector", "--"),
        "t1_boards": t1_boards,
        "t1_seal_time": stock_dict.get("seal_time", "--"),
        "t1_volume_billion": t1_vol_bn,
        "t1_explode_count": t1_explode,
        "role": stock_dict.get("role", "--"),
        "t_gain_pct": round(float(gain_pct), 2) if pd.notna(gain_pct) else None,
        "t_open": round(float(open_price), 2) if pd.notna(open_price) else None,
        "t_high": round(float(high_price), 2) if pd.notna(high_price) else None,
        "t_close": round(float(row.get("现价", np.nan)), 2) if pd.notna(row.get("现价")) else None,
        "t_volume_billion": round(t_vol_bn, 2),
        "t_vol_ratio": round(float(t_vol_ratio), 2) if pd.notna(t_vol_ratio) else None,
        "is_limit_up": status["is_limit_up"],
        "is_explode": status["is_explode"],
        "volume_change_ratio": round(volume_change_ratio, 2) if volume_change_ratio is not None else None,
        "estimated_pnl_pct": pnl_result["estimated_pnl_pct"],
        "pnl_basis": pnl_result["pnl_basis"],
        "money_effect": rating,
        "warning_flags": warning_flags,
    }


def verify_sector(sector_dict: dict, df_t_stocks: pd.DataFrame,
                  df_t_sector: pd.DataFrame) -> dict:
    """
    板块持续性验证 —— 从T日全部A股XLS中查找T-1板块内每只个股的实际表现
    不再依赖板块指数文件的名称匹配（行业名≠概念名，根本匹配不上）
    """
    name = sector_dict.get("name", "")
    t1_zt = sector_dict.get("limit_up_count", 0)
    t1_max_boards = sector_dict.get("max_boards", 0)
    t1_score = sector_dict.get("score_6d", 0)
    t1_continuity = sector_dict.get("continuity_rating", "--")
    t1_volume = sector_dict.get("volume_billion", 0)
    t1_zb_count = sector_dict.get("zb_count", 0)
    t1_avg_gain_pct = sector_dict.get("avg_gain_pct", None)

    # 核心改动：从JSON的板块个股列表中取出T-1日所有股票代码
    t1_sector_stocks = sector_dict.get("stocks", [])
    if not t1_sector_stocks:
        return {
            "name": name, "t1_zt": t1_zt, "t1_max_boards": t1_max_boards,
            "t1_score_6d": t1_score, "t1_continuity": t1_continuity,
            "t1_volume_billion": t1_volume,
            "t1_zb_count": t1_zb_count, "t1_avg_gain_pct": t1_avg_gain_pct,
            "stock_details": [],
            "t_zt_count": 0, "t_explode_count": 0, "t_avg_gain_pct": None,
            "t_total_volume_billion": 0, "volume_change_ratio": None,
            "actual_continuity": "数据缺失", "prediction_hit": None,
        }

    # 在T日XLS中逐一查找这些个股
    stock_details = []
    t_zt_count = 0
    t_explode_count = 0
    t_gains = []
    t_total_volume = 0.0

    for s in t1_sector_stocks:
        code = str(s.get("code", "")).zfill(6)
        s_name = str(s.get("name", ""))
        t1_boards = s.get("boards", 0)
        t1_vol = s.get("volume_billion", 0)

        matched = df_t_stocks[df_t_stocks["代码"] == code]
        if matched.empty:
            stock_details.append({
                "code": code, "name": s_name,
                "t1_boards": t1_boards, "t1_volume_billion": t1_vol,
                "t_gain_pct": None, "is_limit_up": False, "is_explode": False,
                "t_volume_billion": 0, "status": "T日无数据",
            })
            continue

        row = matched.iloc[0]
        status = classify_stock_status(row)
        gain_pct = row.get("涨幅%", np.nan)
        t_vol = row.get("总金额", 0)
        t_vol_bn = t_vol / 1e4 if pd.notna(t_vol) else 0

        if status["is_limit_up"]:
            t_zt_count += 1
        if status["is_explode"]:
            t_explode_count += 1
        if pd.notna(gain_pct):
            t_gains.append(gain_pct)
        t_total_volume += t_vol_bn

        # 判断状态文字
        if status["is_limit_up"]:
            status_str = f"🟢涨停(+{gain_pct:.1f}%)" if pd.notna(gain_pct) else "🟢涨停"
        elif status["is_explode"]:
            status_str = f"💥炸板({gain_pct:+.1f}%)" if pd.notna(gain_pct) else "💥炸板"
        elif pd.notna(gain_pct) and gain_pct >= 5:
            status_str = f"📈强势({gain_pct:+.1f}%)"
        elif pd.notna(gain_pct) and gain_pct <= -5:
            status_str = f"📉大跌({gain_pct:+.1f}%)"
        elif pd.notna(gain_pct):
            status_str = f"{'📈' if gain_pct > 0 else '📉'}{gain_pct:+.1f}%"
        else:
            status_str = "数据异常"

        stock_details.append({
            "code": code, "name": s_name,
            "t1_boards": t1_boards, "t1_volume_billion": t1_vol,
            "t_gain_pct": round(float(gain_pct), 2) if pd.notna(gain_pct) else None,
            "is_limit_up": status["is_limit_up"],
            "is_explode": status["is_explode"],
            "t_volume_billion": round(t_vol_bn, 2),
            "status": status_str,
        })

    # 聚合板块级指标（全部从T日XLS真实数据计算）
    t_avg_gain = round(np.mean(t_gains), 2) if t_gains else None
    volume_change_ratio = round(t_total_volume / t1_volume, 2) if t1_volume > 0 else None

    # 持续性判断：基于T日实际涨停数 vs T-1日
    if t_zt_count >= max(t1_zt * 0.7, 2):
        actual_continuity = "高"
    elif t_zt_count >= 1 or (t_avg_gain is not None and t_avg_gain > 2):
        actual_continuity = "中"
    else:
        actual_continuity = "低"

    # 命中判断
    hit_map = {"高": {"高", "中"}, "中": {"中", "高"}, "低": {"低"}}
    prediction_hit = actual_continuity in hit_map.get(t1_continuity, set())

    return {
        "name": name,
        "t1_zt": t1_zt,
        "t1_max_boards": t1_max_boards,
        "t1_score_6d": t1_score,
        "t1_continuity": t1_continuity,
        "t1_volume_billion": t1_volume,
        "t1_zb_count": t1_zb_count,
        "t1_avg_gain_pct": t1_avg_gain_pct,
        "stock_details": stock_details,
        "t_zt_count": t_zt_count,
        "t_explode_count": t_explode_count,
        "t_avg_gain_pct": t_avg_gain,
        "t_total_volume_billion": round(t_total_volume, 2),
        "volume_change_ratio": volume_change_ratio,
        "actual_continuity": actual_continuity,
        "prediction_hit": prediction_hit,
    }


def compute_signal_accuracy(stock_results: list) -> dict:
    """各类信号命中率统计"""
    if not stock_results:
        return {}

    # 仅统计有效数据（T日有数据的个股）
    valid = [r for r in stock_results if r.get("t_gain_pct") is not None]
    if not valid:
        return {}

    total = len(valid)
    profit_count  = sum(1 for r in valid if r["money_effect"] == "💰盈利机会")
    neutral_count = sum(1 for r in valid if r["money_effect"] == "➖中性")
    trap_count    = sum(1 for r in valid if r["money_effect"] == "⚠️陷阱")

    # 按信号条件分类统计
    def signal_hit_rate(condition_fn):
        cond = [r for r in valid if condition_fn(r)]
        if not cond:
            return None
        hits = sum(1 for r in cond if r["money_effect"] == "💰盈利机会")
        return {"total": len(cond), "hit": hits, "rate": round(hits / len(cond), 2)}

    return {
        "total_stocks":       total,
        "profit_count":       profit_count,
        "neutral_count":      neutral_count,
        "trap_count":         trap_count,
        "win_rate":           round(profit_count / total, 2),
        "signal_boards_ge3":  signal_hit_rate(lambda r: r.get("t1_boards", 0) >= 3),
        "signal_early_seal":  signal_hit_rate(lambda r: seal_time_to_minutes(r.get("t1_seal_time", "")) < 9*60+33),
        "signal_no_explode":  signal_hit_rate(lambda r: r.get("t1_explode_count", 0) == 0),
        "signal_leaders":     signal_hit_rate(lambda r: r.get("role") == "龙头"),
        "signal_army":        signal_hit_rate(lambda r: r.get("role") == "中军"),
        "signal_quant":       signal_hit_rate(lambda r: r.get("role") == "量化"),
        "limit_up_stocks":    [r["name"] for r in valid if r["is_limit_up"]],
        "explode_stocks":     [r["name"] for r in valid if r["is_explode"]],
    }


def detect_new_leaders(pool: dict, df_t: pd.DataFrame) -> list:
    """识别T日新晋龙头：T日高连板但T-1观察池中未出现的个股"""
    pool_codes = set()
    for stock in pool.get("observation_pool", {}).get("leaders", []):
        pool_codes.add(str(stock.get("code", "")).zfill(6))
    for stock in pool.get("observation_pool", {}).get("army", []):
        pool_codes.add(str(stock.get("code", "")).zfill(6))
    for stock in pool.get("all_board_stocks", []):
        pool_codes.add(str(stock.get("code", "")).zfill(6))

    if df_t.empty or "涨幅%" not in df_t.columns:
        return []

    # 近似：T日涨停 且 T-1观察池中无此股
    from analysis_20260316 import classify_limit_pct as clp
    new_leaders = []
    for _, row in df_t.iterrows():
        code = str(row.get("代码", "")).zfill(6)
        if code in pool_codes:
            continue
        gain = row.get("涨幅%", np.nan)
        limit_pct = clp(code)
        if pd.notna(gain) and gain >= (limit_pct - 0.3):
            new_leaders.append({
                "code": code,
                "name": str(row.get("名称", "")),
                "gain_pct": round(float(gain), 2),
                "volume_billion": round(float(row.get("总金额", 0)) / 1e4, 2) if pd.notna(row.get("总金额")) else 0,
                "sector": str(row.get("细分行业", "--")),
            })
    # 按成交额排序，取前10
    new_leaders.sort(key=lambda x: x["volume_billion"], reverse=True)
    return new_leaders[:10]


# ══════════════════════════════════════════════════
# 4. 报告生成
# ══════════════════════════════════════════════════

def _fmt(v, suffix="", default="--", decimals=2):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return default
    if isinstance(v, float):
        return f"{v:.{decimals}f}{suffix}"
    return f"{v}{suffix}"


def generate_backtest_report(t1_date: str, t_date: str,
                              pool: dict,
                              stock_results: list,
                              sector_results: list,
                              signal_acc: dict,
                              new_leaders: list,
                              t1_sentiment: dict,
                              t_sentiment: dict) -> str:
    """生成 Markdown 格式回测报告"""

    lines = []
    t1_str = f"{t1_date[:4]}/{t1_date[4:6]}/{t1_date[6:]}"
    t_str  = f"{t_date[:4]}/{t_date[4:6]}/{t_date[6:]}"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines.append(f"# 📊 复盘回测报告：{t1_str} → {t_str}")
    lines.append(f"*生成时间：{now_str}*")
    lines.append("")
    lines.append(f"> 回测逻辑：以 T-1 日（{t1_str}）复盘报告的核心观察池预测，"
                 f"对照 T 日（{t_str}）全部A股实际数据验证准确率。"
                 f"买入假设：T日集合竞价按今开买入，止损 -7%，止盈取最高价。")
    lines.append("")

    # ── 一、市场情绪连续性 ─────────────────────────────────
    lines.append("---")
    lines.append("## 一、市场情绪连续性")
    lines.append("")

    t1_up = t1_sentiment.get("up_count", 0)
    t_up  = t_sentiment.get("up_count", 0)
    t1_emo = t1_sentiment.get("sentiment", "--")
    t_emo  = t_sentiment.get("sentiment", "--")
    t1_zt = t1_sentiment.get("zt_count", 0)
    t_zt  = t_sentiment.get("zt_count", 0)

    emo_arrow = "✅延续" if t1_emo == t_emo else f"❌变化({t1_emo}→{t_emo})"
    up_arrow  = "↑" if t_up > t1_up else ("↓" if t_up < t1_up else "→")
    zt_arrow  = "↑" if t_zt > t1_zt else ("↓" if t_zt < t1_zt else "→")

    lines.append("| 项目 | T-1 日 | T 日实际 | 变化 |")
    lines.append("|------|--------|---------|------|")
    lines.append(f"| 市场情绪 | {t1_emo} | {t_emo} | {emo_arrow} |")
    lines.append(f"| 上涨家数 | {t1_up} | {t_up} | {up_arrow}({t_up - t1_up:+d}) |")
    lines.append(f"| 涨停数 | {t1_zt} | {t_zt} | {zt_arrow}({t_zt - t1_zt:+d}) |")
    lines.append(f"| 炸板率 | {t1_sentiment.get('zb_rate', 0):.1%} | {t_sentiment.get('zb_rate', 0):.1%} | — |")
    lines.append("")

    # 情绪衰减率
    decay_rate = t_up / t1_up if t1_up > 0 else 1.0
    if decay_rate < SENTIMENT_DROP_THRESHOLD:
        lines.append(f"🚨 **情绪骤降警告**：上涨家数衰减率 {decay_rate:.0%} < {SENTIMENT_DROP_THRESHOLD:.0%}，"
                     f"**建议空仓或大幅减仓**（仓位上限从 {t1_sentiment.get('position','--')} → {t_sentiment.get('position','--')}）")
    elif t1_emo != t_emo:
        lines.append(f"⚠️ **情绪转变**：{t1_emo} → {t_emo}，仓位应从 {t1_sentiment.get('position','--')} 调整至 {t_sentiment.get('position','--')}")
    else:
        lines.append(f"✅ **情绪延续**：连续两日 {t1_emo}，仓位保持 {t1_sentiment.get('position','--')} 上限")
    lines.append("")

    # ── 二、板块持续性回测 ─────────────────────────────────
    lines.append("---")
    lines.append("## 二、板块持续性回测")
    lines.append("")

    sector_hit = sum(1 for s in sector_results if s.get("prediction_hit") is True)
    sector_miss = sum(1 for s in sector_results if s.get("prediction_hit") is False)
    if sector_hit + sector_miss > 0:
        lines.append(f"**板块预判总命中率：{sector_hit}/{sector_hit+sector_miss} = "
                     f"{sector_hit/(sector_hit+sector_miss):.0%}**")
        lines.append("")

    for s in sector_results:
        rating_emoji = {"高": "⭐⭐⭐", "中": "⭐⭐", "低": "⭐"}.get(s["t1_continuity"], "—")
        lines.append(f"### {s['name']}（T-1预判：{rating_emoji}{s['t1_continuity']}）")
        lines.append("")

        # 板块总览表
        stock_count = len(s.get("stock_details", []))
        vol_chg_str = f"{s['volume_change_ratio']:.0%}" if s.get("volume_change_ratio") else "--"
        t1_zb_str = f"{s.get('t1_zb_count', 0)}只" if s.get("t1_zb_count") is not None else "—"
        t1_avg_str = f"{s['t1_avg_gain_pct']:+.1f}%" if s.get("t1_avg_gain_pct") is not None else "—"
        avg_gain_str = f"{s['t_avg_gain_pct']:+.1f}%" if s.get("t_avg_gain_pct") is not None else "—"
        lines.append("| 指标 | T-1 日 | T 日实际（XLS验证） |")
        lines.append("|------|--------|-------------------|")
        lines.append(f"| 涨停数 | {s['t1_zt']}只 | {s['t_zt_count']}只 |")
        lines.append(f"| 炸板数 | {t1_zb_str} | {s['t_explode_count']}只 |")
        lines.append(f"| 板块内平均涨幅 | {t1_avg_str} | {avg_gain_str} |")
        lines.append(f"| 板块内总成交额 | {_fmt(s['t1_volume_billion'], '亿')} | {_fmt(s['t_total_volume_billion'], '亿')} |")
        lines.append(f"| 成交额变化 | — | {vol_chg_str} |")
        lines.append(f"| 覆盖个股数 | {stock_count}只 | — |")
        lines.append("")

        # 板块内逐只个股明细表
        details = s.get("stock_details", [])
        if details:
            lines.append("**板块内个股T日表现（数据来源：T日全部A股XLS）：**")
            lines.append("")
            lines.append("| 代码 | 名称 | T-1连板 | T-1成交额 | T日涨幅% | T日成交额 | T日状态 |")
            lines.append("|------|------|--------|---------|---------|---------|--------|")
            for d in details:
                gain_str = f"{d['t_gain_pct']:+.1f}%" if d.get("t_gain_pct") is not None else "—"
                vol_str = _fmt(d.get("t_volume_billion", 0), "亿")
                lines.append(
                    f"| {d['code']} | {d['name']} | "
                    f"{d['t1_boards']}板 | {_fmt(d['t1_volume_billion'], '亿')} | "
                    f"{gain_str} | {vol_str} | {d['status']} |"
                )
            lines.append("")

        hit_str = "**命中✅**" if s["prediction_hit"] else ("**未命中❌**" if s["prediction_hit"] is False else "无法判断")
        lines.append(f"**持续性验证：** 预判`{s['t1_continuity']}` → 实际`{s['actual_continuity']}` → {hit_str}")
        lines.append("")

    # ── 三、核心观察池个股回测 ────────────────────────────
    lines.append("---")
    lines.append("## 三、核心观察池个股回测")
    lines.append("")

    for role_label, role_key in [("🥇 龙头候选", "龙头"), ("💰 中军候选", "中军"), ("📊 量化高分股", "量化")]:
        role_results = [r for r in stock_results if r.get("role") == role_key]
        if not role_results:
            continue
        lines.append(f"### {role_label}")
        lines.append("")
        lines.append("| 代码 | 名称 | 板块 | T-1连板 | T-1成交额 | T日涨幅% | 涨停/炸板 | 估算P&L | 赚钱效应 | 预警 |")
        lines.append("|------|------|------|--------|---------|---------|---------|--------|---------|------|")
        for r in role_results:
            gain_str = _fmt(r["t_gain_pct"], "%") if r["t_gain_pct"] is not None else "数据缺失"
            status_str = "🟢涨停" if r["is_limit_up"] else ("🔴炸板" if r["is_explode"] else "")
            pnl_str = _fmt(r["estimated_pnl_pct"], "%") if r["estimated_pnl_pct"] is not None else "--"
            warn_str = " ".join(r.get("warning_flags", [])) or "—"
            lines.append(
                f"| {r['code']} | **{r['name']}** | {r['sector']} | "
                f"{r['t1_boards']}板 | {_fmt(r['t1_volume_billion'], '亿')} | "
                f"{gain_str} | {status_str} | {pnl_str} | {r['money_effect']} | {warn_str} |"
            )
        lines.append("")

    # ── 四、赚钱效应挖掘 ──────────────────────────────────
    lines.append("---")
    lines.append("## 四、赚钱效应挖掘")
    lines.append("")

    lines.append("### 4.1 信号可靠性统计")
    lines.append("")
    lines.append("| 信号类型 | 样本数 | 命中数 | 命中率 |")
    lines.append("|--------|--------|--------|--------|")

    def fmt_signal(key, label):
        v = signal_acc.get(key)
        if v is None:
            lines.append(f"| {label} | — | — | 样本不足 |")
        else:
            lines.append(f"| {label} | {v['total']} | {v['hit']} | **{v['rate']:.0%}** |")

    fmt_signal("signal_leaders",   "龙头候选（连板最高+封板早）")
    fmt_signal("signal_army",      "中军候选（成交额前3）")
    fmt_signal("signal_quant",     "量化高分股（5维评分）")
    fmt_signal("signal_boards_ge3","连板≥3板")
    fmt_signal("signal_early_seal","封板时间 < 09:33")
    fmt_signal("signal_no_explode","T-1日无炸板记录")
    lines.append("")

    lines.append(f"**本轮整体理论胜率：{signal_acc.get('profit_count', 0)} / "
                 f"{signal_acc.get('total_stocks', 0)} = "
                 f"{signal_acc.get('win_rate', 0):.0%}**  "
                 f"（💰{signal_acc.get('profit_count',0)} / ➖{signal_acc.get('neutral_count',0)} / ⚠️{signal_acc.get('trap_count',0)}）")
    lines.append("")

    lines.append("### 4.2 假设买入P&L汇总")
    lines.append("")
    lines.append("*假设条件：T日集合竞价按今开买入；止损 -7%；不考虑手续费与冲击成本。*")
    lines.append("")
    lines.append("*合规性依据《交易系统宪法V1.1》：① 情绪∈{强,混沌} ② 主线板块≤2个 ③ 标的=板块人气核心(龙头/中军)*")
    lines.append("")
    lines.append("| 代码 | 名称 | 今开(估) | T日结果 | 估算收益 | 合规性 | 违规原因 | 备注 |")
    lines.append("|------|------|---------|--------|--------|--------|---------|------|")
    t1_top_sectors = [s.get("name", "") for s in pool.get("main_sectors", [])[:2]]
    for r in stock_results:
        if r.get("t_gain_pct") is None:
            continue
        open_str = _fmt(r["t_open"])
        result_str = "🟢涨停" if r["is_limit_up"] else ("🔴炸板" if r["is_explode"] else f"{_fmt(r['t_gain_pct'], '%')}")
        pnl_str = _fmt(r["estimated_pnl_pct"], "%") if r["estimated_pnl_pct"] is not None else "--"
        is_core = r.get("role") in ("龙头", "中军")
        comply = verify_constitution(
            t_sentiment.get("sentiment", ""),
            bool(t1_top_sectors),
            is_core,
            r.get("t1_seal_time", ""),
            stock_role=r.get("role", "--"),
        )
        violation_str = "; ".join(comply["violations"]) if comply["violations"] else "—"
        warn_str = " ".join(r.get("warning_flags", [])) or "—"
        lines.append(
            f"| {r['code']} | **{r['name']}** | {open_str} | {result_str} | "
            f"{pnl_str} | {comply['compliant']} | {violation_str} | {warn_str} |"
        )
    lines.append("")

    # ── 五、亏钱效应规避验证 ──────────────────────────────
    lines.append("---")
    lines.append("## 五、亏钱效应规避验证")
    lines.append("")
    lines.append("验证 T-1 日是否已存在预警信号，若存在则评估是否提前识别了风险。")
    lines.append("")

    # 情绪骤降
    lines.append("### 5.1 情绪骤降预警验证")
    lines.append("")
    if decay_rate < SENTIMENT_DROP_THRESHOLD:
        lines.append(f"- 🚨 **触发**：上涨家数 {t1_up}→{t_up}，衰减率 {decay_rate:.0%} < {SENTIMENT_DROP_THRESHOLD:.0%}")
        lines.append("- **结论**：情绪骤降信号在T日开盘前无法提前知晓，需配合大盘开盘后5分钟量比观察判断")
    else:
        lines.append(f"- ✅ 未触发：上涨家数衰减率 {decay_rate:.0%} ≥ 75%")
    lines.append("")

    # 成交额骤降
    lines.append("### 5.2 个股成交额骤降验证")
    lines.append("")
    volume_drop_stocks = [r for r in stock_results if r.get("volume_change_ratio") is not None
                          and r["volume_change_ratio"] < VOLUME_DROP_THRESHOLD]
    if volume_drop_stocks:
        lines.append("| 代码 | 名称 | T-1成交额 | T日成交额 | 缩量比 | T日结果 |")
        lines.append("|------|------|---------|---------|------|--------|")
        for r in volume_drop_stocks:
            result_str = "🟢涨停" if r["is_limit_up"] else ("🔴炸板" if r["is_explode"] else _fmt(r["t_gain_pct"], "%"))
            lines.append(
                f"| {r['code']} | **{r['name']}** | {_fmt(r['t1_volume_billion'], '亿')} | "
                f"{_fmt(r['t_volume_billion'], '亿')} | {_fmt(r['volume_change_ratio'], 'x', decimals=2)} | {result_str} |"
            )
        lines.append("")
        lines.append("> **规律校验**：出现缩量后，该股最终表现是否变差？积累样本验证「缩量 → 炸板/滞涨」规律。")
    else:
        lines.append("- ✅ 本轮无明显成交额骤降情况（所有个股成交额变化 ≥ 70%）")
    lines.append("")

    # 高位过热换手 & T-1前置预警验证
    lines.append("### 5.3 T-1前置预警 & 高位过热验证")
    lines.append("")
    lines.append("*仅基于T-1已知数据判断是否存在前置预警信号，不使用T日事后数据。*")
    lines.append("")
    explode_or_trap = [r for r in stock_results if r.get("t_gain_pct") is not None
                       and (r["is_explode"] or (r["t_gain_pct"] is not None and r["t_gain_pct"] < -STOP_LOSS_PCT * 100))]
    if explode_or_trap:
        lines.append("**实际形成陷阱的个股（炸板或触发止损）：**")
        lines.append("")
        lines.append("| 代码 | 名称 | T日结果 | T-1连板 | T-1炸板次数 | T-1危险指数 | T-1封板时间 | T-1前置预警 |")
        lines.append("|------|------|--------|--------|----------|----------|----------|----------|")
        for r in explode_or_trap:
            t1_boards = r.get("t1_boards", 0)
            t1_explode = r.get("t1_explode_count", 0)
            danger = round(t1_explode / t1_boards, 2) if t1_boards > 0 else 0
            seal_time = r.get("t1_seal_time", "--")
            result_str = "🔴炸板" if r["is_explode"] else _fmt(r["t_gain_pct"], "%")
            # 基于T-1已知数据的前置预警（不含T日事后信息）
            t1_warnings = []
            if t1_boards > 0 and (t1_explode / t1_boards) > 0.5:
                t1_warnings.append(f"危险指数高({t1_explode}炸/{t1_boards}板={danger})")
            seal_min = seal_time_to_minutes(seal_time)
            if seal_min > 10 * 60:  # 10:00 以后封板
                t1_warnings.append(f"封板偏晚({seal_time})")
            if r.get("role") not in ("龙头", "中军"):
                t1_warnings.append(f"非核心角色({r.get('role','--')})")
            if t1_boards == 0:
                t1_warnings.append("非涨停股(无连板数据)")
            has_warn = " / ".join(t1_warnings) if t1_warnings else "无前置预警（系统盲区）"
            has_icon = "✅" if t1_warnings else "❌"
            lines.append(
                f"| {r['code']} | **{r['name']}** | {result_str} | "
                f"{t1_boards}板 | {t1_explode}次 | {danger} | "
                f"{seal_time} | {has_icon}{has_warn} |"
            )
        lines.append("")
        lines.append("> **系统盲区分析**：若「T-1前置预警」列为❌，说明系统在T-1日无法基于已有数据识别风险，"
                     "需考虑补充更多前置指标（如换手率过高、成交额偏离均值等）。")
    else:
        lines.append("- ✅ 本轮无炸板或止损触发个股")
    lines.append("")

    # ── 新晋龙头 ────────────────────────────────────────
    lines.append("---")
    lines.append("## 六、T日新晋龙头（T-1观察池未收录）")
    lines.append("")
    lines.append("*这些是 T-1 复盘漏掉的、在 T 日才涌现的强势股，是系统盲区，需复盘改进。*")
    lines.append("")
    if new_leaders:
        lines.append("| 代码 | 名称 | 板块 | T日涨幅% | 成交额 |")
        lines.append("|------|------|------|--------|-------|")
        for s in new_leaders[:10]:
            lines.append(f"| {s['code']} | **{s['name']}** | {s['sector']} | {s['gain_pct']:.2f}% | {s['volume_billion']:.1f}亿 |")
    else:
        lines.append("*（新晋龙头识别需 T 日 XLS 数据，当前暂无数据）*")
    lines.append("")

    # ── 七、系统校准建议 ─────────────────────────────────
    lines.append("---")
    lines.append("## 七、系统校准建议")
    lines.append("")

    suggestions = []
    win_rate = signal_acc.get("win_rate", 0)
    if win_rate < 0.4:
        suggestions.append(
            "**预测准确率偏低（<40%）**：建议在情绪混沌时进一步提高进入门槛——"
            "仅关注连板≥3板 且 封板时间<09:33 的个股，过滤首板股。"
        )
    if volume_drop_stocks:
        vols = [r["name"] for r in volume_drop_stocks if r.get("warning_flags")]
        if vols:
            suggestions.append(
                f"**成交额骤降预警命中（{','.join(vols)}）**：将「T日成交额<T-1日70%」"
                "作为持仓减仓信号，而非静态持有等待次日判断。"
            )
    if decay_rate < SENTIMENT_DROP_THRESHOLD:
        suggestions.append(
            f"**情绪骤降发生（{t1_up}→{t_up}，{decay_rate:.0%}）**：建议在开盘后前10分钟"
            "监测指数跌幅是否>1%，若是则直接触发减仓至1成仓位，不等待复盘报告。"
        )
    boards_ge3_signal = signal_acc.get("signal_boards_ge3")
    if boards_ge3_signal and boards_ge3_signal["total"] >= 2:
        rate = boards_ge3_signal["rate"]
        if rate > 0.6:
            suggestions.append(
                f"**连板≥3板信号命中率高（{rate:.0%}）**：当前策略对高连板股识别有效，"
                "建议在仓位分配上给予连板≥3板个股额外权重（可至单股上限20%→25%）。"
            )
        elif rate < 0.4:
            suggestions.append(
                f"**连板≥3板信号命中率低（{rate:.0%}）**：本轮高连板股兑现率不佳，"
                "可能处于末期洗盘阶段——增加「T日封板时间是否比T-1提前」的二次确认。"
            )

    if not suggestions:
        suggestions.append("本轮数据样本不足以生成针对性建议，请积累更多回测数据后再分析。")

    for i, s in enumerate(suggestions, 1):
        lines.append(f"{i}. {s}")
    lines.append("")

    lines.append("---")
    lines.append(f"*本回测报告自动生成，数据来源：observation_pool_{t1_date}.json + 全部Ａ股{t_date}.xls。*")
    lines.append(f"*买入估算基于今开价，不考虑冲击成本和手续费；理论胜率仅供系统优化参考。*")

    return "\n".join(lines)


# ══════════════════════════════════════════════════
# 5. 批量汇总
# ══════════════════════════════════════════════════

def generate_batch_summary(all_run_results: list) -> str:
    """批量模式：所有日期对的回测汇总"""
    lines = []
    lines.append("# 📈 A股回测系统 — 批量汇总报告")
    lines.append(f"*生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}*")
    lines.append("")
    lines.append("| T-1日   | T日     | 情绪T-1 | 情绪T | 整体胜率 | 💰盈利 | ⚠️陷阱 | 板块命中 |")
    lines.append("|---------|---------|---------|------|---------|------|------|---------|")

    for run in all_run_results:
        t1 = run.get("t1_date", "")
        t  = run.get("t_date", "")
        t1_emo  = run.get("t1_emotion", "--")
        t_emo   = run.get("t_emotion", "--")
        win_rate = run.get("win_rate", 0)
        profit  = run.get("profit_count", 0)
        trap    = run.get("trap_count", 0)
        s_hit   = run.get("sector_hit_str", "--")
        lines.append(
            f"| {t1} | {t} | {t1_emo} | {t_emo} | **{win_rate:.0%}** | "
            f"{profit} | {trap} | {s_hit} |"
        )
    lines.append("")

    if all_run_results:
        overall_win = sum(r.get("profit_count", 0) for r in all_run_results)
        overall_total = sum(r.get("total_stocks", 0) for r in all_run_results)
        wr = overall_win / overall_total if overall_total > 0 else 0
        lines.append(f"**累计整体胜率：{overall_win}/{overall_total} = {wr:.0%}**")
        lines.append("")

    lines.append("---")
    lines.append("*每个回测日期的详细报告见对应的 `回测报告_YYYYMMDD_vs_YYYYMMDD.md` 文件。*")
    return "\n".join(lines)


# ══════════════════════════════════════════════════
# 6. 主入口
# ══════════════════════════════════════════════════

def run_single(t1_date: str, t_date: str, save: bool = True) -> dict:
    """执行单次回测，返回摘要 dict"""
    print(f"\n{'='*60}")
    print(f"回测：{t1_date} → {t_date}")
    print(f"{'='*60}")

    # 加载数据
    print("[1/5] 加载 T-1 日观察池快照...")
    pool = load_pool(t1_date)

    print("[2/5] 加载 T 日全部A股 XLS...")
    df_t = load_stocks_xls(t_date)
    print(f"  加载 {len(df_t)} 只股票")

    print("[3/5] 加载 T 日板块指数 XLS（可选）...")
    df_t_sector = load_sector_xls(t_date)
    if df_t_sector.empty:
        print("  (未找到板块指数文件，板块级验证将使用推算值)")
    else:
        print(f"  加载 {len(df_t_sector)} 个板块")

    # T 日市场情绪（从 XLS 估算）
    t_up = int((df_t["涨幅%"] > 0).sum())
    t_down = int((df_t["涨幅%"] < 0).sum())
    limit_pcts = df_t["代码"].apply(classify_limit_pct)
    t_zt = int((df_t["涨幅%"] >= (limit_pcts - 0.3)).sum())
    t_zb_mask = (df_t["最高"].fillna(0) >= (df_t["昨收"].fillna(0) * (1 + limit_pcts / 100) - 0.01))
    t_zb = int((t_zb_mask & (df_t["涨幅%"] < (limit_pcts - 0.3))).sum())
    t_zb_rate = t_zb / (t_zt + t_zb) if (t_zt + t_zb) > 0 else 0
    if t_up > 2500:
        t_sentiment_str = "强"
        t_position = "7成"
    elif t_up < 1800:
        t_sentiment_str = "弱"
        t_position = "1成（建议空仓）"
    else:
        t_sentiment_str = "混沌"
        t_position = "5成"
    t_sentiment = {
        "up_count": t_up, "down_count": t_down,
        "zt_count": t_zt, "zb_count": t_zb, "zb_rate": t_zb_rate,
        "sentiment": t_sentiment_str, "position": t_position,
    }
    t1_sentiment = pool.get("sentiment", {})

    print(f"  T日情绪：{t_sentiment_str}（上涨{t_up}/涨停{t_zt}/炸板{t_zb}）")

    # 回测个股
    print("[4/5] 回测核心观察池个股...")
    obs = pool.get("observation_pool", {})
    all_stock_dicts = (
        [dict(r, role="龙头") for r in obs.get("leaders", [])] +
        [dict(r, role="中军") for r in obs.get("army", [])] +
        [dict(r, role="量化") for r in obs.get("quant_top15", [])]
    )
    # 用 all_board_stocks 补全量化高分股的连板/封板/炸板数据
    board_map = {}
    for b in pool.get("all_board_stocks", []):
        board_map[str(b.get("code", "")).zfill(6)] = b
    for s in all_stock_dicts:
        if s.get("role") == "量化" and s.get("boards", 0) == 0:
            code = str(s.get("code", "")).zfill(6)
            if code in board_map:
                b = board_map[code]
                s["boards"] = b.get("boards", 0)
                s["seal_time"] = b.get("seal_time", "--")
                s["explode_count"] = b.get("explode_count", 0)
                s["volume_billion"] = b.get("volume_billion", 0)
    # 去重（同一支股可能在龙头和中军都出现）
    seen_codes = set()
    dedup_stocks = []
    for s in all_stock_dicts:
        code = str(s.get("code", "")).zfill(6)
        if code not in seen_codes:
            dedup_stocks.append(s)
            seen_codes.add(code)

    stock_results = [backtest_stock(s, df_t, seen_codes) for s in dedup_stocks]
    print(f"  回测 {len(stock_results)} 只个股")

    # 板块验证
    sector_results = [
        verify_sector(sec, df_t, df_t_sector)
        for sec in pool.get("main_sectors", [])[:3]
    ]

    # 信号准确率
    signal_acc = compute_signal_accuracy(stock_results)
    print(f"  整体胜率：{signal_acc.get('win_rate', 0):.0%}（"
          f"盈利{signal_acc.get('profit_count',0)} / "
          f"中性{signal_acc.get('neutral_count',0)} / "
          f"陷阱{signal_acc.get('trap_count',0)}）")

    # 新晋龙头
    print("[5/5] 识别新晋龙头...")
    try:
        new_leaders = detect_new_leaders(pool, df_t)
        print(f"  发现 {len(new_leaders)} 只新晋涨停股（T-1观察池未收录）")
    except Exception as e:
        print(f"  新晋龙头识别失败（{e}），跳过")
        new_leaders = []

    # 生成报告
    report_text = generate_backtest_report(
        t1_date, t_date, pool, stock_results, sector_results,
        signal_acc, new_leaders, t1_sentiment, t_sentiment
    )

    if save:
        output_file = f"回测报告_{t1_date}_vs_{t_date}.md"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(report_text)
        print(f"\n[OK] 回测报告已保存: {output_file}")

    # 返回摘要
    sector_hit_str = "/".join(
        [s["name"][:4] + ("✅" if s.get("prediction_hit") else "❌")
         for s in sector_results if s.get("prediction_hit") is not None]
    )
    return {
        "t1_date": t1_date, "t_date": t_date,
        "t1_emotion": t1_sentiment.get("sentiment", "--"),
        "t_emotion": t_sentiment_str,
        "win_rate": signal_acc.get("win_rate", 0),
        "profit_count": signal_acc.get("profit_count", 0),
        "trap_count": signal_acc.get("trap_count", 0),
        "total_stocks": signal_acc.get("total_stocks", 0),
        "sector_hit_str": sector_hit_str,
        "report_file": f"回测报告_{t1_date}_vs_{t_date}.md" if save else None,
    }


def run_batch():
    """批量模式：扫描所有 observation_pool_*.json，回测所有相邻日期对"""
    pool_files = sorted(glob.glob("observation_pool_*.json"))
    if len(pool_files) < 2:
        print("批量模式需要至少 2 个 observation_pool_*.json 文件。")
        print("请先对各交易日运行 analysis_20260316.py 生成快照。")
        return

    dates = []
    for f in pool_files:
        base = os.path.basename(f)
        date = base.replace("observation_pool_", "").replace(".json", "")
        dates.append(date)

    print(f"发现 {len(dates)} 个快照：{dates}")
    print(f"将回测 {len(dates)-1} 个相邻日期对\n")

    all_results = []
    for i in range(len(dates) - 1):
        t1_date = dates[i]
        t_date  = dates[i + 1]
        # 检查 T 日 XLS 是否存在
        if not os.path.exists(f"全部Ａ股{t_date}.xls"):
            print(f"  [SKIP] 跳过 {t1_date}→{t_date}：缺少 全部A股{t_date}.xls")
            continue
        try:
            result = run_single(t1_date, t_date, save=True)
            all_results.append(result)
        except Exception as e:
            print(f"  [FAIL] {t1_date}→{t_date} 回测失败：{e}")

    # 批量汇总报告
    if all_results:
        summary_text = generate_batch_summary(all_results)
        with open("回测汇总.md", "w", encoding="utf-8") as f:
            f.write(summary_text)
        print(f"\n[OK] 批量回测完成，汇总报告已保存: 回测汇总.md")
    else:
        print("\n无有效回测结果。")


def main():
    parser = argparse.ArgumentParser(
        description="A股复盘回测：验证T-1日预测在T日的实际表现"
    )
    parser.add_argument("--t1",    type=str, help="T-1日期（YYYYMMDD）")
    parser.add_argument("--t",     type=str, help="T日期（YYYYMMDD）")
    parser.add_argument("--batch", action="store_true", help="批量模式：回测所有相邻日期对")

    args = parser.parse_args()

    if args.batch:
        run_batch()
    elif args.t1 and args.t:
        run_single(args.t1, args.t, save=True)
    else:
        parser.print_help()
        print("\n示例：")
        print("  python backtest.py --t1 20260327 --t 20260330")
        print("  python backtest.py --batch")


if __name__ == "__main__":
    main()
