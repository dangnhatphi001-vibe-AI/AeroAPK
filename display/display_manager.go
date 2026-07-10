package display

import (
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

type DisplayConfig struct {
	WaylandSocketPath string
	IsNested          bool
	Backend           string
	CleanupFunc       func() error
}

const (
	displayModeEnv    = "AERODROID_DISPLAY_MODE"
	westonSocketEnv   = "AERODROID_WESTON_SOCKET"
	westonSocketBase  = "wayland-aero"
	westonWidth       = 1080
	westonHeight      = 1920
	westonTimeout     = 7 * time.Second
	westonTickRate    = 100 * time.Millisecond
	staleDialTimeout  = 150 * time.Millisecond
	westonStopTimeout = 2 * time.Second
)

func AutoDetectAndSetup(mode string) (*DisplayConfig, error) {
	mode = normalizeDisplayMode(mode)
	if mode == "" || mode == "auto" {
		mode = autoDisplayMode()
	}

	switch mode {
	case "nested-wayland", "wayland", "weston-wayland":
		cfg, err := setupWestonNested("wayland")
		if err != nil {
			return nil, fmt.Errorf("weston nested wayland setup: %w", err)
		}
		return cfg, nil
	case "nested-x11", "x11", "weston-x11":
		cfg, err := setupWestonNested("x11")
		if err != nil {
			return nil, fmt.Errorf("weston nested x11 setup: %w", err)
		}
		return cfg, nil
	case "headless", "weston-headless":
		cfg, err := setupWestonNested("headless")
		if err != nil {
			return nil, fmt.Errorf("weston headless setup: %w", err)
		}
		return cfg, nil
	case "native", "direct", "host-wayland":
		cfg, err := detectWaylandNative()
		if err != nil {
			return nil, fmt.Errorf("wayland native setup: %w", err)
		}
		return cfg, nil
	default:
		return nil, fmt.Errorf("unknown display mode %q; use auto, nested-wayland, nested-x11, headless, or native", mode)
	}
}

func normalizeDisplayMode(mode string) string {
	if strings.TrimSpace(mode) == "" {
		mode = os.Getenv(displayModeEnv)
	}
	mode = strings.ToLower(strings.TrimSpace(mode))
	mode = strings.ReplaceAll(mode, "_", "-")
	return mode
}

func autoDisplayMode() string {
	if os.Getenv("WAYLAND_DISPLAY") != "" {
		return "nested-wayland"
	}
	if os.Getenv("DISPLAY") != "" {
		return "nested-x11"
	}
	return "headless"
}

func detectWaylandNative() (*DisplayConfig, error) {
	display := os.Getenv("WAYLAND_DISPLAY")
	if display == "" {
		display = "wayland-0"
	}
	socketPath, err := waylandSocketPath(display)
	if err != nil {
		return nil, err
	}

	return &DisplayConfig{
		WaylandSocketPath: socketPath,
		IsNested:          false,
		Backend:           "native",
		CleanupFunc:       func() error { return nil },
	}, nil
}

func setupWestonNested(backend string) (*DisplayConfig, error) {
	runtimeDir := runtimeDir()
	if err := os.MkdirAll(runtimeDir, 0700); err != nil {
		return nil, fmt.Errorf("create runtime directory %s: %w", runtimeDir, err)
	}

	socketName := westonSocketName()
	socketPath := filepath.Join(runtimeDir, socketName)
	if err := removeStaleSocket(socketPath); err != nil {
		return nil, err
	}

	westonPath, err := exec.LookPath("weston")
	if err != nil {
		return nil, fmt.Errorf("weston not found in PATH: %w", err)
	}

	args := []string{
		fmt.Sprintf("--backend=%s", westonBackendModule(backend)),
		"--renderer=gl",
		"--no-config",
		"--idle-time=0",
		fmt.Sprintf("--socket=%s", socketName),
		fmt.Sprintf("--width=%d", westonWidth),
		fmt.Sprintf("--height=%d", westonHeight),
	}
	westonEnv, err := westonEnvironment(backend, runtimeDir)
	if err != nil {
		return nil, err
	}

	if backend == "wayland" {
		parentDisplay := os.Getenv("WAYLAND_DISPLAY")
		if parentDisplay == "" {
			parentDisplay = "wayland-0"
		}
		if _, err := waylandSocketPath(parentDisplay); err != nil {
			return nil, err
		}
		args = append(args, fmt.Sprintf("--display=%s", parentDisplay))
	}

	cmd := exec.Command(westonPath, args...)
	cmd.Env = westonEnv

	cmd.SysProcAttr = &syscall.SysProcAttr{
		Pdeathsig: syscall.SIGTERM,
		Setpgid:   true,
	}

	cmd.Stdin = nil
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("start weston %s: %w", backend, err)
	}

	ready := pollSocketFile(socketPath, westonTimeout, westonTickRate)
	if !ready {
		_ = stopProcess(cmd, westonStopTimeout)
		return nil, fmt.Errorf("weston socket %s did not appear within %v", socketPath, westonTimeout)
	}

	info, err := os.Lstat(socketPath)
	if err != nil {
		_ = stopProcess(cmd, westonStopTimeout)
		return nil, fmt.Errorf("stat created socket %s: %w", socketPath, err)
	}
	if info.Mode()&os.ModeSocket == 0 {
		_ = stopProcess(cmd, westonStopTimeout)
		return nil, fmt.Errorf("%s created but is not a Unix socket", socketPath)
	}

	cleanup := func() error {
		var errs []error
		if err := stopProcess(cmd, westonStopTimeout); err != nil {
			errs = append(errs, err)
		}
		if rmErr := os.Remove(socketPath); rmErr != nil && !os.IsNotExist(rmErr) {
			errs = append(errs, fmt.Errorf("remove socket %s: %w", socketPath, rmErr))
		}
		if len(errs) > 0 {
			return fmt.Errorf("weston cleanup: %v", errs)
		}
		return nil
	}

	return &DisplayConfig{
		WaylandSocketPath: socketPath,
		IsNested:          true,
		Backend:           backend,
		CleanupFunc:       cleanup,
	}, nil
}

