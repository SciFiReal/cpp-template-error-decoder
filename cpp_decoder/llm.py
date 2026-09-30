"""GLM-5.3-Flash 调用核心模块。

把用户收集到的 C++ 模板编译报错日志（GCC / Clang / MSVC）交给 GLM-5.3-Flash，
返回结构化的 Markdown 诊断报告。

本模块刻意保持可离线测试：
- ``zhipuai`` 只在真正发起请求时才 import，单测无需安装该 SDK；
- 日志压缩、错误行定位等纯逻辑函数均可独立测试；
- 发送请求的动作收敛在 :meth:`GLMDecoder._chat`，测试中替换它即可模拟解码。
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MODEL",
    "SYSTEM_PROMPT",
    "DecoderError",
    "GLMDecoder",
    "condense_log",
    "extract_first_error",
    "strip_outer_fence",
]

#: 默认模型（智谱开放平台，https://open.bigmodel.cn ）。
DEFAULT_MODEL = "glm-5.3-flash"

#: 送入 LLM 的日志默认最大字符数（超长会掐头去尾）。
DEFAULT_MAX_CHARS = 12000

SYSTEM_PROMPT = """\
你是一名资深 C++ 编译器诊断专家，精通 GCC / Clang / MSVC 三大编译器的报错格式，
以及 STL、模板元编程（TMP/SFINAE）、C++20 Concepts 的内部实现细节。

用户会粘贴一段被模板“天书”刷屏的编译器输出（可能长达几千行）。
你的任务：穿过海量实例化噪音，找到真正的根因，用中文输出一份严格遵循下述模板的
Markdown 诊断报告。三个二级标题必须一字不差；不要输出模板以外的任何内容，
不要寒暄，不要把整份报告包进代码围栏。

## 【根本原因】
用不超过 3 句中文大白话解释根因（第一句必须能独立看懂）。
必须把 STL 内部符号翻译成人话，例如：
- std::_Rb_tree / _Rb_tree_iterator → std::map / std::set 底层的红黑树实现
- std::__cxx11::basic_string<char> → std::string
- _Iter_less_iter / std::less<T> → 比较 key 时调用 operator<
- std::enable_if<false, ...>::type → SFINAE 探测失败
明确指出：哪个模板 / STL 组件，哪个类型，缺了什么（运算符 / 构造函数 / 约束 / 类型别名 / const 等）。

## 【错误代码位置】
按如下格式输出（行号取自用户自己的代码；若日志无法确定则明确说明）：
- 文件: `main.cpp`
- 行号: 12
出错的用户源码行（从日志中还原，不要臆造）：
```cpp
std::map<Node, int> scores;
```

## 【推荐修复方案】
先用一句话说明修复思路，再给出最小可用、可直接复制的代码 diff：
```diff
- std::map<Node, int> scores;
+ bool operator<(const Node& a, const Node& b) { return a.id < b.id; }  // 或改用带比较器的 map
+ std::map<Node, int> scores;
```

识别规则（必须遵守）：
1. GCC / Clang：真正的错误是第一条 `error:` 行；其后大量 `required from here` /
   `in instantiation of ... required from` 只是实例化路径，不是多个错误。
   MSVC：根因通常是第一条 `error Cxxxx`，后续错误多为连锁反应。
2. 常见模式速查（命中时必须在【根本原因】中点名）：
   - `no match for 'operator<'`，上下文出现 std::map / std::set / _Rb_tree / std::less
     → 作为 key 的自定义类型缺少 `operator<`（或未传入比较器）。
   - `comparison object must be invocable as const` → 比较器不是 const 可调用。
   - `no matching function for call to 'std::vector<...>::push_back(...)'` 或
     `no known conversion for argument 1` → 元素类型与容器声明类型不匹配。
   - `no match for 'operator=='` 且涉及 std::find → 元素类型缺少 `operator==`，
     或对 map / set 应改用成员函数 find()。
   - `template argument deduction/substitution failed` 且候选重载被静默淘汰
     → SFINAE 失败，指出是哪个 enable_if / 条件不满足。
   - `constraints not satisfied` / `the required type ... does not satisfy ...` /
     MSVC 涉及 concept 的 `error C2676` / `C7602` → 指出具体哪个 concept 的哪个原子约束为
     false、为什么。
   - `no type named 'type' in 'struct std::enable_if<false, ...>'` / void_t /
     `std::iterator_traits` 报错 → trait 探测失败，指出缺失的嵌套类型或别名。
   - `use of deleted function '...'` → 指出是哪个成员（const / 引用 / unique_ptr 等）
     导致拷贝或移动构造被删除。
   - `type/value mismatch at argument N in template parameter list` → 模板实参个数或类型不匹配。
   - `undefined reference to ...` → 链接期错误：模板定义没有放在头文件，或缺少显式实例化。
3. 行号优先使用用户代码（如 main.cpp）的行，禁止把系统头文件
   （/usr/include、bits/、MSVC 或 Visual Studio 安装目录）中的行号当作错误位置。
