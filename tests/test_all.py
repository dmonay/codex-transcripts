"""Tests for batch conversion functionality."""

import tempfile
from pathlib import Path

import pytest

from codex_transcripts import find_all_sessions, generate_batch_html


@pytest.fixture
def mock_sessions_dir():
    """Create a mock ~/.codex/sessions-like structure with test sessions."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sessions_dir = Path(tmpdir)

        # Project A (GitHub repo) with 2 sessions
        a_dir = sessions_dir / "2026" / "01" / "01"
        a_dir.mkdir(parents=True)

        (a_dir / "a1.jsonl").write_text(
            '{"timestamp":"2026-01-01T00:00:00.000Z","type":"session_meta","payload":{"cwd":"/repo-a","git":{"repository_url":"git@github.com:example/project-a.git"}}}\n'
            '{"timestamp":"2026-01-01T00:00:01.000Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"Hello from project A"}]}}\n'
            '{"timestamp":"2026-01-01T00:00:02.000Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Hi!"}]}}\n',
            encoding="utf-8",
        )

        (a_dir / "a2.jsonl").write_text(
            '{"timestamp":"2026-01-02T00:00:00.000Z","type":"session_meta","payload":{"cwd":"/repo-a","git":{"repository_url":"git@github.com:example/project-a.git"}}}\n'
            '{"timestamp":"2026-01-02T00:00:01.000Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"Second session in project A"}]}}\n',
            encoding="utf-8",
        )

        # codex exec session (should be excluded)
        (a_dir / "exec1.jsonl").write_text(
            '{"timestamp":"2026-01-02T00:00:10.000Z","type":"session_meta","payload":{"cwd":"/repo-a","source":"exec","originator":"codex_exec","git":{"repository_url":"git@github.com:example/project-a.git"}}}\n'
            '{"timestamp":"2026-01-02T00:00:11.000Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"/tmp/codex_prompt.txt"}]}}\n',
            encoding="utf-8",
        )

        # Project B with 1 session (+ warmup that should be skipped)
        b_dir = sessions_dir / "2026" / "01" / "02"
        b_dir.mkdir(parents=True)

        (b_dir / "b1.jsonl").write_text(
            '{"timestamp":"2026-01-03T00:00:00.000Z","type":"session_meta","payload":{"cwd":"/repo-b","git":{"repository_url":"git@github.com:example/project-b.git"}}}\n'
            '{"timestamp":"2026-01-03T00:00:01.000Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"Hello from project B"}]}}\n',
            encoding="utf-8",
        )

        (b_dir / "warmup.jsonl").write_text(
            '{"timestamp":"2026-01-04T00:00:00.000Z","type":"session_meta","payload":{"cwd":"/repo-b","git":{"repository_url":"git@github.com:example/project-b.git"}}}\n'
            '{"timestamp":"2026-01-04T00:00:01.000Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"warmup"}]}}\n',
            encoding="utf-8",
        )

        yield sessions_dir


@pytest.fixture
def output_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def test_find_all_sessions_groups_by_project(mock_sessions_dir):
    projects = find_all_sessions(mock_sessions_dir)

    assert len(projects) == 2
    display_names = {p["display_name"] for p in projects}
    assert "example/project-a" in display_names
    assert "example/project-b" in display_names

    project_a = next(p for p in projects if p["display_name"] == "example/project-a")
    assert len(project_a["sessions"]) == 2

    project_b = next(p for p in projects if p["display_name"] == "example/project-b")
    assert len(project_b["sessions"]) == 1


def test_generate_batch_html_creates_archive(mock_sessions_dir, output_dir):
    stats = generate_batch_html(mock_sessions_dir, output_dir)

    assert stats["total_projects"] == 2
    assert stats["total_sessions"] == 3
    assert stats["failed_sessions"] == []

    assert (output_dir / "index.html").exists()

    # Project directories use a filesystem-safe name.
    assert (output_dir / "example__project-a").exists()
    assert (output_dir / "example__project-b").exists()

    # Each session gets its own directory with transcript files.
    assert (output_dir / "example__project-a" / "a1" / "index.html").exists()
    assert (output_dir / "example__project-a" / "a2" / "index.html").exists()
    assert (output_dir / "example__project-b" / "b1" / "index.html").exists()
    assert not (output_dir / "example__project-a" / "exec1").exists()

    # Search assets
    assert (output_dir / "search_index.js").exists()
    assert (output_dir / "search_ui.js").exists()

    index_html = (output_dir / "index.html").read_text(encoding="utf-8")
    assert 'id="search-box"' in index_html
    assert "search_index.js" in index_html
    assert "search_ui.js" in index_html
    assert 'id="search-project-select"' in index_html
    assert 'id="modal-project-select"' in index_html
    assert 'id="search-sort-select"' in index_html
    assert 'id="modal-sort-select"' in index_html
    assert 'class="home-btn"' in index_html
    assert 'id="codex-projects-divider"' in index_html

    project_index_html = (output_dir / "example__project-a" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "search_ui.js" in project_index_html
    assert "search_index.js" in project_index_html
    assert "example__project-a" in project_index_html
    assert 'class="home-btn"' in project_index_html

    session_index_html = (
        output_dir / "example__project-a" / "a1" / "index.html"
    ).read_text(encoding="utf-8")
    assert "search_ui.js" in session_index_html
    assert "search_index.js" in session_index_html
    assert "example__project-a" in session_index_html
    assert 'class="home-btn"' in session_index_html

    search_index_js = (output_dir / "search_index.js").read_text(encoding="utf-8")
    assert "Hello from project A" in search_index_js
    assert "Hi!" in search_index_js
    assert "/tmp/codex_prompt.txt" not in search_index_js
    assert "a1/index.html#msg-" in search_index_js


def test_generate_batch_html_includes_json_exports(mock_sessions_dir, output_dir):
    # Add a sanitized export JSON session (the format produced by the daemon/server).
    c_dir = mock_sessions_dir / "2026" / "01" / "03"
    c_dir.mkdir(parents=True)
    (c_dir / "c1.json").write_text(
        '{"session_meta":{"cwd":"/repo-c","git":{"repository_url":"git@github.com:example/project-c.git"}},'
        '"loglines":['
        '{"type":"user","timestamp":"2026-01-05T00:00:00.000Z","message":{"content":[{"type":"text","text":"Hello from project C"}]}},'
        '{"type":"assistant","timestamp":"2026-01-05T00:00:01.000Z","message":{"content":[{"type":"text","text":"Hi C"}]}}'
        "]}",
        encoding="utf-8",
    )

    stats = generate_batch_html(mock_sessions_dir, output_dir)

    assert stats["total_projects"] == 3
    assert stats["total_sessions"] == 4
    assert (output_dir / "example__project-c" / "c1" / "index.html").exists()


def test_generate_batch_html_incremental_skips_unchanged_sessions(
    mock_sessions_dir, output_dir
):
    # First run builds everything.
    generate_batch_html(mock_sessions_dir, output_dir)

    a1_index = output_dir / "example__project-a" / "a1" / "index.html"
    a2_index = output_dir / "example__project-a" / "a2" / "index.html"
    a2_session = mock_sessions_dir / "2026" / "01" / "01" / "a2.jsonl"

    a1_mtime_ns_1 = a1_index.stat().st_mtime_ns
    a2_mtime_ns_1 = a2_index.stat().st_mtime_ns

    # Touch one session and re-run. Unchanged sessions should not be rewritten.
    import time

    time.sleep(0.01)
    a2_session.write_text(
        a2_session.read_text(encoding="utf-8")
        + '{"timestamp":"2026-01-02T00:00:02.000Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"New message"}]}}\n',
        encoding="utf-8",
    )
    time.sleep(0.01)

    generate_batch_html(mock_sessions_dir, output_dir)

    a1_mtime_ns_2 = a1_index.stat().st_mtime_ns
    a2_mtime_ns_2 = a2_index.stat().st_mtime_ns

    assert a1_mtime_ns_2 == a1_mtime_ns_1
    assert a2_mtime_ns_2 != a2_mtime_ns_1
