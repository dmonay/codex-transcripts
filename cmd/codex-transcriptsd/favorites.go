package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

const favoritesStateVersion = 1

type favoriteEntry struct {
	Nickname  string `json:"nickname"`
	AddedUnix int64  `json:"added_unix"`
}

type favoritesState struct {
	Version   int                      `json:"version"`
	Favorites map[string]favoriteEntry `json:"favorites"`
}

var nonProjectDirChars = regexp.MustCompile(`[^A-Za-z0-9_.-]+`)

func githubRepoFromGitURL(url string) string {
	url = strings.TrimSpace(url)
	if url == "" {
		return ""
	}

	// git@github.com:owner/repo.git
	if strings.HasPrefix(url, "git@github.com:") {
		path := strings.TrimPrefix(url, "git@github.com:")
		path = strings.TrimSuffix(path, ".git")
		if strings.Count(path, "/") == 1 {
			return path
		}
	}

	// https://github.com/owner/repo(.git)
	if idx := strings.Index(url, "github.com/"); idx >= 0 {
		path := url[idx+len("github.com/"):]
		path = strings.Trim(path, "/")
		path = strings.TrimSuffix(path, ".git")
		parts := strings.Split(path, "/")
		if len(parts) >= 2 && parts[0] != "" && parts[1] != "" {
			return parts[0] + "/" + parts[1]
		}
	}

	return ""
}

func safeProjectDirName(displayName string) string {
	displayName = strings.TrimSpace(displayName)
	if displayName == "" {
		return "unknown"
	}
	name := strings.ReplaceAll(displayName, "/", "__")
	return nonProjectDirChars.ReplaceAllString(name, "_")
}

func projectNamesFromSessionMeta(meta map[string]any) (dirName string, displayName string) {
	var repoURL string
	if git, ok := meta["git"].(map[string]any); ok {
		if s, ok := git["repository_url"].(string); ok {
			repoURL = s
		}
	}
	if repo := githubRepoFromGitURL(repoURL); repo != "" {
		return safeProjectDirName(repo), repo
	}

	if cwd, ok := meta["cwd"].(string); ok && strings.TrimSpace(cwd) != "" {
		base := filepath.Base(cwd)
		if base == "." || base == string(filepath.Separator) || base == "" {
			base = cwd
		}
		display := base
		return safeProjectDirName(display), display
	}

	return "unknown", "unknown"
}

func readSessionMetaJSONL(path string) (map[string]any, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	sc := bufio.NewScanner(f)
	// Session meta can be large; allow up to 2 MiB line.
	sc.Buffer(make([]byte, 0, 64*1024), 2*1024*1024)
	for sc.Scan() {
		line := bytes.TrimSpace(sc.Bytes())
		if len(line) == 0 {
			continue
		}
		var obj map[string]any
		if err := json.Unmarshal(line, &obj); err != nil {
			continue
		}
		if typ, _ := obj["type"].(string); typ != "session_meta" {
			continue
		}
		if payload, ok := obj["payload"].(map[string]any); ok {
			return payload, nil
		}
		return map[string]any{}, nil
	}
	if err := sc.Err(); err != nil {
		return nil, err
	}
	return map[string]any{}, nil
}

func (a *app) loadFavorites() error {
	path := strings.TrimSpace(a.favoritesPath)
	if path == "" {
		return nil
	}

	b, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return err
	}

	var state favoritesState
	if err := json.Unmarshal(b, &state); err != nil {
		return err
	}
	if state.Version != 0 && state.Version != favoritesStateVersion {
		// Ignore unknown versions, but don't crash the daemon.
		log.Printf("favorites: unsupported version %d (expected %d); ignoring", state.Version, favoritesStateVersion)
		return nil
	}
	if state.Favorites == nil {
		state.Favorites = make(map[string]favoriteEntry)
	}

	a.favMu.Lock()
	a.favorites = state.Favorites
	a.favMu.Unlock()
	return nil
}

func writeFileAtomic(destAbs string, data []byte, perm os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(destAbs), 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(destAbs), filepath.Base(destAbs)+".tmp-*")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	defer func() { _ = os.Remove(tmpPath) }()

	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	_ = os.Chmod(tmpPath, perm)
	return os.Rename(tmpPath, destAbs)
}

func (a *app) saveFavoritesLocked() error {
	state := favoritesState{
		Version:   favoritesStateVersion,
		Favorites: a.favorites,
	}
	b, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	b = append(b, '\n')
	return writeFileAtomic(a.favoritesPath, b, 0o644)
}

func (a *app) dropFavorite(originalRelpath string) {
	originalRelpath = filepath.ToSlash(strings.TrimSpace(originalRelpath))
	if originalRelpath == "" {
		return
	}

	a.favMu.Lock()
	if _, ok := a.favorites[originalRelpath]; ok {
		delete(a.favorites, originalRelpath)
		if err := a.saveFavoritesLocked(); err != nil {
			log.Printf("favorites: failed to persist after delete: %v", err)
		}
	}
	a.favMu.Unlock()
}

