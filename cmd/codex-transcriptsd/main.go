package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	sanitizepkg "github.com/dmonay/codex-transcripts/internal/sanitize"
)

type fileSig struct {
	mtimeNs int64
	size    int64
}

type config struct {
	listenAddr   string
	sessionsDir  string
	dataDir      string
	generatorCmd string
	pollInterval time.Duration
}

type app struct {
	cfg config

	rawDir       string
	sanitizedDir string
	siteDir      string
	trashDir     string

	favoritesPath string
	favMu         sync.Mutex
	favorites     map[string]favoriteEntry // original_relpath (slash) -> entry

	buildID       atomic.Uint64
	lastBuildTime atomic.Int64 // unix seconds

	known map[string]fileSig // relpath (slash) -> signature

	trigger chan struct{}
}

func defaultCodexDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(home, ".codex"), nil
}

func main() {
	var cfg config
	flag.StringVar(&cfg.listenAddr, "listen", "127.0.0.1:7878", "HTTP listen address")
	flag.StringVar(&cfg.sessionsDir, "sessions-dir", "", "Codex sessions directory (default: ~/.codex/sessions)")
	flag.StringVar(&cfg.dataDir, "data-dir", "", "Server data directory (default: ~/.codex/transcriptsd)")
	flag.StringVar(&cfg.generatorCmd, "generator-cmd", "codex-transcripts", "Archive generator command (expected on PATH)")
	flag.DurationVar(&cfg.pollInterval, "poll-interval", 5*time.Second, "Polling interval for filesystem changes (max delay)")
	flag.Parse()

	codexDir, err := defaultCodexDir()
	if err != nil {
		log.Fatal(err)
	}
	if cfg.sessionsDir == "" {
		cfg.sessionsDir = filepath.Join(codexDir, "sessions")
	}
	if cfg.dataDir == "" {
		cfg.dataDir = filepath.Join(codexDir, "transcriptsd")
	}

	a := &app{cfg: cfg}
	if err := a.initDirs(); err != nil {
		log.Fatal(err)
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	a.known = make(map[string]fileSig)
	a.trigger = make(chan struct{}, 1)
	go a.runMirrorLoop(ctx)

	mux := http.NewServeMux()
	mux.HandleFunc("/api/build", a.handleBuildInfo)
	mux.HandleFunc("/api/delete", a.handleDelete)
	mux.HandleFunc("/api/favorites", a.handleFavorites)
	mux.Handle("/", http.FileServer(http.Dir(a.siteDir)))

	srv := &http.Server{
		Addr:              cfg.listenAddr,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdownCtx)
	}()

	log.Printf("codex-transcriptsd serving %s on http://%s", a.siteDir, cfg.listenAddr)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}

func (a *app) initDirs() error {
	rawDir := filepath.Join(a.cfg.dataDir, "raw")
	sanitizedDir := filepath.Join(a.cfg.dataDir, "sanitized")
	siteDir := filepath.Join(a.cfg.dataDir, "site")
	trashDir := filepath.Join(a.cfg.dataDir, "trash")
	favoritesPath := filepath.Join(a.cfg.dataDir, "favorites.json")

	for _, dir := range []string{rawDir, sanitizedDir, siteDir, trashDir} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return err
		}
	}

	a.rawDir = rawDir
	a.sanitizedDir = sanitizedDir
	a.siteDir = siteDir
	a.trashDir = trashDir
	a.favoritesPath = favoritesPath
	a.favorites = make(map[string]favoriteEntry)
	if err := a.loadFavorites(); err != nil {
		log.Printf("load favorites: %v", err)
	}
	return nil
}

func (a *app) runMirrorLoop(ctx context.Context) {
	// Initial sync/build so the site is available quickly.
	if err := a.syncAndBuild(ctx, true); err != nil {
		log.Printf("initial sync/build failed: %v", err)
	}

	ticker := time.NewTicker(a.cfg.pollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := a.syncAndBuild(ctx, false); err != nil {
				log.Printf("sync/build failed: %v", err)
			}
		case <-a.trigger:
			if err := a.syncAndBuild(ctx, false); err != nil {
				log.Printf("sync/build failed: %v", err)
			}
		}
	}
}

func (a *app) requestSync() {
	select {
	case a.trigger <- struct{}{}:
	default:
	}
}

