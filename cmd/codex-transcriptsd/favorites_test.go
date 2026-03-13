package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestGithubRepoFromGitURL(t *testing.T) {
	t.Parallel()

	if got := githubRepoFromGitURL("git@github.com:owner/repo.git"); got != "owner/repo" {
		t.Fatalf("git@ url: got %q", got)
	}
	if got := githubRepoFromGitURL("https://github.com/owner/repo"); got != "owner/repo" {
		t.Fatalf("https url: got %q", got)
	}
	if got := githubRepoFromGitURL("https://github.com/owner/repo.git"); got != "owner/repo" {
		t.Fatalf("https .git url: got %q", got)
	}
	if got := githubRepoFromGitURL("not a repo"); got != "" {
		t.Fatalf("unexpected parse: got %q", got)
	}
}

func TestSafeProjectDirName(t *testing.T) {
	t.Parallel()

	if got := safeProjectDirName("owner/repo"); got != "owner__repo" {
		t.Fatalf("owner/repo: got %q", got)
	}
	if got := safeProjectDirName("weird name !"); got != "weird_name_" {
		t.Fatalf("weird name: got %q", got)
	}
	if got := safeProjectDirName(""); got != "unknown" {
		t.Fatalf("empty: got %q", got)
	}
}

func TestFavoritesSaveLoadRoundTrip(t *testing.T) {
	t.Parallel()

	dataDir := t.TempDir()
	sessionsDir := t.TempDir()

	a := &app{cfg: config{dataDir: dataDir, sessionsDir: sessionsDir}}
	if err := a.initDirs(); err != nil {
		t.Fatalf("initDirs: %v", err)
	}

	a.favMu.Lock()
	a.favorites["2026/01/01/a.jsonl"] = favoriteEntry{Nickname: "alpha", AddedUnix: 123}
	if err := a.saveFavoritesLocked(); err != nil {
		a.favMu.Unlock()
		t.Fatalf("saveFavoritesLocked: %v", err)
	}
	a.favMu.Unlock()

	b := &app{cfg: config{dataDir: dataDir, sessionsDir: sessionsDir}}
	if err := b.initDirs(); err != nil {
		t.Fatalf("initDirs (reload): %v", err)
	}

	b.favMu.Lock()
	ent, ok := b.favorites["2026/01/01/a.jsonl"]
	b.favMu.Unlock()
	if !ok {
		t.Fatalf("expected favorite to be loaded")
	}
	if ent.Nickname != "alpha" || ent.AddedUnix != 123 {
		t.Fatalf("loaded mismatch: %+v", ent)
	}
}

func TestListFavoritesComputesHrefFromSessionMeta(t *testing.T) {
	t.Parallel()

	dataDir := t.TempDir()
	sessionsDir := t.TempDir()

	rel := filepath.ToSlash(filepath.Join("2026", "01", "01", "a.jsonl"))
	abs := filepath.Join(sessionsDir, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(abs, []byte(
		`{"timestamp":"2026-01-01T00:00:00.000Z","type":"session_meta","payload":{"cwd":"/tmp/repo","git":{"repository_url":"git@github.com:example/project-a.git"}}}`+"\n"+
			`{"timestamp":"2026-01-01T00:00:01.000Z","type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"Hello"}]}}`+"\n",
	), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}

	a := &app{cfg: config{dataDir: dataDir, sessionsDir: sessionsDir}}
	if err := a.initDirs(); err != nil {
		t.Fatalf("initDirs: %v", err)
	}

	a.favMu.Lock()
	a.favorites[rel] = favoriteEntry{Nickname: "my fav", AddedUnix: 999}
	a.favMu.Unlock()

	items, err := a.listFavorites()
	if err != nil {
		t.Fatalf("listFavorites: %v", err)
	}
	if len(items) != 1 {
		t.Fatalf("expected 1 item, got %d", len(items))
	}
	if items[0].Href != "example__project-a/a/index.html" {
		t.Fatalf("href mismatch: %q", items[0].Href)
	}
	if items[0].Project != "example/project-a" {
		t.Fatalf("project mismatch: %q", items[0].Project)
	}
	if items[0].Session != "a" {
		t.Fatalf("session mismatch: %q", items[0].Session)
	}
	if items[0].Missing {
		t.Fatalf("did not expect Missing=true")
	}
}