func runtimeDir() string {
	if dir := os.Getenv("XDG_RUNTIME_DIR"); dir != "" {
		return dir
	}
	if sudoUID := os.Getenv("SUDO_UID"); sudoUID != "" {
		return "/run/user/" + sudoUID
	}
	return fmt.Sprintf("/run/user/%d", os.Getuid())
}

func waylandSocketPath(display string) (string, error) {
	var socketPath string
	if filepath.IsAbs(display) {
		socketPath = display
	} else {
		socketPath = filepath.Join(runtimeDir(), display)
	}
	info, err := os.Lstat(socketPath)
	if err != nil {
		return "", fmt.Errorf("stat wayland socket %s: %w", socketPath, err)
	}
	if info.Mode()&os.ModeSocket == 0 {
		return "", fmt.Errorf("%s is not a Unix socket", socketPath)
	}
	return socketPath, nil
}

func westonSocketName() string {
	name := strings.TrimSpace(os.Getenv(westonSocketEnv))
	if name == "" {
		return fmt.Sprintf("%s-%d", westonSocketBase, os.Getpid())
	}
	return filepath.Base(name)
}

func westonBackendModule(backend string) string {
	switch backend {
	case "wayland":
		return "wayland-backend.so"
	case "x11":
		return "x11-backend.so"
	case "headless":
		return "headless-backend.so"
	default:
		return backend
	}
}

