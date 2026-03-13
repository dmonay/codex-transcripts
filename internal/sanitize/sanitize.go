package sanitize

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

type jsonlLine struct {
	Timestamp string          `json:"timestamp"`
	Type      string          `json:"type"`
	Payload   json.RawMessage `json:"payload"`
}

type responseItemPayload struct {
	Type    string          `json:"type"`
	Role    string          `json:"role"`
	Content json.RawMessage `json:"content"`
}

func isMetaPrompt(text string) bool {
	text = strings.TrimSpace(text)
	if text == "" {
		return false
	}
	return strings.HasPrefix(text, "# AGENTS.md instructions") ||
		strings.HasPrefix(text, "<environment_context>") ||
		strings.HasPrefix(text, "<permissions instructions>")
}

func isInternalUserMessage(text string) bool {
	text = strings.TrimSpace(text)
	if text == "" {
		return false
	}
	return strings.HasPrefix(text, "<subagent_notification>") &&
		strings.HasSuffix(text, "</subagent_notification>")
}

func shouldSkipUserText(text string) bool {
	return isMetaPrompt(text) || isInternalUserMessage(text)
}

func extractTextFromContent(content any) string {
	switch c := content.(type) {
	case string:
		return strings.TrimSpace(c)
	case []any:
		var texts []string
		for _, block := range c {
			m, ok := block.(map[string]any)
			if !ok {
				continue
			}
			typ, _ := m["type"].(string)
			if typ != "text" && typ != "input_text" && typ != "output_text" && typ != "summary_text" {
				continue
			}
			txt, _ := m["text"].(string)
			if txt != "" {
				texts = append(texts, txt)
			}
		}
		return strings.TrimSpace(strings.Join(texts, " "))
	default:
		return ""
	}
}

func parseDataURL(url string) (mediaType string, data string, ok bool) {
	url = strings.TrimSpace(url)
	if !strings.HasPrefix(url, "data:") {
		return "", "", false
	}
	header, dataPart, found := strings.Cut(url, ",")
	if !found || dataPart == "" {
		return "", "", false
	}
	if !strings.Contains(header, ";base64") {
		return "", "", false
	}
	media := strings.TrimPrefix(header, "data:")
	media, _, _ = strings.Cut(media, ";")
	if media == "" {
		media = "application/octet-stream"
	}
	return media, dataPart, true
}

func sanitizeMessageContent(content any) []map[string]any {
	switch c := content.(type) {
	case string:
		text := strings.TrimSpace(c)
		if text == "" {
			return nil
		}
		return []map[string]any{{"type": "text", "text": text}}
	case []any:
		var blocks []map[string]any
		for _, item := range c {
			m, ok := item.(map[string]any)
			if !ok {
				continue
			}
			itemType, _ := m["type"].(string)

			switch itemType {
			case "input_text", "output_text", "text", "summary_text":
				txt, _ := m["text"].(string)
				txt = strings.TrimSpace(txt)
				if txt != "" {
					blocks = append(blocks, map[string]any{"type": "text", "text": txt})
				}
			case "input_image", "output_image":
				url, _ := m["image_url"].(string)
				mediaType, data, ok := parseDataURL(url)
				if ok {
					blocks = append(blocks, map[string]any{
						"type": "image",
						"source": map[string]any{
							"type":       "base64",
							"media_type": mediaType,
							"data":       data,
						},
					})
				}
			case "image":
				// Already-normalized export format or a raw data URL.
				if src, ok := m["source"].(map[string]any); ok && src["data"] != nil {
					blocks = append(blocks, map[string]any{"type": "image", "source": src})
					continue
				}
				if url, ok := m["image_url"].(string); ok {
					mediaType, data, ok := parseDataURL(url)
					if ok {
						blocks = append(blocks, map[string]any{
							"type": "image",
							"source": map[string]any{
								"type":       "base64",
								"media_type": mediaType,
								"data":       data,
							},
						})
					}
				}
			default:
				// Strip non-dialogue blocks: tool chatter, reasoning/thinking, etc.
				continue
			}
		}
		return blocks
	default:
		return nil
	}
}

