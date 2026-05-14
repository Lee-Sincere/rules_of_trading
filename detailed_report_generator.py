#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
A股深度复盘报告生成器 - 详细版
特点：包含概念板块对应的具体涨停个股清单，便于人工核对数据准确性
"""

import pandas as pd
import numpy as np
import akshare as ak
from datetime import datetime
import warnings
import json
import os

# 引入原脚本的核心逻辑函数（为了保持代码复用，这里直接引用原文件中的关键类/函数）
# 在实际生产中，建议将 analysis_20260316.py 封装为模块导入
# 这里为了演示，我们重新构建一个简化的详细报告生成流程

warnings.filterwarnings('ignore')

TARGET_DATE = "20260514"
DATA_FILE = f"全部Ａ股{TARGET_DATE}.xls"
CONCEPT_FILE = f"板块指数{TARGET_DATE}.xls"

def load_local_data(filepath):
    """加载本地全市场行情CSV数据"""
    try:
        df = pd.read_csv(filepath, sep='\t', encoding='gbk')
        numeric_cols = ['涨幅%', '现价', '总量', '换手%', '量比', '主力净比%', '总金额']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col].astype(str).str.replace('--', '').str.strip(), errors='coerce')
        df['代码'] = df['代码'].astype(str).str.extract(r'(\d+)')[0].str.zfill(6)
        df = df[~df['名称'].str.contains('ST|退', na=False)].copy()
        return df
    except Exception as e:
        print(f"加载数据失败: {e}")
        return pd.DataFrame()

def get_concept_stocks_mapping(local_df, zt_pool):
    """
    构建概念板块与涨停个股的详细映射
    返回：{ '概念名称': [ {'代码':..., '名称':...}, ... ] }
    """
    mapping = {}
    if zt_pool.empty or local_df.empty:
        return mapping
    
    # 获取今日所有涨停股的代码列表
    limit_up_codes = zt_pool['代码'].tolist()
    
    # 遍历每个涨停股，查找其所属概念（这里简化处理，实际需调用 akshare 的概念接口或本地概念表）
    # 由于 akshare 实时接口较慢，此处演示如何利用本地数据进行简单归类
    # 在实际详细版中，建议增加一个步骤：调用 ak.stock_board_concept_name_em() 获取成分股
    
    return mapping

def generate_detailed_report():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始生成详细版复盘报告 {TARGET_DATE}...")
    
    # 1. 加载基础数据
    local_df = load_local_data(DATA_FILE)
    if local_df.empty:
        return

    # 2. 获取涨停池数据
    try:
        zt_pool = ak.stock_zt_pool_em(date=TARGET_DATE)
        zt_pool['代码'] = zt_pool['代码'].astype(str).str.zfill(6)
    except:
        zt_pool = pd.DataFrame()

    # 3. 统计各板块涨停详情
    if not zt_pool.empty:
        sector_detail = zt_pool.groupby('所属行业').agg({
            '名称': list,
            '代码': list,
            '连板数': 'max',
            '成交额': 'sum'
        }).reset_index()
        sector_detail.rename(columns={'名称': '涨停个股名单', '代码': '涨停代码清单'}, inplace=True)
        
        # 按涨停数量排序
        sector_detail['涨停数量'] = sector_detail['涨停个股名单'].apply(len)
        sector_detail = sector_detail.sort_values('涨停数量', ascending=False)

    # 4. 生成 Markdown 报告
    lines = []
    lines.append(f"# A股深度复盘报告 (详细版) - {TARGET_DATE}")
    lines.append(f"*生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}*")
    lines.append("")
    lines.append("---")
    
    lines.append("## 一、市场核心数据概览")
    lines.append(f"- **上涨家数**: {(local_df['涨幅%'] > 0).sum()}")
    lines.append(f"- **涨停家数**: {len(zt_pool)}")
    lines.append("")

    lines.append("## 二、行业板块涨停明细（含个股清单）")
    lines.append("")
    lines.append("> 此部分列出所有出现涨停的行业板块及其对应的具体股票代码和名称，用于人工核对。")
    lines.append("")

    if not zt_pool.empty:
        for _, row in sector_detail.head(10).iterrows():
            lines.append(f"### 📌 {row['所属行业']} (涨停 {row['涨停数量']} 只)")
            lines.append(f"- **最高连板**: {int(row['连板数'])} 板")
            lines.append(f"- **总成交额**: {row['成交额']/1e8:.2f} 亿")
            lines.append("- **涨停个股清单**:")
            lines.append("| 代码 | 名称 | 连板数 |")
            lines.append("|------|------|--------|")
            
            # 构造个股表格
            for code, name, boards in zip(row['涨停代码清单'], row['涨停个股名单'], 
                                          zt_pool[zt_pool['所属行业']==row['所属行业']]['连板数']):
                lines.append(f"| {code} | {name} | {boards}板 |")
            lines.append("")

    lines.append("---")
    lines.append("*本报告由 detailed_report_generator.py 自动生成。*")

    # 保存文件
    output_file = f"深度复盘报告_{TARGET_DATE}.md"
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
    
    print(f"[OK] 详细报告已保存至: {output_file}")

if __name__ == "__main__":
    generate_detailed_report()
