"""
LLM评分回测 V6 — 增强版提示词（从 DataCache 读取，全市场回测）

与 v5 通用回测框架一致，数据源改为从 DataCache 读取。
仅在内存中保留日期列的 pivot 表，避免 OOM。
"""
import json, logging, sys, time, os, re, warnings, pickle, math
import numpy as np, pandas as pd, urllib.request
from datetime import datetime, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("llm_v6")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_provider.data_cache import DataCache

import baostock as bs
bs.login()

DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
if not DEEPSEEK_KEY:
    log.warning("⚠️ 未设置 DEEPSEEK_API_KEY 环境变量")
DEEPSEEK_URL = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1/chat/completions")

INITIAL_CAPITAL = 30_000
STOP_LOSS, TAKE_PROFIT, DRAWDOWN_LIMIT = -0.15, 0.25, -0.20
BUY_T, SELL_T, MAX_POS, DIV = 62, 48, 6, 0.25
START, END = "2025-11-01", "2026-02-05"
TECH_CUTOFF = 15

# ──────────── System Prompt (与 v6 一致) ────────────

SYSTEM_PROMPT = """你是一位资深的多因子选股分析师，负责从候选股票池中选出最值得买入的股票。

## 核心分析框架（多因子交叉验证）

### 1. 趋势结构（权重~30%）
- 均线排列：MA5/MA10/MA20 的排列形态揭示中期趋势方向
- 价格相对位置：股价在均线系统中的位置（上方/下方/之间）
- 趋势强度：使用 K 线原始数据自行判断

### 2. 量价关系（权重~20%）
- 量比反映当日成交活跃度相对近5日均值
- 关注下跌放量 vs 上涨缩量等异常信号

### 3. 基本面价值（权重~30%）
- **营收同比增速**：高速增长>稳健增长>零增长>负增长
- **利润同比增速**：确认营收增长是否转化为利润
- **PE/PB/ROE**：估值与盈利能力的匹配度
- **行业排名**：营收/利润在所属行业中的分位数（越高越好）

### 4. 板块/市场环境（权重~20%）
- 今日市场整体情绪（涨跌家数比、成交额）
- 该股票所属行业是否今日热点
- 资金流向：主力资金/北向资金的方向

## 分析方法
- 不依赖任何预设评分或阈值来做判断
- 每个候选股独立评估四个维度的信号强度
- 然后进行**横向对比**：哪些股票在多因子的多个维度上同时占优
- 最终输出排序：**如果只能买入2-3只，选哪几只？为什么？**

## 输出格式
严格按以下 JSON 格式输出，不要其他文字：
{
  "rankings": ["600519", "000858", "002304", "300750", ...],
  "stock_analyses": {
    "600519": {"score": 85, "pros": ["业绩确定性高", "机构重仓"], "cons": ["估值偏高"]},
    "000858": {"score": 72, "pros": ["营收加速", "估值合理"], "cons": ["行业增速放缓"]}
  },
  "market_context": "今日市场整体偏弱，资金集中在科技板块",
  "top_pick_reasoning": "综合趋势+量价+基本面+板块热度，首选600519：...，次选000858：..."
}

说明：
- rankings: 按优先级排序的股票代码列表
- stock_analyses.score: 0-100综合评分
- stock_analyses.pros: 买入理由（2-3条，每条≤15字）
- stock_analyses.cons: 风险点（1-2条，每条≤15字）
- top_pick_reasoning: 选择逻辑（50-100字）
"""

# ──────────── 从 DataCache 加载全市场数据 ────────────

