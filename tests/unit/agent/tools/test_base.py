"""
模块: tests.unit.agent.tools.test_base
职责: 校验来源序列化、失败结果与工具描述生成
依赖: agent.tools.base
"""

from __future__ import annotations

from tests.fakes.search import FakeSearchTool

from newsbot.agent.tools.base import Source, ToolResult, tool_schema


def test_source_serializes_to_dict() -> None:
    """摘要与权重必须带进来源字典：去重靠它们判断同事件并挑出保留哪一条。"""
    source = Source(title="标题", url="https://a.example")
    assert source.as_dict() == {"title": "标题", "url": "https://a.example", "weight": 1.0}

    enriched = Source(title="标题", url="https://a.example", snippet="摘要内容", weight=1.2)
    assert enriched.as_dict() == {
        "title": "标题",
        "url": "https://a.example",
        "snippet": "摘要内容",
        "weight": 1.2,
    }


def test_failure_result_is_marked() -> None:
    result = ToolResult.failure("坏了")
    assert result.text == "[工具执行失败] 坏了"
    assert result.sources == []


def test_tool_schema_matches_openai_shape() -> None:
    schema = tool_schema(FakeSearchTool())
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "search"
    assert "properties" in schema["function"]["parameters"]