func westonEnvironment(backend string, runtimeDir string) ([]string, error) {
	env := make([]string, 0, len(os.Environ())+6)
	for _, kv := range os.Environ() {
		if strings.HasPrefix(kv, "XDG_RUNTIME_DIR=") ||
			strings.HasPrefix(kv, "WAYLAND_DISPLAY=") ||
			strings.HasPrefix(kv, "DISPLAY=") ||
			strings.HasPrefix(kv, "NO_AT_BRIDGE=") {
			continue
		}
		env = append(env, kv)
	}

	env = append(env,
		"XDG_RUNTIME_DIR="+runtimeDir,
		"NO_AT_BRIDGE=1",
	)

	switch backend {
	case "wayland":
		parentDisplay := os.Getenv("WAYLAND_DISPLAY")
		if parentDisplay == "" {
			parentDisplay = "wayland-0"
		}
		env = append(env, "WAYLAND_DISPLAY="+parentDisplay)
	case "x11":
		display := os.Getenv("DISPLAY")
		if display == "" {
			return nil, errorsForBackend("DISPLAY is empty", backend)
		}
		env = append(env, "DISPLAY="+display)
		if xauth := xauthorityPath(); xauth != "" {
			env = append(env, "XAUTHORITY="+xauth)
		}
	case "headless":
		// No parent compositor is required.
	default:
		return nil, fmt.Errorf("unsupported weston backend %q", backend)
	}

	return env, nil
}

func errorsForBackend(msg string, backend string) error {
	return fmt.Errorf("%s for weston %s backend", msg, backend)
}

func xauthorityPath() string {
	if xauth := os.Getenv("XAUTHORITY"); xauth != "" {
		return xauth
	}
	candidate := fmt.Sprintf("/run/user/%d/.mutter-Xwaylandauth", os.Getuid())
	entries, err := filepath.Glob(candidate + "*")
	if err == nil && len(entries) > 0 {
		return entries[0]
	}
	return ""
}

func removeStaleSocket(socketPath string) error {
	info, err := os.Lstat(socketPath)
	if errorsIsNotExist(err) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("stat weston socket %s: %w", socketPath, err)
	}
	if info.Mode()&os.ModeSocket == 0 {
		return fmt.Errorf("%s exists and is not a Unix socket", socketPath)
	}
	conn, dialErr := net.DialTimeout("unix", socketPath, staleDialTimeout)
	if dialErr == nil {
		_ = conn.Close()
		return fmt.Errorf("weston socket %s is already active", socketPath)
	}
	if rmErr := os.Remove(socketPath); rmErr != nil {
		return fmt.Errorf("remove stale socket %s: %w", socketPath, rmErr)
	}
	return nil
}

func errorsIsNotExist(err error) bool {
	return err != nil && os.IsNotExist(err)
}

func stopProcess(cmd *exec.Cmd, timeout time.Duration) error {
	if cmd == nil || cmd.Process == nil {
		return nil
	}

	done := make(chan error, 1)
	go func() {
		done <- cmd.Wait()
	}()

	if err := cmd.Process.Signal(syscall.SIGTERM); err != nil && !errors.Is(err, os.ErrProcessDone) {
		return fmt.Errorf("stop weston: %w", err)
	}

	timer := time.NewTimer(timeout)
	defer timer.Stop()

	select {
	case <-done:
		return nil
	case <-timer.C:
		if err := cmd.Process.Kill(); err != nil && !errors.Is(err, os.ErrProcessDone) {
			return fmt.Errorf("kill weston: %w", err)
		}
		<-done
		return nil
	}
}

func pollSocketFile(socketPath string, timeout time.Duration, tickRate time.Duration) bool {
	ticker := time.NewTicker(tickRate)
	defer ticker.Stop()

	deadline := time.NewTimer(timeout)
	defer deadline.Stop()

	for {
		select {
		case <-ticker.C:
			info, err := os.Lstat(socketPath)
			if err == nil && info.Mode()&os.ModeSocket != 0 {
				return true
			}

		case <-deadline.C:
			return false
		}
	}
}
