"""pytest 测试：预置两段真实的 C++ 模板编译报错，模拟 GLM 解码过程。

全部测试离线运行：不安装 zhipuai、不请求网络，
通过替换 ``GLMDecoder._chat`` 来模拟模型返回。
"""

from __future__ import annotations

from io import StringIO

import pytest
from click.testing import CliRunner
from rich.console import Console

from cpp_decoder import __version__
from cpp_decoder.cli import main
from cpp_decoder.formatter import render_report, split_sections
from cpp_decoder.llm import (
    SYSTEM_PROMPT,
    DecoderError,
    GLMDecoder,
    condense_log,
    extract_first_error,
    strip_outer_fence,
)

# ---------------------------------------------------------------------------
# 预置报错 1：std::map 的自定义 key 类型缺少 operator<（GCC 11）
# ---------------------------------------------------------------------------

MAP_ERROR_LOG = """\
$ g++ -std=c++17 main.cpp
In file included from /usr/include/c++/11/map:60,
                 from main.cpp:2:
/usr/include/c++/11/bits/stl_function.h: In instantiation of 'constexpr bool std::less<_Tp>::operator()(const _Tp&, const _Tp&) const [with _Tp = Node]':
/usr/include/c++/11/bits/stl_tree.h:1965:32:   required from 'std::pair<std::_Rb_tree_iterator<_Val>, bool> std::_Rb_tree<_Key, _Val, _KeyOfValue, _Compare, _Alloc>::_M_insert_unique(_Arg&&) [with _Arg = const value_type&; _Key = Node; _Val = std::pair<const Node, int>; _KeyOfValue = std::_Select1st<std::pair<const Node, int> >; _Compare = std::less<Node>; _Alloc = std::allocator<std::pair<const Node, int> >]'
/usr/include/c++/11/bits/stl_map.h:508:32:   required from 'std::map<_Key, _Tp, _Compare, _Alloc>::mapped_type& std::map<_Key, _Tp, _Compare, _Alloc>::operator[](const key_type&) [with _Key = Node; _Tp = int; _Compare = std::less<Node>; _Alloc = std::allocator<std::pair<const Node, int> >]'
main.cpp:13:12:   required from here
/usr/include/c++/11/bits/stl_function.h:408:20: error: no match for 'operator<' (operand types are 'const Node' and 'const Node')
  408 |       { return __x < __y; }
      |               ~~~~~^~~~~
main.cpp:8:10: note:   'const Node' is not derived from 'const std::___pair_type'
main.cpp: In function 'int main()':
main.cpp:12:26: error: no match for 'operator<' (operand types are 'const Node' and 'const Node')
   12 |     std::map<Node, int> scores;
      |                          ^~~~~~
"""

# ---------------------------------------------------------------------------
# 预置报错 2：std::vector<int> 塞进 std::string，元素类型不匹配（GCC 11）
# ---------------------------------------------------------------------------

VECTOR_ERROR_LOG = """\
$ g++ -std=c++17 main.cpp
In file included from /usr/include/c++/11/vector:67,
                 from main.cpp:1:
/usr/include/c++/11/bits/stl_vector.h: In instantiation of 'void std::vector<_Tp, _Alloc>::emplace_back(_Args&& ...) [with _Args = {std::__cxx11::basic_string<char, std::char_traits<char>, std::allocator<char> >&}; _Tp = int; _Alloc = std::allocator<int>]':
/usr/include/c++/11/bits/stl_vector.h:1989:30:   required from 'void std::vector<_Tp, _Alloc>::_M_realloc_insert(_Args&& ...) [with _Args = {std::__cxx11::basic_string<char, std::char_traits<char>, std::allocator<char> >&}; _Tp = int; _Alloc = std::allocator<int>]'
main.cpp: In function 'int main()':
main.cpp:9:7: error: no matching function for call to 'std::vector<int>::push_back(std::__cxx11::basic_string<char>)'
    9 |     v.push_back(name);
      |     ~~^~~~~~~~~~~~~~~
In file included from /usr/include/c++/11/vector:67,
                 from main.cpp:1:
/usr/include/c++/11/bits/stl_vector.h:1187:7: note: candidate: 'void std::vector<_Tp, _Alloc>::push_back(const value_type&) [with _Tp = int; _Alloc = std::allocator<int>]'
 1187 |       push_back(const value_type& __x)
      |       ^~~~~~~~~
/usr/include/c++/11/bits/stl_vector.h:1187:40: note:   no known conversion for argument 1 from 'std::string' {aka 'std::__cxx11::basic_string<char>'} to 'const int&'
"""