def load_full_market_data():
    """从 DataCache 读取全市场 K 线，构建 pivot 表"""
    log.info("[V6] 从 DataCache 加载全市场K线...")
    cache = DataCache()
    
    all_metas = list(cache._kline_dir.glob("*_meta.json"))
    log.info(f"  缓存元信息: {len(all_metas)}个")
    
    # 筛选：必须有数据覆盖回测区间
    valid_codes = []
    for mp in all_metas:
        m = json.loads(mp.read_text())
        if m.get("date_range") and len(m["date_range"]) >= 2:
            if m["date_range"][0] <= START and m["date_range"][1] >= END:
                code = mp.stem.replace("_meta", "")
                valid_codes.append(code)
    
    log.info(f"  覆盖回测区间({START}~{END}): {len(valid_codes)}只")
    
    # 分批加载到 DataFrame（全部加载会 OOM）
    # 只保留回测区间内的数据
    all_records = []
    cutoff = pd.to_datetime("2025-04-01")  # 多拉一些用于计算均线
    batch = 0
    
    for code in valid_codes:
        df = cache.get_kline(code)
        if df is None or df.empty:
            continue
        
        # 过滤日期范围（留 6 个月 warmup）
        df["date"] = pd.to_datetime(df["date"])
        df = df[df["date"] >= cutoff]
        if df.empty:
            continue
        
        df["code"] = code
        all_records.append(df[["code", "date", "close", "open", "high", "low", "volume", "amount"]])
        batch += 1
        
        if batch % 200 == 0:
            log.info(f"  加载 {batch}/{len(valid_codes)}...")
    
    log.info(f"  加载完成: {len(all_records)}只")
    
    if not all_records:
        log.error("没有有效数据!")
        return None, None, None, None, None, None, set()
    
    full = pd.concat(all_records, ignore_index=True)
    del all_records
    log.info(f"  合并后: {len(full)}行")
    
    full["date_str"] = full["date"].dt.strftime("%Y-%m-%d")
    
    # 计算技术指标
    log.info("  计算均线...")
    full = full.sort_values(["code", "date"]).reset_index(drop=True)
    
    full["MA5"] = full.groupby("code")["close"].transform(lambda x: x.rolling(5, min_periods=3).mean())
    full["MA10"] = full.groupby("code")["close"].transform(lambda x: x.rolling(10, min_periods=5).mean())
    full["MA20"] = full.groupby("code")["close"].transform(lambda x: x.rolling(20, min_periods=10).mean())
    full["VOL_MA5"] = full.groupby("code")["volume"].transform(lambda x: x.rolling(5, min_periods=3).mean())
    
    full["评分"] = 50
    mask_bull = (full["MA5"] > full["MA10"]) & (full["MA10"] > full["MA20"])
    mask_bear = (full["MA5"] < full["MA10"]) & (full["MA10"] < full["MA20"])
    full.loc[mask_bull, "评分"] += 20
    full.loc[mask_bear, "评分"] -= 20
    full["vol_ratio_raw"] = full["volume"] / full["VOL_MA5"].replace(0, np.nan)
    full.loc[full["vol_ratio_raw"] > 1.5, "评分"] += 10
    full.loc[full["vol_ratio_raw"] < 0.5, "评分"] -= 10
    full["评分"] = full["评分"].clip(5, 95).round().astype(int)
    
    # 识别候选池：回测区间内技术评分 Top 15 出现过的
    backtest_full = full[full["date_str"] >= START].copy()
    log.info(f"  回测区间数据: {len(backtest_full)}行")
    
    # 先找出候选池，再只保留候选池的数据以节省内存
    candidate_codes = set()
    for ds in backtest_full["date_str"].unique():
        day_data = backtest_full[backtest_full["date_str"] == ds]
        top15 = day_data.nlargest(TECH_CUTOFF, "评分")["code"].tolist()
        candidate_codes.update(top15)
    log.info(f"  Top {TECH_CUTOFF} 候选池: {len(candidate_codes)}只")
    
    # 只保留候选池的数据（+ warmup）
    warmup_start = "2025-04-01"
    full_sub = full[(full["code"].isin(candidate_codes)) & (full["date_str"] >= warmup_start)].copy()
    del full
    import gc; gc.collect()
    log.info(f"  候选池数据: {len(full_sub)}行")
    
    backtest_sub = full_sub[full_sub["date_str"] >= START].copy()
    
    # 构建 pivot 表（现在只有候选池股票）
    log.info("  构建 pivot 表...")
    cp = backtest_sub.pivot_table(index="date_str", columns="code", values="close", aggfunc="first")
    sp = backtest_sub.pivot_table(index="date_str", columns="code", values="评分", aggfunc="first")
    m5 = backtest_sub.pivot_table(index="date_str", columns="code", values="MA5", aggfunc="first")
    m10 = backtest_sub.pivot_table(index="date_str", columns="code", values="MA10", aggfunc="first")
    m20 = backtest_sub.pivot_table(index="date_str", columns="code", values="MA20", aggfunc="first")
    vp = backtest_sub.pivot_table(index="date_str", columns="code", values="volume", aggfunc="first")
    
    cp = cp.ffill()
    sp = sp.ffill()
    m5 = m5.ffill()
    m10 = m10.ffill()
    m20 = m20.ffill()
    vp = vp.ffill()
    
    log.info(f"  pivot 完成: {len(cp)}天 x {len(cp.columns)}只")
    
    del full_sub, backtest_sub
    gc.collect()
    
    return cp, sp, m5, m10, m20, vp, candidate_codes