4. 修复 diff 必须是最小修改、与用户代码风格一致；多个等价方案时选最惯用的一个。
5. 全程使用中文；代码、类型名、报错关键词保留英文原文。
"""

# GCC / Clang：`main.cpp:12:26: error: ...`（兼容 Windows 盘符路径）。
_GCC_CLANG_ERROR_RE = re.compile(
    r"^.*?:\d+:\d+:\s*(?:fatal\s+)?error:.*$", re.MULTILINE
)
# MSVC：`main.cpp(12): error C2676: ...`。
_MSVC_ERROR_RE = re.compile(
    r"^.*?\(\d+\):\s*(?:fatal\s+)?error\s+C\d+:.*$", re.MULTILINE
)

# LLM 偶尔会用一整个 ```markdown 围栏包住报告，这里识别并剥掉。
_OUTER_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*\n(.*?)\n```\s*$", re.DOTALL)

_TRUNCATE_MARK = "\n... [中间省略 {skipped} 行] ...\n"


def extract_first_error(log: str) -> str:
    """从日志中定位第一条 ``error:`` / ``error Cxxxx`` 行，找不到返回空串。

    根因几乎总是出现在第一条错误行上，其余多为实例化噪音，因此把它作为
    提示交给模型可以显著提高解码准确率。
    """
    for pattern in (_GCC_CLANG_ERROR_RE, _MSVC_ERROR_RE):
        match = pattern.search(log)
        if match:
            return match.group(0).strip()
    return ""


def condense_log(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """把超长日志压缩到 ``max_chars`` 以内：保留前 60% 与后 40%。

    GCC / Clang 的第一条错误行通常靠前，实例化链条的收尾（`required from
    here`，指向用户代码）通常靠后，所以掐头去尾而不是直接截断。
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head_len = int(max_chars * 0.6)
    tail_len = max_chars - head_len
    skipped = text.count("\n", head_len, len(text) - tail_len)
    return text[:head_len] + _TRUNCATE_MARK.format(skipped=skipped) + text[-tail_len:]


def strip_outer_fence(text: str) -> str:
    """剥掉模型误加的最外层 ``` 围栏，保证报告以 ``## 【...】`` 开头。"""
    match = _OUTER_FENCE_RE.match(text.strip())
    return match.group(1) if match else text


class DecoderError(RuntimeError):
    """配置缺失（无 API Key）或调用 LLM 失败时的业务异常。"""


class GLMDecoder:
    """GLM-5.3-Flash 解码器：把模板报错日志变成 Markdown 诊断报告。

    Usage::

        decoder = GLMDecoder()          # 从环境变量 ZHIPUAI_API_KEY 读 Key
        report = decoder.decode(log)    # report 是结构化 Markdown 字符串
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        temperature: float = 0.2,
        max_output_tokens: int = 2048,
    ) -> None:
        self.api_key = api_key or os.getenv("ZHIPUAI_API_KEY") or os.getenv("GLM_API_KEY")
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self._client = None

    @property
    def client(self):
        """惰性创建 zhipuai 客户端；Key 缺失 / SDK 未安装时抛 :class:`DecoderError`。"""
        if self._client is None:
            if not self.api_key:
                raise DecoderError(
                    "未找到智谱 API Key。请设置环境变量 ZHIPUAI_API_KEY（或 GLM_API_KEY），"
                    "也可以用 --api-key 传入；Key 可在 https://open.bigmodel.cn 免费申请。"
                )
            try:
                from zhipuai import ZhipuAI
            except ImportError as exc:  # pragma: no cover - 依赖在 pyproject 中声明
                raise DecoderError(
                    "缺少依赖 zhipuai，请执行：pip install zhipuai"
                ) from exc
            self._client = ZhipuAI(api_key=self.api_key)
        return self._client

    def build_messages(
        self, error_log: str, max_chars: int = DEFAULT_MAX_CHARS
    ) -> List[Dict[str, str]]:
        """构造对话消息：系统提示 +（压缩后的日志 + 首条错误行提示）。"""
        condensed = condense_log(error_log, max_chars=max_chars)
        first_error = extract_first_error(error_log) or "（未自动定位到错误行，请分析完整日志）"
        user_content = (
            "请分析以下 C++ 编译器输出，并严格按系统指令输出诊断报告。\n\n"
            f"自动捕获的第一条错误行：\n{first_error}\n\n"
            "完整编译日志：\n"
            f"~~~text\n{condensed}\n~~~"
        )
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

    def _chat(self, messages: List[Dict[str, str]]) -> str:
        """真正发起一次对话请求；单测通过替换本方法离线模拟解码。"""
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_output_tokens,
        )
        return (response.choices[0].message.content or "").strip()

    def decode(self, error_log: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
        """解码入口：编译日志 → 结构化 Markdown 报告。"""
        if not error_log or not error_log.strip():
            raise DecoderError("编译日志为空，无法解码。")
        messages = self.build_messages(error_log, max_chars=max_chars)
        reply = self._chat(messages)
        if not reply:
            raise DecoderError("GLM 返回了空内容，请稍后重试或更换模型。")
        return strip_outer_fence(reply)
