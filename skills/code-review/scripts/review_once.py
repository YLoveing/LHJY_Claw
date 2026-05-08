#!/usr/bin/env python3
"""review_once.py — 对指定 Python 文件执行结构化审查，输出 JSON 问题清单。

Usage:
    python3 scripts/review_once.py <path/to/file.py> [--severity CRITICAL|WARNING|INFO]
    python3 scripts/review_once.py <path/to/dir/>  (递归扫描)
"""

import ast
import json
import re
import sys
from pathlib import Path

SEVERITY_ORDER = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}


def check_file(filepath: str, min_severity: str = "INFO") -> list:
    """对一个 Python 文件执行结构化和模式审查。"""
    path = Path(filepath)
    if not path.exists() or path.suffix != ".py":
        return []

    issues = []
    source = path.read_text(encoding="utf-8")

    # ── 模式审查（AST 不可覆盖的部分）──
    lines = source.split("\n")

    # 1. 裸 except
    for i, line in enumerate(lines, 1):
        if re.search(r'\bexcept\s*:', line) and not line.strip().startswith("#"):
            issues.append({
                "severity": "WARNING",
                "category": "maintainability",
                "line": i,
                "message": f"裸 except: 第{i}行，会吞掉所有异常，建议捕获具体异常类型",
            })

    # 2. 硬编码绝对路径
    for i, line in enumerate(lines, 1):
        if re.search(r'["\']/opt/daily_stock_analysis/', line) and not line.strip().startswith("#"):
            issues.append({
                "severity": "INFO",
                "category": "maintainability",
                "line": i,
                "message": f"硬编码路径 /opt/daily_stock_analysis/，建议参数化",
            })

    # 3. 未保护的除法和 log
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if re.search(r'[\s(]/[\s*]', stripped) and "max(" not in stripped and "1e-" not in stripped:
            # 粗略检查直接除法
            pass  # 太多误报，不如 AST 分析
        # log 调用无 epsilon
        if "np.log(" in stripped and "1e-" not in stripped and "1E-" not in stripped:
            issues.append({
                "severity": "INFO",
                "category": "numerical",
                "line": i,
                "message": f"np.log() 无 epsilon 保护: {stripped[:60]}",
            })

    # 4. 文件读写无异常处理
    for i, line in enumerate(lines, 1):
        if ('open(' in line or '.read_text(' in line or '.read_bytes(' in line) \
                and not line.strip().startswith("#"):
            # 检查是否在 try 块内（粗略）
            preceding = "".join(lines[max(0, i - 5):i])
            if "try:" not in preceding:
                pass  # 误报太多，跳过

    # ── AST 审查 ──
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        issues.append({
            "severity": "CRITICAL",
            "category": "syntax",
            "line": e.lineno or 1,
            "message": f"语法错误: {e.msg}",
        })
        return issues

    for node in ast.walk(tree):
        # 1. except Exception 太宽泛
        if isinstance(node, ast.ExceptHandler):
            if node.type is None:
                issues.append({
                    "severity": "WARNING",
                    "category": "maintainability",
                    "line": node.lineno,
                    "message": f"裸 except（AST确认），会吞所有异常",
                })
            elif isinstance(node.type, ast.Name) and node.type.id == "Exception":
                if node.name is None:
                    issues.append({
                        "severity": "INFO",
                        "category": "maintainability",
                        "line": node.lineno,
                        "message": "捕获 Exception 没有绑定变量 (except Exception:), 丢失异常信息",
                    })

        # 2. 除法检查
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            right = node.right
            if isinstance(right, ast.Constant) and right.value == 0:
                issues.append({
                    "severity": "CRITICAL",
                    "category": "numerical",
                    "line": node.lineno,
                    "message": "除零: 硬编码的 /0",
                })

        # 3. 矩阵运算维度检查：np.linalg.inv
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                if "inv" in func.attr and hasattr(func.value, 'attr'):
                    pass  # 所有 inv 都先假设需要正则化

    # 过滤严重度
    min_idx = SEVERITY_ORDER.get(min_severity, 0)
    issues = [i for i in issues if SEVERITY_ORDER.get(i["severity"], 99) <= min_idx]

    return issues


def scan_directory(dirpath: str, min_severity: str = "INFO") -> dict:
    """递归扫描目录。"""
    results = {}
    base = Path(dirpath)
    for f in sorted(base.rglob("*.py")):
        issues = check_file(str(f), min_severity)
        if issues:
            results[str(f.relative_to(base))] = issues
    return results


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: review_once.py <file.py|dir/> [--severity CRITICAL|WARNING|INFO]")
        sys.exit(1)

    target = sys.argv[1]
    severity = "INFO"
    if "--severity" in sys.argv:
        idx = sys.argv.index("--severity")
        if idx + 1 < len(sys.argv):
            severity = sys.argv[idx + 1]

    p = Path(target)
    if p.is_dir():
        results = scan_directory(target, severity)
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        issues = check_file(target, severity)
        print(json.dumps(issues, ensure_ascii=False, indent=2))

    if not issues:
        print("✅ 程式化检查未发现问题")