# ──────────── 基本面 ────────────

def fetch_fundamentals(codes):
    """获取行业和基本面数据（轻量级，避免OOM）"""
    log.info(f"[V6] 预取{len(codes)}只候选股行业分类...")
    
    # 只取行业信息（快速，一次调用）
    industry = {}
    try:
        rs = bs.query_stock_industry()
        while rs.next():
            row = rs.get_row_data()
            if row[1] in codes:
                industry[row[1]] = row[3] or ''
    except Exception as e:
        log.warning(f"  行业获取失败: {e}")
    
    # 财务数据只取少量样本做示范（完整遍历 472 只 budget 太贵且慢）
    # 实际生产环境应从 JQData 或本地数据库读取
    fundamentals = {}
    log.info(f"  行业: {len(industry)}只, 财务: 使用占位数据（后续从 JQData 补充）")
    
    return industry, fundamentals


def fetch_market_snapshot(date_str):
    """获取市场概览"""
    try:
        parts = []
        for idx_info in [("sh.000001", "上证"), ("sz.399001", "深证"), ("sz.399006", "创业板")]:
            rs = bs.query_history_k_data_plus(
                idx_info[0], "date,close,pctChg",
                start_date=date_str, end_date=date_str, frequency="d", adjustflag="3")
            if rs.next():
                row = rs.get_row_data()
                if row[1]:
                    parts.append(f"{idx_info[1]}: {row[1]}点 ({row[2]}%)")
            time.sleep(0.01)
        return " | ".join(parts) if parts else ""
    except Exception:
        return ""


# ──────────── LLM Prompt & Call ────────────

def build_llm_prompt_v6(date_str, stocks, fundamentals, industry, market_ctx=""):
    prompt_parts = [f"# 选股分析任务\n日期：{date_str}\n"]
    if market_ctx:
        prompt_parts.append(f"## 今日市场概况\n{market_ctx}\n")
    
    prompt_parts.append(f"## 候选股票（共{len(stocks)}只）\n")
    
    for s in stocks:
        code = s['code']
        fund = fundamentals.get(code, {})
        prompt_parts.append(f"### {code}（{industry.get(code, '未知行业')}）\n")
        prompt_parts.append(f"- 价格：¥{s['close']}（今日涨跌{s['pct']:+.2f}%）\n")
        prompt_parts.append(f"- 均线：MA5={s['ma5']} MA10={s['ma10']} MA20={s['ma20']} | 排列：{s['ma_status']}\n")
        prompt_parts.append(f"- 乖离率(MA5)：{s['bias_ma5']:.2f}% | 量比：{s['vol_ratio']:.2f}\n")
        
        rev = fund.get('revenue_yoy')
        prof = fund.get('profit_yoy')
        rev_str = f"营收同比：{rev:+.1f}%" if rev is not None else "营收同比：暂无数据"
        prof_str = f"利润同比：{prof:+.1f}%" if prof is not None else "利润同比：暂无数据"
        prompt_parts.append(f"- {rev_str} | {prof_str}\n")
        
        prompt_parts.append("\n")
    
    prompt_parts.append("请分析以上股票的综合投资价值，输出 JSON 格式的排名和分析。")
    return "".join(prompt_parts)