func (a *app) syncAndBuild(ctx context.Context, forceBuild bool) error {
	changed, err := a.syncMirror()
	if err != nil {
		return err
	}
	if !changed && !forceBuild {
		return nil
	}
	if err := a.buildSite(ctx); err != nil {
		return err
	}
	a.buildID.Add(1)
	a.lastBuildTime.Store(time.Now().Unix())
	return nil
}

func (a *app) syncMirror() (bool, error) {
	sessionsRootAbs, err := filepath.Abs(a.cfg.sessionsDir)
	if err != nil {
		return false, err
	}

	current := make(map[string]fileSig)
	var changed bool

	walkErr := filepath.WalkDir(sessionsRootAbs, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			return nil
		}

		name := d.Name()
		if !strings.HasSuffix(strings.ToLower(name), ".jsonl") {
			return nil
		}

		relOS, err := filepath.Rel(sessionsRootAbs, path)
		if err != nil {
			return nil
		}
		relSlash := filepath.ToSlash(relOS)

		info, err := d.Info()
		if err != nil {
			return nil
		}
		sig := fileSig{mtimeNs: info.ModTime().UnixNano(), size: info.Size()}
		current[relSlash] = sig

		prev, ok := a.known[relSlash]
		if !ok || prev != sig {
			if err := a.copyAndSanitize(path, relSlash); err != nil {
				log.Printf("sync %s: %v", relSlash, err)
			} else {
				changed = true
			}
		}
		return nil
	})
	if walkErr != nil {
		return changed, walkErr
	}

	for relSlash := range a.known {
		if _, ok := current[relSlash]; ok {
			continue
		}
		a.removeMirrored(relSlash)
		changed = true
	}

	a.known = current
	return changed, nil
}

func (a *app) copyAndSanitize(srcAbs, originalRelSlash string) error {
	rawDest := filepath.Join(a.rawDir, filepath.FromSlash(originalRelSlash))
	if err := copyFileAtomic(srcAbs, rawDest); err != nil {
		return err
	}

	sanitizedRel := strings.TrimSuffix(originalRelSlash, ".jsonl") + ".json"
	sanitizedDest := filepath.Join(a.sanitizedDir, filepath.FromSlash(sanitizedRel))
	if err := os.MkdirAll(filepath.Dir(sanitizedDest), 0o755); err != nil {
		return err
	}

	// Sanitize from the raw mirror so originals remain untouched.
	return sanitizepkg.SanitizeJSONLToExport(rawDest, sanitizedDest, originalRelSlash)
}

func (a *app) removeMirrored(originalRelSlash string) {
	_ = os.Remove(filepath.Join(a.rawDir, filepath.FromSlash(originalRelSlash)))
	sanitizedRel := strings.TrimSuffix(originalRelSlash, ".jsonl") + ".json"
	_ = os.Remove(filepath.Join(a.sanitizedDir, filepath.FromSlash(sanitizedRel)))
}

func copyFileAtomic(srcAbs, destAbs string) error {
	if err := os.MkdirAll(filepath.Dir(destAbs), 0o755); err != nil {
		return err
	}

	in, err := os.Open(srcAbs)
	if err != nil {
		return err
	}
	defer in.Close()

	tmp, err := os.CreateTemp(filepath.Dir(destAbs), filepath.Base(destAbs)+".tmp-*")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	defer func() { _ = os.Remove(tmpPath) }()

	if _, err := io.Copy(tmp, in); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmpPath, destAbs)
}

func (a *app) buildSite(ctx context.Context) error {
	parts := strings.Fields(a.cfg.generatorCmd)
	if len(parts) == 0 {
		return fmt.Errorf("generator-cmd is empty")
	}
	cmdName := parts[0]
	baseArgs := parts[1:]

	args := append([]string{}, baseArgs...)
	args = append(args,
		"all",
		"--source", a.sanitizedDir,
		"--output", a.siteDir,
		"--no-open",
		"--quiet",
	)

	cmd := exec.CommandContext(ctx, cmdName, args...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		msg := strings.TrimSpace(string(out))
		if msg != "" {
			return fmt.Errorf("generator failed: %w: %s", err, msg)
		}
		return fmt.Errorf("generator failed: %w", err)
	}
	return nil
}

func (a *app) handleBuildInfo(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"build_id":        a.buildID.Load(),
		"last_build_unix": a.lastBuildTime.Load(),
	})
}

