"""Tests for HTML generation from Codex CLI session JSONL."""

import tempfile
from pathlib import Path

import pytest

from codex_transcripts import (
    generate_html,
    get_session_summary,
    parse_session_file,
)


@pytest.fixture
def sample_session_path():
    return Path(__file__).parent / "sample_session.jsonl"


@pytest.fixture
def output_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def test_parse_session_file_codex_jsonl(sample_session_path):
    data = parse_session_file(sample_session_path)
    assert "loglines" in data
    assert "session_meta" in data

    loglines = data["loglines"]
    assert any(e["type"] == "user" for e in loglines)
    assert any(e["type"] == "assistant" for e in loglines)

    tool_use_blocks = []
    tool_result_blocks = []
    thinking_blocks = []
    image_blocks = []
    for entry in loglines:
        message = entry.get("message") or {}
        for block in message.get("content", []) or []:
            if block.get("type") == "tool_use":
                tool_use_blocks.append(block)
            if block.get("type") == "tool_result":
                tool_result_blocks.append(block)
            if block.get("type") == "thinking":
                thinking_blocks.append(block)
            if block.get("type") == "image":
                image_blocks.append(block)

    assert tool_use_blocks == []
    assert tool_result_blocks == []
    assert thinking_blocks == []
    assert image_blocks, "expected at least one image block"


def test_get_session_summary_skips_meta(sample_session_path):
    summary = get_session_summary(sample_session_path)
    assert summary != "(no summary)"
    assert summary.startswith("Create a hello world function")


def test_parse_session_file_skips_subagent_notifications(output_dir):
    session = output_dir / "session.jsonl"
    session.write_text(
        "\n".join(
            [
                '{"timestamp":"2026-01-01T00:00:00.000Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"<subagent_notification>\\n{\\"agent_id\\":\\"agent-123\\",\\"status\\":{\\"completed\\":\\"Internal status\\"}}\\n</subagent_notification>"}]}}',
                '{"timestamp":"2026-01-01T00:00:01.000Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"Actual user prompt"}]}}',
                '{"timestamp":"2026-01-01T00:00:02.000Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Assistant reply"}]}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    data = parse_session_file(session)

    assert data["loglines"] == [
        {
            "type": "user",
            "timestamp": "2026-01-01T00:00:01.000Z",
            "message": {"content": [{"type": "text", "text": "Actual user prompt"}]},
        },
        {
            "type": "assistant",
            "timestamp": "2026-01-01T00:00:02.000Z",
            "message": {"content": [{"type": "text", "text": "Assistant reply"}]},
        },
    ]


def test_get_session_summary_skips_subagent_notifications(output_dir):
    session = output_dir / "session.jsonl"
    session.write_text(
        "\n".join(
            [
                '{"timestamp":"2026-01-01T00:00:00.000Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"<subagent_notification>\\n{\\"agent_id\\":\\"agent-123\\",\\"status\\":{\\"completed\\":\\"Internal status\\"}}\\n</subagent_notification>"}]}}',
                '{"timestamp":"2026-01-01T00:00:01.000Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"Actual user prompt"}]}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert get_session_summary(session) == "Actual user prompt"


def test_generate_html_creates_pages_and_index(sample_session_path, output_dir):
    generate_html(sample_session_path, output_dir)

    assert (output_dir / "index.html").exists()
    assert (output_dir / "page-001.html").exists()
    assert (output_dir / "page-002.html").exists()

    index_html = (output_dir / "index.html").read_text(encoding="utf-8")
    page_002_html = (output_dir / "page-002.html").read_text(encoding="utf-8")
    assert "Codex transcript" in index_html

    # Newest-first ordering: the newest prompt is on index.html.
    assert "Prompt six" in index_html
    assert "Reply six" in index_html
    assert "Create a hello world function" not in index_html

    # Oldest prompt is on the older page.
    assert "Create a hello world function" in page_002_html
    assert "I'll create that function for you." in page_002_html

    # No tool chatter or commits; only chat.
    assert "exec_command" not in index_html
    assert "tool_use" not in index_html
    assert "tool_result" not in index_html
    assert "abc1234" not in index_html
    assert "exec_command" not in page_002_html
    assert "tool_use" not in page_002_html
    assert "tool_result" not in page_002_html
    assert "abc1234" not in page_002_html

    # Image data URL rendered to <img>.
    assert "data:image/gif;base64," in index_html


def test_generate_html_orders_pages_newest_first(output_dir):
    # Build a session with 6 prompts; PROMPTS_PER_PAGE is 5 so we expect 2 pages.
    session = output_dir / "session.jsonl"
    lines = [
        '{"timestamp":"2026-01-01T00:00:00.000Z","type":"session_meta","payload":{"cwd":"/tmp/repo","git":{"repository_url":"git@github.com:example/repo.git"}}}',
    ]
    for i in range(1, 7):
        lines.append(
            '{"timestamp":"2026-01-01T00:00:%02d.000Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"Prompt %d"}]}}'
            % (i, i)
        )
        lines.append(
            '{"timestamp":"2026-01-01T00:00:%02d.500Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Reply %d"}]}}'
            % (i, i)
        )

    session.write_text("\n".join(lines) + "\n", encoding="utf-8")

    generate_html(session, output_dir)

    index_html = (output_dir / "index.html").read_text(encoding="utf-8")
    assert (output_dir / "page-001.html").exists()
    page_002_html = (output_dir / "page-002.html").read_text(encoding="utf-8")

    # Page 1 (index.html) should contain the newest prompts.
    assert "Prompt 6" in index_html
    assert "Reply 6" in index_html
    assert "Prompt 1" not in index_html

    # Older content should be on later pages.
    assert "Prompt 1" in page_002_html

    # Within a page, newest prompts should appear first.
    assert index_html.index("Prompt 6") < index_html.index("Prompt 5")
    assert index_html.index("Prompt 5") < index_html.index("Prompt 4")

    # Within a conversation bucket, newest messages should come first too.
    assert index_html.index("Reply 6") < index_html.index("Prompt 6")