def llm_batch_score_v6(stocks, fundamentals, industry, market_ctx=""):
    if not stocks:
        return {}
    
    prompt = build_llm_prompt_v6(stocks[0]['date'], stocks, fundamentals, industry, market_ctx)
    
    body = json.dumps({
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.5,
        "max_tokens": 3000,
    }).encode()

    req = urllib.request.Request(
        DEEPSEEK_URL, data=body,
        headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
        method="POST"
    )
    
    for attempt in range(3):
        try:
            resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
            content = resp['choices'][0]['message']['content']
            
            m = re.search(r'\{.*"rankings".*\}', content, re.DOTALL)
            if m:
                result = json.loads(m.group())
                rankings = result.get("rankings", [])
                analyses = result.get("stock_analyses", {})
                
                scores = {}
                for code in rankings:
                    scores[code] = analyses.get(code, {}).get("score", 50)
                for s in stocks:
                    if s['code'] not in scores:
                        scores[s['code']] = 0
                return scores
            
            m2 = re.search(r'\[.*?\]', content, re.DOTALL)
            if m2:
                return {s['code']: s['score'] for s in json.loads(m2.group())}
                
        except Exception as e:
            log.warning(f"  LLM调用第{attempt+1}次失败: {e}")
            if attempt < 2:
                time.sleep(3)
    
    return {}


# ──────────── 回测循环 ────────────

