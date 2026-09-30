"""``cpp-decode`` 命令行入口。

支持两种输入方式::

    g++ main.cpp 2>&1 | cpp-decode          # 管道
    cpp-decode -f build.log                 # 日志文件
"""

from __future__ import annotations

import locale
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.text import Text

from . import __version__
from .formatter import render_report
from .llm import DEFAULT_MAX_CHARS, DEFAULT_MODEL, DecoderError, GLMDecoder, extract_first_error

__all__ = ["main"]

console = Console()

USAGE_EXAMPLES = """\
用法示例：
  g++ main.cpp 2>&1 | cpp-decode
  clang++ -std=c++20 main.cpp 2>&1 | cpp-decode
  cl /std:c++20 /EHsc main.cpp 2>&1 | cpp-decode
  cpp-decode -f build.log
"""


def _ensure_safe_stdout() -> None:
    """Windows 默认 GBK 控制台无法编码 emoji 时降级为占位符，避免崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (OSError, ValueError):  # pragma: no cover - 个别流不支持
                pass


def _decode_bytes(data: bytes) -> str:
    """编译器输出编码不统一：GCC / Clang 多为 UTF-8，MSVC 跟随系统代码页。"""
    for encoding in ("utf-8", locale.getpreferredencoding(False)):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _read_input(log_file: Optional[Path]) -> str:
    """读取编译日志：优先 -f 文件，否则从标准输入管道读取。"""
    if log_file is not None:
        return log_file.read_text(encoding="utf-8", errors="replace")
    if sys.stdin.isatty():
        raise click.UsageError("没有检测到输入日志。\n\n" + USAGE_EXAMPLES)
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        return _decode_bytes(buffer.read())
    return sys.stdin.read()


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "-f",
    "--file",
    "log_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="读取编译日志文件（如 -f build.log）；缺省时从标准输入管道读取。",
)
@click.option("--model", default=DEFAULT_MODEL, show_default=True, help="智谱 GLM 模型名称。")
@click.option(
    "--api-key",
    envvar="ZHIPUAI_API_KEY",
    help="智谱开放平台 API Key（缺省读环境变量 ZHIPUAI_API_KEY）。",
)
@click.option(
    "--max-chars",
    default=DEFAULT_MAX_CHARS,
    show_default=True,
    help="送入 LLM 的日志最大字符数，超长部分自动掐头去尾。",
)
@click.option(
    "--raw",
    is_flag=True,
    help="输出原始 Markdown，不渲染富文本面板（便于脚本二次处理）。",
)
@click.version_option(__version__, "-V", "--version", prog_name="cpp-decode")
def main(
    log_file: Optional[Path],
    model: str,
    api_key: Optional[str],
    max_chars: int,
    raw: bool,
) -> None:
    """把 C++ 模板编译报错（天书）解码成 3 句人话 + 行号 + 修复 diff。

    依赖智谱 GLM-5.3-Flash，请先设置环境变量 ZHIPUAI_API_KEY。
    """
    _ensure_safe_stdout()
    text = _read_input(log_file)
    if not text.strip():
        raise click.UsageError("输入为空：没有读到任何编译日志内容。")

    first_error = extract_first_error(text)
    if not raw and first_error:
        console.print(Text(f"🎯 已捕获第一条错误行：{first_error}", style="dim"))

    decoder = GLMDecoder(api_key=api_key, model=model)
    try:
        if raw:
            report = decoder.decode(text, max_chars=max_chars)
        else:
            with console.status(f"{model} 正在解码模板报错…", spinner="dots"):
                report = decoder.decode(text, max_chars=max_chars)
    except DecoderError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:  # 网络抖动 / SDK 异常等
        raise click.ClickException(f"调用 GLM API 失败：{exc}") from exc

    if raw:
        click.echo(report)
    else:
        console.print()
        render_report(report, console=console)


if __name__ == "__main__":  # pragma: no cover
    main()
