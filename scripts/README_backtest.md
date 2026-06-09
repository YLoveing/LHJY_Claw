# 回测脚本

## 统一入口

```bash
python3 scripts/run_backtest.py --scenario=quick   # 快速（基于选股器数据，默认）
python3 scripts/run_backtest.py --scenario=hs300   # 沪深300（v4 幸存者偏差消除）
python3 scripts/run_backtest.py --scenario=full    # 全A股（2300+只，耗时较长）
python3 scripts/run_backtest.py --scenario=compare # 多策略对比
python3 scripts/run_backtest.py --all              # 跑全部场景
python3 scripts/run_backtest.py --days=60          # 指定天数
python3 scripts/run_backtest.py --save             # 保存到 reports/
```

## 流水线集成

`run_and_send.sh` 在 **周五 18:00** 收盘时段自动执行 `--scenario=quick --days=30`，结果推送到你的 QQ。

## 文件说明

| 文件 | 说明 |
|------|------|
| `run_backtest.py` | **统一运行器**，所有场景的入口 |
| `backtest_compare.py` | 多策略对比框架 |
| `backtest_full.py` | 全A股（2300+只）逐日回测 |
| `backtest_hs300_v4_survivorship.py` | 沪深300 + 幸存者偏差消除 |
| `quick_backtest.py` | 快速回测（基于选股器候选股） |