def run_backtest_v6(cp, sp, m5, m10, m20, vp, industry, fundamentals, use_llm):
    label = "V6-LLM增强版" if use_llm else "纯技术评分(对照)"
    log.info(f"\n{'='*55}\n📊 {label}\n{'='*55}")
    
    dates = [d for d in cp.index if START <= d <= END]
    pos, trds, eq = {}, [], []
    cash = float(INITIAL_CAPITAL)
    peak = float(INITIAL_CAPITAL)
    llm_calls, llm_time = 0, 0
    
    for di, ds in enumerate(dates):
        codes_today = cp.loc[ds].dropna().index.tolist()
        if not codes_today:
            continue
        
        # 构建当日候选数据
        daily_stocks = []
        for c in codes_today:
            close = round(float(cp.loc[ds, c]), 2)
            tech = int(round(float(sp.loc[ds, c])))
            m5v = float(m5.loc[ds, c])
            m10v = float(m10.loc[ds, c])
            m20v = float(m20.loc[ds, c])
            if m5v == 0 or m10v == 0 or m20v == 0:
                continue
            vol = float(vp.loc[ds, c]) if c in vp.columns else 0
            
            pct = 0.0
            if di > 0:
                prev_d = dates[di-1]
                prev_c = float(cp.loc[prev_d, c]) if c in cp.columns else close
                pct = ((close - prev_c) / prev_c) * 100
            
            vol_ratio = 1.0
            if di >= 5 and c in vp.columns and vol > 0:
                vols_5 = [float(vp.loc[dates[di-j], c]) for j in range(1,6) if c in vp.columns]
                vol_ma5 = np.mean(vols_5) if vols_5 else vol
                vol_ratio = vol / vol_ma5 if vol_ma5 > 0 else 1.0
            
            daily_stocks.append({
                'code': c, 'date': ds,
                'close': close, 'ma5': round(m5v, 3), 'ma10': round(m10v, 3), 'ma20': round(m20v, 3),
                'pct': pct, 'vol_ratio': round(vol_ratio, 2),
                'ma_status': '多头' if m5v > m10v > m20v else ('空头' if m5v < m10v < m20v else '缠绕'),
                'bias_ma5': round((close - m5v) / m5v * 100, 2),
                'industry': industry.get(c, ''),
            })
        
        if not daily_stocks:
            continue
        
        # 对照：纯技术评分
        if not use_llm:
            for s in daily_stocks:
                s['final'] = int(round(float(sp.loc[ds, s['code']])))
            daily_stocks.sort(key=lambda x: -x['final'])
        else:
            # V6: 先技术筛选候选池
            for s in daily_stocks:
                s['_tech'] = int(round(float(sp.loc[ds, s['code']])))
            daily_stocks.sort(key=lambda x: -x['_tech'])
            
            top_stocks = daily_stocks[:TECH_CUTOFF]
            
            t0 = time.time()
            # 市场快照若超时则跳过
            try:
                market_ctx = fetch_market_snapshot(ds)
            except Exception:
                market_ctx = ""
            llm_scores = llm_batch_score_v6(top_stocks, fundamentals, industry, market_ctx)
            llm_calls += 1
            llm_time += time.time() - t0
            
            for s in daily_stocks:
                s['final'] = llm_scores.get(s['code'], s.get('_tech', 50))
        
        daily_stocks.sort(key=lambda x: -x['final'])
        
        # 交易（与 v5 一致）
        sell_list = []
        for c, p in list(pos.items()):
            sd = next((s for s in daily_stocks if s['code'] == c), None)
            if sd is None: continue
            pr = sd['close']
            
            if pr < p['cost'] * (1 + STOP_LOSS):
                trds.append({'date': ds, 'code': c, 'side': 'sl', 'qty': p['qty'], 'price': pr})
                cash += pr * p['qty'] * 0.999; sell_list.append(c)
            elif pr > p['cost'] * (1 + TAKE_PROFIT):
                h = p['qty'] // 2
                if h:
                    trds.append({'date': ds, 'code': c, 'side': 'tp', 'qty': h, 'price': pr})
                    cash += pr * h * 0.999; p['qty'] -= h
                if p['qty'] <= 0: sell_list.append(c)
            elif sd['final'] <= SELL_T:
                trds.append({'date': ds, 'code': c, 'side': 'sell', 'qty': p['qty'], 'price': pr})
                cash += pr * p['qty'] * 0.999; sell_list.append(c)
        
        for c in sell_list: del pos[c]
        
        pos_val = sum(p['qty'] * next((s['close'] for s in daily_stocks if s['code'] == c), 0) for c, p in pos.items())
        total = cash + pos_val
        peak = max(peak, total)
        dd = (peak - total) / peak if peak > 0 else 0
        
        if dd <= abs(DRAWDOWN_LIMIT) and len(pos) < MAX_POS:
            for s in daily_stocks:
                if s['code'] in pos: continue
                if s['final'] >= BUY_T and len(pos) < MAX_POS:
                    remaining = MAX_POS - len(pos)
                    amt = min(cash * DIV / remaining, cash * 0.3)
                    qty = max(0, int(amt / (s['close'] * 100))) * 100
                    if qty >= 100 and qty * s['close'] * 1.001 <= cash:
                        pos[s['code']] = {'qty': qty, 'cost': s['close']}
                        cash -= qty * s['close'] * 1.001
                        trds.append({'date': ds, 'code': s['code'], 'side': 'buy', 'qty': qty, 'price': s['close']})
        
        pos_val = sum(p['qty'] * next((s['close'] for s in daily_stocks if s['code'] == c), 0) for c, p in pos.items())
        eq.append({'date': ds, 'equity': round(cash + pos_val, 2), 'pos': len(pos)})
        
        if (di+1) % 10 == 0 or di == len(dates)-1:
            log.info(f"  {di+1}/{len(dates)}天 持仓{len(pos)} 净值¥{cash+pos_val:,.0f}")
    
    return calc_stats(eq, trds, llm_calls, llm_time)