func (a *app) handleDelete(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		_ = json.NewEncoder(w).Encode(map[string]any{"error": "method not allowed"})
		return
	}

	type deleteRequest struct {
		OriginalRelpath string `json:"original_relpath"`
		Hard            bool   `json:"hard"`
	}
	var req deleteRequest
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

	info, statErr := os.Stat(targetAbs)
	if statErr != nil {
		if errors.Is(statErr, os.ErrNotExist) {
			w.WriteHeader(http.StatusNotFound)
			_ = json.NewEncoder(w).Encode(map[string]any{"error": "not found"})
			return
		}
		w.WriteHeader(http.StatusInternalServerError)
		_ = json.NewEncoder(w).Encode(map[string]any{"error": "stat failed"})
		return
	}
	if info.IsDir() {
		w.WriteHeader(http.StatusBadRequest)
		_ = json.NewEncoder(w).Encode(map[string]any{"error": "refusing to delete a directory"})
		return
	}

	hard := req.Hard || r.URL.Query().Get("hard") == "1"
	if hard {
		if err := os.Remove(targetAbs); err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			_ = json.NewEncoder(w).Encode(map[string]any{"error": "delete failed"})
			return
		}
		a.dropFavorite(cleanRel)
		a.requestSync()
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(map[string]any{"deleted": true, "hard": true})
		return
	}

	trashedTo, err := a.trashFile(targetAbs, cleanRel)
	if err != nil {
		w.WriteHeader(http.StatusInternalServerError)
		_ = json.NewEncoder(w).Encode(map[string]any{"error": "trash failed"})
		return
	}

	a.dropFavorite(cleanRel)
	a.requestSync()
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"deleted":     true,
		"hard":        false,
		"trashed_to":  trashedTo,
		"relpath":     cleanRel,
		"sessionsDir": a.cfg.sessionsDir,
	})
}

func (a *app) resolveSessionRelpath(rel string) (absPath string, cleanRel string, err error) {
	rel = filepath.Clean(filepath.FromSlash(rel))
	if rel == "." || rel == string(filepath.Separator) {
		return "", "", fmt.Errorf("invalid original_relpath")
	}
	if filepath.IsAbs(rel) {
		return "", "", fmt.Errorf("original_relpath must be relative")
	}
	if strings.HasPrefix(rel, ".."+string(filepath.Separator)) || rel == ".." {
		return "", "", fmt.Errorf("original_relpath must not escape sessions-dir")
	}

	sessionsRootAbs, err := filepath.Abs(a.cfg.sessionsDir)
	if err != nil {
		return "", "", fmt.Errorf("sessions-dir invalid")
	}
	targetAbs, err := filepath.Abs(filepath.Join(sessionsRootAbs, rel))
	if err != nil {
		return "", "", fmt.Errorf("original_relpath invalid")
	}
	relCheck, err := filepath.Rel(sessionsRootAbs, targetAbs)
	if err != nil {
		return "", "", fmt.Errorf("original_relpath invalid")
	}
	if strings.HasPrefix(relCheck, ".."+string(filepath.Separator)) || relCheck == ".." {
		return "", "", fmt.Errorf("original_relpath must not escape sessions-dir")
	}

	// Extra safety: only allow deleting .jsonl session files by default.
	if strings.ToLower(filepath.Ext(rel)) != ".jsonl" {
		return "", "", fmt.Errorf("refusing to delete non-.jsonl file")
	}

	return targetAbs, filepath.ToSlash(rel), nil
}

func (a *app) trashFile(srcAbs, originalRel string) (string, error) {
	relOS := filepath.FromSlash(originalRel)
	destDir := filepath.Join(a.trashDir, filepath.Dir(relOS))
	if err := os.MkdirAll(destDir, 0o755); err != nil {
		return "", err
	}

	base := filepath.Base(relOS)
	dest := filepath.Join(destDir, fmt.Sprintf("%s.%d.deleted", base, time.Now().UnixNano()))
	if err := os.Rename(srcAbs, dest); err == nil {
		return dest, nil
	}

	// Fallback for cross-device rename: copy then delete.
	in, err := os.Open(srcAbs)
	if err != nil {
		return "", err
	}
	defer in.Close()

	out, err := os.Create(dest)
	if err != nil {
		return "", err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return "", err
	}
	if err := out.Close(); err != nil {
		return "", err
	}
	if err := os.Remove(srcAbs); err != nil {
		return "", err
	}
	return dest, nil
}