# ---------------------------------------------------------------------------
# 模拟 GLM 返回的结构化报告（与 System Prompt 约定的三段式一致）
# ---------------------------------------------------------------------------

MOCK_REPORT = """\
## 【根本原因】
`std::map` 需要比较 key，而你的 `Node` 结构体没有定义 `operator<`。
map 底层的红黑树（std::_Rb_tree）插入时会对两个 Node 调用 `operator<`。
补上比较运算符或提供比较器即可。

## 【错误代码位置】
- 文件: `main.cpp`
- 行号: 12
```cpp
std::map<Node, int> scores;
```

## 【推荐修复方案】
给 Node 重载 `operator<`，让红黑树可以比较两个节点：
```diff
  struct Node { int id; std::string name; };
+ bool operator<(const Node& a, const Node& b) { return a.id < b.id; }
  std::map<Node, int> scores;
```
"""


@pytest.fixture
def mock_chat(monkeypatch):
    """替换 GLMDecoder._chat，记录请求消息并返回预置报告。"""
    calls = {}

    def fake_chat(self, messages):
        calls["messages"] = messages
        return MOCK_REPORT

    monkeypatch.setattr(GLMDecoder, "_chat", fake_chat)
    return calls


# ---------------------------------------------------------------------------
# llm.py：日志压缩 / 错误行定位 / 围栏剥离
# ---------------------------------------------------------------------------


def test_condense_log_short_passthrough():
    text = "main.cpp:1:1: error: boom\n"
    assert condense_log(text, max_chars=12000) == text


def test_condense_log_truncates_middle():
    text = "\n".join(f"line {i}" for i in range(1000))
    out = condense_log(text, max_chars=400)
    assert len(out) < len(text)
    assert "[中间省略" in out
    assert out.startswith("line 0")
    assert "line 999" in out


def test_extract_first_error_gcc():
    first = extract_first_error(MAP_ERROR_LOG)
    assert first.startswith(
        "/usr/include/c++/11/bits/stl_function.h:408:20: error: no match for 'operator<'"
    )


def test_extract_first_error_msvc():
    log = (
        "main.cpp(12): error C2676: binary '<': 'const Node' does not define "
        "this operator or a conversion to a type acceptable to the predefined operator\n"
    )
    assert extract_first_error(log).startswith("main.cpp(12): error C2676")


def test_extract_first_error_no_match():
    assert extract_first_error("hello world\nnothing to see") == ""


def test_strip_outer_fence():
    wrapped = "```markdown\n## 【根本原因】\n正文\n```"
    assert strip_outer_fence(wrapped) == "## 【根本原因】\n正文"
    assert strip_outer_fence("## 【根本原因】\n正文") == "## 【根本原因】\n正文"


def test_system_prompt_covers_common_template_patterns():
    for keyword in (
        "std::vector",
        "std::map",
        "std::find",
        "SFINAE",
        "Concepts",
        "operator<",
        "_Rb_tree",
    ):
        assert keyword in SYSTEM_PROMPT, f"System Prompt 缺少关键词: {keyword}"


# ---------------------------------------------------------------------------
# llm.py：模拟解码
# ---------------------------------------------------------------------------


