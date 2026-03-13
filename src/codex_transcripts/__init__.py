"""Convert Codex CLI session JSONL to clean, mobile-friendly HTML pages with pagination."""

import json
import html
import re
import shutil
import subprocess
import tempfile
import webbrowser
from datetime import datetime
from pathlib import Path

import click
from click_default_group import DefaultGroup
import httpx
from jinja2 import Environment, PackageLoader
import markdown

# Set up Jinja2 environment
_jinja_env = Environment(
    loader=PackageLoader("codex_transcripts", "templates"),
    autoescape=True,
)

# Load macros template and expose macros
_macros_template = _jinja_env.get_template("macros.html")
_macros = _macros_template.module


def get_template(name):
    """Get a Jinja2 template by name."""
    return _jinja_env.get_template(name)


# Regex to match git commit output: [branch hash] message
COMMIT_PATTERN = re.compile(r"\[[\w\-/]+ ([a-f0-9]{7,})\] (.+?)(?:\n|$)")

# Regex to detect GitHub repo from git push output (e.g., github.com/owner/repo/pull/new/branch)
GITHUB_REPO_PATTERN = re.compile(
    r"github\.com/([a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+)/pull/new/"
)

PROMPTS_PER_PAGE = 5
LONG_TEXT_THRESHOLD = (
    300  # Characters - text blocks longer than this are shown in index
)
LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo


def extract_text_from_content(content):
    """Extract plain text from message content.

    Handles both string content and array content.

    Args:
        content: Either a string or a list of content blocks like
                 [{"type": "input_text", "text": "..."}, {"type": "input_image", ...}]

    Returns:
        The extracted text as a string, or empty string if no text found.
    """
    if isinstance(content, str):
        return content.strip()
    elif isinstance(content, list):
        texts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in {"text", "input_text", "output_text", "summary_text"}:
                text = block.get("text", "")
                if text:
                    texts.append(text)
        return " ".join(texts).strip()
    return ""


def is_meta_prompt(text):
    """Return True if this user text looks like session boilerplate."""
    if not isinstance(text, str):
        return False
    text = text.strip()
    if not text:
        return False
    meta_prefixes = (
        "# AGENTS.md instructions",
        "<environment_context>",
        "<permissions instructions>",
    )
    return text.startswith(meta_prefixes)


def is_internal_user_message(text):
    """Return True for internal transport messages that are not user-authored."""
    if not isinstance(text, str):
        return False
    text = text.strip()
    if not text:
        return False
    return text.startswith("<subagent_notification>") and text.endswith(
        "</subagent_notification>"
    )


def should_skip_user_text(text):
    return is_meta_prompt(text) or is_internal_user_message(text)


# Module-level variable for GitHub repo (set by generate_html)
_github_repo = None


def get_session_summary(filepath, max_length=200):
    """Extract a human-readable summary from a session file.

    Supports both JSON and JSONL formats.
    Returns a summary string or "(no summary)" if none found.
    """
    filepath = Path(filepath)
    try:
        if filepath.suffix == ".jsonl":
            return _get_jsonl_summary(filepath, max_length)
        else:
            # For JSON files, try to get first user message
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            loglines = data.get("loglines", [])
            for entry in loglines:
                if entry.get("type") == "user":
                    msg = entry.get("message", {})
                    content = msg.get("content", "")
                    text = extract_text_from_content(content)
                    if text and not should_skip_user_text(text):
                        if len(text) > max_length:
                            return text[: max_length - 3] + "..."
                        return text
            return "(no summary)"
    except Exception:
        return "(no summary)"


def _get_jsonl_summary(filepath, max_length=200):
    """Extract summary from a Codex CLI session JSONL file."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if obj.get("type") != "response_item":
                    continue

                payload = obj.get("payload", {})
                if payload.get("type") != "message" or payload.get("role") != "user":
                    continue

                content = payload.get("content", [])
                text = extract_text_from_content(content)
                if not text:
                    continue
                if should_skip_user_text(text):
                    continue
                if text.strip().lower() == "warmup":
                    continue

                if len(text) > max_length:
                    return text[: max_length - 3] + "..."
                return text
    except Exception:
        return "(no summary)"

    return "(no summary)"


def find_local_sessions(folder, limit=10):
    """Find recent JSONL session files in the given folder.

    Returns a list of (Path, summary) tuples sorted by modification time.
    Excludes agent files and warmup/empty sessions.
    """
    folder = Path(folder)
    if not folder.exists():
        return []

    results = []
    session_files = list(folder.glob("**/*.jsonl")) + list(folder.glob("**/*.json"))
    for f in session_files:
        if f.name.startswith("agent-"):
            continue
        meta = read_session_meta(f)
        if is_exec_session_meta(meta):
            continue
        summary = get_session_summary(f)
        # Skip boring/empty sessions
        if summary.lower() == "warmup" or summary == "(no summary)":
            continue
        results.append((f, summary))

    # Sort by modification time, most recent first
    results.sort(key=lambda x: x[0].stat().st_mtime, reverse=True)
    return results[:limit]


def github_repo_from_git_url(url):
    """Extract owner/repo from a GitHub git remote URL."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()

    # git@github.com:owner/repo.git
    if url.startswith("git@github.com:"):
        path = url[len("git@github.com:") :]
        if path.endswith(".git"):
            path = path[: -len(".git")]
        if path.count("/") == 1:
            return path

    # https://github.com/owner/repo(.git)
    if "github.com/" in url:
        path = url.split("github.com/", 1)[1]
        path = path.strip("/")
        if path.endswith(".git"):
            path = path[: -len(".git")]
        parts = path.split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"

    return None


def safe_project_dir_name(display_name):
    """Convert a display name to a filesystem-safe directory name."""
    if not display_name:
        return "unknown"
    name = display_name.replace("/", "__")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def read_session_meta(filepath):
    """Read session metadata from a session file.

    Supports:
    - Codex CLI session JSONL files (first `session_meta` entry)
    - Already-normalized export JSON files (top-level `session_meta`)
    """
    try:
        filepath = Path(filepath)
        if filepath.suffix == ".json":
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data.get("session_meta", {}) or {}
            return {}

        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "session_meta":
                    return obj.get("payload", {}) or {}
    except OSError:
        return {}
    return {}


def is_exec_session_meta(meta):
    """Return True if this session was launched via `codex exec`."""
    if not isinstance(meta, dict):
        return False
    source = str(meta.get("source") or "").strip().lower()
    if source == "exec":
        return True
    originator = str(meta.get("originator") or "").strip().lower()
    if originator.startswith("codex_exec"):
        return True
    return False


def get_project_names_for_session(filepath, meta=None):
    """Return (dir_name, display_name) for a session file."""
    meta = meta if isinstance(meta, dict) else read_session_meta(filepath)
    repo = github_repo_from_git_url((meta.get("git") or {}).get("repository_url"))
    if repo:
        return safe_project_dir_name(repo), repo
    cwd = meta.get("cwd")
    if cwd:
        display = Path(cwd).name or str(cwd)
        return safe_project_dir_name(display), display
    return "unknown", "unknown"


def find_all_sessions(folder, include_agents=False):
    """Find all Codex CLI sessions in a folder, grouped by project.

    Returns a list of project dicts, each containing:
    - name: filesystem-safe project directory name
    - display_name: human-readable project name (e.g. owner/repo)
    - sessions: list of session dicts with path, summary, mtime, size

    Sessions are sorted by modification time (most recent first) within each project.
    Projects are sorted by their most recent session.
    """
    folder = Path(folder)
    if not folder.exists():
        return []

    projects = {}

    session_files = list(folder.glob("**/*.jsonl")) + list(folder.glob("**/*.json"))
    for session_file in session_files:
        # include_agents is kept for API compatibility; Codex sessions don't use agent-* naming.

        meta = read_session_meta(session_file)
        if is_exec_session_meta(meta):
            continue

        # Get summary and skip boring sessions
        summary = get_session_summary(session_file)
        if summary.lower() == "warmup" or summary == "(no summary)":
            continue

        project_dir_name, project_display_name = get_project_names_for_session(
            session_file, meta=meta
        )
        if project_dir_name not in projects:
            projects[project_dir_name] = {
                "name": project_dir_name,
                "display_name": project_display_name,
                "sessions": [],
            }

        stat = session_file.stat()
        projects[project_dir_name]["sessions"].append(
            {
                "path": session_file,
                "summary": summary,
                "mtime": stat.st_mtime,
                "mtime_ns": getattr(
                    stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)
                ),
                "size": stat.st_size,
                "original_relpath": meta.get("original_relpath"),
            }
        )

    # Sort sessions within each project by mtime (most recent first)
    for project in projects.values():
        project["sessions"].sort(key=lambda s: s["mtime"], reverse=True)

    # Convert to list and sort projects by most recent session
    result = list(projects.values())
    result.sort(
        key=lambda p: p["sessions"][0]["mtime"] if p["sessions"] else 0, reverse=True
    )

    return result


def _build_search_text(loglines):
    """Build a single searchable string for a session from normalized loglines."""
    parts = []
    for entry in loglines or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") not in {"user", "assistant"}:
            continue
        message = entry.get("message") or {}
        text = extract_text_from_content(message.get("content", []))
        if entry.get("type") == "user" and should_skip_user_text(text):
            continue
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _message_text_from_json(message_json):
    if not message_json:
        return ""
    try:
        message_data = json.loads(message_json)
    except json.JSONDecodeError:
        return ""
    return extract_text_from_content(message_data.get("content", []))


def _build_conversations(loglines):
    conversations = []
    current_conv = None
    for entry in loglines:
        log_type = entry.get("type")
        timestamp = entry.get("timestamp", "")
        message_data = entry.get("message", {})
        if not message_data:
            continue

        message_json = json.dumps(message_data)

        is_user_prompt = False
        user_text = None
        if log_type == "user":
            content = message_data.get("content", "")
            text = extract_text_from_content(content)
            if text and should_skip_user_text(text):
                continue
            has_image = any(
                isinstance(block, dict) and block.get("type") == "image"
                for block in (content if isinstance(content, list) else [])
            )
            if text or has_image:
                is_user_prompt = True
                user_text = text or "(image)"

        if is_user_prompt:
            if current_conv:
                conversations.append(current_conv)
            current_conv = {
                "user_text": user_text,
                "timestamp": timestamp,
                "messages": [(log_type, message_json, timestamp)],
            }
        elif current_conv:
            current_conv["messages"].append((log_type, message_json, timestamp))

    if current_conv:
        conversations.append(current_conv)

    return conversations


