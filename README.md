# unitgen

用 LLM 生成单元测试，**然后真实运行它们**，给出通过/失败报告。

想法很简单：让 AI 写测试很容易，但 AI 写的测试对不对，只有跑过才知道。
unitgen 把「生成 → 执行 → 报告」做成一个闭环：

```
unitgen examples/calc.py
```

1. 用 `ast` 解析你的模块，列出函数、签名、docstring（这一步不用 LLM，又快又准）
2. 把源码 + 函数清单发给 OpenAI 兼容的 `/chat/completions`，让它写测试文件
3. 测试写进临时目录的沙盒（被测模块复制进去），子进程真实跑一遍
4. 逐条打印 通过/失败/出错/超时 + 汇总

## 安装

零依赖，Python 3.10+：

```bash
git clone https://github.com/ljiang9/unitgen.git
cd unitgen
python3 -m unitgen --help
```

## 配置

```bash
export UNITGEN_API_KEY="你的 key"          # 或 OPENAI_API_KEY
export UNITGEN_BASE_URL="https://..."      # 可选，默认 api.openai.com/v1
export UNITGEN_MODEL="gpt-4o-mini"         # 可选
```

任何 OpenAI 兼容的接口都能用（DeepSeek / Qwen / 本地 Ollama 等）。

## 用法

```bash
# 完整闭环：生成 → 运行 → 报告
unitgen examples/calc.py

# 只给 divide 写测试
unitgen examples/calc.py --func divide

# 把生成的测试存下来（同时也会运行）
unitgen examples/calc.py -o test_out.py

# 只看 prompt，不联网（调 prompt 时用）
unitgen examples/calc.py --dry-run

# 只生成不运行
unitgen examples/calc.py --no-run

# JSON 输出，方便接 CI
unitgen examples/calc.py --json

# 单轮运行超时（默认 30 秒）
unitgen examples/calc.py --timeout 10
```

示例输出：

```
[通过] test_add_basic
[通过] test_add_negative
[失败] test_divide_by_zero：应该抛出 ValueError
----------------------------------------
共 3 个测试：2 通过，1 失败，0 出错，0 超时
```

## 沙盒说明（诚实版）

生成的测试是 LLM 写的、不可信的代码，所以运行在沙盒里：

- 被测模块 + 测试文件复制到**临时目录**运行，跑完删除
- 整轮运行有 **30 秒超时**（`--timeout` 可调），超时直接杀掉子进程
- 生成的测试如果 `import` 了**非标准库模块**，会警告并**跳过运行**（用 `sys.stdlib_module_names` 判定）
- 生成的测试有**语法错误**时直接报干净的中文错误，不炸 traceback

但沙盒是 best-effort，不是安全边界：恶意测试在 30 秒内仍可能做坏事
（删临时目录里的文件、发网络请求等）。**不要对完全不可信的模块用 unitgen**，
生产环境请用真正的沙箱（容器/虚拟机）。

## 已知局限

- **生成的测试可能是错的**——这正是要运行它们的原因。失败≠你的代码有 bug，
  可能是 AI 的测试写错了，请人工看一眼失败信息。
- 沙盒是 best-effort（见上），不是安全承诺。
- 函数解析只看模块顶层 `def`，类方法暂不支持（`--func` 也只匹配顶层函数）。
- 超时是整轮测试共用一个计时器，不是单条测试。

## License

MIT
