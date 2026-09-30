"""cpp-template-error-decoder：把 C++ 模板“天书”报错翻译成人话的终端工具。

拦截或读取 GCC / Clang / MSVC 编译 C++ 模板时产生的海量报错日志，
调用智谱 GLM-5.3-Flash 提取根因，在终端输出 3 句人话解释 +
对应源码行号 + 修复建议 diff。
"""

from .llm import (
    DEFAULT_MAX_CHARS,
    DEFAULT_MODEL,
    SYSTEM_PROMPT,
    DecoderError,
    GLMDecoder,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MODEL",
    "SYSTEM_PROMPT",
    "DecoderError",
    "GLMDecoder",
    "__version__",
]
