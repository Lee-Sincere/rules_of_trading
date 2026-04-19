#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""
Date: 2026/3/14
Desc: 增强版A股数据分析脚本
整合了同花顺概念分析、炸板率计算，用于支持交易复盘。
"""
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta
from collections import Counter
import warnings
warnings.filterwarnings('ignore')

def get_recent_trade_date():
    """获取最近的一个交易日日期（格式：YYYYMMDD）。简化处理，通常为今日或昨日。"""
    today = datetime.now()
    # 如果是周末，则返回上一个周五
    if today.weekday() >= 5:  # 5=周六，6=周日
        days_to_friday = (today.weekday() - 4) % 7
        target_date = today - timedelta(days=days_to_friday)
    else:
        target_date = today
    return target_date.strftime("%Y%m%d")

def calculate_explode_rate(target_date=None):
    """
    计算指定日期的精确炸板率。
    
    Parameters
    ----------
    target_date : str, optional
        日期格式：YYYYMMDD，默认为最近交易日。
        
    Returns
    -------
    tuple
        (涨停总数, 炸板数, 炸板率, 涨停池DataFrame)
    """
    if target_date is None:
        target_date = get_recent_trade_date()
    
    try:
        zt_pool_df = ak.stock_zt_pool_em(date=target_date)
        if zt_pool_df.empty:
            print(f"{target_date} 无涨停数据")
            return 0, 0, 0.0, pd.DataFrame()
        
        # 计算涨停与炸板
        zt_count = len(zt_pool_df)  # 涨停总数
        explode_count = len(zt_pool_df[zt_pool_df['炸板次数'] > 0])  # 炸板数
        explode_rate = explode_count / zt_count if zt_count > 0 else 0.0
        
        print(f"[炸板率分析] 日期: {target_date}")
        print(f"  涨停总数: {zt_count}")
        print(f"  炸板数: {explode_count}")
        print(f"  炸板率: {explode_rate:.2%}")
        
        return zt_count, explode_count, explode_rate, zt_pool_df
        
    except Exception as e:
        print(f"计算炸板率时出错: {e}")
        return 0, 0, 0.0, pd.DataFrame()

def find_concept_for_stock(stock_code, concept_map):
    """
    为单只股票查找其所属的概念板块。
    注意：此方法为近似匹配，通过遍历概念成分股实现。
    
    Parameters
    ----------
    stock_code : str
        股票代码，如 '000001'
    concept_map : dict
        概念名称到代码的映射，来自 stock_board_concept_name_ths
        
    Returns
    -------
    list
        该股票所属的概念名称列表
    """
    stock_concepts = []
    # 将代码统一为字符串，去掉可能的前缀
    clean_code = str(stock_code).zfill(6)
    
    for concept_name, concept_code in concept_map.items():
        try:
            # 获取该概念的所有成分股
            cons_df = ak.stock_board_concept_cons_em(symbol=concept_code)
            if not cons_df.empty and clean_code in cons_df['代码'].astype(str).str.zfill(6).values:
                stock_concepts.append(concept_name)
        except Exception as e:
            # 某些概念可能无法获取成分股，静默跳过
            continue
            
    return stock_concepts

def analyze_limit_up_concepts(target_date=None):
    """
    核心分析函数：分析涨停股的共同概念，找出市场主线。
    
    Parameters
    ----------
    target_date : str, optional
        日期格式：YYYYMMDD，默认为最近交易日。
        
    Returns
    -------
    tuple
        (概念统计DataFrame, 股票-概念明细Dict, 涨停池DataFrame)
    """
    if target_date is None:
        target_date = get_recent_trade_date()
    
    print(f"\n[概念主线分析] 日期: {target_date}")
    print("="*50)
    
    # 1. 获取涨停池数据
    zt_count, _, _, zt_pool_df = calculate_explode_rate(target_date)
    if zt_pool_df.empty:
        return pd.DataFrame(), {}, pd.DataFrame()
    
    # 2. 获取所有概念板块映射
    print("正在获取概念板块列表...")
    concept_df = ak.stock_board_concept_name_ths()
    concept_map = dict(zip(concept_df['name'], concept_df['code']))
    print(f"共获取 {len(concept_map)} 个概念板块")

    # 3. 获取每个概念的成分股（避免对每只股票重复调用接口）
    print("正在获取概念成分股数据（可能需要一些时间）...")
    concept_to_stocks = {}
    total_concepts = len(concept_map)
    success_count = 0
    fail_count = 0

    for idx, (concept_name, concept_code) in enumerate(concept_map.items()):
        try:
            cons_df = ak.stock_board_concept_cons_em(symbol=concept_code)
            if not cons_df.empty:
                # 统一为 6 位字符串
                concept_to_stocks[concept_name] = set(cons_df['代码'].astype(str).str.zfill(6).values)
            else:
                concept_to_stocks[concept_name] = set()
            success_count += 1
        except Exception:
            # 某些概念可能无法获取成分股（例如接口请求失败、被代理阻断等），继续处理其他概念
            concept_to_stocks[concept_name] = set()
            fail_count += 1

        # 进度显示（每 50 个概念更新一次）
        if (idx + 1) % 50 == 0 or (idx + 1) == total_concepts:
            done = idx + 1
            pct = done / total_concepts * 100
            print(f"\r  已获取 {done}/{total_concepts} 个概念成分股 ({pct:.1f}%)", end="", flush=True)
    print()  # 换行

    if fail_count > 0:
        print(f"已成功获取 {success_count} 个概念成分股，{fail_count} 个概念失败（可能是网络/代理限制导致）。")
        print("若要获得概念分析结果，请确保 akshare 可以正常访问同花顺/东方财富接口。")

    # 4. 分析每只涨停股所属概念
    stock_concept_detail = {}  # 存储每只股票的详细概念
    all_concepts = []          # 存储所有出现的概念，用于统计

    print("正在分析涨停股所属概念...")
    for idx, row in zt_pool_df.iterrows():
        stock_code = row['代码']
        stock_name = row['名称']
        clean_code = str(stock_code).zfill(6)

        # 查找该股票所属概念
        concepts = [name for name, codes in concept_to_stocks.items() if clean_code in codes]
        stock_concept_detail[f"{stock_code} {stock_name}"] = concepts
        all_concepts.extend(concepts)

        # 进度显示
        if (idx + 1) % 10 == 0 or (idx + 1) == len(zt_pool_df):
            print(f"  已分析 {idx+1}/{len(zt_pool_df)} 只股票...", end="\r", flush=True)
    print()  # 换行
    
    # 4. 统计概念出现频次
    concept_counter = Counter(all_concepts)
    
    # 转换为DataFrame并排序
    concept_stats = pd.DataFrame(
        concept_counter.items(), 
        columns=['概念名称', '出现次数']
    )
    concept_stats['占比'] = concept_stats['出现次数'] / len(zt_pool_df)
    concept_stats = concept_stats.sort_values('出现次数', ascending=False).reset_index(drop=True)
    
    print(f"\n分析完成！共分析了 {len(zt_pool_df)} 只涨停股。")
    print(f"出现过的概念数量: {len(concept_stats)}")
    
    # 5. 打印TOP 10概念
    if not concept_stats.empty:
        print(f"\n【涨停股关联概念 TOP 10】")
        print("-" * 40)
        for i, row in concept_stats.head(10).iterrows():
            print(f"{i+1:2d}. {row['概念名称']:20} 出现: {row['出现次数']:3d} 次 | 占比: {row['占比']:.1%}")
    
    return concept_stats, stock_concept_detail, zt_pool_df

def generate_daily_report(target_date=None):
    """
    生成每日复盘报告的核心数据。
    此函数输出可直接用于填写《复盘报告模板》的数据。
    """
    if target_date is None:
        target_date = get_recent_trade_date()
    
    print(f"生成每日复盘报告数据 - {target_date}")
    print("="*60)
    
    # 1. 获取市场涨跌家数（此处需要用其他接口，以下为模拟）
    # 在实际使用中，您需要用 ak.stock_zh_a_spot_em() 等接口获取
    up_count = "需从行情接口获取"  # 示例：2060
    down_count = "需从行情接口获取"
    
    # 2. 计算炸板率
    zt_count, explode_count, explode_rate, _ = calculate_explode_rate(target_date)
    
    # 3. 分析概念主线
    concept_stats, stock_detail, zt_pool_df = analyze_limit_up_concepts(target_date)
    
    # 4. 整理输出报告字典
    report_data = {
        '日期': target_date,
        '上涨家数': up_count,
        '下跌家数': down_count,
        '涨停家数': int(zt_count),
        '炸板家数': int(explode_count),
        '炸板率': round(explode_rate, 4),
        '核心概念列表': concept_stats.head(5).to_dict('records') if not concept_stats.empty else [],
        '涨停股总数': len(zt_pool_df) if not zt_pool_df.empty else 0,
    }
    
    # 5. 判断市场情绪
    if up_count != "需从行情接口获取":
        up_count_int = int(up_count)
        if up_count_int > 2500:
            market_sentiment = "强"
        elif up_count_int < 1800:
            market_sentiment = "弱"
        else:
            market_sentiment = "混沌"
        
        if explode_rate > 0.3:
            market_sentiment = "弱"  # 炸板率过高，情绪定为弱
    else:
        market_sentiment = "需计算"
    
    report_data['市场情绪'] = market_sentiment
    
    print(f"\n【报告数据摘要】")
    for key, value in report_data.items():
        if key not in ['核心概念列表']:
            print(f"  {key}: {value}")
    
    return report_data, concept_stats, stock_detail

# 主函数，用于直接测试
if __name__ == "__main__":
    # 示例用法1：计算今日炸板率
    print("示例1：计算炸板率")
    calculate_explode_rate()
    
    # 示例用法2：分析涨停股概念
    print("\n" + "="*60)
    report, stats, detail = generate_daily_report()
    
    # 示例用法3：获取具体某只股票的概念
    print("\n" + "="*60)
    print("示例3：查询单只股票概念属性")
    concept_df = ak.stock_board_concept_name_ths()
    concept_map = dict(zip(concept_df['name'], concept_df['code']))
    
    # 测试股票：宁德时代
    test_concepts = find_concept_for_stock('300750', concept_map)
    print(f"宁德时代所属概念: {test_concepts[:10]}")  # 只显示前10个