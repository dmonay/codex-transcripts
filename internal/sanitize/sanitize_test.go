package sanitize

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

type exportFile struct {
	SessionMeta map[string]any `json:"session_meta"`
	Loglines    []struct {
		Type      string `json:"type"`
		Timestamp string `json:"timestamp"`
		Message   struct {
			Content []map[string]any `json:"content"`
		} `json:"message"`
	} `json:"loglines"`
}

func TestSanitizeJSONLToExport_StripsMetaAndNonDialogue(t *testing.T) {
	t.Parallel()

	tmp := t.TempDir()
	inPath := filepath.Join(tmp, "sess.jsonl")
	outPath := filepath.Join(tmp, "sess.json")

	input := "" +
		`{"timestamp":"2026-01-01T00:00:00.000Z","type":"session_meta","payload":{"id":"sess-123","cwd":"/Users/test/project","originator":"codex_cli_rs","source":"cli","git":{"repository_url":"git@github.com:example/project.git"}}}` + "\n" +
		`{"timestamp":"2026-01-01T00:00:01.000Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"# AGENTS.md instructions for /Users/test/project\n\n<INSTRUCTIONS>\nThis is meta.\n</INSTRUCTIONS>"}]}}` + "\n" +
		`{"timestamp":"2026-01-01T00:00:02.000Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"Create a hello world function"}]}}` + "\n" +
		`{"timestamp":"2026-01-01T00:00:03.000Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"I'll create that function for you."}]}}` + "\n" +
		`{"timestamp":"2026-01-01T00:00:04.000Z","type":"response_item","payload":{"type":"function_call","name":"exec_command","arguments":"{\"cmd\":\"echo hi\"}","call_id":"call_1"}}` + "\n" +
		`{"timestamp":"2026-01-01T00:00:06.000Z","type":"response_item","payload":{"type":"reasoning","summary":[{"type":"summary_text","text":"I will now add tests."}]}}` + "\n" +
		`{"timestamp":"2026-01-01T00:01:00.000Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"Here is an image:"},{"type":"input_image","image_url":"data:image/gif;base64,R0lGODlhAQABAPAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw=="}]}}` + "\n" +
		`{"timestamp":"2026-01-01T00:01:01.000Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Thanks for the image."}]}}` + "\n"

	if err := os.WriteFile(inPath, []byte(input), 0o644); err != nil {
		t.Fatal(err)
	}

	if err := SanitizeJSONLToExport(inPath, outPath, "2026/01/01/sess.jsonl"); err != nil {
		t.Fatalf("SanitizeJSONLToExport: %v", err)
	}

	raw, err := os.ReadFile(outPath)
	if err != nil {
		t.Fatal(err)
	}

	var out exportFile
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatalf("unmarshal output: %v", err)
	}

	if got := out.SessionMeta["id"]; got != "sess-123" {
		t.Fatalf("session_meta.id = %#v, want %#v", got, "sess-123")
	}
	if got := out.SessionMeta["original_relpath"]; got != "2026/01/01/sess.jsonl" {
		t.Fatalf("session_meta.original_relpath = %#v, want %#v", got, "2026/01/01/sess.jsonl")
	}

	if len(out.Loglines) != 4 {
		t.Fatalf("loglines len = %d, want %d", len(out.Loglines), 4)
	}
	if out.Loglines[0].Type != "user" {
		t.Fatalf("loglines[0].type = %q, want %q", out.Loglines[0].Type, "user")
	}
	firstText := out.Loglines[0].Message.Content[0]["text"]
	if firstText != "Create a hello world function" {
		t.Fatalf("first user text = %#v, want %#v", firstText, "Create a hello world function")
	}

	// Image prompt should preserve both text and image (as base64) blocks.
	blocks := out.Loglines[2].Message.Content
	if len(blocks) != 2 {
		t.Fatalf("image prompt blocks len = %d, want %d", len(blocks), 2)
	}
	if blocks[1]["type"] != "image" {
		t.Fatalf("image prompt blocks[1].type = %#v, want %#v", blocks[1]["type"], "image")
	}
	source, ok := blocks[1]["source"].(map[string]any)
	if !ok {
		t.Fatalf("image prompt blocks[1].source type = %T, want map", blocks[1]["source"])
	}
	if source["media_type"] != "image/gif" {
		t.Fatalf("image media_type = %#v, want %#v", source["media_type"], "image/gif")
	}
	if source["data"] == "" {
		t.Fatalf("image data is empty")
	}
}

func TestSanitizeJSONLToExport_StripsSubagentNotifications(t *testing.T) {
	t.Parallel()

	tmp := t.TempDir()
	inPath := filepath.Join(tmp, "sess.jsonl")
	outPath := filepath.Join(tmp, "sess.json")

	input := "" +
		`{"timestamp":"2026-01-01T00:00:00.000Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"<subagent_notification>\n{\"agent_id\":\"agent-123\",\"status\":{\"completed\":\"Internal status\"}}\n</subagent_notification>"}]}}` + "\n" +
		`{"timestamp":"2026-01-01T00:00:01.000Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"Actual user prompt"}]}}` + "\n" +
		`{"timestamp":"2026-01-01T00:00:02.000Z","type":"response_item","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"Assistant reply"}]}}` + "\n"

	if err := os.WriteFile(inPath, []byte(input), 0o644); err != nil {
		t.Fatal(err)
	}

	if err := SanitizeJSONLToExport(inPath, outPath, "2026/01/01/sess.jsonl"); err != nil {
		t.Fatalf("SanitizeJSONLToExport: %v", err)
	}

	raw, err := os.ReadFile(outPath)
	if err != nil {
		t.Fatal(err)
	}

	var out exportFile
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatalf("unmarshal output: %v", err)
	}

	if len(out.Loglines) != 2 {
		t.Fatalf("loglines len = %d, want %d", len(out.Loglines), 2)
	}
	if got := out.Loglines[0].Message.Content[0]["text"]; got != "Actual user prompt" {
		t.Fatalf("first user text = %#v, want %#v", got, "Actual user prompt")
	}
}