def test_decode_builds_prompt_and_returns_report(mock_chat):
    decoder = GLMDecoder(api_key="test-key")
    report = decoder.decode(MAP_ERROR_LOG)
    assert report == MOCK_REPORT

    messages = mock_chat["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    # 用户提示里必须带原始日志与首条错误行提示
    assert "operator<" in messages[1]["content"]
    assert "std::map<Node, int> scores" in messages[1]["content"]


def test_decode_sends_vector_log_to_llm(mock_chat):
    decoder = GLMDecoder(api_key="test-key")
    decoder.decode(VECTOR_ERROR_LOG)
    content = mock_chat["messages"][1]["content"]
    assert "push_back" in content
    assert "no known conversion for argument 1" in content


def test_decode_rejects_empty_log():
    decoder = GLMDecoder(api_key="test-key")
    with pytest.raises(DecoderError):
        decoder.decode("   \n  ")


def test_decode_condenses_huge_log(mock_chat):
    decoder = GLMDecoder(api_key="test-key")
    huge_log = VECTOR_ERROR_LOG + "\n" + ("x" * 50000)
    decoder.decode(huge_log, max_chars=2000)
    assert len(mock_chat["messages"][1]["content"]) < 5000


def test_client_requires_api_key(monkeypatch):
    monkeypatch.delenv("ZHIPUAI_API_KEY", raising=False)
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    decoder = GLMDecoder()
    with pytest.raises(DecoderError, match="API Key"):
        _ = decoder.client


# ---------------------------------------------------------------------------
# formatter.py：板块切分与富文本渲染
# ---------------------------------------------------------------------------


def test_split_sections():
    sections = split_sections(MOCK_REPORT)
    names = [name for name, _ in sections]
    assert names == ["根本原因", "错误代码位置", "推荐修复方案"]
    bodies = dict(sections)
    assert "红黑树" in bodies["根本原因"]
    assert "行号: 12" in bodies["错误代码位置"]
    assert "operator<" in bodies["推荐修复方案"]


def test_split_sections_no_match():
    assert split_sections("没有板块标题的普通文本") == []


def test_render_report_panels():
    buffer = StringIO()
    console = Console(file=buffer, width=120, force_terminal=False)
    render_report(MOCK_REPORT, console=console)
    output = buffer.getvalue()
    assert "根本原因" in output
    assert "错误代码位置" in output
    assert "推荐修复方案" in output


def test_render_report_fallback_without_sections():
    buffer = StringIO()
    console = Console(file=buffer, width=120, force_terminal=False)
    render_report("模型没有按格式输出的一段话", console=console)
    assert "模型没有按格式输出的一段话" in buffer.getvalue()


# ---------------------------------------------------------------------------
# cli.py：管道输入 / 文件输入 / 空输入
# ---------------------------------------------------------------------------


def test_cli_reads_piped_input(mock_chat):
    runner = CliRunner()
    result = runner.invoke(main, [], input=MAP_ERROR_LOG)
    assert result.exit_code == 0, result.output
    assert "根本原因" in result.output
    assert "推荐修复方案" in result.output


def test_cli_reads_log_file(mock_chat, tmp_path):
    log_file = tmp_path / "build.log"
    log_file.write_text(VECTOR_ERROR_LOG, encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(main, ["-f", str(log_file)])
    assert result.exit_code == 0, result.output
    content = mock_chat["messages"][1]["content"]
    assert "push_back" in content


def test_cli_empty_input_fails():
    runner = CliRunner()
    result = runner.invoke(main, [], input="")
    assert result.exit_code != 0
    assert "输入为空" in result.output


def test_cli_raw_mode_outputs_markdown(mock_chat):
    runner = CliRunner()
    result = runner.invoke(main, ["--raw"], input=MAP_ERROR_LOG)
    assert result.exit_code == 0, result.output
    assert result.output.strip() == MOCK_REPORT.strip()


def test_cli_version():
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output