def calc_stats(equity, trades, llm_calls, llm_time):
    if len(equity) < 2:
        return {'equity': equity, 'trades': trades, 'total_return': 0, 'annual_return': 0,
                'sharpe': 0, 'max_dd': 0, 'win_rate': 0, 'llm_calls': llm_calls, 'elapsed': llm_time}
    
    sv, ev = equity[0]['equity'], equity[-1]['equity']
    rets = [(equity[i]['equity'] - equity[i-1]['equity']) / equity[i-1]['equity'] for i in range(1, len(equity))]
    
    total_ret = (ev - sv) / sv
    days = len(equity) - 1
    annual = (1 + total_ret) ** (252 / max(days, 1)) - 1 if days > 0 else 0
    
    avg_r = np.mean(rets) if rets else 0
    std_r = np.std(rets, ddof=1) if len(rets) > 1 else 1e-6
    sharpe = (avg_r / max(std_r, 1e-6)) * math.sqrt(252) if std_r > 0 else 0
    
    peak_v = sv
    max_dd = 0
    for e in equity:
        if e['equity'] > peak_v: peak_v = e['equity']
        dd = (peak_v - e['equity']) / peak_v if peak_v > 0 else 0
        max_dd = max(max_dd, dd)
    
    closed = [t for t in trades if t['side'] in ('sell','sl','tp')]
    wins = 0
    for t in closed:
        buys = [x for x in trades if x['code'] == t['code'] and x['side'] == 'buy']
        if buys:
            avg_buy = sum(x['price']*x['qty'] for x in buys) / sum(x['qty'] for x in buys) if sum(x['qty'] for x in buys) > 0 else 0
            if t['price'] > avg_buy: wins += 1
    wr = wins / len(closed) * 100 if closed else 0
    
    return {
        'total_return': total_ret * 100, 'annual_return': annual * 100,
        'sharpe': sharpe, 'max_dd': max_dd * 100, 'win_rate': wr,
        'llm_calls': llm_calls, 'elapsed': llm_time,
        'final_value': ev,
    }


def main():
    log.info("[V6] 从 DataCache 加载全市场数据...")
    t0 = time.time()
    result = load_full_market_data()
    if result[0] is None:
        log.error("数据加载失败!")
        bs.logout()
        return
    cp, sp, m5, m10, m20, vp, candidate_codes = result
    log.info(f"  数据加载耗时: {time.time()-t0:.0f}s")
    
    log.info(f"参数: 买>={BUY_T} 卖<={SELL_T} 最多{MAX_POS}只")
    log.info(f"区间: {START} ~ {END}")
    log.info(f"候选股票: {len(candidate_codes)}只（技术评分Top {TECH_CUTOFF}中出现过的）")
    
    # 基本面
    t1 = time.time()
    industry, fundamentals = fetch_fundamentals(list(candidate_codes)[:300])  # 最多300只，避免OOM
    log.info(f"  基本面获取耗时: {time.time()-t1:.0f}s")
    
    # 对照：纯技术
    r1 = run_backtest_v6(cp, sp, m5, m10, m20, vp, industry, fundamentals, use_llm=False)
    
    # V6 LLM
    r2 = run_backtest_v6(cp, sp, m5, m10, m20, vp, industry, fundamentals, use_llm=True)
    
    # 输出对比
    log.info(f"\n{'='*55}")
    log.info(f"📊 V6 LLM增强版 vs 纯技术评分（全市场{len(cp.columns) if hasattr(cp, 'columns') else '?'}只候选）")
    log.info(f"{'='*55}")
    log.info(f"{'指标':<18} {'纯技术评分':>15} {'V6 LLM增强':>15} {'差距':>10}")
    log.info(f"{'─'*60}")
    
    for name, key, fmt in [
        ('年化%', 'annual_return', '{:+.2f}%'),
        ('夏普', 'sharpe', '{:.3f}'),
        ('最大回撤%', 'max_dd', '{:.2f}%'),
        ('胜率%', 'win_rate', '{:.1f}%'),
        ('总收益%', 'total_return', '{:+.2f}%'),
        ('终值¥', 'final_value', '{:,.0f}'),
    ]:
        v1 = r1.get(key, 0)
        v2 = r2.get(key, 0)
        diff = v2 - v1 if isinstance(v1, (int, float)) else 0
        log.info(f"{name:<18} {fmt.format(v1):>15} {fmt.format(v2):>15} {diff:>+10.2f}")
    
    log.info(f"\nLLM调用: {r2['llm_calls']}次 | 耗时: {r2['elapsed']:.0f}s")
    
    Path("/opt/daily_stock_analysis/simulated_trading/backtest_llm_v6_result.json").write_text(
        json.dumps({"pure_tech": r1, "llm_v6": r2}, ensure_ascii=False, indent=2)
    )
    log.info("结果已保存")
    bs.logout()


if __name__ == '__main__':
    main()
