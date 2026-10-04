#!/usr/bin/env python3
"""unitgen —— 用 LLM 生成单元测试，并真实运行、给出报告。

闭环：ast 解析 → LLM 生成 → 沙盒运行 → 报告。
只用 Python 标准库。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error

VERSION = "0.1.0"
DEFAULT_TIMEOUT = 30
DEFAULT_MODEL = "gpt-4o-mini"


# ---------------------------------------------------------------- 模块解析

def analyze_module(path):
    """用 ast 解析模块，返回 (源码, 函数信息列表)。LLM 不参与这一步。"""
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as e:
        print(f"error: 被测模块有语法错误: {e}", file=sys.stderr)
        sys.exit(2)
    funcs = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            if node.args.vararg:
                args.append("*" + node.args.vararg.arg)
            if node.args.kwarg:
                args.append("**" + node.args.kwarg.arg)
            funcs.append({
                "name": node.name,
                "args": args,
                "async": isinstance(node, ast.AsyncFunctionDef),
                "docstring": ast.get_docstring(node) or "",
                "lineno": node.lineno,
            })
    return source, funcs


# ---------------------------------------------------------------- Prompt 构建

def build_prompt(module_name, source, funcs, only_func=None):
    targets = [f for f in funcs if f["name"] == only_func] if only_func else funcs
    lines = []
    for f in targets:
        sig = f"{f['name']}({', '.join(f['args'])})"
        if f["async"]:
            sig = "async " + sig
        doc = f"：{f['docstring']}" if f["docstring"] else ""
        lines.append(f"- {sig}{doc}")
    func_list = "\n".join(lines) if lines else "(无顶层函数)"

    return f"""你是一位资深 Python 测试工程师。请为下面的 Python 模块编写单元测试。

模块名：{module_name}
待测函数：
{func_list}

模块源码：
```python
{source}
```

要求（必须遵守）：
1. 只输出 Python 代码，不要任何解释文字。
2. 每个测试是一个以 test_ 开头的普通函数，用 plain assert 做断言。
3. 不要 import pytest / unittest / nose 等任何测试框架；只用 Python 标准库。
4. 用 `from {module_name} import ...` 导入被测模块。
5. 覆盖正常情况、边界情况和异常情况。断言异常时不要用 pytest.raises，用下面这种写法：
```python
def test_divide_by_zero():
    try:
        divide(1, 0)
    except ValueError:
        pass
    else:
        assert False, "应该抛出 ValueError"
```
6. 测试函数互相独立，不依赖执行顺序，不读写被测模块目录之外的文件。
7. 如果某个函数是 async 的，测试里用 asyncio.run() 调用它。
"""


# ---------------------------------------------------------------- LLM 调用

def get_config():
    key = os.environ.get("UNITGEN_API_KEY") or os.environ.get("OPENAI_API_KEY")
    base = (os.environ.get("UNITGEN_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1").rstrip("/")
    model = (os.environ.get("UNITGEN_MODEL")
             or os.environ.get("OPENAI_MODEL")
             or DEFAULT_MODEL)
    return key, base, model


def call_llm(prompt, key, base, model):
    url = base + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你只输出 Python 测试代码，不输出解释。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        print(f"error: API 请求失败 (HTTP {e.code}): {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"error: 网络请求失败: {e.reason}", file=sys.stderr)
        sys.exit(1)
    except (json.JSONDecodeError, KeyError) as e:
        print(f"error: API 返回无法解析: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        print("error: API 返回结构异常，缺少 choices/message", file=sys.stderr)
        sys.exit(1)


def extract_code(text):
    """从 LLM 回复里剥出代码：优先取 ```python 围栏，否则全文。"""
    start = text.find("```")
    if start == -1:
        return text.strip() + "\n"
    first_nl = text.find("\n", start)
    if first_nl == -1:
        return text.strip() + "\n"
    end = text.find("```", first_nl)
    code = text[first_nl + 1:end] if end != -1 else text[first_nl + 1:]
    return code.strip() + "\n"


# ---------------------------------------------------------------- 安全检查

def check_test_imports(test_source, module_name):
    """检查生成的测试是否导入了非标准库模块。返回坏模块名列表。"""
    tree = ast.parse(test_source)
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mods.add(node.module.split(".")[0])
    stdlib = set(sys.stdlib_module_names)  # Python 3.10+
    bad = sorted(m for m in mods if m not in stdlib and m != module_name)
    return bad


# ---------------------------------------------------------------- 沙盒运行

RUNNER_CODE = r'''
import importlib.util
import json
import sys
import traceback

path = sys.argv[1]
try:
    spec = importlib.util.spec_from_file_location("generated_tests", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
except Exception:
    print(json.dumps({"fatal": traceback.format_exc(limit=5)[-1500:]}))
    sys.exit(0)

results = []
names = [n for n in dir(mod) if n.startswith("test_") and callable(getattr(mod, n))]
for name in sorted(names):
    try:
        getattr(mod, name)()
        results.append({"name": name, "status": "pass"})
    except AssertionError as e:
        results.append({"name": name, "status": "fail",
                        "message": str(e)[:500] or "assert 失败（无消息）"})
    except Exception:
        results.append({"name": name, "status": "error",
                        "message": traceback.format_exc(limit=5)[-800:]})
print(json.dumps({"tests": results}))
'''


