# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Streaming text chunks must preserve formatting whitespace."""

from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


@pytest.mark.parametrize(
    ("chunk_type", "content", "event_type"),
    [
        ("llm_output", " ", "chat.delta"),
        ("llm_output", "\n", "chat.delta"),
        ("content_chunk", "\n\n", "chat.delta"),
        ("llm_reasoning", " ", "chat.reasoning"),
    ],
)
def test_parse_stream_chunk_preserves_whitespace(
    chunk_type: str,
    content: str,
    event_type: str,
) -> None:
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(
        SimpleNamespace(type=chunk_type, payload={"content": content})
    )

    assert parsed == {"event_type": event_type, "content": content}


def test_stream_text_payload_skips_only_absent_or_empty_content() -> None:
    assert JiuWenSwarmDeepAdapter._stream_text_payload("chat.delta", None) is None
    assert JiuWenSwarmDeepAdapter._stream_text_payload("chat.delta", "") is None
    assert JiuWenSwarmDeepAdapter._stream_text_payload(
        "chat.delta", " hello"
    ) == {"event_type": "chat.delta", "content": " hello"}


@pytest.mark.parametrize("chunk_type", ["llm_output", "content_chunk"])
@pytest.mark.parametrize("character_chunks", [False, True])
def test_shared_and_native_text_aggregation_preserve_markdown(
    chunk_type: str, character_chunks: bool,
) -> None:
    from openjiuwen.core.session.stream.base import OutputSchema
    from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk

    parts = [
        "# Retrieval report", "\n\n", "- Parent", "\n", "    ",
        "- Nested item", "\n\n", "```python", "\n", "    ",
        "print('31 seconds')", "\n", "```", "\n\n", "under the", " ",
        "300-word target", "\n",
    ]
    expected = "".join(parts)
    chunks = list(expected) if character_chunks else parts
    for parser in (parse_stream_chunk, JiuWenSwarmDeepAdapter._parse_stream_chunk):
        actual = []
        for index, content in enumerate(chunks):
            parsed = parser(OutputSchema(
                type=chunk_type, index=index, payload={"content": content},
            ))
            if parsed is not None:
                assert parsed["event_type"] == "chat.delta"
                actual.append(parsed["content"])
        assert "".join(actual) == expected


@pytest.mark.parametrize("chunk_type", ["llm_output", "content_chunk"])
@pytest.mark.parametrize("content", [None, ""])
def test_shared_text_parser_still_skips_absent_or_empty_chunks(
    chunk_type: str, content: str | None,
) -> None:
    from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk

    assert parse_stream_chunk(
        SimpleNamespace(type=chunk_type, payload={"content": content})
    ) is None


def test_shared_text_whitespace_does_not_change_tool_final_or_reasoning_policy() -> None:
    from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk

    assert parse_stream_chunk(SimpleNamespace(
        type="tool_call", payload={"tool_call": {"name": "read_file"}},
    )) == {"event_type": "chat.tool_call", "tool_call": {"name": "read_file"}}
    assert parse_stream_chunk(SimpleNamespace(
        type="answer", payload={"output": {"output": "done"}},
    ), _has_streamed_content=True) == {"event_type": "chat.final", "content": "done"}
    assert parse_stream_chunk(SimpleNamespace(
        type="llm_reasoning", payload={"content": " "},
    )) is None