type favoriteResponseItem struct {
	OriginalRelpath string `json:"original_relpath"`
	Nickname        string `json:"nickname,omitempty"`
	AddedUnix       int64  `json:"added_unix,omitempty"`
	Project         string `json:"project,omitempty"`
	Session         string `json:"session,omitempty"`
	Href            string `json:"href,omitempty"`
	Missing         bool   `json:"missing,omitempty"`
}

func (a *app) listFavorites() ([]favoriteResponseItem, error) {
	a.favMu.Lock()
	copied := make([]favoriteResponseItem, 0, len(a.favorites))
	for rel, ent := range a.favorites {
		copied = append(copied, favoriteResponseItem{
			OriginalRelpath: rel,
			Nickname:        ent.Nickname,
			AddedUnix:       ent.AddedUnix,
		})
	}
	a.favMu.Unlock()

	// Sort newest first.
	sort.Slice(copied, func(i, j int) bool {
		if copied[i].AddedUnix != copied[j].AddedUnix {
			return copied[i].AddedUnix > copied[j].AddedUnix
		}
		return copied[i].OriginalRelpath < copied[j].OriginalRelpath
	})

	for i := range copied {
		abs, cleanRel, err := a.resolveSessionRelpath(copied[i].OriginalRelpath)
		if err != nil {
			copied[i].Missing = true
			continue
		}
		copied[i].OriginalRelpath = cleanRel

		if _, err := os.Stat(abs); err != nil {
			copied[i].Missing = true
			continue
		}

		meta, err := readSessionMetaJSONL(abs)
		if err != nil {
			copied[i].Missing = true
			continue
		}

		projectDir, projectDisplay := projectNamesFromSessionMeta(meta)
		session := strings.TrimSuffix(filepath.Base(filepath.FromSlash(cleanRel)), ".jsonl")

		copied[i].Project = projectDisplay
		copied[i].Session = session
		copied[i].Href = filepath.ToSlash(filepath.Join(projectDir, session, "index.html"))
	}

	return copied, nil
}

func (a *app) handleFavorites(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	switch r.Method {
	case http.MethodGet:
		items, err := a.listFavorites()
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			_ = json.NewEncoder(w).Encode(map[string]any{"error": "failed to list favorites"})
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"favorites": items})
		return
	case http.MethodPost:
		type favoriteRequest struct {
			OriginalRelpath string `json:"original_relpath"`
			Favorite        bool   `json:"favorite"`
			Nickname        string `json:"nickname"`
		}
		var req favoriteRequest
		r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			_ = json.NewEncoder(w).Encode(map[string]any{"error": "invalid json"})
			return
		}

		rel := strings.TrimSpace(req.OriginalRelpath)
		if rel == "" {
			w.WriteHeader(http.StatusBadRequest)
			_ = json.NewEncoder(w).Encode(map[string]any{"error": "missing original_relpath"})
			return
		}

		targetAbs, cleanRel, err := a.resolveSessionRelpath(rel)
		if err != nil {
			w.WriteHeader(http.StatusBadRequest)
			_ = json.NewEncoder(w).Encode(map[string]any{"error": err.Error()})
			return
		}
		if _, err := os.Stat(targetAbs); err != nil {
			if errors.Is(err, os.ErrNotExist) {
				w.WriteHeader(http.StatusNotFound)
				_ = json.NewEncoder(w).Encode(map[string]any{"error": "not found"})
				return
			}
			w.WriteHeader(http.StatusInternalServerError)
			_ = json.NewEncoder(w).Encode(map[string]any{"error": "stat failed"})
			return
		}

		if !req.Favorite {
			a.favMu.Lock()
			delete(a.favorites, cleanRel)
			err := a.saveFavoritesLocked()
			a.favMu.Unlock()
			if err != nil {
				w.WriteHeader(http.StatusInternalServerError)
				_ = json.NewEncoder(w).Encode(map[string]any{"error": "failed to save favorites"})
				return
			}
			_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "favorite": false})
			return
		}

		nick := strings.TrimSpace(req.Nickname)
		if len(nick) > 80 {
			w.WriteHeader(http.StatusBadRequest)
			_ = json.NewEncoder(w).Encode(map[string]any{"error": "nickname too long"})
			return
		}

		now := time.Now().Unix()
		a.favMu.Lock()
		ent, ok := a.favorites[cleanRel]
		if !ok {
			ent.AddedUnix = now
		}
		ent.Nickname = nick
		a.favorites[cleanRel] = ent
		err = a.saveFavoritesLocked()
		a.favMu.Unlock()
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			_ = json.NewEncoder(w).Encode(map[string]any{"error": "failed to save favorites"})
			return
		}

		_ = json.NewEncoder(w).Encode(map[string]any{"ok": true, "favorite": true, "nickname": nick})
		return
	default:
		w.WriteHeader(http.StatusMethodNotAllowed)
		_ = json.NewEncoder(w).Encode(map[string]any{"error": "method not allowed"})
		return
	}
}