def run_tests_in_sandbox(module_path, module_name, test_source, timeout):
    """把被测模块和测试文件拷进临时目录，子进程运行，返回结果列表。"""
    tmpdir = tempfile.mkdtemp(prefix="unitgen_")
    try:
        shutil.copy2(module_path, os.path.join(tmpdir, os.path.basename(module_path)))
        test_path = os.path.join(tmpdir, "test_generated.py")
        with open(test_path, "w", encoding="utf-8") as f:
            f.write(test_source)
        try:
            proc = subprocess.run(
                [sys.executable, "-c", RUNNER_CODE, test_path],
                cwd=tmpdir, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return [{"name": "<整轮运行>", "status": "timeout",
                     "message": f"超过 {timeout} 秒未结束，已终止"}], True
        out = proc.stdout.strip()
        if not out:
            msg = (proc.stderr.strip()[-500:] or "子进程无输出") if proc.returncode else "无测试输出"
            return [{"name": "<整轮运行>", "status": "error", "message": msg}], False
        try:
            data = json.loads(out.splitlines()[-1])
        except json.JSONDecodeError:
            return [{"name": "<整轮运行>", "status": "error",
                     "message": "测试运行器输出无法解析"}], False
        if "fatal" in data:
            return [{"name": "<加载测试文件>", "status": "error",
                     "message": data["fatal"]}], False
        return data.get("tests", []), False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------- 报告

STATUS_LABEL = {"pass": "通过", "fail": "失败", "error": "出错", "timeout": "超时"}


def print_report(results, timed_out, as_json, module_name):
    summary = {"total": len(results),
               "passed": sum(1 for r in results if r["status"] == "pass"),
               "failed": sum(1 for r in results if r["status"] == "fail"),
               "errors": sum(1 for r in results if r["status"] == "error"),
               "timeouts": sum(1 for r in results if r["status"] == "timeout")}
    if as_json:
        print(json.dumps({"module": module_name, "tests": results,
                          "summary": summary}, ensure_ascii=False, indent=2))
        return summary
    for r in results:
        label = STATUS_LABEL.get(r["status"], r["status"])
        line = f"[{label}] {r['name']}"
        if r.get("message"):
            first = r["message"].strip().splitlines()[0][:200]
            line += f"：{first}"
        print(line)
    print("-" * 40)
    print(f"共 {summary['total']} 个测试："
          f"{summary['passed']} 通过，{summary['failed']} 失败，"
          f"{summary['errors']} 出错，{summary['timeouts']} 超时")
    return summary


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="unitgen",
        description="用 LLM 为 Python 模块生成单元测试，并真实运行、给出报告。",
    )
    ap.add_argument("module", help="被测模块路径，如 examples/calc.py")
    ap.add_argument("--func", help="只为指定函数生成测试")
    ap.add_argument("-o", "--output", help="把生成的测试写入文件（默认只放临时目录运行）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要发送的 prompt，不联网")
    ap.add_argument("--no-run", action="store_true", help="只打印生成的测试，不运行")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出测试结果")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                    help=f"单轮运行超时秒数（默认 {DEFAULT_TIMEOUT}）")
    ap.add_argument("--version", action="version", version="unitgen " + VERSION)
    args = ap.parse_args(argv)

    if not os.path.isfile(args.module):
        print(f"error: 找不到文件: {args.module}", file=sys.stderr)
        return 2
    module_name = os.path.splitext(os.path.basename(args.module))[0]

    source, funcs = analyze_module(args.module)
    if args.func and not any(f["name"] == args.func for f in funcs):
        print(f"error: 模块里没有函数 {args.func!r}，可用函数: "
              + ", ".join(f["name"] for f in funcs), file=sys.stderr)
        return 2

    prompt = build_prompt(module_name, source, funcs, args.func)

    if args.dry_run:
        print(prompt)
        return 0

    key, base, model = get_config()
    if not key:
        print("error: 未找到 API key。请设置环境变量 UNITGEN_API_KEY 或 OPENAI_API_KEY。",
              file=sys.stderr)
        return 2

    raw = call_llm(prompt, key, base, model)
    test_source = extract_code(raw)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(test_source)
        print(f"测试已写入: {args.output}")

    if args.no_run:
        if not args.output:
            print(test_source)
        return 0

    # 生成的测试先过语法关：报错要干净，不要 traceback 炸屏
    try:
        ast.parse(test_source)
    except SyntaxError as e:
        print(f"error: LLM 生成的测试文件有语法错误，已跳过运行: {e}", file=sys.stderr)
        return 1

    bad = check_test_imports(test_source, module_name)
    if bad:
        print(f"警告: 生成的测试导入了非标准库模块 {bad}，为安全起见已跳过运行。",
              file=sys.stderr)
        print("（沙盒只允许标准库；详见 README 的沙盒说明）", file=sys.stderr)
        return 2

    results, timed_out = run_tests_in_sandbox(
        args.module, module_name, test_source, args.timeout)
    summary = print_report(results, timed_out, args.json, module_name)
    if summary["failed"] or summary["errors"] or summary["timeouts"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
