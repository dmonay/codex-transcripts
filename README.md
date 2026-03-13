# codex-transcripts

Convert Codex CLI sessions (`.jsonl`) into a browsable, searchable HTML archive.

Codex CLI stores sessions under `~/.codex/sessions/` (typically nested by date).

## CLI (Static HTML)

Build/update the archive and open it:
```bash
uv run codex-transcripts
```

Explicit form:
```bash
uv run codex-transcripts all --source ~/.codex/sessions --output ~/.codex/transcripts --open
```

## Daemon Server (Live, Go)

Run a local server that:
- copies sessions into `~/.codex/transcriptsd/raw`
- sanitizes them into `~/.codex/transcriptsd/sanitized` (originals untouched)
- regenerates the archive into `~/.codex/transcriptsd/site`
- serves at `http://127.0.0.1:7878` and updates as sessions change (default poll: 5s)

Install the Python generator (used by the daemon to render the HTML archive):
```bash
uv tool install -e .
```

Build + run the daemon:
```bash
go build -o codex-transcriptsd ./cmd/codex-transcriptsd
./codex-transcriptsd --listen 127.0.0.1:7878
```

Open:
```bash
open http://127.0.0.1:7878/
```

Daemonize on macOS (launchd): `contrib/launchd/com.codex-transcriptsd.plist.example`

### Delete From `~/.codex/sessions`

When served by the daemon, pages include a **Delete** button that removes the original session file
from `~/.codex/sessions` (default: moves it into `~/.codex/transcriptsd/trash`).

Hard delete:
```bash
curl -X POST 'http://127.0.0.1:7878/api/delete?hard=1' \
  -H 'Content-Type: application/json' \
  -d '{"original_relpath":"2026/01/01/example.jsonl"}'
```
