#!/usr/bin/env bash
# =====================================================
# 一键代码质量检查脚本
# 顺序：isort → black → pytest → coverage
# =====================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

echo "===== 1/4 isort — 导入排序 ====="
isort --settings-path pyproject.toml --check-only --diff . 2>&1 || {
    echo "⚠ isort 发现格式问题，自动修复中..."
    isort --settings-path pyproject.toml .
    echo "✅ isort 修复完成"
}

echo ""
echo "===== 2/4 black — 代码格式化 ====="
black --config pyproject.toml --check . 2>&1 || {
    echo "⚠ black 发现格式问题，自动修复中..."
    black --config pyproject.toml .
    echo "✅ black 修复完成"
}

echo ""
echo "===== 3/4 pytest — 单元测试 ====="
python3 -m pytest tests/ -v --tb=short

echo ""
echo "===== 4/4 coverage — 覆盖率报告 ====="
python3 -m pytest tests/ --cov=. --cov-report=term-missing --cov-report=html -q 2>&1

echo ""
echo "===== ✅ 全部检查完成 ====="
