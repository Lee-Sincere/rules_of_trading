#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
A股全市场深度复盘分析脚本 - 2026/3/16
基于《A股系统化交易全流程工作手册》模板
"""

import pandas as pd
import numpy as np
import akshare as ak
from datetime import datetime
import warnings
import sys
import io
import json

warnings.filterwarnings('ignore')
# sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

TARGET_DATE = "20260511"  # 分析目标日期，格式 YYYYMMDD
DATA_FILE = f"全部Ａ股{TARGET_DATE}.xls"
# 同花顺导出的板块指数文件（两种命名都支持，优先新格式）
# 新格式：板块指数{DATE}.xls   旧格式：板块指数-概念{DATE}.xls
CONCEPT_FILE = f"板块指数{TARGET_DATE}.xls"

# 超级主题关键词映射 —— 解决CSRC行业分类无法识别主题概念集群的问题
SUPER_THEMES = {
    "AI算力基础设施": ["算力租赁", "数据中心", "液冷服务器", "CPO概念", "东数西算",
                      "存储芯片", "云计算", "国资云", "数字水印", "信息安全"],
    "AI应用与生态":   ["DeepSeek概念", "AIGC概念", "AI智能体", "人工智能", "大数据",
                      "数据要素", "虚拟现实", "智慧城市", "工业互联"],
    "半导体芯片链":   ["芯片", "存储芯片", "PCB概念", "汽车电子", "OLED概念",
                      "消费电子概念", "小米概念"],
    "通信与卫星":     ["5G概念", "6G概念", "卫星导航", "物联网", "车联网",
                      "低空经济", "商业航天"],
    "新能源与储能":   ["储能", "光伏", "新能源车", "风电", "绿色电力",
                      "核电核能", "氢能源", "燃料电池"],
    "军工国防":       ["国防军工", "军民融合", "大飞机", "低空经济"],
}

# ══════════════════════════════════════════════════
# 1. 数据读取与清洗
# ══════════════════════════════════════════════════

def load_local_data(filepath):
    """加载本地全市场行情CSV数据"""
    df = pd.read_csv(filepath, sep='\t', encoding='gbk')
    numeric_cols = [
        '涨幅%', '现价', '涨跌', '总量', '现量', '换手%', '今开', '最高', '最低',
        '昨收', '总金额', '量比', '振幅%', '均价', '内盘', '外盘', '内外比',
        '委比%', '涨速%', '主力净比%', '主力净额', '开盘金额', '开盘抢筹%',
        '短换手%', '2分钟金额'
    ]
    
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace('--', '').str.strip(),
                errors='coerce'
            )
    df['代码'] = df['代码'].astype(str).str.extract(r'(\d+)')[0].str.zfill(6)
    # 去除ST/退市
    df = df[~df['名称'].str.contains('ST|退', na=False)].copy()
    return df


def classify_limit_pct(code):
    """根据股票代码判断涨停幅度"""
    c = str(code).zfill(6)
    if c.startswith('688') or c.startswith('300') or c.startswith('301'):
        return 20.0   # 科创板/创业板 ±20%
    if c.startswith('8') or c.startswith('4'):
        return 30.0   # 北交所 ±30%
    return 10.0       # 主板 ±10%


def identify_limit_and_break(df):
    """识别涨停与炸板股"""
    df = df.copy()
    df['涨停幅度'] = df['代码'].apply(classify_limit_pct)
    df['理论涨停价'] = (df['昨收'] * (1 + df['涨停幅度'] / 100)).round(2)
    df['理论跌停价'] = (df['昨收'] * (1 - df['涨停幅度'] / 100)).round(2)
    # 涨停判定：涨幅接近涨停幅度（允许0.3%误差）
    df['是否涨停'] = df['涨幅%'] >= (df['涨停幅度'] - 0.3)
    # 炸板判定：最高触及涨停价，但未收涨停
    df['是否炸板'] = (
        (df['最高'] >= df['理论涨停价'] - 0.01) &
        (~df['是否涨停'])
    )
    return df


# ─── 板块指数文件列名与策略映射说明 ───────────────────────────────────────
# 【A类·主线判定】涨停数 / 强弱度% / 涨幅% / 内部上涨比(涨跌数解析)
#   涨停数     → 板块宽度，>10=宽基强势主线，>5=有效主线
#   强弱度%    → 概念指数超额收益 vs 全市场，>2.5=机构主动认可，<0=跑输大盘
#   内部上涨比  → 成分股涨幅家数/总家数，>0.9=全板块集体联动（信号可信度高）
#
# 【B类·资金活跃度】换手Z / 量比 / 量涨速% / 开盘换手Z / 开盘金额
#   换手Z      → 换手率Z分数（当日换手 vs 近期均值偏差），>6=极强异动
#   量涨速%    → 量能加速度（2分钟金额变化速率），>0=资金持续涌入
#   开盘换手Z  → 集合竞价阶段异动程度，>0.05=竞价阶段即有大资金参与
#
# 【C类·趋势动量】3日涨幅% / 5日涨幅% / 连涨天 / 月初至今% / 开盘昨比%
#   3日涨幅%   → 今日以外的近期趋势，>0=非单日骗局，趋势延续中
#   连涨天     → 连续上涨天数，>2=趋势成立
#   开盘昨比%  → 今日开盘/昨日开盘-1，>0=开盘强度逐日增强（动量自我强化）
#
# 【D类·技术形态（新）】短期形态 / 中期形态 / 长期形态
#   同花顺技术打分（0-15），三周期形态共振=多头趋势最强信号
#   短期形态≥12 且 中期形态≥6 = 短中期共振，最佳买入窗口
#   三期均≥8 = 长短中期全面共振，强趋势延续概率>70%
#
# 【E类·攻守强度（新）】攻击波% / 回头波%
#   攻击波%   → 从最低点到最高点的振幅（多头力量）
#   回头波%   → 从最高点到收盘的回撤（空头力量），一般为负值
#   攻守比    = 攻击波% / |回头波%|，>2=多头完全主导，1-2=多头占优，<1=空头反压
#
# 【F类·估值参考】市盈率 / 市净率 / 流通市值
#   流通市值  → 过小的板块（<500亿）易被操纵，信号噪音大，需谨慎
#
# 【不使用列】现价/涨跌/涨速%/总量/振幅%/昨收/今开/最高/最低/均价/
#   AB股总市值/流通股本Z/流通股(亿)/总股本(亿)/创建日期 — 原始数据或重复信息
# ─────────────────────────────────────────────────────────────────────────

def load_concept_board_local(filepath):
    """加载本地板块指数文件，兼容新（55列）和旧（36列）两种格式，自动回退文件名。"""
    import os

    # 文件名自动回退：新格式→旧格式
    candidates = [
        filepath,
        filepath.replace('板块指数', '板块指数-概念'),
    ]
    actual_path = None
    for p in candidates:
        if os.path.exists(p):
            actual_path = p
            break
    if actual_path is None:
        return pd.DataFrame()

    try:
        df = pd.read_csv(actual_path, sep='\t', encoding='gbk')
    except Exception as e:
        print(f"  [警告] 概念文件读取失败: {e}")
        return pd.DataFrame()

    df.columns = df.columns.str.strip()
    # 清洗代码列（同花顺格式：='880669'）
    df['代码'] = df['代码'].astype(str).str.extract(r'(\d+)')[0]

    # 所有需要转为数值的列（按A-E类策略维度组织，仅转换存在的列）
    numeric_cols = [
        # A类·主线判定
        '涨幅%', '涨停数', '跌停数', '强弱度%',
        # B类·资金活跃
        '量比', '换手%', '换手Z', '量涨速%', '短换手%', '开盘换手Z', '开盘昨比%',
        '总金额', '开盘金额', '主力净额', '主力净比%', '2分钟金额',
        # C类·趋势动量
        '昨涨幅%', '3日涨幅%', '5日涨幅%', '10日涨幅%', '20日涨幅%',
        '60日涨幅%', '一年涨幅%', '月初至今%', '年初至今%', '连涨天',
        # D类·技术形态（新列）
        '短期形态', '中期形态', '长期形态',
        # E类·攻守强度（新列）
        '攻击波%', '回头波%', '现均差%', '开盘%',
        # F类·估值参考
        '市盈率', '市净率', '振幅%',
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(
                df[col].astype(str).str.replace('--', '').str.strip(), errors='coerce'
            )

    # 解析「168|15」格式的涨跌数 → 内部上涨比（板块内部宽度）
    if '涨跌数' in df.columns:
        parts = df['涨跌数'].astype(str).str.split('|', expand=True)
        df['内部上涨数'] = pd.to_numeric(parts[0], errors='coerce')
        df['内部下跌数'] = pd.to_numeric(parts[1], errors='coerce')
        total = df['内部上涨数'] + df['内部下跌数']
        df['内部上涨比'] = (df['内部上涨数'] / total.replace(0, np.nan)).fillna(0.5)

    # 解析流通市值（"15776.54亿" → 数值 15776.54）
    if '流通市值' in df.columns:
        df['流通市值_亿'] = pd.to_numeric(
            df['流通市值'].astype(str).str.replace('亿', '').str.strip(), errors='coerce'
        )

    # 预计算衍生指标
    # D类：三周期形态综合分（0-100）,短期权重最高
    form_cols = [c for c in ['短期形态', '中期形态', '长期形态'] if c in df.columns]
    if form_cols:
        weights = {'短期形态': 0.45, '中期形态': 0.35, '长期形态': 0.20}
        df['形态综合分_raw'] = sum(
            df[c].fillna(0) * weights.get(c, 0.33) for c in form_cols
        )

    # E类：攻守比（攻击波% / |回头波%|），自然上限裁为 0-10
    if '攻击波%' in df.columns and '回头波%' in df.columns:
        df['攻守比'] = (df['攻击波%'].fillna(0) /
                       (df['回头波%'].fillna(0).abs() + 0.1)).clip(0, 10)

    return df


# ══════════════════════════════════════════════════
# 2. 从 akshare 补充专业数据
# ══════════════════════════════════════════════════

def fetch_akshare_data(date):
    """获取akshare涨停池、炸板池、强势股池"""
    result = {}

    # 涨停池（含首次封板时间、连板数、所属行业）
    try:
        df = ak.stock_zt_pool_em(date=date)
        df['首次封板时间'] = df['首次封板时间'].astype(str).str.zfill(6)
        df['代码'] = df['代码'].astype(str).str.zfill(6)
        result['zt_pool'] = df
    except Exception as e:
        print(f"  [警告] 涨停池获取失败: {e}")
        result['zt_pool'] = pd.DataFrame()

    # 炸板池
    try:
        df = ak.stock_zt_pool_zbgc_em(date=date)
        df['代码'] = df['代码'].astype(str).str.zfill(6)
        result['zb_pool'] = df
    except Exception as e:
        print(f"  [警告] 炸板池获取失败: {e}")
        result['zb_pool'] = pd.DataFrame()

    # 强势股池（近期高点/量比/60日新高等）
    try:
        df = ak.stock_zt_pool_strong_em(date=date)
        df['代码'] = df['代码'].astype(str).str.zfill(6)
        result['strong_pool'] = df
    except Exception as e:
        print(f"  [警告] 强势股池获取失败: {e}")
        result['strong_pool'] = pd.DataFrame()

    return result


def analyze_concept_boards_multidim(concept_raw_df, zt_pool=None):
    """
    概念板块8维度量化评分体系（本地数据版）

    ┌─────────────────────────────────────────────────────────────────┐
    │ 维度  名称        权重   数据列            投资含义               │
    ├─────────────────────────────────────────────────────────────────┤
    │  A1  涨停宽度     20%   涨停数            主线广度，>10宽基强势  │
    │  A2  相对强度     15%   强弱度%           超额收益，>2.5机构认可 │
    │  A3  今日涨幅     13%   涨幅%             板块即时动量           │
    │  B1  换手Z活跃   12%   换手Z             资金异常涌入程度        │
    │  B2  内部宽幅     8%    内部上涨比        成分股联动真实性        │
    │  C1  趋势动量     8%    3日涨幅%          中期趋势延续性          │
    │  D1  形态共振    12%   短/中/长期形态    技术面多周期支撑        │
    │  E1  攻守强度    12%   攻击波/回头波     多空力量对比            │
    └─────────────────────────────────────────────────────────────────┘

    信号旗帜：
      🔴 旗舰主线：强弱度%>2.5 且 内部上涨比>0.85（机构主动拉板）
      ⭐ 量能异动：换手Z>6（资金极度活跃，次日延续概率高）
      📈 趋势延续：3日涨幅%>1（中期趋势中的今日强势，可信度高）
      🎯 形态共振：短期形态≥12 且 中期形态≥6（短中期技术面同向）
      ⚡ 攻守强势：攻守比>2（多头攻击力是空头防守力的2倍以上）
    """
    if concept_raw_df is None or concept_raw_df.empty:
        return pd.DataFrame()

    df = concept_raw_df.copy()
    df = df[df['涨停数'].fillna(0) >= 2].copy()
    if df.empty:
        return df

    def norm(s):
        mn, mx = s.min(), s.max()
        return ((s - mn) / (mx - mn) * 100).clip(0, 100) if mx > mn else pd.Series(50.0, index=s.index)

    def fill(col, default=0.0):
        return df[col].fillna(default) if col in df.columns else pd.Series(default, index=df.index)

    # ── A类：主线判定 ──────────────────────────────────────
    s_A1 = norm(fill('涨停数'))             # 涨停宽度
    s_A2 = norm(fill('强弱度%'))            # 相对强度
    s_A3 = norm(fill('涨幅%'))              # 今日涨幅

    # ── B类：资金活跃 ──────────────────────────────────────
    s_B1 = norm(fill('换手Z'))              # 换手Z活跃度
    s_B2 = norm(fill('内部上涨比', 0.5))    # 内部涨跌宽幅

    # ── C类：趋势动量 ──────────────────────────────────────
    s_C1 = norm(fill('3日涨幅%'))           # 3日趋势动量

    # ── D类：技术形态共振（仅新格式文件含此列）────────────
    if '形态综合分_raw' in df.columns:
        s_D1 = norm(fill('形态综合分_raw'))
    elif '短期形态' in df.columns:
        # 临时计算（列存在但未预处理）
        raw = fill('短期形态') * 0.45 + fill('中期形态') * 0.35 + fill('长期形态') * 0.20
        s_D1 = norm(raw)
    else:
        s_D1 = pd.Series(50.0, index=df.index)  # 旧文件无此列，中性处理

    # ── E类：攻守强度（仅新格式文件含此列）──────────────
    if '攻守比' in df.columns:
        s_E1 = norm(fill('攻守比'))
    elif '攻击波%' in df.columns and '回头波%' in df.columns:
        ratio = (fill('攻击波%') / (fill('回头波%').abs() + 0.1)).clip(0, 10)
        df['攻守比'] = ratio
        s_E1 = norm(ratio)
    else:
        s_E1 = pd.Series(50.0, index=df.index)

    # ── 综合得分 ────────────────────────────────────────────
    df['综合得分'] = (
        s_A1 * 0.20 +
        s_A2 * 0.15 +
        s_A3 * 0.13 +
        s_B1 * 0.12 +
        s_B2 * 0.08 +
        s_C1 * 0.08 +
        s_D1 * 0.12 +
        s_E1 * 0.12
    )

    # ── 信号旗帜 ────────────────────────────────────────────
    df['是否旗舰主线'] = (fill('强弱度%')   > 2.5) & (fill('内部上涨比', 0.5) > 0.85)
    df['是否量能异动'] =  fill('换手Z')     > 6.0
    df['是否趋势延续'] =  fill('3日涨幅%')  > 1.0
    if '短期形态' in df.columns:
        df['是否形态共振'] = (fill('短期形态') >= 12) & (fill('中期形态') >= 6)
    else:
        df['是否形态共振'] = False
    if '攻守比' in df.columns:
        df['是否攻守强势'] = fill('攻守比') > 2.0
    else:
        df['是否攻守强势'] = False

    # 信号数量汇总（多旗帜同时亮起 = 高确定性板块）
    flag_cols = ['是否旗舰主线', '是否量能异动', '是否趋势延续', '是否形态共振', '是否攻守强势']
    df['信号数'] = df[flag_cols].sum(axis=1).astype(int)

    df = df.sort_values(['综合得分', '涨停数'], ascending=False).reset_index(drop=True)
    return df


def identify_super_themes(concept_scored_df):
    """
    超级主题聚类：将各自独立的概念板块归并为超级主题，识别A股日内最强主题集群。

    背景：A股热点往往以"主题集群"形式出现。3/18日的 算力租赁+数据中心+液冷服务器+CPO
    +东数西算+云计算+存储芯片 其实都是"AI算力基础设施"主题的不同切面，单独看任何一个
    都不如将它们聚合后看主题总强度。聚合后可判断是否形成超级主线、是否值得重仓参与。

    输出：每个超级主题的加总涨停数、平均综合得分、命中子概念列表
    """
    if concept_scored_df is None or concept_scored_df.empty:
        return pd.DataFrame()

    results = []
    matched_concepts = set()

    for theme_name, keywords in SUPER_THEMES.items():
        theme_rows = concept_scored_df[concept_scored_df['名称'].isin(keywords)]
        if theme_rows.empty:
            continue
        total_zt = theme_rows['涨停数'].sum()
        avg_score = theme_rows['综合得分'].mean()
        max_rel_str = theme_rows['强弱度%'].max() if '强弱度%' in theme_rows.columns else 0
        flagship = theme_rows[theme_rows['是否旗舰主线']]['名称'].tolist() if '是否旗舰主线' in theme_rows.columns else []
        matched = theme_rows['名称'].tolist()
        matched_concepts.update(matched)
        results.append({
            '超级主题':     theme_name,
            '子概念数':     len(theme_rows),
            '合计涨停数':   int(total_zt),
            '平均综合得分': round(avg_score, 1),
            '最强相对强度': round(float(max_rel_str), 2),
            '旗舰子概念':   '、'.join(flagship) if flagship else '--',
            '命中子概念':   '、'.join(matched),
        })

    if not results:
        return pd.DataFrame()

    theme_df = pd.DataFrame(results)
    theme_df['主题热度指数'] = theme_df['合计涨停数'] * theme_df['平均综合得分'] / 100
    theme_df = theme_df.sort_values('主题热度指数', ascending=False).reset_index(drop=True)
    return theme_df


# ══════════════════════════════════════════════════
# 3. 市场情绪分析
# ══════════════════════════════════════════════════

def analyze_market_sentiment(df, zt_pool, zb_pool):
    """量化市场情绪，输出情绪结论和仓位建议"""
    total = len(df)
    up_count = int((df['涨幅%'] > 0).sum())
    down_count = int((df['涨幅%'] < 0).sum())
    flat_count = int((df['涨幅%'] == 0).sum())

    # 涨停/炸板计数（优先用akshare数据，否则用本地估算）
    if not zt_pool.empty:
        zt_count = len(zt_pool)
        # 统计炸板次数>0的算炸板过
        zb_count_pool = int((zt_pool['炸板次数'] > 0).sum())
    else:
        zt_count = int(df['是否涨停'].sum())
        zb_count_pool = 0

    if not zb_pool.empty:
        zb_count = len(zb_pool)
    else:
        zb_count = int(df['是否炸板'].sum())

    total_zt_zb = zt_count + zb_count
    zb_rate = zb_count / total_zt_zb if total_zt_zb > 0 else 0.0

    # 情绪基础判断
    if up_count > 2500:
        base_sentiment = "强"
    elif up_count < 1800:
        base_sentiment = "弱"
    else:
        base_sentiment = "混沌"

    # 炸板率修正（>30%加重负面评价）
    sentiment = base_sentiment
    if zb_rate > 0.30 and sentiment == "强":
        sentiment = "混沌"
        sentiment_note = f"炸板率{zb_rate:.1%}>30%，情绪从'强'降级为'混沌'"
    elif zb_rate > 0.30 and sentiment == "混沌":
        sentiment_note = f"炸板率{zb_rate:.1%}>30%，情绪偏弱，谨慎操作"
    else:
        sentiment_note = ""

    # 仓位建议
    position_map = {"强": "7成", "混沌": "5成", "弱": "1成（建议空仓）"}
    position = position_map[sentiment]

    return {
        "total": total,
        "up_count": up_count,
        "down_count": down_count,
        "flat_count": flat_count,
        "zt_count": zt_count,
        "zb_count": zb_count,
        "zb_rate": zb_rate,
        "base_sentiment": base_sentiment,
        "sentiment": sentiment,
        "sentiment_note": sentiment_note,
        "position": position,
    }


# ══════════════════════════════════════════════════
# 4. 主线板块识别
# ══════════════════════════════════════════════════

def analyze_main_sectors(zt_pool, zb_pool, local_df):
    """识别主线板块——6维度量化综合评分

    评分维度（满分100分）：
      - 涨停宽度 (25%)：板块涨停数量，反映市场参与广度
      - 连板深度 (25%)：最高连板数，代理趋势持续强度
      - 梯队厚度 (15%)：2连板以上数量，反映主升浪阶段
      - 封板时间 (15%)：最早封板时间越早得分越高（资金前瞻性）
      - 量能活跃 (10%)：板块平均量比
      - 主力认可 (10%)：板块平均主力净流入比
    """
    if zt_pool.empty:
        # 降级：用本地数据的细分行业
        zt_local = local_df[local_df['是否涨停']].copy()
        sector_stats = zt_local.groupby('细分行业').agg(
            涨停数量=('名称', 'count'),
            总成交额=('总金额', 'sum'),
        ).sort_values(['涨停数量', '总成交额'], ascending=False).reset_index()
        sector_stats.rename(columns={'细分行业': '行业'}, inplace=True)
        sector_stats['连板最高'] = 1
        sector_stats['多连板数'] = 0
        sector_stats['最早封板'] = '--'
        sector_stats['综合得分'] = (sector_stats['涨停数量'] * 10).clip(0, 100)
        return sector_stats

    # 合并本地量能与资金流数据
    local_sub = local_df[['代码', '量比', '主力净比%']].copy()
    zt_enriched = zt_pool.merge(local_sub, on='代码', how='left')

    # 封板时间字符串转数字（格式如 093000）
    zt_enriched['首次封板时间_int'] = pd.to_numeric(
        zt_enriched['首次封板时间'].astype(str).str.replace(':', ''), errors='coerce'
    ).fillna(150000)

    sector_stats = zt_enriched.groupby('所属行业').agg(
        涨停数量=('名称', 'count'),
        总成交额=('成交额', 'sum'),
        连板最高=('连板数', 'max'),
        多连板数=('连板数', lambda x: (x >= 2).sum()),
        最早封板=('首次封板时间', 'min'),
        最早封板_int=('首次封板时间_int', 'min'),
        平均量比=('量比', lambda x: x.fillna(1.0).mean()),
        平均净流入=('主力净比%', lambda x: x.fillna(0.0).mean()),
    ).reset_index()
    sector_stats.rename(columns={'所属行业': '行业'}, inplace=True)

    # ── 各维度归一化 (0→100) ─────────────────────────────
    def norm(s):
        mn, mx = s.min(), s.max()
        return (s - mn) / (mx - mn) * 100 if mx > mn else pd.Series(50.0, index=s.index)

    # 封板时间得分：09:30→100分，15:00→0分
    t = sector_stats['最早封板_int']
    sector_stats['封板时间得分'] = ((150000 - t) / (150000 - 93000) * 100).clip(0, 100)

    sector_stats['综合得分'] = (
        norm(sector_stats['涨停数量'])            * 0.25 +
        norm(sector_stats['连板最高'])            * 0.25 +
        norm(sector_stats['多连板数'])            * 0.15 +
        sector_stats['封板时间得分']              * 0.15 +
        norm(sector_stats['平均量比'])            * 0.10 +
        norm(sector_stats['平均净流入'])          * 0.10
    )

    sector_stats = sector_stats.sort_values(
        ['综合得分', '总成交额'], ascending=False
    ).reset_index(drop=True)

    return sector_stats


def get_sector_leaders(zt_pool, sector, local_df):
    """在指定板块内找出人气核心（涨停最早、成交额最大的前3只）"""
    if zt_pool.empty:
        return pd.DataFrame()

    sector_df = zt_pool[zt_pool['所属行业'] == sector].copy()
    if sector_df.empty:
        return sector_df

    # 融合本地数据（获取量比、换手率、主力净比 等）
    local_sub = local_df[['代码', '量比', '换手%', '主力净比%', '开盘抢筹%', '委比%']].copy()
    sector_df = sector_df.merge(local_sub, on='代码', how='left')

    # 人气核心排序规则：连板数 > 首次封板时间最早 > 成交额
    sector_df['首次封板时间_int'] = pd.to_numeric(
        sector_df['首次封板时间'].astype(str).str.replace(':', ''), errors='coerce'
    )
    sector_df = sector_df.sort_values(
        ['连板数', '首次封板时间_int', '成交额'],
        ascending=[False, True, False]
    )
    return sector_df


# ══════════════════════════════════════════════════
# 5. 板块持续性分析（量化打分）
# ══════════════════════════════════════════════════

def evaluate_sector_continuity(sector_name, sector_df, market_up_count):
    """
    板块持续性量化评估，输出高/中/低结论
    评分维度：
      - 梯队完整性（连板梯队宽度）
      - 与指数共振（大盘是否上涨）
      - 中军表现（主力成交额、量比）
    """
    score = 0
    reasons = []

    # 梯队完整性
    if sector_df.empty:
        return "低", []

    max_lianban = sector_df['连板数'].max()
    first_board_count = len(sector_df[sector_df['连板数'] == 1])
    two_plus_count = len(sector_df[sector_df['连板数'] >= 2])

    if max_lianban >= 3:
        score += 3
        reasons.append(f"最高{max_lianban}连板，梯队极佳")
    elif max_lianban == 2:
        score += 2
        reasons.append(f"最高{max_lianban}连板，梯队良好")
    else:
        score += 1
        reasons.append("全部首板，梯队一般")

    if first_board_count >= 3:
        score += 1
        reasons.append(f"首板宽度{first_board_count}只，跟风效应强")

    # 与指数共振
    if market_up_count > 2500:
        score += 2
        reasons.append("大盘上涨，与指数正向共振")
    elif market_up_count > 1800:
        score += 1
        reasons.append("大盘震荡，板块独立走强")
    else:
        reasons.append("大盘下跌，逆势板块")

    # 中军表现（成交额前3名）
    top3 = sector_df.head(3)
    avg_vol_ratio = top3['量比'].mean() if '量比' in top3.columns else 0
    total_amt = sector_df['成交额'].sum() / 1e8  # 亿元

    if avg_vol_ratio >= 3:
        score += 2
        reasons.append(f"中军平均量比{avg_vol_ratio:.1f}，资金活跃")
    elif avg_vol_ratio >= 1.5:
        score += 1
        reasons.append(f"中军平均量比{avg_vol_ratio:.1f}，资金尚可")

    if total_amt >= 50:
        score += 1
        reasons.append(f"板块总成交{total_amt:.1f}亿，资金参与度高")

    # 综合结论
    if score >= 6:
        conclusion = "高"
    elif score >= 4:
        conclusion = "中"
    else:
        conclusion = "低"

    return conclusion, reasons


# ══════════════════════════════════════════════════
# 6. 量化机会评分体系（次日观察池）
# ══════════════════════════════════════════════════

def build_opportunity_pool(local_df, zt_pool, strong_pool):
    """
    量化机会评分体系
    评分维度（0-100分）：
      1. 资金面得分 (30%)：主力净比%
      2. 动量得分 (25%)：量比
      3. 活跃度得分 (20%)：换手%
      4. 开盘质量得分 (15%)：开盘抢筹%
      5. 筹码面得分 (10%)：委比%
    筛选条件：涨幅 > 5%，且不在炸板股中
    """
    # 合并涨停池连板信息
    candidates = local_df[local_df['涨幅%'] > 5].copy()

    if not zt_pool.empty:
        zt_info = zt_pool[['代码', '连板数', '首次封板时间', '所属行业']].copy()
        zt_info.columns = ['代码', '连板数_ak', '首次封板时间', '所属行业_ak']
        candidates = candidates.merge(zt_info, on='代码', how='left')
    else:
        candidates['连板数_ak'] = 0
        candidates['首次封板时间'] = '--'
        candidates['所属行业_ak'] = candidates.get('细分行业', '--')

    # 连板奖励
    candidates['连板数_ak'] = candidates['连板数_ak'].fillna(0).astype(int)

    # 归一化函数
    def norm(series):
        mn, mx = series.min(), series.max()
        if mx == mn:
            return pd.Series(50.0, index=series.index)
        return ((series - mn) / (mx - mn) * 100).clip(0, 100)

    fill_zero = lambda col: candidates[col].fillna(0) if col in candidates.columns else pd.Series(0, index=candidates.index)

    s_capital    = norm(fill_zero('主力净比%'))   # 资金流向
    s_momentum   = norm(fill_zero('量比'))          # 动量
    s_activity   = norm(fill_zero('换手%'))         # 活跃
    s_open_qual  = norm(fill_zero('开盘抢筹%'))     # 开盘质量
    s_order_book = norm(fill_zero('委比%'))          # 委买强度

    candidates['综合得分'] = (
        s_capital    * 0.30 +
        s_momentum   * 0.25 +
        s_activity   * 0.20 +
        s_open_qual  * 0.15 +
        s_order_book * 0.10
    )

    # 连板加分
    candidates['综合得分'] += candidates['连板数_ak'] * 3

    candidates = candidates.sort_values('综合得分', ascending=False)

    # 标注是否在强势股池
    if not strong_pool.empty:
        strong_codes = set(strong_pool['代码'].astype(str).str.zfill(6))
        candidates['是否强势股池'] = candidates['代码'].isin(strong_codes)
    else:
        candidates['是否强势股池'] = False

    return candidates


# ══════════════════════════════════════════════════
# 7. 核心观察池（次日重点关注）
# ══════════════════════════════════════════════════

def build_observation_pool(zt_pool, sector_stats, opportunity_df, local_df, top_k=8):
    """
    构建核心观察池：
    - 主线板块内，连板最高→封板最早→成交额最大的前2只（人气龙头候选）
    - 主线板块内，成交额前3的中军
    - 量化综合得分前5的强势股
    """
    pool = {}

    # 主线板块（取前2个板块）
    top_sectors = sector_stats['行业'].head(2).tolist()

    if not zt_pool.empty:
        for sector in top_sectors:
            # 调用 get_sector_leaders 获取经本地数据增强、多维度排序的结果
            sector_enriched = get_sector_leaders(zt_pool, sector, local_df)
            if sector_enriched.empty:
                continue
            # 龙头候选：按连板数↓ → 首次封板时间↑ → 成交额↓（已在 get_sector_leaders 排好序）
            pool[f"{sector}_龙头候选"] = sector_enriched.head(2)[
                ['代码', '名称', '首次封板时间', '连板数', '成交额', '所属行业']
            ].values.tolist()
            # 中军候选：按成交额倒序取前3
            mid_army = sector_enriched.sort_values('成交额', ascending=False)
            pool[f"{sector}_中军候选"] = mid_army.head(3)[
                ['代码', '名称', '首次封板时间', '连板数', '成交额', '所属行业']
            ].values.tolist()

    # 量化高分股
    top_scores = opportunity_df.head(top_k)[['代码', '名称', '涨幅%', '量比', '换手%', '主力净比%', '综合得分', '是否强势股池', '所属行业_ak']].copy()
    pool['量化高分股'] = top_scores.fillna('--').values.tolist()
    pool['量化高分股_cols'] = list(top_scores.columns)

    return pool


# ══════════════════════════════════════════════════
# 8. 报告生成
# ══════════════════════════════════════════════════

def format_amt(val):
    """格式化金额为亿元"""
    try:
        return f"{float(val)/1e8:.2f}亿"
    except:
        return "--"


def generate_report(target_date, sentiment_data, sector_stats, zt_pool, zb_pool,
                    strong_pool, opportunity_df, obs_pool, local_df,
                    concept_board_df=None, super_theme_df=None):

    date_str = f"{target_date[:4]}/{target_date[4:6]}/{target_date[6:]}"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    s = sentiment_data

    lines = []
    lines.append(f"# A股复盘报告 {date_str}")
    lines.append(f"*生成时间：{now_str}*")
    lines.append("")
    lines.append("---")

    # ─── 一、市场情绪判断 ────────────────────────────────
    lines.append("## 一、市场情绪判断")
    lines.append("")
    lines.append("### 📊 数据统计")
    lines.append("")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 全市场股票总数（非ST） | {s['total']} 家 |")
    lines.append(f"| 上涨家数 | **{s['up_count']}** 家 |")
    lines.append(f"| 下跌家数 | {s['down_count']} 家 |")
    lines.append(f"| 平盘家数 | {s['flat_count']} 家 |")
    lines.append(f"| 涨停家数（非ST） | **{s['zt_count']}** 家 |")
    lines.append(f"| 炸板家数 | {s['zb_count']} 家 |")
    lines.append(f"| 炸板率 | **{s['zb_rate']:.1%}** |")
    lines.append("")

    # 情绪图示
    sentiment_emoji = {"强": "🟢 强", "混沌": "🟡 混沌", "弱": "🔴 弱"}
    lines.append(f"### ✅ 情绪结论：{sentiment_emoji.get(s['sentiment'], s['sentiment'])}")
    lines.append("")
    lines.append("**判断依据：**")
    lines.append(f"- 上涨家数 **{s['up_count']}**：")
    if s['up_count'] > 2500:
        lines.append(f"  - > 2500，基础判断为 **强**")
    elif s['up_count'] < 1800:
        lines.append(f"  - < 1800，基础判断为 **弱**")
    else:
        lines.append(f"  - 介于 1800-2500 之间，基础判断为 **混沌**")
    if s['sentiment_note']:
        lines.append(f"- ⚠️ {s['sentiment_note']}")
    lines.append(f"- 炸板率 {s['zb_rate']:.1%}（>30% 为偏负面信号）")
    lines.append("")
    lines.append(f"**➡ 对应仓位上限：{s['position']}**")
    lines.append("")

    # ─── 二、主线板块识别 ─────────────────────────────────
    lines.append("---")
    lines.append("## 二、主线板块识别")
    lines.append("")
    lines.append("### 涨停板块分布（6维度综合评分排序）")
    lines.append("")
    lines.append("| 排名 | 行业/板块 | 涨停数 | 2+连板 | 连板最高 | 最早封板 | 总成交额 | 综合得分 |")
    lines.append("|------|----------|--------|--------|---------|---------|---------|---------|")
    for i, row in sector_stats.head(10).iterrows():
        amt_str = format_amt(row['总成交额'])
        lianban_str = f"{int(row['连板最高'])}连板" if row['连板最高'] > 1 else "首板"
        multi_lb = int(row.get('多连板数', 0))
        score_str = f"{row.get('综合得分', 0):.1f}"
        lines.append(
            f"| {i+1} | **{row['行业']}** | {row['涨停数量']} 只 | "
            f"{multi_lb} 只 | {lianban_str} | {row['最早封板']} | {amt_str} | {score_str} |"
        )
    lines.append("")
    lines.append("**6维评分说明：** 涨停宽度(25%) + 连板深度(25%) + 梯队厚度(15%) + 封板早晚(15%) + 量能(10%) + 主力净流入(10%)")
    lines.append("")

    # 主线识别结论
    top2 = sector_stats.head(2)['行业'].tolist()
    lines.append(f"### 🎯 主线板块识别结论（不超过2个）")
    lines.append("")
    for idx, sector in enumerate(top2):
        row = sector_stats[sector_stats['行业'] == sector].iloc[0]
        lines.append(f"**主线{idx+1}：{sector}**")
        lines.append(f"- 涨停只数：{row['涨停数量']}")
        lines.append(f"- 连板最高：{int(row['连板最高'])} 板")
        lines.append(f"- 最早封板：{row['最早封板']}")
        lines.append(f"- 总成交额：{format_amt(row['总成交额'])}")
        lines.append("")

    # ─── 二-B、概念板块多维度分析 ──────────────────────────
    if concept_board_df is not None and not concept_board_df.empty:
        lines.append("---")
        lines.append("## 二-B、概念板块8维度量化分析")
        lines.append("")
        lines.append("> **CSRC行业分类盲区：** 算力租赁/数据中心/液冷服务器/CPO被分散到计算机/电子/通信三个")
        lines.append("> 行业，单看行业排名严重低估主题热度。概念板块视角通过8个维度还原资金参与的真实质量。")
        lines.append("")

        # 快速预警：高信号数量板块（≥3个旗帜同时亮起）
        high_conf = concept_board_df[concept_board_df.get('信号数', 0) >= 3] if '信号数' in concept_board_df.columns else pd.DataFrame()
        if not high_conf.empty:
            hc_names = '、'.join([f"**{r['名称']}**({int(r.get('信号数',0))}🚦)" for _, r in high_conf.head(5).iterrows()])
            lines.append(f"🚨 **高确定性板块（≥3信号同时亮起）：** {hc_names}")
            lines.append("")

        # 旗舰主线预警
        flagship_df = concept_board_df[concept_board_df.get('是否旗舰主线', False) == True] if '是否旗舰主线' in concept_board_df.columns else pd.DataFrame()
        if not flagship_df.empty:
            fl_names = '、'.join([f"**{r['名称']}**(强弱{r.get('强弱度%',0):.1f}%,{int(r['涨停数'])}停)" for _, r in flagship_df.head(5).iterrows()])
            lines.append(f"🔴 **旗舰主线（强弱度>2.5且宽度>85%）：** {fl_names}")
            lines.append("")

        # 量能异动预警
        vol_df = concept_board_df[concept_board_df.get('是否量能异动', False) == True] if '是否量能异动' in concept_board_df.columns else pd.DataFrame()
        if not vol_df.empty:
            vl_names = '、'.join([f"**{r['名称']}**(换手Z={r.get('换手Z',0):.1f})" for _, r in vol_df.head(5).iterrows()])
            lines.append(f"⭐ **量能异动（换手Z>6）：** {vl_names}")
            lines.append("")

        # 形态共振预警（新列）
        form_df = concept_board_df[concept_board_df.get('是否形态共振', False) == True] if '是否形态共振' in concept_board_df.columns else pd.DataFrame()
        if not form_df.empty:
            fm_names = '、'.join([f"**{r['名称']}**(短{int(r.get('短期形态',0))}/中{int(r.get('中期形态',0))})" for _, r in form_df.head(5).iterrows()])
            lines.append(f"🎯 **形态共振（短期≥12且中期≥6）：** {fm_names}")
            lines.append("")

        # 攻守强势预警（新列）
        atk_df = concept_board_df[concept_board_df.get('是否攻守强势', False) == True] if '是否攻守强势' in concept_board_df.columns else pd.DataFrame()
        if not atk_df.empty:
            ak_names = '、'.join([f"**{r['名称']}**(攻守{r.get('攻守比',0):.1f}x)" for _, r in atk_df.head(5).iterrows()])
            lines.append(f"⚡ **攻守强势（攻守比>2x）：** {ak_names}")
            lines.append("")

        # ─ 主表：8维度综合评分 TOP 20
        # 根据是否含新列决定表格宽度
        has_new_cols = '短期形态' in concept_board_df.columns and '攻守比' in concept_board_df.columns

        lines.append("### 📊 概念板块8维综合评分 TOP 20（涨停数≥2）")
        lines.append("")
        if has_new_cols:
            lines.append("| # | 板块 | 综合分 | 信号 | 涨停数 | 涨幅% | 强弱度% | 换手Z | 内涨比 | 3日% | 形态分 | 攻守比 |")
            lines.append("|---|------|--------|------|--------|------|---------|------|--------|------|--------|--------|")
        else:
            lines.append("| # | 板块 | 综合分 | 信号 | 涨停数 | 涨幅% | 强弱度% | 换手Z | 内涨比 | 3日% | 连涨天 |")
            lines.append("|---|------|--------|------|--------|------|---------|------|--------|------|--------|")

        for i, crow in concept_board_df.head(20).iterrows():
            flags = []
            if crow.get('是否旗舰主线', False): flags.append("🔴")
            if crow.get('是否量能异动', False): flags.append("⭐")
            if crow.get('是否趋势延续', False): flags.append("📈")
            if crow.get('是否形态共振', False): flags.append("🎯")
            if crow.get('是否攻守强势', False): flags.append("⚡")
            flag_str = ''.join(flags) if flags else "—"
            inner_pct = f"{crow.get('内部上涨比', 0)*100:.0f}%" if '内部上涨比' in crow.index else "--"

            if has_new_cols:
                form_raw = crow.get('形态综合分_raw', crow.get('短期形态', 0) * 0.45 + crow.get('中期形态', 0) * 0.35)
                lines.append(
                    f"| {i+1} | **{crow['名称']}** | {crow['综合得分']:.1f} | {flag_str} | "
                    f"{int(crow['涨停数'])} | {crow.get('涨幅%', 0):.2f}% | "
                    f"{crow.get('强弱度%', 0):.2f}% | {crow.get('换手Z', 0):.1f} | "
                    f"{inner_pct} | {crow.get('3日涨幅%', 0):.2f}% | "
                    f"{form_raw:.1f} | {crow.get('攻守比', 0):.1f}x |"
                )
            else:
                lines.append(
                    f"| {i+1} | **{crow['名称']}** | {crow['综合得分']:.1f} | {flag_str} | "
                    f"{int(crow['涨停数'])} | {crow.get('涨幅%', 0):.2f}% | "
                    f"{crow.get('强弱度%', 0):.2f}% | {crow.get('换手Z', 0):.1f} | "
                    f"{inner_pct} | {crow.get('3日涨幅%', 0):.2f}% | {int(crow.get('连涨天', 0))} |"
                )
        lines.append("")
        lines.append("**8维评分权重：** A1涨停宽度(20%) A2相对强度(15%) A3今日涨幅(13%) B1换手Z(12%) B2内部宽幅(8%) C1趋势动量(8%) D1形态共振(12%) E1攻守强度(12%)")
        lines.append("")
        lines.append("<details><summary>📖 信号旗帜含义</summary>")
        lines.append("")
        lines.append("| 旗帜 | 触发条件 | 策略含义 |")
        lines.append("|------|---------|---------|")
        lines.append("| 🔴旗舰 | 强弱度>2.5 且 内部上涨比>85% | 机构主动拉升，非散户自发联动 |")
        lines.append("| ⭐量异 | 换手Z>6 | 资金极度异常涌入，次日延续概率高 |")
        lines.append("| 📈延续 | 3日涨幅>1% | 中期趋势中的今日强势，排除单日骗局 |")
        lines.append("| 🎯形态 | 短期≥12且中期≥6 | 短中期技术面同向，多头趋势完整 |")
        lines.append("| ⚡攻守 | 攻守比>2x | 今日多头攻击力是空头的2倍，K线结构强 |")
        lines.append("")
        lines.append("</details>")
        lines.append("")

        # ─ 超级主题集群
        if super_theme_df is not None and not super_theme_df.empty:
            lines.append("### 🧩 超级主题集群（多概念合并，解决主题分散问题）")
            lines.append("")
            lines.append("| 超级主题 | 子概念数 | 合计涨停 | 均综合分 | 热度指数 | 最强相对强度 | 旗舰子概念 |")
            lines.append("|---------|---------|---------|---------|---------|-----------|---------|")
            for _, tr in super_theme_df.head(6).iterrows():
                lines.append(
                    f"| **{tr['超级主题']}** | {tr['子概念数']} | {tr['合计涨停数']} | "
                    f"{tr['平均综合得分']} | **{tr.get('主题热度指数', 0):.1f}** | "
                    f"{tr['最强相对强度']:.2f}% | {tr['旗舰子概念']} |"
                )
            lines.append("")
            top_theme = super_theme_df.iloc[0]
            strength = '🔥 超强（>30涨停）' if top_theme['合计涨停数'] > 30 else ('💪 强（10-30）' if top_theme['合计涨停数'] > 10 else '📌 中等（<10）')
            lines.append(f"**✅ 最强主题集群：{top_theme['超级主题']}** — {strength}")
            lines.append(f"- 命中子概念：*{top_theme['命中子概念']}*")
            lines.append("")

    # ─── 三、各主线板块持续性分析 ──────────────────────────
    lines.append("---")
    lines.append("## 三、主线板块持续性分析")
    lines.append("")

    for sector in top2:
        sector_df_full = zt_pool[zt_pool['所属行业'] == sector].copy() if not zt_pool.empty else pd.DataFrame()
        # 补充本地数据
        if not sector_df_full.empty:
            local_sub = local_df[['代码','量比','换手%','主力净比%']].copy()
            sector_df_full = sector_df_full.merge(local_sub, on='代码', how='left')

        continuity, reasons = evaluate_sector_continuity(sector, sector_df_full, s['up_count'])

        lines.append(f"### 🔍 {sector}")
        lines.append("")

        # 梯队展示
        if not sector_df_full.empty:
            # 连板梯队
            lb_counts = sector_df_full['连板数'].value_counts().sort_index(ascending=False)
            梯队_str = "、".join([f"{n}板{cnt}只" for n, cnt in lb_counts.items()])
            lines.append(f"**涨停梯队：** {梯队_str}")

            # 成交额排名前3（中军）
            top3 = sector_df_full.sort_values('成交额', ascending=False).head(3)
            mid_army_str = "、".join([f"{r['名称']}({format_amt(r['成交额'])})" for _, r in top3.iterrows()])
            lines.append(f"**中军（成交额前3）：** {mid_army_str}")

            # 最高连板股
            top_lianban = sector_df_full.loc[sector_df_full['连板数'].idxmax()]
            lines.append(f"**领头连板股：** {top_lianban['名称']} ({int(top_lianban['连板数'])}连板，首次封板{top_lianban['首次封板时间']})")
        lines.append("")

        lines.append(f"**与指数共振：** 大盘上涨 {s['up_count']} 只，{'与指数正向共振 ✅' if s['up_count'] > 2000 else '板块走强幅度>大盘'}")
        lines.append("")
        lines.append(f"**持续性预判：** {'⭐⭐⭐ 高' if continuity == '高' else ('⭐⭐ 中' if continuity == '中' else '⭐ 低')}")
        lines.append("")
        lines.append("**评判理由：**")
        for r in reasons:
            lines.append(f"- {r}")
        lines.append("")

    # ─── 四、连板梯队总览 ─────────────────────────────────
    lines.append("---")
    lines.append("## 四、连板梯队总览")
    lines.append("")
    if not zt_pool.empty:
        lianban_stocks = zt_pool[zt_pool['连板数'] >= 2].sort_values('连板数', ascending=False)
        if not lianban_stocks.empty:
            lines.append("| 代码 | 名称 | 连板数 | 首次封板时间 | 炸板次数 | 成交额 | 所属行业 |")
            lines.append("|------|------|--------|------------|---------|-------|---------|")
            for _, r in lianban_stocks.iterrows():
                lines.append(
                    f"| {r['代码']} | **{r['名称']}** | **{int(r['连板数'])}连板** | "
                    f"{r['首次封板时间']} | {r['炸板次数']} | {format_amt(r['成交额'])} | {r['所属行业']} |"
                )
        else:
            lines.append("*今日无2连板及以上股票。*")
    else:
        lines.append("*（数据不可用）*")
    lines.append("")

    # ─── 五、核心观察池 ────────────────────────────────────
    lines.append("---")
    lines.append("## 五、核心观察池（次日重点关注）")
    lines.append("")

    for sector in top2:
        leader_key = f"{sector}_龙头候选"
        mid_key = f"{sector}_中军候选"

        lines.append(f"### {sector} 板块")
        lines.append("")
        lines.append("**🥇 人气龙头候选（涨停最早 + 连板）：**")
        lines.append("")
        lines.append("| 代码 | 名称 | 首次封板 | 连板数 | 成交额 | 所属行业 |")
        lines.append("|------|------|---------|--------|-------|---------|")
        leader_list = obs_pool.get(leader_key, [])
        if leader_list:
            for r in leader_list:
                lines.append(f"| {r[0]} | **{r[1]}** | {r[2]} | {r[3]}板 | {format_amt(r[4])} | {r[5]} |")
        else:
            lines.append("| -- | -- | -- | -- | -- | -- |")
        lines.append("")
        lines.append("**💰 中军候选（成交额前3）：**")
        lines.append("")
        lines.append("| 代码 | 名称 | 首次封板 | 连板数 | 成交额 | 所属行业 |")
        lines.append("|------|------|---------|--------|-------|---------|")
        mid_list = obs_pool.get(mid_key, [])
        if mid_list:
            for r in mid_list:
                lines.append(f"| {r[0]} | **{r[1]}** | {r[2]} | {r[3]}板 | {format_amt(r[4])} | {r[5]} |")
        else:
            lines.append("| -- | -- | -- | -- | -- | -- |")
        lines.append("")

    # ─── 六、量化机会评分体系 ─────────────────────────────
    lines.append("---")
    lines.append("## 六、量化机会评分体系")
    lines.append("")
    lines.append("**评分维度说明：**")
    lines.append("")
    lines.append("| 维度 | 权重 | 指标 | 含义 |")
    lines.append("|------|------|------|------|")
    lines.append("| 资金面 | 30% | 主力净比% | 主力资金净流入/总成交额，越高表示主力越积极建仓 |")
    lines.append("| 动量 | 25% | 量比 | 当日成交量/近期均量，>3为量能放大 |")
    lines.append("| 活跃度 | 20% | 换手% | 流通盘换手率，反映市场参与活跃程度 |")
    lines.append("| 开盘质量 | 15% | 开盘抢筹% | 竞价阶段相对前日成交占比，>0表示开盘活跃抢筹 |")
    lines.append("| 筹码面 | 10% | 委比% | (委买-委卖)/(委买+委卖)，正值表示买方更强 |")
    lines.append("")
    lines.append("**💡 连板股额外加分：每多1连板 +3分**")
    lines.append("")

    # 综合评分排行榜
    cols_show = ['代码', '名称', '涨幅%', '量比', '换手%', '主力净比%', '综合得分', '所属行业_ak']
    cols_show = [c for c in cols_show if c in opportunity_df.columns]
    top_opportunity = opportunity_df.head(15)[cols_show].copy()

    lines.append("### 🏆 综合得分 TOP 15（涨幅>5%标的）")
    lines.append("")
    lines.append("| 排名 | 代码 | 名称 | 涨幅% | 量比 | 换手% | 主力净比% | 综合得分 | 板块 | 强势股池 |")
    lines.append("|------|------|------|------|------|------|---------|---------|------|------|")
    for rank, (_, row) in enumerate(opportunity_df.head(15).iterrows(), 1):
        in_strong = "✅" if row.get('是否强势股池', False) else ""
        lines.append(
            f"| {rank} | {row['代码']} | **{row['名称']}** | "
            f"{row['涨幅%']:.2f}% | "
            f"{row.get('量比', 0):.2f} | "
            f"{row.get('换手%', 0):.2f}% | "
            f"{row.get('主力净比%', 0):.2f}% | "
            f"**{row['综合得分']:.1f}** | "
            f"{row.get('所属行业_ak', '--')} | {in_strong} |"
        )
    lines.append("")

    # ─── 七、炸板股分析 ───────────────────────────────────
    lines.append("---")
    lines.append("## 七、炸板股分析（风险提示）")
    lines.append("")
    if not zb_pool.empty:
        lines.append("*炸板股反映情绪不稳定，需关注后续变化：*")
        lines.append("")
        lines.append("| 代码 | 名称 | 涨跌幅 | 炸板次数 | 首次封板时间 | 成交额 | 所属行业 |")
        lines.append("|------|------|--------|---------|------------|-------|---------|")
        for _, r in zb_pool.iterrows():
            lines.append(
                f"| {r['代码']} | {r['名称']} | {r['涨跌幅']:.2f}% | "
                f"**{r['炸板次数']}次** | {r['首次封板时间']} | "
                f"{format_amt(r['成交额'])} | {r['所属行业']} |"
            )
    else:
        lines.append("*（炸板数据不可用）*")
    lines.append("")

    # ─── 八、次日交易计划要点 ─────────────────────────────
    lines.append("---")
    lines.append("## 八、次日交易计划要点")
    lines.append("")

    # 情绪与仓位
    lines.append(f"### 1. 情绪与仓位")
    lines.append(f"- **市场情绪：** {s['sentiment']}（{s['sentiment_note'] if s['sentiment_note'] else '无特殊修正'}）")
    lines.append(f"- **建议总仓位上限：** {s['position']}")
    lines.append(f"- **单股仓位上限：** 总仓位 × 30%")
    lines.append("")

    # 重点关注板块
    lines.append(f"### 2. 重点关注板块")
    for sector in top2:
        row = sector_stats[sector_stats['行业'] == sector].iloc[0]
        continuity, _ = evaluate_sector_continuity(
            sector,
            zt_pool[zt_pool['所属行业'] == sector] if not zt_pool.empty else pd.DataFrame(),
            s['up_count']
        )
        lines.append(f"- **{sector}**（持续性：{continuity}，涨停{row['涨停数量']}只，连板最高{int(row['连板最高'])}板）")
    lines.append("")

    # 重点观察个股
    lines.append(f"### 3. 重点观察个股（不超过3只）")
    if not zt_pool.empty:
        # 优先连板股中的领头羊
        top_lianban = zt_pool[zt_pool['所属行业'].isin(top2)].sort_values(['连板数','成交额'], ascending=False).head(3)
        if not top_lianban.empty:
            for _, r in top_lianban.iterrows():
                lines.append(
                    f"- **{r['名称']}**（{r['代码']}）- {r['所属行业']} | "
                    f"{int(r['连板数'])}连板 | 首封{r['首次封板时间']} | "
                    f"成交额{format_amt(r['成交额'])}"
                )
        else:
            # 主线板块中成交额最大的
            top_amt = zt_pool[zt_pool['所属行业'].isin(top2)].sort_values('成交额', ascending=False).head(3)
            for _, r in top_amt.iterrows():
                lines.append(f"- **{r['名称']}**（{r['代码']}）- {r['所属行业']} | 成交额{format_amt(r['成交额'])}")
    lines.append("")

    # 买入条件确认
    lines.append("### 4. 次日买点确认清单（盘中逐项核对）")
    lines.append("")
    lines.append("| # | 条件 | 核对要点 |")
    lines.append("|---|------|---------|")
    lines.append(f"| 1 | 大盘情绪 | 开盘后上涨家数是否持续扩张？是否出现单边下杀？ |")
    lines.append(f"| 2 | 主线持续 | {'/'.join(top2)} 板块整体是否集体走强而非迅速分化？ |")
    lines.append(f"| 3 | 竞价选股 | 目标股竞价成交额是否排板块前3？高开幅度是否在-2%~+3%？ |")
    lines.append(f"| 4 | 量比确认 | 开盘5分钟内量比是否稳定 **> 3** ？ |")
    lines.append(f"| 5 | 分时形态 | 股价是否在分时均线（黄线）上方获得支撑，且在均线±1%内？ |")
    lines.append(f"| 6 | 止损设定 | 买入前是否已明确设定 **-7%** 止损价？ |")
    lines.append("")

    lines.append("### 5. 风控关键提示")
    lines.append(f"- 大盘情绪若从'{s['sentiment']}'转为'弱'，立刻执行**空仓纪律**")
    lines.append(f"- 单股止损线：买入价 × 93%（即 -7%）")
    lines.append(f"- 动态止盈：股价自当日高点回撤 7% 时，卖出")
    lines.append(f"- 板块退潮信号：主线核心股中有2只触及跌停，立刻清仓")
    lines.append("")

    # ─── 九、量化指标解读 ─────────────────────────────────
    lines.append("---")
    lines.append("## 九、今日市场量化指标概览")
    lines.append("")

    # 全市场涨幅分布
    change_pct = local_df['涨幅%'].dropna()
    lines.append("### 涨幅分布")
    lines.append("")
    bins_labels = [
        ("<-5%", change_pct[change_pct < -5]),
        ("-5%~0%", change_pct[(change_pct >= -5) & (change_pct < 0)]),
        ("0%~2%", change_pct[(change_pct >= 0) & (change_pct < 2)]),
        ("2%~5%", change_pct[(change_pct >= 2) & (change_pct < 5)]),
        ("5%~9%", change_pct[(change_pct >= 5) & (change_pct < 9)]),
        ("≥9.9%(涨停区)", change_pct[change_pct >= 9.9]),
    ]
    lines.append("| 区间 | 家数 | 占比 |")
    lines.append("|------|------|------|")
    for label, subset in bins_labels:
        pct = len(subset) / len(change_pct) * 100
        lines.append(f"| {label} | {len(subset)} | {pct:.1f}% |")
    lines.append("")

    # 量比分布（>3的为超强）
    vol_ratio = local_df['量比'].dropna()
    vr_gt3 = (vol_ratio >= 3).sum()
    vr_gt1 = ((vol_ratio >= 1) & (vol_ratio < 3)).sum()
    vr_lt1 = (vol_ratio < 1).sum()
    lines.append("### 量比分布")
    lines.append("")
    lines.append("| 区间 | 家数 | 解读 |")
    lines.append("|------|------|------|")
    lines.append(f"| ≥ 3（放量强势） | {vr_gt3} | 成交量是近期均量3倍以上，资金高度活跃 |")
    lines.append(f"| 1~3（温和放量） | {vr_gt1} | 成交量略高于均量，有一定活跃度 |")
    lines.append(f"| < 1（缩量） | {vr_lt1} | 成交量低于均量，场外资金观望为主 |")
    lines.append("")

    # 主力净流入分析  
    if '主力净比%' in local_df.columns:
        mf = local_df['主力净比%'].dropna()
        mf_in = (mf > 0).sum()
        mf_out = (mf < 0).sum()
        top_inflow = local_df.nlargest(5, '主力净比%')[['代码','名称','主力净比%','涨幅%','总金额']]
        lines.append("### 主力资金净流向")
        lines.append("")
        lines.append(f"- 净流入（主力净比%>0）：**{mf_in}** 只")
        lines.append(f"- 净流出（主力净比%<0）：**{mf_out}** 只")
        lines.append("")
        lines.append("**主力净流入比例 TOP 5：**")
        lines.append("")
        lines.append("| 代码 | 名称 | 主力净比% | 涨幅% | 总成交额(万) |")
        lines.append("|------|------|---------|------|------------|")
        for _, r in top_inflow.iterrows():
            lines.append(f"| {r['代码']} | {r['名称']} | {r['主力净比%']:.2f}% | {r['涨幅%']:.2f}% | {r['总金额']/10000:.0f} |")
        lines.append("")

    # ─── 十、系统纪律提醒 ─────────────────────────────────
    lines.append("---")
    lines.append("## 十、系统纪律提醒")
    lines.append("")
    lines.append("```")
    lines.append("【买入5大条件 — 须同时满足】")
    lines.append(f'  ① 大盘情绪为"强"或"混沌" → 当前：{s["sentiment"]} ✓')
    lines.append(f'  ② 主线明确（不超过2个）→ {"/".join(top2)}')
    lines.append("  ③ 标的为板块内人气核心（热度榜/涨停时间前2）")
    lines.append("  ④ 集合竞价成交额进入板块前三")
    lines.append("  ⑤ 开盘5分钟量比>3，分时回踩均线支撑±1%")
    lines.append("")
    lines.append("【卖出条件】")
    lines.append("  • 硬性止损：成本价 -7%，无条件卖出")
    lines.append("  • 回落止盈：自当日高点回撤 7%")
    lines.append("  • 板块退潮：核心股2只触跌停，立刻清仓")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append(f"*本报告由量化分析脚本自动生成，数据来源：同花顺行情导出 + akshare。*")
    lines.append(f"*仅供交易系统执行参考，不构成投资建议。*")

    return "\n".join(lines)


# ══════════════════════════════════════════════════
# 9. 观察池快照输出（供 backtest.py 回测使用）
# ══════════════════════════════════════════════════

def save_observation_pool_json(date, sentiment_data, sector_stats, zt_pool,
                                obs_pool, opportunity_df, concept_board_df,
                                zb_pool=None, local_df=None):
    """
    将当日预测结果序列化为 JSON 快照文件 observation_pool_{date}.json。
    backtest.py 读取此文件作为 T-1 日的 ground-truth 预测数据，避免解析 Markdown。
    """
    if zb_pool is None:
        zb_pool = pd.DataFrame()
    if local_df is None:
        local_df = pd.DataFrame()
    def safe_float(v, default=0.0):
        try:
            return round(float(v), 4)
        except Exception:
            return default

    def safe_int(v, default=0):
        try:
            return int(v)
        except Exception:
            return default

    # ── 1. 市场情绪 ──────────────────────────────────────
    sentiment_snap = {
        "up_count":          safe_int(sentiment_data.get("up_count")),
        "down_count":        safe_int(sentiment_data.get("down_count")),
        "flat_count":        safe_int(sentiment_data.get("flat_count")),
        "zt_count":          safe_int(sentiment_data.get("zt_count")),
        "zb_count":          safe_int(sentiment_data.get("zb_count")),
        "zb_rate":           safe_float(sentiment_data.get("zb_rate")),
        "sentiment":         str(sentiment_data.get("sentiment", "")),
        "position":          str(sentiment_data.get("position", "")),
    }

    # ── 2. 主线板块（含持续性评级 + 板块内全量个股）───────────────────────
    main_sectors_snap = []
    for _, row in sector_stats.head(5).iterrows():
        sector_name = str(row["行业"])
        # 复用 evaluate_sector_continuity 取得评级
        if not zt_pool.empty and "所属行业" in zt_pool.columns:
            sec_df = zt_pool[zt_pool["所属行业"] == sector_name].copy()
        else:
            sec_df = pd.DataFrame()
        continuity, reasons = evaluate_sector_continuity(
            sector_name, sec_df, sentiment_data.get("up_count", 0)
        )
        # 保存板块内全量涨停个股（回测时用这些代码去T日XLS查实际表现）
        sector_stocks = []
        if not sec_df.empty:
            for _, sr in sec_df.iterrows():
                sector_stocks.append({
                    "code":          str(sr["代码"]),
                    "name":          str(sr["名称"]),
                    "boards":        safe_int(sr.get("连板数", 0)),
                    "seal_time":     str(sr.get("首次封板时间", "--")),
                    "explode_count": safe_int(sr.get("炸板次数", 0)),
                    "volume_billion": safe_float(sr.get("成交额", 0) / 1e8),
                })
        # 板块内炸板数（从 zb_pool 按所属行业匹配）
        sector_zb_count = 0
        if not zb_pool.empty and "所属行业" in zb_pool.columns:
            sector_zb_count = len(zb_pool[zb_pool["所属行业"] == sector_name])
        # 板块内涨停个股平均涨幅（从 local_df 按涨停池个股代码匹配）
        sector_avg_gain = None
        if not sec_df.empty and not local_df.empty and "涨幅%" in local_df.columns:
            sector_codes = sec_df["代码"].tolist()
            matched_local = local_df[local_df["代码"].isin(sector_codes)]
            if not matched_local.empty:
                gains = matched_local["涨幅%"].dropna()
                if len(gains) > 0:
                    sector_avg_gain = safe_float(gains.mean())
        main_sectors_snap.append({
            "name":             sector_name,
            "limit_up_count":   safe_int(row.get("涨停数量", 0)),
            "max_boards":       safe_int(row.get("连板最高", 0)),
            "multi_board_count": safe_int(row.get("多连板数", 0)),
            "earliest_seal":    str(row.get("最早封板", "--")),
            "volume_billion":   safe_float(row.get("总成交额", 0) / 1e8),
            "score_6d":         safe_float(row.get("综合得分", 0)),
            "continuity_rating": continuity,
            "continuity_reasons": reasons,
            "zb_count":         sector_zb_count,
            "avg_gain_pct":     sector_avg_gain,
            "stocks":           sector_stocks,
        })

    # ── 3. 核心观察池 ─────────────────────────────────────
    leaders = []
    army = []
    top2 = [s["name"] for s in main_sectors_snap[:2]]
    for sector_name in top2:
        leader_key = f"{sector_name}_龙头候选"
        mid_key    = f"{sector_name}_中军候选"
        # 龙头候选 cols: 代码,名称,首次封板时间,连板数,成交额,所属行业
        for r in obs_pool.get(leader_key, []):
            # 查 T-1 日炸板次数（从 zt_pool）
            explode_count = 0
            if not zt_pool.empty and len(r) > 0:
                matched = zt_pool[zt_pool["代码"] == str(r[0])]
                if not matched.empty:
                    explode_count = safe_int(matched.iloc[0].get("炸板次数", 0))
            leaders.append({
                "code":          str(r[0]),
                "name":          str(r[1]),
                "sector":        str(r[5]),
                "boards":        safe_int(r[3]),
                "seal_time":     str(r[2]),
                "volume_billion": safe_float(r[4] / 1e8 if r[4] else 0),
                "explode_count": explode_count,
                "role":          "龙头",
            })
        # 中军候选
        for r in obs_pool.get(mid_key, []):
            explode_count = 0
            if not zt_pool.empty and len(r) > 0:
                matched = zt_pool[zt_pool["代码"] == str(r[0])]
                if not matched.empty:
                    explode_count = safe_int(matched.iloc[0].get("炸板次数", 0))
            army.append({
                "code":          str(r[0]),
                "name":          str(r[1]),
                "sector":        str(r[5]),
                "boards":        safe_int(r[3]),
                "seal_time":     str(r[2]),
                "volume_billion": safe_float(r[4] / 1e8 if r[4] else 0),
                "explode_count": explode_count,
                "role":          "中军",
            })
    # 量化高分股 cols: 代码,名称,涨幅%,量比,换手%,主力净比%,综合得分,是否强势股池,所属行业_ak
    quant_cols = obs_pool.get("量化高分股_cols", [])
    quant_top15 = []
    for r in obs_pool.get("量化高分股", []):
        row_dict = dict(zip(quant_cols, r)) if quant_cols else {}
        quant_top15.append({
            "code":    str(row_dict.get("代码", r[0] if len(r) > 0 else "")),
            "name":    str(row_dict.get("名称", r[1] if len(r) > 1 else "")),
            "sector":  str(row_dict.get("所属行业_ak", "--")),
            "score":   safe_float(row_dict.get("综合得分", 0)),
            "gain_pct": safe_float(row_dict.get("涨幅%", 0)),
            "vol_ratio": safe_float(row_dict.get("量比", 0)),
            "turnover_pct": safe_float(row_dict.get("换手%", 0)),
            "lead_net_pct": safe_float(row_dict.get("主力净比%", 0)),
            "boards":  0,  # quant pool doesn't carry boards directly
        })

    # ── 4. 连板梯队全量 ───────────────────────────────────
    all_board_stocks = []
    if not zt_pool.empty:
        lb_stocks = zt_pool[zt_pool["连板数"] >= 2].sort_values("连板数", ascending=False)
        for _, r in lb_stocks.iterrows():
            all_board_stocks.append({
                "code":          str(r["代码"]),
                "name":          str(r["名称"]),
                "boards":        safe_int(r["连板数"]),
                "seal_time":     str(r["首次封板时间"]),
                "explode_count": safe_int(r.get("炸板次数", 0)),
                "volume_billion": safe_float(r["成交额"] / 1e8),
                "sector":        str(r["所属行业"]),
            })

    # ── 5. 概念板块 TOP20 ─────────────────────────────────
    concept_top20 = []
    if concept_board_df is not None and not concept_board_df.empty:
        for _, r in concept_board_df.head(20).iterrows():
            signals = []
            if r.get("是否旗舰主线", False): signals.append("旗舰")
            if r.get("是否量能异动", False): signals.append("量异")
            if r.get("是否趋势延续", False): signals.append("趋势")
            if r.get("是否形态共振", False): signals.append("形态")
            if r.get("是否攻守强势", False): signals.append("攻守")
            concept_top20.append({
                "name":        str(r.get("名称", "")),
                "score":       safe_float(r.get("综合得分", 0)),
                "limit_count": safe_int(r.get("涨停数", 0)),
                "gain_pct":    safe_float(r.get("涨幅%", 0)),
                "rela_pct":    safe_float(r.get("强弱度%", 0)),
                "turnover_z":  safe_float(r.get("换手Z", 0)),
                "signals":     signals,
                "signal_count": len(signals),
            })

    # ── 组装 & 写文件 ─────────────────────────────────────
    snapshot = {
        "date":            date,
        "generated_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sentiment":       sentiment_snap,
        "main_sectors":    main_sectors_snap,
        "observation_pool": {
            "leaders":    leaders,
            "army":       army,
            "quant_top15": quant_top15,
        },
        "all_board_stocks": all_board_stocks,
        "concept_top20":   concept_top20,
    }

    output_path = f"observation_pool_{date}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    print(f"  [OK] 观察池快照已保存: {output_path}")
    return output_path


# ══════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════

def main():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始分析 {TARGET_DATE} A股全市场数据...")
    print("=" * 60)

    # 1. 加载本地数据
    print("[1/6] 加载本地行情数据...")
    local_df = load_local_data(DATA_FILE)
    local_df = identify_limit_and_break(local_df)
    print(f"  共加载 {len(local_df)} 只股票（已去除ST/退市）")

    # 2. 获取akshare数据
    print("[2/6] 获取 akshare 补充数据（涨停池/炸板池/强势股池）...")
    ak_data = fetch_akshare_data(TARGET_DATE)
    zt_pool    = ak_data['zt_pool']
    zb_pool    = ak_data['zb_pool']
    strong_pool = ak_data['strong_pool']
    print(f"  涨停池: {len(zt_pool)} 只 | 炸板池: {len(zb_pool)} 只 | 强势股池: {len(strong_pool)} 只")

    # 3. 市场情绪
    print("[3/6] 分析市场情绪...")
    sentiment_data = analyze_market_sentiment(local_df, zt_pool, zb_pool)
    print(f"  上涨: {sentiment_data['up_count']} | 下跌: {sentiment_data['down_count']} | 情绪: {sentiment_data['sentiment']}")

    # 4. 板块分析
    print("[4/7] 分析主线板块（6维度评分）...")
    sector_stats = analyze_main_sectors(zt_pool, zb_pool, local_df)

    # 5. 量化机会评分
    print("[5/7] 计算量化机会评分...")
    opportunity_df = build_opportunity_pool(local_df, zt_pool, strong_pool)

    # 6. 概念板块多维度分析（本地文件驱动，无需实时API，支持历史日期）
    print("[6/7] 加载并分析概念板块数据（本地文件）...")
    concept_raw = load_concept_board_local(CONCEPT_FILE)
    if not concept_raw.empty:
        concept_board_df = analyze_concept_boards_multidim(concept_raw, zt_pool)
        super_theme_df   = identify_super_themes(concept_board_df)
        print(f"  概念板块: {len(concept_board_df)} 个（涨停数≥2）| 超级主题: {len(super_theme_df)} 个")
        if not super_theme_df.empty:
            top = super_theme_df.iloc[0]
            print(f"  最强主题集群: {top['超级主题']} (合计涨停{top['合计涨停数']}只, 均分{top['平均综合得分']})")
    else:
        concept_board_df = pd.DataFrame()
        super_theme_df   = pd.DataFrame()
        print(f"  未找到概念文件 {CONCEPT_FILE}，概念分析跳过")

    # 7. 构建观察池
    print("[7/7] 构建核心观察池（调用 get_sector_leaders 增强排序）...")
    obs_pool = build_observation_pool(zt_pool, sector_stats, opportunity_df, local_df)

    # 生成报告
    print("\n生成复盘报告...")
    report_text = generate_report(
        TARGET_DATE, sentiment_data, sector_stats, zt_pool, zb_pool,
        strong_pool, opportunity_df, obs_pool, local_df, concept_board_df, super_theme_df
    )

    # 保存报告
    output_file = f"极简复盘报告_{TARGET_DATE}.md"
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(report_text)

    print(f"\n[OK] 复盘报告已保存至: {output_file}")
    print(f"   字符数: {len(report_text)}")

    # 保存观察池 JSON 快照（供 backtest.py 回测使用）
    save_observation_pool_json(
        TARGET_DATE, sentiment_data, sector_stats, zt_pool,
        obs_pool, opportunity_df, concept_board_df,
        zb_pool=zb_pool, local_df=local_df
    )

    print("=" * 60)
    return report_text


if __name__ == "__main__":
    main()