def _timestamp_to_epoch_seconds(timestamp):
    if not isinstance(timestamp, str) or not timestamp.strip():
        return 0
    ts = timestamp.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(ts).timestamp())
    except ValueError:
        return 0


def format_timestamp_hover_title(timestamp):
    if not isinstance(timestamp, str) or not timestamp.strip():
        return ""
    ts = timestamp.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return timestamp

    local_dt = dt.astimezone(LOCAL_TIMEZONE)
    hour = local_dt.strftime("%I").lstrip("0") or "0"
    tzname = local_dt.tzname() or local_dt.strftime("%Z")
    return (
        f"{local_dt.strftime('%A')}, {local_dt.strftime('%B')} {local_dt.day}, "
        f"{local_dt.year} {hour}:{local_dt.strftime('%M')} "
        f"{local_dt.strftime('%p')} {tzname}"
    )


def _write_archive_search_assets(output_dir, search_entries):
    """Write static JS assets used for archive-wide search."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    search_entries = list(search_entries or [])
    search_entries.sort(key=lambda e: int(e.get("mtime") or 0), reverse=True)

    (output_dir / "search_index.js").write_text(
        "window.__CODEX_TRANSCRIPTS_SEARCH_INDEX = "
        + json.dumps(search_entries, ensure_ascii=False)
        + ";\n",
        encoding="utf-8",
    )
    (output_dir / "search_ui.js").write_text(
        ARCHIVE_SEARCH_UI_JS,
        encoding="utf-8",
    )


ARCHIVE_STATE_VERSION = 7
ARCHIVE_STATE_FILENAME = ".codex-transcripts-state.json"


def _session_source_key(source_folder, session_path):
    """Return a stable key for a session file relative to the source folder."""
    source_folder = Path(source_folder).resolve()
    session_path = Path(session_path).resolve()
    try:
        return session_path.relative_to(source_folder).as_posix()
    except ValueError:
        return session_path.as_posix()


def _load_archive_state(output_dir):
    path = Path(output_dir) / ARCHIVE_STATE_FILENAME
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return {"version": ARCHIVE_STATE_VERSION, "sessions": {}}
    except Exception:
        return {"version": ARCHIVE_STATE_VERSION, "sessions": {}}

    if not isinstance(state, dict) or state.get("version") != ARCHIVE_STATE_VERSION:
        return {"version": ARCHIVE_STATE_VERSION, "sessions": {}}

    sessions = state.get("sessions", {})
    if not isinstance(sessions, dict):
        sessions = {}
    return {"version": ARCHIVE_STATE_VERSION, "sessions": sessions}


def _save_archive_state(output_dir, state):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / ARCHIVE_STATE_FILENAME

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(state, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _cleanup_archive_output(output_dir, projects):
    """Remove project/session directories for sessions that no longer exist."""
    output_dir = Path(output_dir)
    expected_projects = {p["name"] for p in projects}
    expected_sessions = {
        (p["name"], s["path"].stem) for p in projects for s in p.get("sessions", [])
    }

    for child in output_dir.iterdir():
        if not child.is_dir():
            continue
        if child.name not in expected_projects:
            shutil.rmtree(child, ignore_errors=True)
            continue
        for sub in child.iterdir():
            if sub.is_dir() and (child.name, sub.name) not in expected_sessions:
                shutil.rmtree(sub, ignore_errors=True)


def generate_batch_html(
    source_folder, output_dir, include_agents=False, progress_callback=None
):
    """Generate HTML archive for all sessions in a Codex sessions folder.

    Creates:
    - Master index.html listing all projects
    - Per-project directories with index.html listing sessions
    - Per-session directories with transcript pages

    Args:
        source_folder: Path to the Codex sessions folder
        output_dir: Path for output archive
        include_agents: Ignored (kept for CLI compatibility)
        progress_callback: Optional callback(project_name, session_name, current, total)
            called after each session is processed

    Returns statistics dict with total_projects, total_sessions, failed_sessions, output_dir.
    """
    source_folder = Path(source_folder)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    state = _load_archive_state(output_dir)
    cached_sessions = state.get("sessions", {}) or {}
    new_sessions_state = {}

    # Find all sessions
    projects = find_all_sessions(source_folder, include_agents=include_agents)

    # Calculate total for progress tracking
    total_session_count = sum(len(p["sessions"]) for p in projects)
    processed_count = 0
    successful_sessions = 0
    failed_sessions = []
    search_entries = []

    # Process each project
    for project in projects:
        project_dir = output_dir / project["name"]
        project_dir.mkdir(exist_ok=True)

        # Process each session
        for session in project["sessions"]:
            session_key = _session_source_key(source_folder, session["path"])
            session_name = session["path"].stem
            session_dir = project_dir / session_name
            mtime_ns = int(session.get("mtime_ns") or 0)
            size = int(session.get("size") or 0)

            cached = (
                cached_sessions.get(session_key)
                if isinstance(cached_sessions, dict)
                else None
            )
            cached_search = (
                cached.get("search_entries") if isinstance(cached, dict) else None
            )
            up_to_date = (
                isinstance(cached, dict)
                and cached.get("mtime_ns") == mtime_ns
                and cached.get("size") == size
                and isinstance(cached_search, list)
                and (session_dir / "index.html").exists()
            )

            if up_to_date:
                new_sessions_state[session_key] = cached
                search_entries.extend(cached_search)
                successful_sessions += 1
                processed_count += 1
                if progress_callback:
                    progress_callback(
                        project["name"],
                        session_name,
                        processed_count,
                        total_session_count,
                    )
                continue

            # Generate transcript HTML with error handling
            try:
                data = parse_session_file(session["path"])
                generate_html(
                    session["path"],
                    session_dir,
                    data=data,
                    search_enabled=True,
                    archive_root="../../",
                    project_dir=project["name"],
                )
                conversations = _build_conversations(data.get("loglines", []))
                # Must match generate_html(): newest-first pagination.
                conversations.reverse()
                session_search_entries = []
                for conv_index, conv in enumerate(conversations):
                    conv_ts = conv.get("timestamp", "")
                    conv_epoch = _timestamp_to_epoch_seconds(conv_ts)
                    page_num = (conv_index // PROMPTS_PER_PAGE) + 1
                    page_file = (
                        "index.html" if page_num == 1 else f"page-{page_num:03d}.html"
                    )
                    msg_id = make_msg_id(conv_ts) if conv_ts else ""
                    anchor = f"#{msg_id}" if msg_id else ""
                    text = "\n".join(
                        _message_text_from_json(msg_json)
                        for _, msg_json, _ in conv.get("messages", [])
                    ).strip()

                    if not text:
                        continue

                    session_search_entries.append(
                        {
                            "project": project.get("display_name") or project["name"],
                            "project_dir": project["name"],
                            "session": session_name,
                            "mtime": conv_epoch,
                            "date": (
                                datetime.fromtimestamp(conv_epoch).strftime(
                                    "%Y-%m-%d %H:%M"
                                )
                                if conv_epoch
                                else conv_ts
                            ),
                            "summary": conv.get("user_text") or session["summary"],
                            "href": f"{project['name']}/{session_name}/{page_file}{anchor}",
                            "text": text,
                        }
                    )
                search_entries.extend(session_search_entries)
                new_sessions_state[session_key] = {
                    "mtime_ns": mtime_ns,
                    "size": size,
                    "search_entries": session_search_entries,
                }
                successful_sessions += 1
            except Exception as e:
                failed_sessions.append(
                    {
                        "project": project["name"],
                        "session": session_name,
                        "error": str(e),
                    }
                )
                if isinstance(cached, dict) and isinstance(cached_search, list):
                    new_sessions_state[session_key] = cached
                    search_entries.extend(cached_search)

            processed_count += 1

            # Call progress callback if provided
            if progress_callback:
                progress_callback(
                    project["name"], session_name, processed_count, total_session_count
                )

        # Generate project index
        _generate_project_index(project, project_dir)

    # Generate master index
    _generate_master_index(projects, output_dir)

    # Remove output for deleted sessions/projects
    _cleanup_archive_output(output_dir, projects)

    # Search assets
    _write_archive_search_assets(output_dir, search_entries)
    _save_archive_state(
        output_dir,
        {"version": ARCHIVE_STATE_VERSION, "sessions": new_sessions_state},
    )

    return {
        "total_projects": len(projects),
        "total_sessions": successful_sessions,
        "failed_sessions": failed_sessions,
        "output_dir": output_dir,
    }


def _generate_project_index(project, output_dir):
    """Generate index.html for a single project."""
    template = get_template("project_index.html")

    # Format sessions for template
    sessions_data = []
    for session in project["sessions"]:
        mod_time = datetime.fromtimestamp(session["mtime"])
        sessions_data.append(
            {
                "name": session["path"].stem,
                "summary": session["summary"],
                "date": mod_time.strftime("%Y-%m-%d %H:%M"),
                "size_kb": session["size"] / 1024,
                "original_relpath": session.get("original_relpath"),
            }
        )

    html_content = template.render(
        project_name=project.get("display_name") or project["name"],
        sessions=sessions_data,
        session_count=len(sessions_data),
        search_enabled=True,
        archive_root="../",
        project_dir=project["name"],
        css=CSS,
        js=JS,
    )

    output_path = output_dir / "index.html"
    output_path.write_text(html_content, encoding="utf-8")


def _generate_master_index(projects, output_dir):
    """Generate master index.html listing all projects."""
    template = get_template("master_index.html")

    # Format projects for template
    projects_data = []
    total_sessions = 0

    for project in projects:
        session_count = len(project["sessions"])
        total_sessions += session_count

        # Get most recent session date
        if project["sessions"]:
            most_recent = datetime.fromtimestamp(project["sessions"][0]["mtime"])
            recent_date = most_recent.strftime("%Y-%m-%d")
        else:
            recent_date = "N/A"

        projects_data.append(
            {
                "name": project["name"],
                "display_name": project.get("display_name") or project["name"],
                "session_count": session_count,
                "recent_date": recent_date,
            }
        )

    html_content = template.render(
        projects=projects_data,
        total_projects=len(projects),
        total_sessions=total_sessions,
        search_enabled=True,
        archive_root="",
        project_dir="",
        css=CSS,
        js=JS,
    )

    output_path = output_dir / "index.html"
    output_path.write_text(html_content, encoding="utf-8")


def parse_session_file(filepath):
    """Parse a session file and return normalized data.

    Supports both JSON and JSONL formats.
    Returns a dict with:
    - session_meta: payload from the first session_meta entry (if present)
    - loglines: normalized entries in a message-like format
    """
    filepath = Path(filepath)

    if filepath.suffix == ".jsonl":
        return _parse_jsonl_file(filepath)
    else:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Already-normalized export format
        if isinstance(data, dict) and "loglines" in data:
            data.setdefault("session_meta", {})
            return data
        return {"session_meta": {}, "loglines": []}


def _parse_jsonl_file(filepath):
    """Parse a Codex CLI session JSONL file and convert to a message-like format."""

    def parse_data_url(url):
        if not isinstance(url, str):
            return None
        if not url.startswith("data:"):
            return None
        header, _, data = url.partition(",")
        if ";base64" not in header or not data:
            return None
        media_type = header[5:].split(";", 1)[0] or "application/octet-stream"
        return media_type, data

    def normalize_message_content(content):
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        if not isinstance(content, list):
            return []
        blocks = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type in {"input_text", "output_text"}:
                text = item.get("text", "")
                if text:
                    blocks.append({"type": "text", "text": text})
            elif item_type in {"tool_result", "tool_use"}:
                # Omit tool chatter for a chat-only view.
                continue
            elif item_type in {"input_image", "output_image"}:
                parsed = parse_data_url(item.get("image_url"))
                if parsed:
                    media_type, data = parsed
                    blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": data,
                            },
                        }
                    )
            else:
                # Unknown block: render as JSON
                blocks.append(
                    {"type": "text", "text": json.dumps(item, ensure_ascii=False)}
                )
        return blocks

    session_meta = {}
    loglines = []

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = obj.get("type")
            timestamp = obj.get("timestamp", "")

            if entry_type == "session_meta":
                session_meta = obj.get("payload", {}) or {}
                continue

            if entry_type != "response_item":
                continue

            payload = obj.get("payload", {}) or {}
            payload_type = payload.get("type")

            if payload_type != "message":
                continue

            role = payload.get("role")
            if role not in {"user", "assistant"}:
                continue

            raw_content = payload.get("content", [])
            if role == "user":
                text = extract_text_from_content(raw_content)
                if text and should_skip_user_text(text):
                    continue

            content = normalize_message_content(raw_content)
            if not content:
                continue

            loglines.append(
                {
                    "type": role,
                    "timestamp": timestamp,
                    "message": {"content": content},
                }
            )

    return {"session_meta": session_meta, "loglines": loglines}


def detect_github_repo(session_meta, loglines):
    """Detect GitHub repo (owner/name) from session metadata or tool output."""
    repo = github_repo_from_git_url(
        ((session_meta or {}).get("git") or {}).get("repository_url")
    )
    if repo:
        return repo

    for entry in loglines:
        message = entry.get("message", {})
        content = message.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                result_content = block.get("content", "")
                if isinstance(result_content, str):
                    match = GITHUB_REPO_PATTERN.search(result_content)
                    if match:
                        return match.group(1)
    return None


def format_json(obj):
    try:
        if isinstance(obj, str):
            obj = json.loads(obj)
        formatted = json.dumps(obj, indent=2, ensure_ascii=False)
        return f'<pre class="json">{html.escape(formatted)}</pre>'
    except (json.JSONDecodeError, TypeError):
        return f"<pre>{html.escape(str(obj))}</pre>"


def render_markdown_text(text):
    if not text:
        return ""
    return markdown.markdown(text, extensions=["fenced_code", "tables"])


def is_json_like(text):
    if not text or not isinstance(text, str):
        return False
    text = text.strip()
    return (text.startswith("{") and text.endswith("}")) or (
        text.startswith("[") and text.endswith("]")
    )


def render_todo_write(tool_input, tool_id):
    todos = tool_input.get("todos", [])
    if not todos:
        return ""
    return _macros.todo_list(todos, tool_id)


def render_write_tool(tool_input, tool_id):
    """Render Write tool calls with file path header and content preview."""
    file_path = tool_input.get("file_path", "Unknown file")
    content = tool_input.get("content", "")
    return _macros.write_tool(file_path, content, tool_id)


def render_edit_tool(tool_input, tool_id):
    """Render Edit tool calls with diff-like old/new display."""
    file_path = tool_input.get("file_path", "Unknown file")
    old_string = tool_input.get("old_string", "")
    new_string = tool_input.get("new_string", "")
    replace_all = tool_input.get("replace_all", False)
    return _macros.edit_tool(file_path, old_string, new_string, replace_all, tool_id)


def render_bash_tool(tool_input, tool_id):
    """Render Bash tool calls with command as plain text."""
    command = tool_input.get("command", "")
    description = tool_input.get("description", "")
    return _macros.bash_tool(command, description, tool_id)


def render_content_block(block):
    if not isinstance(block, dict):
        return f"<p>{html.escape(str(block))}</p>"
    block_type = block.get("type", "")
    if block_type == "image":
        source = block.get("source", {})
        media_type = source.get("media_type", "image/png")
        data = source.get("data", "")
        return _macros.image_block(media_type, data)
    elif block_type == "thinking":
        content_html = render_markdown_text(block.get("thinking", ""))
        return _macros.thinking(content_html)
    elif block_type == "text":
        content_html = render_markdown_text(block.get("text", ""))
        return _macros.assistant_text(content_html)
    elif block_type == "tool_use":
        tool_name = block.get("name", "Unknown tool")
        tool_input = block.get("input", {})
        tool_id = block.get("id", "")
        if tool_name == "TodoWrite":
            return render_todo_write(tool_input, tool_id)
        if tool_name == "Write":
            return render_write_tool(tool_input, tool_id)
        if tool_name == "Edit":
            return render_edit_tool(tool_input, tool_id)
        if tool_name == "Bash":
            return render_bash_tool(tool_input, tool_id)
        description = tool_input.get("description", "")
        display_input = {k: v for k, v in tool_input.items() if k != "description"}
        input_json = json.dumps(display_input, indent=2, ensure_ascii=False)
        return _macros.tool_use(tool_name, description, input_json, tool_id)
    elif block_type == "tool_result":
        content = block.get("content", "")
        is_error = block.get("is_error", False)
        has_images = False

        # Check for git commits and render with styled cards
        if isinstance(content, str):
            commits_found = list(COMMIT_PATTERN.finditer(content))
            if commits_found:
                # Build commit cards + remaining content
                parts = []
                last_end = 0
                for match in commits_found:
                    # Add any content before this commit
                    before = content[last_end : match.start()].strip()
                    if before:
                        parts.append(f"<pre>{html.escape(before)}</pre>")

                    commit_hash = match.group(1)
                    commit_msg = match.group(2)
                    parts.append(
                        _macros.commit_card(commit_hash, commit_msg, _github_repo)
                    )
                    last_end = match.end()

                # Add any remaining content after last commit
                after = content[last_end:].strip()
                if after:
                    parts.append(f"<pre>{html.escape(after)}</pre>")

                content_html = "".join(parts)
            else:
                content_html = f"<pre>{html.escape(content)}</pre>"
        elif isinstance(content, list):
            # Handle tool result content that contains multiple blocks (text, images, etc.)
            parts = []
            for item in content:
                if isinstance(item, dict):
                    item_type = item.get("type", "")
                    if item_type == "text":
                        text = item.get("text", "")
                        if text:
                            parts.append(f"<pre>{html.escape(text)}</pre>")
                    elif item_type == "image":
                        source = item.get("source", {})
                        media_type = source.get("media_type", "image/png")
                        data = source.get("data", "")
                        if data:
                            parts.append(_macros.image_block(media_type, data))
                            has_images = True
                    else:
                        # Unknown type, render as JSON
                        parts.append(format_json(item))
                else:
                    # Non-dict item, escape as text
                    parts.append(f"<pre>{html.escape(str(item))}</pre>")
            content_html = "".join(parts) if parts else format_json(content)
        elif is_json_like(content):
            content_html = format_json(content)
        else:
            content_html = format_json(content)
        return _macros.tool_result(content_html, is_error, has_images)
    else:
        return format_json(block)


def render_user_message_content(message_data):
    content = message_data.get("content", "")
    if isinstance(content, str):
        if is_json_like(content):
            return _macros.user_content(format_json(content))
        return _macros.user_content(render_markdown_text(content))
    elif isinstance(content, list):
        return "".join(render_content_block(block) for block in content)
    return f"<p>{html.escape(str(content))}</p>"


def render_assistant_message(message_data):
    content = message_data.get("content", [])
    if not isinstance(content, list):
        return f"<p>{html.escape(str(content))}</p>"
    return "".join(render_content_block(block) for block in content)


def make_msg_id(timestamp):
    return f"msg-{timestamp.replace(':', '-').replace('.', '-')}"


def analyze_conversation(messages):
    """Analyze messages in a conversation to extract stats and long texts."""
    tool_counts = {}  # tool_name -> count
    long_texts = []
    commits = []  # list of (hash, message, timestamp)

    for log_type, message_json, timestamp in messages:
        if not message_json:
            continue
        try:
            message_data = json.loads(message_json)
        except json.JSONDecodeError:
            continue

        content = message_data.get("content", [])
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type", "")

            if block_type == "tool_use":
                tool_name = block.get("name", "Unknown")
                tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1
            elif block_type == "tool_result":
                # Check for git commit output
                result_content = block.get("content", "")
                if isinstance(result_content, str):
                    for match in COMMIT_PATTERN.finditer(result_content):
                        commits.append((match.group(1), match.group(2), timestamp))
            elif block_type == "text":
                text = block.get("text", "")
                if len(text) >= LONG_TEXT_THRESHOLD:
                    long_texts.append(text)

    return {
        "tool_counts": tool_counts,
        "long_texts": long_texts,
        "commits": commits,
    }


def format_tool_stats(tool_counts):
    """Format tool counts into a concise summary string."""
    if not tool_counts:
        return ""

    # Abbreviate common tool names
    abbrev = {
        "Bash": "bash",
        "Read": "read",
        "Write": "write",
        "Edit": "edit",
        "Glob": "glob",
        "Grep": "grep",
        "Task": "task",
        "TodoWrite": "todo",
        "WebFetch": "fetch",
        "WebSearch": "search",
    }

    parts = []
    for name, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
        short_name = abbrev.get(name, name.lower())
        parts.append(f"{count} {short_name}")

    return " · ".join(parts)


def is_tool_result_message(message_data):
    """Check if a message contains only tool_result blocks."""
    content = message_data.get("content", [])
    if not isinstance(content, list):
        return False
    if not content:
        return False
    return all(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def render_message(log_type, message_json, timestamp):
    if not message_json:
        return ""
    try:
        message_data = json.loads(message_json)
    except json.JSONDecodeError:
        return ""
    if log_type == "user":
        content_html = render_user_message_content(message_data)
        # Check if this is a tool result message
        if is_tool_result_message(message_data):
            role_class, role_label = "tool-reply", "Tool reply"
        else:
            role_class, role_label = "user", "User"
    elif log_type == "assistant":
        content_html = render_assistant_message(message_data)
        role_class, role_label = "assistant", "Assistant"
    else:
        return ""
    if not content_html.strip():
        return ""
    msg_id = make_msg_id(timestamp)
    timestamp_title = format_timestamp_hover_title(timestamp)
    return _macros.message(
        role_class, role_label, msg_id, timestamp, timestamp_title, content_html
    )


CSS = """
:root { --bg-color: #0a0f1a; --card-bg: #111827; --user-bg: #0c2340; --user-border: #4da3ff; --fav-border: #fbbf24; --fav-bg: rgba(251, 191, 36, 0.06); --fav-text: #fcd34d; --assistant-bg: #111827; --assistant-border: #334155; --thinking-bg: #0f1d32; --thinking-border: #3b82f6; --thinking-text: #cbd5e1; --tool-bg: #0f2344; --tool-border: #60a5fa; --tool-result-bg: #0c1a2e; --tool-error-bg: #3b0f1a; --text-color: #f1f5f9; --text-muted: #94a3b8; --code-bg: #060d1a; --code-text: #a5d8ff; --inline-code-bg: rgba(56, 189, 248, 0.10); --inline-code-border: rgba(56, 189, 248, 0.25); --inline-code-text: #7dd3fc; --link-color: #60a5fa; --mark-bg: rgba(96, 165, 250, 0.35); --mark-text: #f1f5f9; --mono-font: 'SF Mono', 'Fira Code', 'JetBrains Mono', 'Cascadia Code', ui-monospace, monospace; --commit-bg: rgba(251, 191, 36, 0.10); --commit-border: #fbbf24; --commit-text: #fcd34d; --commit-hash: #fbbf24; --commit-msg: #f1f5f9; }
* { box-sizing: border-box; }
html { color-scheme: dark; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg-color); color: var(--text-color); margin: 0; padding: 16px; line-height: 1.7; font-size: 15px; }
.container { max-width: 920px; margin: 0 auto; }
h1 { font-size: 1.5rem; margin-bottom: 24px; padding-bottom: 8px; border-bottom: 2px solid var(--user-border); }
.message-content h2 { font-size: 1.2rem; margin: 20px 0 12px; color: var(--text-color); border-bottom: 1px solid rgba(120, 181, 255, 0.12); padding-bottom: 6px; }
.message-content h3 { font-size: 1.05rem; margin: 16px 0 8px; color: var(--text-color); }
.message-content h4 { font-size: 0.95rem; margin: 12px 0 6px; color: var(--text-muted); }
.header-row { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; border-bottom: 2px solid var(--user-border); padding-bottom: 10px; margin-bottom: 16px; }
.header-row h1 { border-bottom: none; padding-bottom: 0; margin-bottom: 0; flex: 1; min-width: 200px; }
.header-actions { display: flex; align-items: center; gap: 6px; }
.search-bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 20px; padding: 10px 14px; background: rgba(255,255,255,0.02); border: 1px solid rgba(120, 181, 255, 0.12); border-radius: 10px; }
.message { margin-bottom: 20px; border-radius: 12px; box-shadow: 0 10px 30px rgba(0,0,0,0.35); border: 1px solid rgba(120, 181, 255, 0.10); }
.message.user { background: var(--user-bg); border-left: 4px solid var(--user-border); }
.message.assistant { background: var(--card-bg); border-left: 4px solid var(--assistant-border); border: 1px solid rgba(148, 163, 184, 0.12); }
.message.tool-reply { background: var(--thinking-bg); border-left: 4px solid var(--tool-border); }
.tool-reply .role-label { color: var(--tool-border); }
.tool-reply .tool-result { background: transparent; padding: 0; margin: 0; }
.tool-reply .tool-result .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, var(--thinking-bg)); }
.message-header { display: flex; justify-content: space-between; align-items: center; padding: 8px 20px; background: rgba(255,255,255,0.03); font-size: 0.85rem; border-radius: 12px 12px 0 0; }
.role-label { font-weight: 600; text-transform: uppercase; font-size: 0.75rem; letter-spacing: 1px; }
.user .role-label { color: var(--user-border); }
time { color: var(--text-muted); font-size: 0.8rem; }
.timestamp-link { color: inherit; text-decoration: none; position: relative; }
.timestamp-link:hover { text-decoration: underline; }
.timestamp-link .tooltip { display: none; position: absolute; right: 0; bottom: calc(100% + 8px); background: #1e293b; color: #e2e8f0; border: 1px solid rgba(148, 163, 184, 0.25); border-radius: 8px; padding: 6px 12px; font-size: 0.8rem; white-space: nowrap; z-index: 100; box-shadow: 0 8px 24px rgba(0,0,0,0.5); pointer-events: none; }
.timestamp-link .tooltip::after { content: ''; position: absolute; top: 100%; right: 16px; border: 6px solid transparent; border-top-color: #1e293b; }
.timestamp-link:hover .tooltip { display: block; }
.message:target { animation: highlight 2s ease-out; }
@keyframes highlight { 0% { background-color: rgba(77, 163, 255, 0.25); } 100% { background-color: transparent; } }
.message-content { padding: 20px; }
.message-content p { margin: 0 0 12px 0; }
.message-content p:last-child { margin-bottom: 0; }
.thinking { background: var(--thinking-bg); border: 1px solid var(--thinking-border); border-radius: 8px; padding: 12px; margin: 12px 0; font-size: 0.9rem; color: var(--thinking-text); }
.thinking-label { font-size: 0.75rem; font-weight: 600; text-transform: uppercase; color: #f57c00; margin-bottom: 8px; }
.thinking p { margin: 8px 0; }
.assistant-text { margin: 8px 0; }
.tool-use { background: var(--tool-bg); border: 1px solid var(--tool-border); border-radius: 8px; padding: 12px; margin: 12px 0; }
.tool-header { font-weight: 600; color: var(--tool-border); margin-bottom: 8px; display: flex; align-items: center; gap: 8px; }
.tool-icon { font-size: 1.1rem; }
.tool-description { font-size: 0.9rem; color: var(--text-muted); margin-bottom: 8px; font-style: italic; }
.tool-result { background: var(--tool-result-bg); border-radius: 8px; padding: 12px; margin: 12px 0; }
.tool-result.tool-error { background: var(--tool-error-bg); }
.file-tool { border-radius: 8px; padding: 12px; margin: 12px 0; }
.write-tool { background: linear-gradient(135deg, rgba(77, 163, 255, 0.18) 0%, rgba(96, 165, 250, 0.10) 100%); border: 1px solid rgba(96, 165, 250, 0.35); }
.edit-tool { background: linear-gradient(135deg, rgba(30, 58, 138, 0.35) 0%, rgba(15, 26, 47, 0.0) 100%); border: 1px solid rgba(120, 181, 255, 0.28); }
.file-tool-header { font-weight: 600; margin-bottom: 4px; display: flex; align-items: center; gap: 8px; font-size: 0.95rem; }
.write-header { color: var(--link-color); }
.edit-header { color: var(--link-color); }
.file-tool-icon { font-size: 1rem; }
.file-tool-path { font-family: var(--mono-font); background: rgba(255,255,255,0.06); padding: 2px 8px; border-radius: 4px; border: 1px solid rgba(120, 181, 255, 0.12); border-left: 3px solid var(--user-border); }
.file-tool-fullpath { font-family: var(--mono-font); font-size: 0.8rem; color: var(--text-muted); margin-bottom: 8px; word-break: break-all; }
.file-content { margin: 0; }
.edit-section { display: flex; margin: 4px 0; border-radius: 4px; overflow: hidden; }
.edit-label { padding: 8px 12px; font-weight: bold; font-family: var(--mono-font); display: flex; align-items: flex-start; }
.edit-old { background: rgba(220, 38, 38, 0.10); }
.edit-old .edit-label { color: #fecaca; background: rgba(220, 38, 38, 0.18); }
.edit-old .edit-content { color: #fecaca; }
.edit-new { background: rgba(34, 197, 94, 0.10); }
.edit-new .edit-label { color: #bbf7d0; background: rgba(34, 197, 94, 0.18); }
.edit-new .edit-content { color: #bbf7d0; }
.edit-content { margin: 0; flex: 1; background: transparent; font-size: 0.85rem; }
.edit-replace-all { font-size: 0.75rem; font-weight: normal; color: var(--text-muted); }
.write-tool .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, rgba(15, 35, 68, 0.95)); }
.edit-tool .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, rgba(15, 26, 47, 0.95)); }
.todo-list { background: linear-gradient(135deg, rgba(77, 163, 255, 0.14) 0%, rgba(96, 165, 250, 0.06) 100%); border: 1px solid rgba(120, 181, 255, 0.20); border-radius: 8px; padding: 12px; margin: 12px 0; }
.todo-header { font-weight: 600; color: var(--link-color); margin-bottom: 10px; display: flex; align-items: center; gap: 8px; font-size: 0.95rem; }
.todo-items { list-style: none; margin: 0; padding: 0; }
.todo-item { display: flex; align-items: flex-start; gap: 10px; padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.06); font-size: 0.9rem; }
.todo-item:last-child { border-bottom: none; }
.todo-icon { flex-shrink: 0; width: 20px; height: 20px; display: flex; align-items: center; justify-content: center; font-weight: bold; border-radius: 50%; }
.todo-completed .todo-icon { color: #9cc5ff; background: rgba(77, 163, 255, 0.18); }
.todo-completed .todo-content { color: rgba(230, 238, 252, 0.75); text-decoration: line-through; }
.todo-in-progress .todo-icon { color: #9cc5ff; background: rgba(77, 163, 255, 0.18); }
.todo-in-progress .todo-content { color: var(--text-color); font-weight: 500; }
.todo-pending .todo-icon { color: var(--text-muted); background: rgba(255,255,255,0.05); }
.todo-pending .todo-content { color: var(--text-muted); }
pre { font-family: var(--mono-font); background: var(--code-bg); color: var(--code-text); padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 0.85rem; line-height: 1.5; margin: 8px 0; white-space: pre-wrap; word-wrap: break-word; }
pre.json { color: #e0e0e0; }
code { font-family: var(--mono-font); background: var(--inline-code-bg); border: 1px solid var(--inline-code-border); color: var(--inline-code-text); padding: 2px 7px; border-radius: 5px; font-size: 0.85em; font-weight: 500; }
pre code { background: none; padding: 0; border: none; color: inherit; font-weight: normal; }
.container a { color: var(--link-color); }
.container a:hover { color: #9cc5ff; }
.user-content { margin: 0; }
.truncatable { position: relative; }
.truncatable.truncated .truncatable-content { max-height: 200px; overflow: hidden; }
.truncatable.truncated::after { content: ''; position: absolute; bottom: 32px; left: 0; right: 0; height: 60px; background: linear-gradient(to bottom, transparent, var(--card-bg)); pointer-events: none; }
.message.user .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, var(--user-bg)); }
.message.tool-reply .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, var(--thinking-bg)); }
.tool-use .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, var(--tool-bg)); }
.tool-result .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, var(--tool-result-bg)); }
.expand-btn { display: none; width: 100%; padding: 6px 16px; margin-top: 4px; background: transparent; border: none; border-top: 1px solid rgba(120, 181, 255, 0.10); cursor: pointer; font-size: 0.8rem; color: var(--link-color); letter-spacing: 0.3px; }
.expand-btn:hover { color: #9cc5ff; background: rgba(255,255,255,0.02); }
.truncatable.truncated .expand-btn, .truncatable.expanded .expand-btn { display: block; }
.pagination { display: flex; justify-content: center; align-items: center; gap: 6px; margin: 24px 0; flex-wrap: wrap; }
.pagination a, .pagination span { padding: 5px 10px; border-radius: 6px; text-decoration: none; font-size: 0.85rem; }
.pagination a { background: rgba(255,255,255,0.03); color: var(--link-color); border: 1px solid rgba(120, 181, 255, 0.25); }
.pagination a:hover { background: rgba(120, 181, 255, 0.10); }
.pagination .current { background: var(--user-border); color: white; }
.pagination .disabled { color: var(--text-muted); border: 1px solid rgba(155, 176, 203, 0.25); }
.pagination .index-link { background: var(--user-border); color: white; }
.pagination .ellipsis { color: var(--text-muted); border: none; padding: 5px 4px; font-size: 0.85rem; letter-spacing: 1px; }
.pagination-compact { display: flex; justify-content: center; align-items: center; gap: 10px; margin: 16px 0; font-size: 0.85rem; color: var(--text-muted); }
.pagination-compact a { padding: 5px 10px; border-radius: 6px; text-decoration: none; background: rgba(255,255,255,0.03); color: var(--link-color); border: 1px solid rgba(120, 181, 255, 0.25); font-size: 0.85rem; }
.pagination-compact a:hover { background: rgba(120, 181, 255, 0.10); }
.pagination-compact .disabled { padding: 5px 10px; border-radius: 6px; color: var(--text-muted); border: 1px solid rgba(155, 176, 203, 0.25); font-size: 0.85rem; }
.pagination-compact .page-info { color: var(--text-muted); }
.pagination-compact .index-link { padding: 5px 10px; border-radius: 6px; text-decoration: none; background: var(--user-border); color: white; font-size: 0.85rem; }
details.continuation { margin-bottom: 20px; }
details.continuation summary { cursor: pointer; padding: 12px 16px; background: var(--user-bg); border-left: 4px solid var(--user-border); border-radius: 12px; font-weight: 500; color: var(--text-muted); }
details.continuation summary:hover { background: rgba(25, 118, 210, 0.15); }
details.continuation[open] summary { border-radius: 12px 12px 0 0; margin-bottom: 0; }
.index-item { margin-bottom: 16px; border-radius: 12px; overflow: hidden; box-shadow: 0 10px 30px rgba(0,0,0,0.35); background: rgba(13, 42, 74, 0.65); border-left: 4px solid var(--user-border); border: 1px solid rgba(120, 181, 255, 0.10); }
.index-item a { display: block; text-decoration: none; color: inherit; }
.index-item a:hover { background: rgba(120, 181, 255, 0.08); }
.index-item-header { display: flex; justify-content: space-between; align-items: center; padding: 8px 16px; background: rgba(255,255,255,0.03); font-size: 0.85rem; }
.index-item-number { font-weight: 600; color: var(--user-border); }
.index-item-content { padding: 16px; }
.index-item-stats { padding: 8px 16px 12px 32px; font-size: 0.85rem; color: var(--text-muted); border-top: 1px solid rgba(255,255,255,0.06); }
.index-item--favorite { border-left: 4px solid var(--fav-border); border-color: rgba(251, 191, 36, 0.22); background: linear-gradient(135deg, rgba(251, 191, 36, 0.18) 0%, rgba(13, 42, 74, 0.65) 70%); }
.index-item--favorite .index-item-number { color: var(--fav-text); }
.index-item--favorite a:hover { background: rgba(251, 191, 36, 0.10); }
.favorites-section { margin-bottom: 28px; border: 1px solid rgba(251, 191, 36, 0.22); background: linear-gradient(180deg, rgba(251, 191, 36, 0.10) 0%, rgba(13, 42, 74, 0.0) 100%); border-radius: 14px; padding: 14px; box-shadow: 0 20px 60px rgba(0,0,0,0.35); }
.favorites-section .index-item { box-shadow: 0 6px 18px rgba(0,0,0,0.25); }
.section-header { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; margin-bottom: 12px; }
.section-title { font-size: 0.95rem; margin: 0; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.8px; }
.section-meta { color: rgba(155, 176, 203, 0.85); font-size: 0.85rem; }
.section-empty { color: var(--text-muted); margin: 0; }
.section-divider { margin: 32px 0 16px; padding-top: 20px; border-top: 1px solid rgba(120, 181, 255, 0.18); }
.index-item-commit { margin-top: 6px; padding: 4px 8px; background: var(--commit-bg); border-radius: 4px; font-size: 0.85rem; color: var(--commit-text); }
.index-item-commit code { background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px; font-size: 0.8rem; margin-right: 6px; color: var(--commit-hash); }
.commit-card { margin: 8px 0; padding: 10px 14px; background: var(--commit-bg); border-left: 4px solid var(--commit-border); border-radius: 6px; }
.commit-card a { text-decoration: none; color: var(--commit-msg); display: block; }
.commit-card a:hover { color: var(--commit-text); }
.commit-card-hash { font-family: var(--mono-font); color: var(--commit-hash); font-weight: 600; margin-right: 8px; }
.index-commit { margin-bottom: 12px; padding: 10px 16px; background: var(--commit-bg); border-left: 4px solid var(--commit-border); border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.20); }
.index-commit a { display: block; text-decoration: none; color: inherit; }
.index-commit a:hover { background: rgba(251, 191, 36, 0.08); margin: -10px -16px; padding: 10px 16px; border-radius: 8px; }
.index-commit-header { display: flex; justify-content: space-between; align-items: center; font-size: 0.85rem; margin-bottom: 4px; }
.index-commit-hash { font-family: var(--mono-font); color: var(--commit-hash); font-weight: 600; }
.index-commit-msg { color: var(--commit-msg); }
.index-item-long-text { margin-top: 8px; padding: 12px; background: rgba(255,255,255,0.03); border-radius: 8px; border-left: 3px solid var(--assistant-border); }
.index-item-long-text .truncatable.truncated::after { background: linear-gradient(to bottom, transparent, var(--card-bg)); }
.index-item-long-text-content { color: var(--text-color); }
#search-box { display: flex; align-items: center; gap: 8px; }
#search-box input { padding: 6px 12px; border: 1px solid rgba(120, 181, 255, 0.25); border-radius: 6px; font-size: 16px; width: 180px; background: rgba(255,255,255,0.03); color: var(--text-color); }
#search-box input::placeholder { color: rgba(155, 176, 203, 0.75); }
#search-box select { padding: 6px 10px; border: 1px solid rgba(120, 181, 255, 0.25); border-radius: 6px; font-size: 16px; background: rgba(255,255,255,0.03); color: var(--text-color); max-width: 220px; }
.search-bar input { padding: 6px 12px; border: 1px solid rgba(120, 181, 255, 0.25); border-radius: 6px; font-size: 16px; flex: 1; min-width: 140px; background: rgba(255,255,255,0.03); color: var(--text-color); }
.search-bar input::placeholder { color: rgba(155, 176, 203, 0.75); }
.search-bar select { padding: 6px 10px; border: 1px solid rgba(120, 181, 255, 0.25); border-radius: 6px; font-size: 16px; background: rgba(255,255,255,0.03); color: var(--text-color); max-width: 220px; }
.search-bar button:not(.danger-btn):not(.fav-btn) { background: rgba(77, 163, 255, 0.15); color: var(--link-color); border: 1px solid rgba(120, 181, 255, 0.35); border-radius: 6px; padding: 6px 10px; cursor: pointer; display: flex; align-items: center; justify-content: center; }
.search-bar button:not(.danger-btn):not(.fav-btn):hover { background: rgba(77, 163, 255, 0.25); }
.home-btn { background: rgba(77, 163, 255, 0.15); color: var(--link-color); border: 1px solid rgba(120, 181, 255, 0.35); border-radius: 6px; padding: 6px 10px; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; text-decoration: none; font-size: 0.85rem; }
.home-btn:hover { background: rgba(77, 163, 255, 0.25); }
.fav-btn { background: rgba(255, 196, 77, 0.12); color: #ffe0a3; border: 1px solid rgba(255, 196, 77, 0.35); border-radius: 6px; padding: 6px 8px; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; font-size: 0.85rem; }
.fav-btn:hover { background: rgba(255, 196, 77, 0.20); }
.fav-btn:disabled { opacity: 0.6; cursor: not-allowed; }
.fav-btn[data-favorited="1"] { background: rgba(255, 196, 77, 0.22); border-color: rgba(255, 196, 77, 0.55); color: #fff0cf; }
.danger-btn { background: rgba(255, 77, 77, 0.08); color: #ff9999; border: 1px solid rgba(255, 120, 120, 0.20); border-radius: 6px; padding: 6px 8px; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; font-size: 0.8rem; }
.danger-btn:hover { background: rgba(255, 77, 77, 0.20); color: #ffb3b3; border-color: rgba(255, 120, 120, 0.35); }
.danger-btn:disabled { opacity: 0.6; cursor: not-allowed; }
.fav-btn--sm { padding: 4px 8px; font-size: 0.8rem; }
.danger-btn--sm { padding: 4px 6px; font-size: 0.75rem; }
.index-item-actions { display: inline-flex; align-items: center; gap: 8px; }
#search-box button:not(.danger-btn):not(.fav-btn), #modal-search-btn, #modal-close-btn { background: rgba(77, 163, 255, 0.15); color: var(--link-color); border: 1px solid rgba(120, 181, 255, 0.35); border-radius: 6px; padding: 6px 10px; cursor: pointer; display: flex; align-items: center; justify-content: center; }
#search-box button:not(.danger-btn):not(.fav-btn):hover, #modal-search-btn:hover { background: rgba(77, 163, 255, 0.25); }
#modal-close-btn { background: rgba(155, 176, 203, 0.15); color: var(--text-color); border: 1px solid rgba(155, 176, 203, 0.35); margin-left: 8px; }
#modal-close-btn:hover { background: rgba(155, 176, 203, 0.25); }
#search-modal[open] { border: 1px solid rgba(120, 181, 255, 0.20); border-radius: 12px; box-shadow: 0 20px 60px rgba(0,0,0,0.55); padding: 0; width: 90vw; max-width: 900px; height: 80vh; max-height: 80vh; display: flex; flex-direction: column; background: var(--card-bg); color: var(--text-color); }
#search-modal::backdrop { background: rgba(0,0,0,0.5); }
.search-modal-header { display: flex; align-items: center; gap: 8px; padding: 16px; border-bottom: 1px solid rgba(120, 181, 255, 0.15); background: rgba(255,255,255,0.02); border-radius: 12px 12px 0 0; }
.search-modal-header select { padding: 8px 10px; border: 1px solid rgba(120, 181, 255, 0.25); border-radius: 6px; font-size: 16px; background: rgba(255,255,255,0.03); color: var(--text-color); max-width: 260px; }
.search-modal-header input { flex: 1; padding: 8px 12px; border: 1px solid rgba(120, 181, 255, 0.25); border-radius: 6px; font-size: 16px; background: rgba(255,255,255,0.03); color: var(--text-color); }
.search-modal-header input::placeholder { color: rgba(155, 176, 203, 0.75); }
#search-status { padding: 8px 16px; font-size: 0.85rem; color: var(--text-muted); border-bottom: 1px solid rgba(255,255,255,0.06); }
#search-results { flex: 1; overflow-y: auto; padding: 16px; }
.search-result { margin-bottom: 16px; border-radius: 8px; overflow: hidden; box-shadow: 0 10px 30px rgba(0,0,0,0.35); border: 1px solid rgba(120, 181, 255, 0.10); background: rgba(255,255,255,0.02); }
.search-result a { display: block; text-decoration: none; color: inherit; }
.search-result a:hover { background: rgba(120, 181, 255, 0.08); }
.search-result-page { padding: 6px 12px; background: rgba(255,255,255,0.03); font-size: 0.8rem; color: var(--text-muted); border-bottom: 1px solid rgba(255,255,255,0.06); }
.search-result-content { padding: 12px; }
.search-result mark { background: var(--mark-bg); color: var(--mark-text); padding: 1px 2px; border-radius: 3px; }
@media (max-width: 600px) { body { padding: 8px; } .message, .index-item { border-radius: 8px; } .message-content, .index-item-content { padding: 12px; } .message-header { padding: 8px 12px; } pre { font-size: 0.8rem; padding: 8px; } #search-box input { width: 120px; } #search-box select { max-width: 120px; } .search-bar { padding: 8px 10px; } .search-bar input { min-width: 100px; } .search-bar select { max-width: 120px; } .search-modal-header select { max-width: 140px; } #search-modal[open] { width: 95vw; height: 90vh; } .header-actions { gap: 4px; } }
"""

JS = """
document.querySelectorAll('time[data-timestamp]').forEach(function(el) {
    const timestamp = el.getAttribute('data-timestamp');
    const date = new Date(timestamp);
    const now = new Date();
    const isToday = date.toDateString() === now.toDateString();
    const timeStr = date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    if (isToday) { el.textContent = timeStr; }
    else { el.textContent = date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' ' + timeStr; }
});
document.querySelectorAll('pre.json').forEach(function(el) {
    let text = el.textContent;
    text = text.replace(/"([^"]+)":/g, '<span style="color: #ce93d8">"$1"</span>:');
    text = text.replace(/: "([^"]*)"/g, ': <span style="color: #81d4fa">"$1"</span>');
    text = text.replace(/: (\\d+)/g, ': <span style="color: #ffcc80">$1</span>');
    text = text.replace(/: (true|false|null)/g, ': <span style="color: #f48fb1">$1</span>');
    el.innerHTML = text;
});
document.querySelectorAll('.truncatable').forEach(function(wrapper) {
    const content = wrapper.querySelector('.truncatable-content');
    const btn = wrapper.querySelector('.expand-btn');
    if (content.scrollHeight > 250) {
        wrapper.classList.add('truncated');
        btn.innerHTML = '\u25BC Show more';
        btn.addEventListener('click', function() {
            if (wrapper.classList.contains('truncated')) { wrapper.classList.remove('truncated'); wrapper.classList.add('expanded'); btn.innerHTML = '\u25B2 Show less'; }
            else { wrapper.classList.remove('expanded'); wrapper.classList.add('truncated'); btn.innerHTML = '\u25BC Show more'; }
        });
    }
});
"""

# Search UI for batch archive (works on file:// via <script src> assets).
ARCHIVE_SEARCH_UI_JS = r"""
(function () {
  const index = window.__CODEX_TRANSCRIPTS_SEARCH_INDEX;
  if (!Array.isArray(index) || index.length === 0) return;

  const searchBox = document.getElementById("search-box");
  const searchInput = document.getElementById("search-input");
  const searchBtn = document.getElementById("search-btn");
  const projectSelect = document.getElementById("search-project-select");
  const sortSelect = document.getElementById("search-sort-select");
  const modal = document.getElementById("search-modal");
  const modalProjectSelect = document.getElementById("modal-project-select");
  const modalSortSelect = document.getElementById("modal-sort-select");
  const modalInput = document.getElementById("modal-search-input");
  const modalSearchBtn = document.getElementById("modal-search-btn");
  const modalCloseBtn = document.getElementById("modal-close-btn");
  const searchStatus = document.getElementById("search-status");
  const searchResults = document.getElementById("search-results");

  if (
    !searchBox ||
    !searchInput ||
    !searchBtn ||
    !projectSelect ||
    !sortSelect ||
    !modal ||
    !modalProjectSelect ||
    !modalSortSelect ||
    !modalInput ||
    !modalSearchBtn ||
    !modalCloseBtn ||
    !searchStatus ||
    !searchResults
  )
    return;

  const root = window.__CODEX_ARCHIVE_ROOT || "";

  const projectMap = new Map();
  index.forEach((entry) => {
    const dir = (entry.project_dir || "").trim();
    if (!dir) return;
    if (!projectMap.has(dir)) projectMap.set(dir, entry.project || dir);
  });

  function populateProjectSelect(selectEl) {
    selectEl.innerHTML = "";

    const allOption = document.createElement("option");
    allOption.value = "";
    allOption.textContent = "All projects";
    selectEl.appendChild(allOption);

    const items = Array.from(projectMap.entries()).sort((a, b) =>
      (a[1] || a[0]).localeCompare(b[1] || b[0])
    );
    for (const [dir, label] of items) {
      const opt = document.createElement("option");
      opt.value = dir;
      opt.textContent = label || dir;
      selectEl.appendChild(opt);
    }
  }

  populateProjectSelect(projectSelect);
  populateProjectSelect(modalProjectSelect);

  const defaultProject = (window.__CODEX_TRANSCRIPTS_DEFAULT_PROJECT || "").trim();
  const initialProject = projectMap.has(defaultProject) ? defaultProject : "";
  projectSelect.value = initialProject;
  modalProjectSelect.value = initialProject;

  function populateSortSelect(selectEl) {
    selectEl.innerHTML = "";

    const options = [
      ["relevance", "Relevance"],
      ["newest", "Newest"],
      ["oldest", "Oldest"],
    ];
    for (const [value, label] of options) {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label;
      selectEl.appendChild(opt);
    }
  }

  populateSortSelect(sortSelect);
  populateSortSelect(modalSortSelect);

  sortSelect.value = "relevance";
  modalSortSelect.value = "relevance";

  function setProjectFilter(value) {
    projectSelect.value = value;
    modalProjectSelect.value = value;
  }

  function getProjectFilter() {
    return (modalProjectSelect.value || projectSelect.value || "").trim();
  }

  function getProjectLabel(dir) {
    return dir ? projectMap.get(dir) || dir : "All projects";
  }

  function setSort(value) {
    sortSelect.value = value;
    modalSortSelect.value = value;
  }

  function getSort() {
    return (modalSortSelect.value || sortSelect.value || "relevance").trim();
  }

  function normalizeForSearch(text) {
    return String(text || "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
  }

  index.forEach((entry) => {
    const combined =
      (entry.project || "") + " " + (entry.summary || "") + " " + (entry.text || "");
    entry.__haystack = normalizeForSearch(combined);
    entry.__mtime = Number(entry.mtime || 0);
  });

  function scoreEntry(query, entry) {
    const q = String(query || "").trim();
    const qNorm = normalizeForSearch(q);
    if (!qNorm) return null;

    const terms = qNorm.split(/\s+/).filter(Boolean);
    const hay = entry.__haystack || "";

    const indices = [];
    for (const term of terms) {
      const idx = hay.indexOf(term);
      if (idx === -1) return null;
      indices.push(idx);
    }

    const phraseIdx = hay.indexOf(qNorm);
    if (phraseIdx !== -1) {
      return phraseIdx;
    }

    let start = indices[0];
    let end = indices[0] + terms[0].length;
    for (let i = 1; i < indices.length; i++) {
      start = Math.min(start, indices[i]);
      end = Math.max(end, indices[i] + terms[i].length);
    }
    const window = end - start;
    return window * 10 + start;
  }

  function clearResults() {
    searchResults.innerHTML = "";
  }

  function snippetForEntry(entry, query) {
    const text = String(entry.text || "");
    if (!text) return "";

    const lower = text.toLowerCase();
    const q = String(query || "").trim().toLowerCase();

    let idx = q ? lower.indexOf(q) : -1;
    let matchLen = q.length;

    if (idx === -1) {
      const terms = normalizeForSearch(query).split(/\s+/).filter(Boolean);
      for (const term of terms) {
        const tIdx = lower.indexOf(term);
        if (tIdx !== -1) {
          idx = tIdx;
          matchLen = term.length;
          break;
        }
      }
    }

    if (idx === -1) idx = 0;

    const context = 120;
    const start = Math.max(0, idx - context);
    const end = Math.min(text.length, idx + matchLen + context);
    let snippet = text.slice(start, end).replace(/\s+/g, " ").trim();
    if (start > 0) snippet = "…" + snippet;
    if (end < text.length) snippet = snippet + "…";
    return snippet;
  }

  function buildHighlightedFragment(text, terms) {
    const frag = document.createDocumentFragment();
    const value = String(text || "");
    if (!terms.length) {
      frag.appendChild(document.createTextNode(value));
      return frag;
    }

    const escapedTerms = terms
      .slice()
      .sort((a, b) => b.length - a.length)
      .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    const re = new RegExp("(" + escapedTerms.join("|") + ")", "gi");

    let last = 0;
    let match;
    while ((match = re.exec(value)) !== null) {
      const before = value.slice(last, match.index);
      if (before) frag.appendChild(document.createTextNode(before));
      const mark = document.createElement("mark");
      mark.textContent = match[0];
      frag.appendChild(mark);
      last = match.index + match[0].length;
    }
    const rest = value.slice(last);
    if (rest) frag.appendChild(document.createTextNode(rest));
    return frag;
  }

  function renderResult(entry, query, terms) {
    const wrapper = document.createElement("div");
    wrapper.className = "search-result";

    const link = document.createElement("a");
    link.href = root + (entry.href || "");

    const page = document.createElement("div");
    page.className = "search-result-page";
    page.textContent =
      (entry.project || "Unknown project") +
      (entry.date ? " · " + entry.date : "");

    const content = document.createElement("div");
    content.className = "search-result-content";

    const title = document.createElement("div");
    title.style.fontWeight = "600";
    title.textContent = entry.summary || "(no summary)";

    const snippet = snippetForEntry(entry, query);
    if (snippet) {
      const preview = document.createElement("div");
      preview.style.color = "var(--text-muted)";
      preview.style.fontSize = "0.9rem";
      preview.style.marginTop = "6px";
      preview.appendChild(buildHighlightedFragment(snippet, terms));
      content.appendChild(preview);
    }

    content.prepend(title);

    link.appendChild(page);
    link.appendChild(content);
    wrapper.appendChild(link);
    return wrapper;
  }

  function performSearch(query) {
    const q = (query || "").trim();
    if (!q) {
      searchStatus.textContent = "Enter a search term";
      clearResults();
      return;
    }

    const projectFilter = getProjectFilter();
    const sortMode = getSort();
    const terms = normalizeForSearch(q).split(/\s+/).filter(Boolean);
    const scored = [];
    for (const entry of index) {
      if (projectFilter && entry.project_dir !== projectFilter) continue;
      const s = scoreEntry(q, entry);
      if (s === null) continue;
      scored.push([s, entry]);
    }

    scored.sort((a, b) => {
      const scoreA = a[0];
      const scoreB = b[0];
      const mA = a[1].__mtime || 0;
      const mB = b[1].__mtime || 0;

      if (sortMode === "newest") {
        if (mA !== mB) return mB - mA;
        return scoreA - scoreB;
      }
      if (sortMode === "oldest") {
        if (mA !== mB) return mA - mB;
        return scoreA - scoreB;
      }
      // relevance
      if (scoreA !== scoreB) return scoreA - scoreB;
      return mB - mA;
    });

    const limit = 50;
    const matches = scored.slice(0, limit).map((x) => x[1]);

    searchStatus.textContent =
      "Found " +
      scored.length +
      " result(s)" +
      (projectFilter ? " in " + getProjectLabel(projectFilter) : "") +
      (sortMode === "newest"
        ? " (sorted newest)"
        : sortMode === "oldest"
        ? " (sorted oldest)"
        : "") +
      (scored.length > limit ? " (showing top " + limit + ")" : "");

    clearResults();
    for (const entry of matches) {
      searchResults.appendChild(renderResult(entry, q, terms));
    }
  }

  function openModal(query) {
    modalProjectSelect.value = projectSelect.value;
    modalSortSelect.value = sortSelect.value;
    modalInput.value = query || "";
    clearResults();
    searchStatus.textContent = "";
    modal.showModal();
    modalInput.focus();
    if (query) performSearch(query);
  }

  function closeModal() {
    modal.close();
  }

  searchBtn.addEventListener("click", function () {
    openModal(searchInput.value);
  });

  searchInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") openModal(searchInput.value);
  });

  projectSelect.addEventListener("change", function () {
    setProjectFilter(projectSelect.value);
  });

  modalProjectSelect.addEventListener("change", function () {
    setProjectFilter(modalProjectSelect.value);
    if (modal.open) performSearch(modalInput.value);
  });

  sortSelect.addEventListener("change", function () {
    setSort(sortSelect.value);
  });

  modalSortSelect.addEventListener("change", function () {
    setSort(modalSortSelect.value);
    if (modal.open) performSearch(modalInput.value);
  });

  modalSearchBtn.addEventListener("click", function () {
    performSearch(modalInput.value);
  });

  modalInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") performSearch(modalInput.value);
  });

  modalCloseBtn.addEventListener("click", closeModal);

  modal.addEventListener("click", function (e) {
    if (e.target === modal) closeModal();
  });

  document.addEventListener("keydown", function (e) {
    const key = (e.key || "").toLowerCase();
    if ((e.ctrlKey || e.metaKey) && key === "k") {
      e.preventDefault();
      openModal(searchInput.value);
    }
  });
})();
"""

# JavaScript to fix relative URLs when served via gisthost.github.io or gistpreview.github.io
# Fixes issue #26: Pagination links broken on gisthost.github.io
GIST_PREVIEW_JS = r"""
(function() {
    var hostname = window.location.hostname;
    if (hostname !== 'gisthost.github.io' && hostname !== 'gistpreview.github.io') return;
    // URL format: https://gisthost.github.io/?GIST_ID/filename.html
    var match = window.location.search.match(/^\?([^/]+)/);
    if (!match) return;
    var gistId = match[1];

    function rewriteLinks(root) {
        (root || document).querySelectorAll('a[href]').forEach(function(link) {
            var href = link.getAttribute('href');
            // Skip already-rewritten links (issue #26 fix)
            if (href.startsWith('?')) return;
            // Skip external links and anchors
            if (href.startsWith('http') || href.startsWith('#') || href.startsWith('//')) return;
            // Handle anchor in relative URL (e.g., page-001.html#msg-123)
            var parts = href.split('#');
            var filename = parts[0];
            var anchor = parts.length > 1 ? '#' + parts[1] : '';
            link.setAttribute('href', '?' + gistId + '/' + filename + anchor);
        });
    }

    // Run immediately
    rewriteLinks();

    // Also run on DOMContentLoaded in case DOM isn't ready yet
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function() { rewriteLinks(); });
    }

    // Use MutationObserver to catch dynamically added content
    // gistpreview.github.io may add content after initial load
    var observer = new MutationObserver(function(mutations) {
        mutations.forEach(function(mutation) {
            mutation.addedNodes.forEach(function(node) {
                if (node.nodeType === 1) { // Element node
                    rewriteLinks(node);
                    // Also check if the node itself is a link
                    if (node.tagName === 'A' && node.getAttribute('href')) {
                        var href = node.getAttribute('href');
                        if (!href.startsWith('?') && !href.startsWith('http') &&
                            !href.startsWith('#') && !href.startsWith('//')) {
                            var parts = href.split('#');
                            var filename = parts[0];
                            var anchor = parts.length > 1 ? '#' + parts[1] : '';
                            node.setAttribute('href', '?' + gistId + '/' + filename + anchor);
                        }
                    }
                }
            });
        });
    });

    // Start observing once body exists
    function startObserving() {
        if (document.body) {
            observer.observe(document.body, { childList: true, subtree: true });
        } else {
            setTimeout(startObserving, 10);
        }
    }
    startObserving();

    // Handle fragment navigation after dynamic content loads
    // gisthost.github.io/gistpreview.github.io loads content dynamically, so the browser's
    // native fragment navigation fails because the element doesn't exist yet
    function scrollToFragment() {
        var hash = window.location.hash;
        if (!hash) return false;
        var targetId = hash.substring(1);
        var target = document.getElementById(targetId);
        if (target) {
            target.scrollIntoView({ behavior: 'smooth', block: 'start' });
            return true;
        }
        return false;
    }

    // Try immediately in case content is already loaded
    if (!scrollToFragment()) {
        // Retry with increasing delays to handle dynamic content loading
        var delays = [100, 300, 500, 1000, 2000];
        delays.forEach(function(delay) {
            setTimeout(scrollToFragment, delay);
        });
    }
})();
"""


def inject_gist_preview_js(output_dir):
    """Inject gist preview JavaScript into all HTML files in the output directory."""
    output_dir = Path(output_dir)
    for html_file in output_dir.glob("*.html"):
        content = html_file.read_text(encoding="utf-8")
        # Insert the gist preview JS before the closing </body> tag
        if "</body>" in content:
            content = content.replace(
                "</body>", f"<script>{GIST_PREVIEW_JS}</script>\n</body>"
            )
            html_file.write_text(content, encoding="utf-8")


def create_gist(output_dir, public=False):
    """Create a GitHub gist from the HTML files in output_dir.

    Returns the gist ID on success, or raises click.ClickException on failure.
    """
    output_dir = Path(output_dir)
    html_files = list(output_dir.glob("*.html"))
    if not html_files:
        raise click.ClickException("No HTML files found to upload to gist.")

    # Build the gh gist create command
    # gh gist create file1 file2 ... --public/--private
    cmd = ["gh", "gist", "create"]
    cmd.extend(str(f) for f in sorted(html_files))
    if public:
        cmd.append("--public")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        # Output is the gist URL, e.g., https://gist.github.com/username/GIST_ID
        gist_url = result.stdout.strip()
        # Extract gist ID from URL
        gist_id = gist_url.rstrip("/").split("/")[-1]
        return gist_id, gist_url
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.strip() if e.stderr else str(e)
        raise click.ClickException(f"Failed to create gist: {error_msg}")
    except FileNotFoundError:
        raise click.ClickException(
            "gh CLI not found. Install it from https://cli.github.com/ and run 'gh auth login'."
        )


def generate_pagination_html(current_page, total_pages):
    return _macros.pagination(current_page, total_pages)


def generate_pagination_compact_html(current_page, total_pages):
    return _macros.pagination_compact(current_page, total_pages)


def generate_index_pagination_html(total_pages):
    """Generate pagination for index page where Index is current (first page)."""
    return _macros.index_pagination(total_pages)


def generate_index_pagination_compact_html(total_pages):
    """Generate compact top pagination for index page."""
    return _macros.index_pagination_compact(total_pages)


def generate_html(
    json_path,
    output_dir,
    github_repo=None,
    *,
    data=None,
    search_enabled=False,
    archive_root="",
    project_dir="",
):
    output_dir = Path(output_dir)
    output_dir.mkdir(exist_ok=True)

    # Load session file (supports both JSON and JSONL)
    data = data if isinstance(data, dict) else parse_session_file(json_path)

    session_meta = data.get("session_meta", {}) or {}
    loglines = data.get("loglines", [])

    # Auto-detect GitHub repo if not provided
    if github_repo is None:
        github_repo = detect_github_repo(session_meta, loglines)
        if github_repo:
            print(f"Auto-detected GitHub repo: {github_repo}")
        else:
            print(
                "Warning: Could not auto-detect GitHub repo. Commit links will be disabled."
            )

    # Set module-level variable for render functions
    global _github_repo
    _github_repo = github_repo

    conversations = _build_conversations(loglines)
    # Newest-first: page 1 should show the most recent prompts.
    conversations.reverse()

    total_convs = len(conversations)
    total_pages = (total_convs + PROMPTS_PER_PAGE - 1) // PROMPTS_PER_PAGE

    page_template = get_template("page.html")

    if total_pages == 0:
        page_content = page_template.render(
            css=CSS,
            js=JS,
            page_num=1,
            total_pages=1,
            pagination_html=generate_pagination_html(1, 1),
            pagination_compact_html=generate_pagination_compact_html(1, 1),
            messages_html="",
            search_enabled=search_enabled,
            archive_root=archive_root,
            project_dir=project_dir,
            session_meta=session_meta,
        )
        (output_dir / "index.html").write_text(page_content, encoding="utf-8")
        print(f"Generated {output_dir / 'index.html'} (0 prompts)")
        return

    for page_num in range(1, total_pages + 1):
        start_idx = (page_num - 1) * PROMPTS_PER_PAGE
        end_idx = min(start_idx + PROMPTS_PER_PAGE, total_convs)
        page_convs = conversations[start_idx:end_idx]
        messages_html = []
        for conv in page_convs:
            # Render newest messages first so timestamps are strictly descending.
            for msg_log_type, msg_json, msg_timestamp in reversed(conv["messages"]):
                msg_html = render_message(msg_log_type, msg_json, msg_timestamp)
                if msg_html:
                    messages_html.append(msg_html)

        page_content = page_template.render(
            css=CSS,
            js=JS,
            page_num=page_num,
            total_pages=total_pages,
            pagination_html=generate_pagination_html(page_num, total_pages),
            pagination_compact_html=generate_pagination_compact_html(page_num, total_pages),
            messages_html="".join(messages_html),
            search_enabled=search_enabled,
            archive_root=archive_root,
            project_dir=project_dir,
            session_meta=session_meta,
        )

        if page_num == 1:
            # index.html is the canonical entrypoint, but pagination links expect
            # page-001.html for page 1 when navigating from older pages.
            (output_dir / "index.html").write_text(page_content, encoding="utf-8")
            if total_pages > 1:
                (output_dir / "page-001.html").write_text(
                    page_content, encoding="utf-8"
                )
        else:
            (output_dir / f"page-{page_num:03d}.html").write_text(
                page_content, encoding="utf-8"
            )

    print(
        f"Generated {output_dir / 'index.html'} ({total_convs} prompts, {total_pages} pages)"
    )


@click.group(cls=DefaultGroup, default="all", default_if_no_args=True)
@click.version_option(None, "-v", "--version", package_name="codex-transcripts")
def cli():
    """Convert Codex CLI session files to mobile-friendly HTML pages."""
    pass


def is_url(path):
    """Check if a path is a URL (starts with http:// or https://)."""
    return path.startswith("http://") or path.startswith("https://")


def fetch_url_to_tempfile(url):
    """Fetch a URL and save to a temporary file.

    Returns the Path to the temporary file.
    Raises click.ClickException on network errors.
    """
    try:
        response = httpx.get(url, timeout=60.0, follow_redirects=True)
        response.raise_for_status()
    except httpx.RequestError as e:
        raise click.ClickException(f"Failed to fetch URL: {e}")
    except httpx.HTTPStatusError as e:
        raise click.ClickException(
            f"Failed to fetch URL: {e.response.status_code} {e.response.reason_phrase}"
        )

    # Determine file extension from URL
    url_path = url.split("?")[0]  # Remove query params
    if url_path.endswith(".jsonl"):
        suffix = ".jsonl"
    elif url_path.endswith(".json"):
        suffix = ".json"
    else:
        suffix = ".jsonl"  # Default to JSONL

    # Extract a name from the URL for the temp file
    url_name = Path(url_path).stem or "session"

    temp_dir = Path(tempfile.gettempdir())
    temp_file = temp_dir / f"codex-url-{url_name}{suffix}"
    temp_file.write_text(response.text, encoding="utf-8")
    return temp_file


@cli.command("json")
@click.argument("json_file", type=click.Path())
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    help="Output directory. If not specified, writes to temp dir and opens in browser.",
)
@click.option(
    "-a",
    "--output-auto",
    is_flag=True,
    help="Auto-name output subdirectory based on filename (uses -o as parent, or current dir).",
)
@click.option(
    "--repo",
    help="GitHub repo (owner/name) for commit links. Auto-detected from git push output if not specified.",
)
@click.option(
    "--gist",
    is_flag=True,
    help="Upload to GitHub Gist and output a gisthost.github.io URL.",
)
@click.option(
    "--json",
    "include_json",
    is_flag=True,
    help="Include the original JSON session file in the output directory.",
)
@click.option(
    "--open",
    "open_browser",
    is_flag=True,
    help="Open the generated index.html in your default browser (default if no -o specified).",
)
def json_cmd(json_file, output, output_auto, repo, gist, include_json, open_browser):
    """Convert a Codex CLI session JSONL file (or URL) to HTML."""
    # Handle URL input
    if is_url(json_file):
        click.echo(f"Fetching {json_file}...")
        temp_file = fetch_url_to_tempfile(json_file)
        json_file_path = temp_file
        # Use URL path for naming
        url_name = Path(json_file.split("?")[0]).stem or "session"
    else:
        # Validate that local file exists
        json_file_path = Path(json_file)
        if not json_file_path.exists():
            raise click.ClickException(f"File not found: {json_file}")
        url_name = None

    # Determine output directory and whether to open browser
    # If no -o specified, use temp dir and open browser by default
    auto_open = output is None and not gist and not output_auto
    if output_auto:
        # Use -o as parent dir (or current dir), with auto-named subdirectory
        parent_dir = Path(output) if output else Path(".")
        output = parent_dir / (url_name or json_file_path.stem)
    elif output is None:
        output = (
            Path(tempfile.gettempdir())
            / f"codex-session-{url_name or json_file_path.stem}"
        )

    output = Path(output)
    generate_html(json_file_path, output, github_repo=repo)

    # Show output directory
    click.echo(f"Output: {output.resolve()}")

    # Copy JSON file to output directory if requested
    if include_json:
        output.mkdir(exist_ok=True)
        json_dest = output / json_file_path.name
        shutil.copy(json_file_path, json_dest)
        json_size_kb = json_dest.stat().st_size / 1024
        click.echo(f"JSON: {json_dest} ({json_size_kb:.1f} KB)")

    if gist:
        # Inject gist preview JS and create gist
        inject_gist_preview_js(output)
        click.echo("Creating GitHub gist...")
        gist_id, gist_url = create_gist(output)
        preview_url = f"https://gisthost.github.io/?{gist_id}/index.html"
        click.echo(f"Gist: {gist_url}")
        click.echo(f"Preview: {preview_url}")

    if open_browser or auto_open:
        index_url = (output / "index.html").resolve().as_uri()
        webbrowser.open(index_url)


