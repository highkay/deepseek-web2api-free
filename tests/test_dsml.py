"""Unit tests for tool_dsml parser/builder."""
import pytest

from tool_dsml import (
    parse_dsml_tool_calls,
    strip_dsml_markup,
    _auto_type,
    build_dsml_tool_prompt,
)


def test_strip_dsml_markup_basic():
    s = "<|DSML|tool_calls>...</|DSML|tool_calls>"
    out = strip_dsml_markup(s)
    assert out == "<tool_calls>...</tool_calls>"


def test_strip_dsml_markup_preserves_inner_text():
    s = "before <|DSML|tool_calls>x</|DSML|tool_calls> after"
    out = strip_dsml_markup(s)
    assert out == "before <tool_calls>x</tool_calls> after"


def test_strip_dsml_markup_preserves_unknown_tags():
    s = "a <b>kept</b> c"
    out = strip_dsml_markup(s)
    assert out == s


def test_parse_dsml_tool_calls_basic():
    text = (
        "Some intro\n"
        "<tool_calls>"
        '<invoke name="get_weather">'
        '<parameter name="city"><![CDATA[Beijing]]></parameter>'
        "</invoke>"
        "</tool_calls>\n"
        "Some outro"
    )
    tcs, cleaned = parse_dsml_tool_calls(text, ["get_weather"])
    assert len(tcs) == 1
    assert tcs[0]["function"]["name"] == "get_weather"
    assert '"city"' in tcs[0]["function"]["arguments"]
    assert '"Beijing"' in tcs[0]["function"]["arguments"]
    # Cleaned text should not contain any DSML markup.
    assert "<tool_calls>" not in cleaned
    assert "<invoke" not in cleaned
    assert "Some intro" in cleaned
    assert "Some outro" in cleaned


def test_parse_dsml_tool_calls_dsml_prefix():
    text = (
        "<|DSML|tool_calls>\n"
        '<|DSML|invoke name="f">\n'
        '<|DSML|parameter name="x"><![CDATA[1]]></|DSML|parameter>\n'
        "</|DSML|invoke>\n"
        "</|DSML|tool_calls>"
    )
    tcs, _ = parse_dsml_tool_calls(text, ["f"])
    assert len(tcs) == 1
    args = tcs[0]["function"]["arguments"]
    assert "1" in args


def test_parse_dsml_tool_calls_bare_invoke():
    text = '<invoke name="f"><parameter name="x"><![CDATA[hello]]></parameter></invoke>'
    tcs, _ = parse_dsml_tool_calls(text, ["f"])
    assert len(tcs) == 1


def test_parse_dsml_tool_calls_empty():
    tcs, cleaned = parse_dsml_tool_calls("nothing here", [])
    assert tcs == []
    assert cleaned == "nothing here"


def test_auto_type_booleans():
    assert _auto_type("true") is True
    assert _auto_type("True") is True
    assert _auto_type("false") is False
    assert _auto_type("FALSE") is False


def test_auto_type_null():
    assert _auto_type("null") is None
    assert _auto_type("None") is None


def test_auto_type_numbers():
    assert _auto_type("42") == 42
    assert isinstance(_auto_type("42"), int)
    assert _auto_type("3.14") == 3.14
    assert isinstance(_auto_type("3.14"), float)


def test_auto_type_string_fallback():
    assert _auto_type("hello") == "hello"
    assert _auto_type("") == ""


def test_build_dsml_tool_prompt_lists_tools():
    tools = [{
        "type": "function",
        "function": {
            "name": "f",
            "description": "Does the thing",
            "parameters": {"type": "object"},
        },
    }]
    out = build_dsml_tool_prompt(tools, "auto")
    assert "f" in out
    assert "Does the thing" in out
    assert "tool_calls" in out  # format example mentions the tag


def test_build_dsml_tool_prompt_required():
    tools = [{"function": {"name": "f"}}]
    out = build_dsml_tool_prompt(tools, "required")
    assert "MUST" in out


def test_build_dsml_tool_prompt_none_returns_empty():
    out = build_dsml_tool_prompt([], "auto")
    assert out == ""
    out = build_dsml_tool_prompt([{"function": {"name": "f"}}], "none")
    assert out == ""


def test_parse_dsml_tool_calls_handles_cdata_escapes():
    text = (
        "<tool_calls>"
        '<invoke name="f">'
        '<parameter name="x"><![CDATA[a]]>b]]></parameter>'
        "</invoke>"
        "</tool_calls>"
    )
    tcs, _ = parse_dsml_tool_calls(text, ["f"])
    assert len(tcs) == 1
    # CDATA escaping produces `a]]>b` literally in the source (the model
    # is expected to write it that way); the parser just extracts the
    # raw text. The escaped form would be `a]]]]><![CDATA[>b` from the
    # model side, but the parser does not perform the inverse — we
    # just verify it doesn't crash and returns the raw bytes.
    args = tcs[0]["function"]["arguments"]
    assert "a]]>b" in args