func forEachJSONLLine(path string, fn func(line []byte) error) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()

	r := io.Reader(f)
	buf := make([]byte, 64*1024)
	var pending []byte
	for {
		n, readErr := r.Read(buf)
		if n > 0 {
			pending = append(pending, buf[:n]...)
			for {
				idx := bytes.IndexByte(pending, '\n')
				if idx == -1 {
					break
				}
				line := bytes.TrimSpace(pending[:idx])
				pending = pending[idx+1:]
				if len(line) == 0 {
					continue
				}
				if err := fn(line); err != nil {
					return err
				}
			}
		}
		if readErr != nil {
			if errors.Is(readErr, io.EOF) {
				break
			}
			return readErr
		}
	}

	last := bytes.TrimSpace(pending)
	if len(last) > 0 {
		if err := fn(last); err != nil {
			return err
		}
	}
	return nil
}

func readSessionMeta(inPath string) (map[string]any, error) {
	meta := map[string]any{}
	err := forEachJSONLLine(inPath, func(line []byte) error {
		var l jsonlLine
		if err := json.Unmarshal(line, &l); err != nil {
			return nil
		}
		if l.Type != "session_meta" || len(l.Payload) == 0 {
			return nil
		}
		var payload map[string]any
		if err := json.Unmarshal(l.Payload, &payload); err != nil {
			return nil
		}
		for k, v := range payload {
			meta[k] = v
		}
		// First session_meta wins.
		return io.EOF
	})
	if err != nil && !errors.Is(err, io.EOF) {
		return nil, err
	}
	return meta, nil
}

// SanitizeJSONLToExport reads a Codex CLI session JSONL file and writes an already-normalized
// export JSON file in the format consumed by the existing Python HTML generator.
//
// It strips tool chatter, reasoning/thinking traces, and session boilerplate prompts.
func SanitizeJSONLToExport(inPath, outPath, originalRelpath string) error {
	meta, err := readSessionMeta(inPath)
	if err != nil {
		return err
	}
	if originalRelpath != "" {
		meta["original_relpath"] = filepath.ToSlash(originalRelpath)
	}

	if err := os.MkdirAll(filepath.Dir(outPath), 0o755); err != nil {
		return err
	}

	tmpFile, err := os.CreateTemp(filepath.Dir(outPath), filepath.Base(outPath)+".tmp-*")
	if err != nil {
		return err
	}
	tmpPath := tmpFile.Name()
	defer func() { _ = os.Remove(tmpPath) }()
	defer tmpFile.Close()

	metaJSON, err := json.Marshal(meta)
	if err != nil {
		return err
	}

	if _, err := fmt.Fprintf(tmpFile, `{"session_meta":%s,"loglines":[`, metaJSON); err != nil {
		return err
	}

	first := true
	err = forEachJSONLLine(inPath, func(line []byte) error {
		var l jsonlLine
		if err := json.Unmarshal(line, &l); err != nil {
			return nil
		}
		if l.Type != "response_item" || len(l.Payload) == 0 {
			return nil
		}

		var payload responseItemPayload
		if err := json.Unmarshal(l.Payload, &payload); err != nil {
			return nil
		}
		if payload.Type != "message" {
			return nil
		}
		if payload.Role != "user" && payload.Role != "assistant" {
			return nil
		}

		var content any
		if len(payload.Content) == 0 {
			return nil
		}
		if err := json.Unmarshal(payload.Content, &content); err != nil {
			return nil
		}

		if payload.Role == "user" {
			text := extractTextFromContent(content)
			if shouldSkipUserText(text) {
				return nil
			}
		}

		blocks := sanitizeMessageContent(content)
		if len(blocks) == 0 {
			return nil
		}

		logline := map[string]any{
			"type":      payload.Role,
			"timestamp": l.Timestamp,
			"message": map[string]any{
				"content": blocks,
			},
		}
		b, err := json.Marshal(logline)
		if err != nil {
			return nil
		}

		if !first {
			if _, err := tmpFile.WriteString(","); err != nil {
				return err
			}
		}
		first = false
		_, err = tmpFile.Write(b)
		return err
	})
	if err != nil {
		return err
	}

	if _, err := tmpFile.WriteString(`]}` + "\n"); err != nil {
		return err
	}
	if err := tmpFile.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpPath, outPath); err != nil {
		return err
	}
	return nil
}