@cli.command("all")
@click.option(
    "-s",
    "--source",
    type=click.Path(exists=True),
    help="Source directory containing Codex sessions (default: ~/.codex/sessions).",
)
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    default=None,
    help="Output directory for the archive (default: ~/.codex/transcripts).",
)
@click.option(
    "--include-agents",
    is_flag=True,
    help="Ignored (kept for compatibility).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be converted without creating files.",
)
@click.option(
    "--open/--no-open",
    "open_browser",
    default=True,
    help="Open the generated archive in your default browser.",
)
@click.option(
    "-q",
    "--quiet",
    is_flag=True,
    help="Suppress all output except errors.",
)
def all_cmd(source, output, include_agents, dry_run, open_browser, quiet):
    """Convert all local Codex CLI sessions to a browsable HTML archive.

    Creates a directory structure with:
    - Master index listing all projects
    - Per-project pages listing sessions
    - Individual session transcripts
    """
    # Default source folder
    if source is None:
        source = Path.home() / ".codex" / "sessions"
    else:
        source = Path(source)

    if not source.exists():
        raise click.ClickException(f"Source directory not found: {source}")

    output = Path(output) if output else Path.home() / ".codex" / "transcripts"

    if not quiet:
        click.echo(f"Scanning {source}...")

    projects = find_all_sessions(source, include_agents=include_agents)

    if not projects:
        if not quiet:
            click.echo("No sessions found.")
        return

    # Calculate totals
    total_sessions = sum(len(p["sessions"]) for p in projects)

    if not quiet:
        click.echo(f"Found {len(projects)} projects with {total_sessions} sessions")

    if dry_run:
        # Dry-run always outputs (it's the point of dry-run), but respects --quiet
        if not quiet:
            click.echo("\nDry run - would convert:")
            for project in projects:
                click.echo(
                    f"\n  {project.get('display_name') or project['name']} ({len(project['sessions'])} sessions)"
                )
                for session in project["sessions"][:3]:  # Show first 3
                    mod_time = datetime.fromtimestamp(session["mtime"])
                    click.echo(
                        f"    - {session['path'].stem} ({mod_time.strftime('%Y-%m-%d')})"
                    )
                if len(project["sessions"]) > 3:
                    click.echo(f"    ... and {len(project['sessions']) - 3} more")
        return

    if not quiet:
        click.echo(f"\nGenerating archive in {output}...")

    # Progress callback for non-quiet mode
    def on_progress(project_name, session_name, current, total):
        if not quiet and current % 10 == 0:
            click.echo(f"  Processed {current}/{total} sessions...")

    # Generate the archive using the library function
    stats = generate_batch_html(
        source,
        output,
        include_agents=include_agents,
        progress_callback=on_progress,
    )

    # Report any failures
    if stats["failed_sessions"]:
        click.echo(f"\nWarning: {len(stats['failed_sessions'])} session(s) failed:")
        for failure in stats["failed_sessions"]:
            click.echo(
                f"  {failure['project']}/{failure['session']}: {failure['error']}"
            )

    if not quiet:
        click.echo(
            f"\nGenerated archive with {stats['total_projects']} projects, "
            f"{stats['total_sessions']} sessions"
        )
        click.echo(f"Output: {output.resolve()}")

    if open_browser:
        index_url = (output / "index.html").resolve().as_uri()
        webbrowser.open(index_url)


def main():
    cli()
