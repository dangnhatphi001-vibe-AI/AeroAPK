package container

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
	"phishadow-droid/display"
)

const (
	InternalInitArg = "__container-init"

	cgroup2SuperMagic = 0x63677270
	defaultStateRoot  = "/run/phishadowd"

	binderfsMaxName = 255
)

var requiredControllers = []string{"cpu", "memory", "pids"}

type Config struct {
	ID              string   `json:"id"`
	RootFS          string   `json:"rootfs"`
	Hostname        string   `json:"hostname"`
	WaylandSocket   string   `json:"wayland_socket"`
	DisplayMode     string   `json:"display_mode"`
	StateDir        string   `json:"state_dir"`
	Command         []string `json:"command"`
	MemoryMaxBytes  int64    `json:"memory_max_bytes"`
	CPUQuotaMicros  int64    `json:"cpu_quota_micros"`
	CPUPeriodMicros int64    `json:"cpu_period_micros"`
	PIDsMax         int64    `json:"pids_max"`
}

type Runner struct {
	Config Config
}

type cgroupV2 struct {
	path string
}

type binderfsDevice struct {
	Name  [binderfsMaxName + 1]byte
	Major uint32
	Minor uint32
}

func (r Runner) Run(ctx context.Context) error {
	if runtime.GOOS != "linux" {
		return errors.New("PhiShadow Droid requires Linux")
	}

	cfg := r.Config
	if cfg.WaylandSocket == "" {
		displayCfg, err := display.AutoDetectAndSetup(cfg.DisplayMode)
		if err != nil {
			return fmt.Errorf("display setup: %w", err)
		}
		defer func() {
			if cerr := displayCfg.CleanupFunc(); cerr != nil {
				fmt.Fprintf(os.Stderr, "phishadowd: display cleanup error: %v\n", cerr)
			}
		}()
		cfg.WaylandSocket = displayCfg.WaylandSocketPath
	}
	if err := cfg.normalize(); err != nil {
		return err
	}
	if !hasEffectiveCapability(21) {
		return errors.New("CAP_SYS_ADMIN is required for namespaces, mount, pivot_root and binderfs")
	}
	if err := os.MkdirAll(cfg.StateDir, 0o700); err != nil {
		return fmt.Errorf("create state dir %s: %w", cfg.StateDir, err)
	}

	configPath := filepath.Join(cfg.StateDir, "config.json")
	if err := writeJSON(configPath, cfg, 0o600); err != nil {
		return err
	}

	cg, err := createContainerCgroup(cfg)
	if err != nil {
		return err
	}
	defer cg.remove()

	self, err := os.Executable()
	if err != nil {
		return fmt.Errorf("resolve executable: %w", err)
	}

	cmd := exec.Command(self, InternalInitArg, "--config", configPath)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Env = append(os.Environ(), "PHISHADOW_CONTAINER_ID="+cfg.ID)

	pidfd := -1
	cloneFlags := uintptr(
		unix.CLONE_NEWNS |
			unix.CLONE_NEWPID |
			unix.CLONE_NEWUTS |
			unix.CLONE_NEWIPC |
			unix.CLONE_NEWNET,
	)
	cmd.SysProcAttr = &syscall.SysProcAttr{
		Cloneflags: cloneFlags,
		Pdeathsig:  syscall.SIGTERM,
		Setpgid:    true,
		PidFD:      &pidfd,
	}
	if os.Geteuid() != 0 {
		cmd.SysProcAttr.Credential = &syscall.Credential{Uid: 0, Gid: 0}
	}

	cgroupFD, err := cg.openFD()
	useCgroupFD := err == nil
	if useCgroupFD {
		cmd.SysProcAttr.UseCgroupFD = true
		cmd.SysProcAttr.CgroupFD = cgroupFD
	}

	err = cmd.Start()
	if cgroupFD >= 0 {
		_ = unix.Close(cgroupFD)
	}
	if err != nil && useCgroupFD && isCgroupFDFallbackError(err) {
		cmd = exec.Command(self, InternalInitArg, "--config", configPath)
		cmd.Stdin = os.Stdin
		cmd.Stdout = os.Stdout
		cmd.Stderr = os.Stderr
		cmd.Env = append(os.Environ(), "PHISHADOW_CONTAINER_ID="+cfg.ID)
		pidfd = -1
		cmd.SysProcAttr = &syscall.SysProcAttr{
			Cloneflags: cloneFlags,
			Pdeathsig:  syscall.SIGTERM,
			Setpgid:    true,
			PidFD:      &pidfd,
		}
		if os.Geteuid() != 0 {
			cmd.SysProcAttr.Credential = &syscall.Credential{Uid: 0, Gid: 0}
		}
		err = cmd.Start()
	}
	if err != nil {
		return fmt.Errorf("start container init: %w", err)
	}
	defer closePidFD(pidfd)

	if err := cg.attach(cmd.Process.Pid); err != nil {
		_ = cmd.Process.Kill()
		_, _ = cmd.Process.Wait()
		return err
	}

	if err := r.setupNetwork(cmd.Process.Pid); err != nil {
		fmt.Fprintf(os.Stderr, "phishadowd: network setup error: %v\n", err)
	}
	defer r.cleanupNetwork()

	return supervise(ctx, cmd, cg)
}

func findDefaultInterface() (string, error) {
	data, err := os.ReadFile("/proc/net/route")
	if err != nil {
		return "", err
	}
	lines := strings.Split(string(data), "\n")
	for _, line := range lines {
		fields := strings.Fields(line)
		if len(fields) >= 2 && fields[1] == "00000000" {
			return fields[0], nil
		}
	}
	return "", fmt.Errorf("default route interface not found in /proc/net/route")
}

func runCmdWithCaps(name string, arg ...string) error {
	cmd := exec.Command(name, arg...)
	cmd.SysProcAttr = &syscall.SysProcAttr{
		AmbientCaps: []uintptr{
			unix.CAP_NET_ADMIN,
			unix.CAP_SYS_ADMIN,
			unix.CAP_DAC_OVERRIDE,
			unix.CAP_SYS_PTRACE,
		},
	}
	var stderr strings.Builder
	cmd.Stderr = &stderr
	err := cmd.Run()
	if err != nil {
		return fmt.Errorf("execute %s %v: %w (stderr: %q)", name, arg, err, strings.TrimSpace(stderr.String()))
	}
	return nil
}

func outputCmdWithCaps(name string, arg ...string) ([]byte, error) {
	cmd := exec.Command(name, arg...)
	cmd.SysProcAttr = &syscall.SysProcAttr{
		AmbientCaps: []uintptr{
			unix.CAP_NET_ADMIN,
			unix.CAP_SYS_ADMIN,
			unix.CAP_DAC_OVERRIDE,
			unix.CAP_SYS_PTRACE,
		},
	}
	var stderr strings.Builder
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("execute %s %v: %w (stderr: %q)", name, arg, err, strings.TrimSpace(stderr.String()))
	}
	return out, nil
}

func (r Runner) setupNetwork(pid int) error {
	// Load required NAT and MASQUERADE modules
	_ = runCmdWithCaps("modprobe", "xt_MASQUERADE")
	_ = runCmdWithCaps("modprobe", "nft_masq")
	_ = runCmdWithCaps("modprobe", "nft_chain_nat")

	// Enable IP forwarding
	_ = os.WriteFile("/proc/sys/net/ipv4/ip_forward", []byte("1"), 0o644)

	// Delete existing wl-host interface if it exists
	_ = runCmdWithCaps("ip", "link", "delete", "wl-host")

	// Create veth pair
	if err := runCmdWithCaps("ip", "link", "add", "wl-host", "type", "veth", "peer", "name", "wl-guest"); err != nil {
		return fmt.Errorf("create veth pair: %w", err)
	}

	// Set IP for wl-host
	if err := runCmdWithCaps("ip", "addr", "add", "192.168.250.1/24", "dev", "wl-host"); err != nil {
		return fmt.Errorf("set wl-host IP: %w", err)
	}

	// Bring wl-host up
	if err := runCmdWithCaps("ip", "link", "set", "wl-host", "up"); err != nil {
		return fmt.Errorf("bring wl-host up: %w", err)
	}

	// Move wl-guest to container netns
	pidStr := strconv.Itoa(pid)
	if err := runCmdWithCaps("ip", "link", "set", "wl-guest", "netns", pidStr); err != nil {
		return fmt.Errorf("move wl-guest to netns: %w", err)
	}

	// Configure wl-guest (as eth0) inside container netns
	if err := runCmdWithCaps("nsenter", "-t", pidStr, "-n", "ip", "link", "set", "wl-guest", "name", "eth0"); err != nil {
		return fmt.Errorf("rename wl-guest to eth0: %w", err)
	}
	if err := runCmdWithCaps("nsenter", "-t", pidStr, "-n", "ip", "link", "set", "eth0", "up"); err != nil {
		return fmt.Errorf("bring eth0 up: %w", err)
	}

	// Configure iptables MASQUERADE on the host
	if defIf, err := findDefaultInterface(); err == nil {
		checkCmd := exec.Command("iptables", "-t", "nat", "-C", "POSTROUTING", "-s", "192.168.250.0/24", "-o", defIf, "-j", "MASQUERADE")
		checkCmd.SysProcAttr = &syscall.SysProcAttr{
			AmbientCaps: []uintptr{
				unix.CAP_NET_ADMIN,
				unix.CAP_SYS_ADMIN,
				unix.CAP_DAC_OVERRIDE,
				unix.CAP_SYS_PTRACE,
			},
		}
		if err := checkCmd.Run(); err != nil {
			_ = runCmdWithCaps("iptables", "-t", "nat", "-A", "POSTROUTING", "-s", "192.168.250.0/24", "-o", defIf, "-j", "MASQUERADE")
		}
	}

	// Ensure runtime state dir exists
	_ = os.MkdirAll("/run/phishadowd", 0o755)

	// Kill any existing dnsmasq using our pid file
	if pidData, err := os.ReadFile("/run/phishadowd/dnsmasq.pid"); err == nil {
		if oldPid, err := strconv.Atoi(strings.TrimSpace(string(pidData))); err == nil {
			_ = syscall.Kill(oldPid, syscall.SIGTERM)
		}
		_ = os.Remove("/run/phishadowd/dnsmasq.pid")
	}

	// Start dnsmasq DHCP server strictly bound to wl-host with disabled DNS port
	dnsmasqCmd := exec.Command("dnsmasq",
		"--keep-in-foreground",
		"--port=0",
		"--interface=wl-host",
		"--bind-interfaces",
		"--dhcp-range=192.168.250.2,192.168.250.2,12h",
		"--dhcp-option=option:router,192.168.250.1",
		"--dhcp-option=option:dns-server,8.8.8.8,1.1.1.1",
		"--dhcp-leasefile=/run/phishadowd/dnsmasq.leases",
	)
	dnsmasqCmd.SysProcAttr = &syscall.SysProcAttr{
		AmbientCaps: []uintptr{
			unix.CAP_NET_ADMIN,
			unix.CAP_SYS_ADMIN,
			unix.CAP_DAC_OVERRIDE,
			unix.CAP_SYS_PTRACE,
		},
	}
	if err := dnsmasqCmd.Start(); err == nil {
		_ = os.WriteFile("/run/phishadowd/dnsmasq.pid", []byte(strconv.Itoa(dnsmasqCmd.Process.Pid)), 0o644)
	} else {
		fmt.Fprintf(os.Stderr, "phishadowd: failed to start dnsmasq DHCP server: %v\n", err)
	}

	// Start background goroutine to configure network resolver and captive portal
	go func() {
		deadline := time.Now().Add(180 * time.Second)
		for time.Now().Before(deadline) {
			// Check if container process has completed boot
			out, err := outputCmdWithCaps("nsenter", "-t", pidStr, "-m", "-p", "-u", "-i", "-n", "--", "/system/bin/getprop", "sys.boot_completed")
			if err == nil && strings.TrimSpace(string(out)) == "1" {
				// Disable captive portal verification inside container to speed up validation
				_ = runCmdWithCaps("nsenter", "-t", pidStr, "-m", "-p", "-u", "-i", "-n", "--", "/system/bin/settings", "put", "global", "captive_portal_mode", "0")
				// Add fallback routing table lookup rule for native and non-marked traffic
				_ = runCmdWithCaps("nsenter", "-t", pidStr, "-n", "ip", "rule", "add", "from", "all", "lookup", "main", "priority", "30000")
				return
			}
			time.Sleep(2 * time.Second)
		}
	}()

	return nil
}

func (r Runner) cleanupNetwork() {
	// Kill dnsmasq
	if pidData, err := os.ReadFile("/run/phishadowd/dnsmasq.pid"); err == nil {
		if pid, err := strconv.Atoi(strings.TrimSpace(string(pidData))); err == nil {
			_ = syscall.Kill(pid, syscall.SIGTERM)
		}
		_ = os.Remove("/run/phishadowd/dnsmasq.pid")
	}

	// Delete host-side interface, which also deletes guest-side interface
	_ = runCmdWithCaps("ip", "link", "delete", "wl-host")

	// Remove iptables MASQUERADE rule
	if defIf, err := findDefaultInterface(); err == nil {
		_ = runCmdWithCaps("iptables", "-t", "nat", "-D", "POSTROUTING", "-s", "192.168.250.0/24", "-o", defIf, "-j", "MASQUERADE")
	}
}


func RunInitFromConfig(configPath string) error {
	if runtime.GOOS != "linux" {
		return errors.New("container init requires Linux")
	}

	var cfg Config
	data, err := os.ReadFile(configPath)
	if err != nil {
		return fmt.Errorf("read config %s: %w", configPath, err)
	}
	if err := json.Unmarshal(data, &cfg); err != nil {
		return fmt.Errorf("decode config: %w", err)
	}
	if err := cfg.normalize(); err != nil {
		return err
	}

	runtime.LockOSThread()
	defer runtime.UnlockOSThread()

	if unix.Getpid() != 1 {
		return fmt.Errorf("internal init must be PID 1 inside CLONE_NEWPID namespace, got pid %d", unix.Getpid())
	}
	if err := unix.Sethostname([]byte(cfg.Hostname)); err != nil {
		return fmt.Errorf("set hostname: %w", err)
	}
	if err := setupNetworkNamespace(); err != nil {
		return err
	}
	if err := setupMountNamespace(cfg); err != nil {
		return err
	}
	if err := pivotIntoRoot(cfg.RootFS); err != nil {
		return err
	}

	env := buildContainerEnv(os.Environ(), cfg)
	execPath, err := resolveExecutable(cfg.Command[0], env)
	if err != nil {
		return err
	}

	// Close all inherited file descriptors (except stdin, stdout, stderr)
	// to prevent Zygote from crashing due to non-allowlisted open files.
	for fd := 3; fd < 256; fd++ {
		_ = unix.Close(fd)
	}

	if err := unix.Exec(execPath, cfg.Command, env); err != nil {
		return fmt.Errorf("exec %s: %w", execPath, err)
	}
	return nil
}

func (cfg *Config) normalize() error {
	cfg.ID = sanitizeID(cfg.ID)
	if cfg.ID == "" {
		cfg.ID = "phishadow-" + strconv.Itoa(os.Getpid())
	}
	if cfg.RootFS == "" {
		cfg.RootFS = "/var/lib/phishadow/aosp-rootfs"
	}
	rootfs, err := filepath.Abs(cfg.RootFS)
	if err != nil {
		return fmt.Errorf("absolute rootfs: %w", err)
	}
	rootInfo, err := os.Stat(rootfs)
	if err != nil {
		return fmt.Errorf("stat rootfs %s: %w", rootfs, err)
	}
	if !rootInfo.IsDir() {
		return fmt.Errorf("rootfs %s is not a directory", rootfs)
	}
	cfg.RootFS = rootfs

	if cfg.Hostname == "" {
		cfg.Hostname = "phishadow-droid"
	}
	if len(cfg.Hostname) > 63 {
		return errors.New("hostname must be <= 63 bytes")
	}

	if cfg.WaylandSocket == "" {
		runtimeDir := os.Getenv("XDG_RUNTIME_DIR")
		if runtimeDir == "" {
			if sudoUID := os.Getenv("SUDO_UID"); sudoUID != "" {
				runtimeDir = "/run/user/" + sudoUID
			} else {
				return errors.New("XDG_RUNTIME_DIR is empty; pass --wayland-socket explicitly")
			}
		}
		cfg.WaylandSocket = filepath.Join(runtimeDir, "wayland-0")
	}
	wayland, err := filepath.Abs(cfg.WaylandSocket)
	if err != nil {
		return fmt.Errorf("absolute wayland socket: %w", err)
	}
	if err := validateSocket(wayland); err != nil {
		return err
	}
	cfg.WaylandSocket = wayland

	if cfg.StateDir == "" {
		cfg.StateDir = filepath.Join(defaultStateRoot, cfg.ID)
	}
	stateDir, err := filepath.Abs(cfg.StateDir)
	if err != nil {
		return fmt.Errorf("absolute state dir: %w", err)
	}
	cfg.StateDir = stateDir

	if len(cfg.Command) == 0 {
		cfg.Command = []string{"/init"}
	}
	if cfg.Command[0] == "" {
		return errors.New("container command cannot be empty")
	}
	if cfg.MemoryMaxBytes <= 0 {
		cfg.MemoryMaxBytes = 1024 * 1024 * 1024
	}
	if cfg.CPUQuotaMicros <= 0 {
		cfg.CPUQuotaMicros = 200000
	}
	if cfg.CPUPeriodMicros <= 0 {
		cfg.CPUPeriodMicros = 100000
	}
	if cfg.CPUQuotaMicros > cfg.CPUPeriodMicros*1024 {
		return errors.New("cpu quota is unreasonably high")
	}
	if cfg.PIDsMax <= 0 {
		cfg.PIDsMax = 512
	}
	return nil
}

func sanitizeID(id string) string {
	id = strings.TrimSpace(id)
	var b strings.Builder
	for _, r := range id {
		switch {
		case r >= 'a' && r <= 'z':
			b.WriteRune(r)
		case r >= 'A' && r <= 'Z':
			b.WriteRune(r)
		case r >= '0' && r <= '9':
			b.WriteRune(r)
		case r == '-', r == '_', r == '.':
			b.WriteRune(r)
		}
	}
	return b.String()
}

func validateSocket(path string) error {
	info, err := os.Stat(path)
	if err != nil {
		return fmt.Errorf("stat wayland socket %s: %w", path, err)
	}
	if info.Mode()&os.ModeSocket == 0 {
		return fmt.Errorf("wayland path %s is not a Unix socket", path)
	}
	return nil
}

func writeJSON(path string, value any, perm os.FileMode) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return fmt.Errorf("encode %s: %w", path, err)
	}
	data = append(data, '\n')
	if err := os.WriteFile(path, data, perm); err != nil {
		return fmt.Errorf("write %s: %w", path, err)
	}
	return nil
}

func createContainerCgroup(cfg Config) (*cgroupV2, error) {
	var st unix.Statfs_t
	if err := unix.Statfs("/sys/fs/cgroup", &st); err != nil {
		return nil, fmt.Errorf("statfs /sys/fs/cgroup: %w", err)
	}
	if uint64(st.Type) != cgroup2SuperMagic {
		return nil, errors.New("/sys/fs/cgroup is not cgroup v2")
	}

	root := "/sys/fs/cgroup"
	if err := enableControllers(root, requiredControllers); err != nil {
		return nil, err
	}

	base := filepath.Join(root, "phishadowd")
	if err := os.MkdirAll(base, 0o755); err != nil {
		return nil, fmt.Errorf("create cgroup base %s: %w", base, err)
	}
	if err := enableControllers(base, requiredControllers); err != nil {
		return nil, err
	}

	path := filepath.Join(base, cfg.ID)
	if err := RemoveStaleCgroup(path); err != nil {
		return nil, fmt.Errorf("remove stale cgroup %s: %w", path, err)
	}
	if err := os.Mkdir(path, 0o755); err != nil {
		return nil, fmt.Errorf("create cgroup %s: %w", path, err)
	}

	cg := &cgroupV2{path: path}
	if err := cg.applyLimits(cfg); err != nil {
		_ = os.Remove(path)
		return nil, err
	}
	return cg, nil
}

func enableControllers(dir string, controllers []string) error {
	availableRaw, err := os.ReadFile(filepath.Join(dir, "cgroup.controllers"))
	if err != nil {
		return fmt.Errorf("read cgroup.controllers in %s: %w", dir, err)
	}
	available := splitFields(string(availableRaw))
	subtreePath := filepath.Join(dir, "cgroup.subtree_control")

	for _, controller := range controllers {
		if !available[controller] {
			return fmt.Errorf("cgroup controller %q is not available in %s", controller, dir)
		}
		if controllerEnabled(dir, controller) {
			continue
		}
		if err := os.WriteFile(subtreePath, []byte("+"+controller), 0o644); err != nil {
			if controllerEnabled(dir, controller) {
				continue
			}
			return fmt.Errorf("enable cgroup controller %s in %s: %w", controller, dir, err)
		}
	}
	return nil
}

func controllerEnabled(dir string, controller string) bool {
	data, err := os.ReadFile(filepath.Join(dir, "cgroup.subtree_control"))
	if err != nil {
		return false
	}
	return splitFields(string(data))[controller]
}

func splitFields(s string) map[string]bool {
	fields := strings.Fields(s)
	out := make(map[string]bool, len(fields))
	for _, field := range fields {
		out[field] = true
	}
	return out
}

func (cg *cgroupV2) applyLimits(cfg Config) error {
	memoryHigh := cfg.MemoryMaxBytes * 9 / 10
	if memoryHigh < 64*1024*1024 {
		memoryHigh = cfg.MemoryMaxBytes
	}

	writes := []struct {
		name     string
		value    string
		optional bool
	}{
		{"memory.oom.group", "1", false},
		{"memory.high", strconv.FormatInt(memoryHigh, 10), false},
		{"memory.max", strconv.FormatInt(cfg.MemoryMaxBytes, 10), false},
		{"memory.swap.max", "0", true},
		{"cpu.max", fmt.Sprintf("%d %d", cfg.CPUQuotaMicros, cfg.CPUPeriodMicros), false},
		{"pids.max", strconv.FormatInt(cfg.PIDsMax, 10), false},
	}

	for _, write := range writes {
		path := filepath.Join(cg.path, write.name)
		if err := os.WriteFile(path, []byte(write.value), 0o644); err != nil {
			if write.optional && errors.Is(err, os.ErrNotExist) {
				continue
			}
			return fmt.Errorf("write %s=%s: %w", path, write.value, err)
		}
	}
	return nil
}

func (cg *cgroupV2) openFD() (int, error) {
	fd, err := unix.Open(cg.path, unix.O_DIRECTORY|unix.O_RDONLY|unix.O_CLOEXEC, 0)
	if err != nil {
		return -1, fmt.Errorf("open cgroup fd %s: %w", cg.path, err)
	}
	return fd, nil
}

func (cg *cgroupV2) attach(pid int) error {
	path := filepath.Join(cg.path, "cgroup.procs")
	if err := os.WriteFile(path, []byte(strconv.Itoa(pid)), 0o644); err != nil {
		return fmt.Errorf("attach pid %d to %s: %w", pid, path, err)
	}
	return nil
}

func (cg *cgroupV2) kill() {
	_ = os.WriteFile(filepath.Join(cg.path, "cgroup.kill"), []byte("1"), 0o644)
}

func (cg *cgroupV2) remove() {
	cg.kill()
	deadline := time.Now().Add(2 * time.Second)
	for {
		err := os.Remove(cg.path)
		if err == nil || errors.Is(err, os.ErrNotExist) {
			return
		}
		if time.Now().After(deadline) {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func RemoveStaleCgroup(path string) error {
	if _, err := os.Stat(path); err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return err
	}
	stale := &cgroupV2{path: path}
	stale.kill()
	deadline := time.Now().Add(2 * time.Second)
	for {
		err := os.Remove(path)
		if err == nil || errors.Is(err, os.ErrNotExist) {
			return nil
		}
		if time.Now().After(deadline) {
			return err
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func isCgroupFDFallbackError(err error) bool {
	return errors.Is(err, unix.EINVAL) ||
		errors.Is(err, unix.ENOSYS) ||
		errors.Is(err, unix.EOPNOTSUPP) ||
		strings.Contains(err.Error(), "function not implemented") ||
		strings.Contains(err.Error(), "invalid argument")
}

func closePidFD(pidfd int) {
	// Let Go runtime manage and close the pidfd automatically
}

func supervise(ctx context.Context, cmd *exec.Cmd, cg *cgroupV2) error {
	waitCh := make(chan error, 1)
	go func() {
		waitCh <- cmd.Wait()
	}()

	sigCh := make(chan os.Signal, 4)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM, syscall.SIGHUP, syscall.SIGQUIT)
	defer signal.Stop(sigCh)

	terminating := false
	ctxDone := ctx.Done()
	for {
		select {
		case err := <-waitCh:
			return err
		case sig := <-sigCh:
			if !terminating {
				terminating = true
				forwardSignal(cmd, sig)
				continue
			}
			cg.kill()
			_ = cmd.Process.Kill()
		case <-ctxDone:
			if !terminating {
				terminating = true
				forwardSignal(cmd, syscall.SIGTERM)
				time.AfterFunc(10*time.Second, func() {
					cg.kill()
					_ = cmd.Process.Kill()
				})
			}
			ctxDone = nil
		}
	}
}

func forwardSignal(cmd *exec.Cmd, sig os.Signal) {
	if cmd.Process == nil {
		return
	}
	_ = cmd.Process.Signal(sig)
}

func setupNetworkNamespace() error {
	fd, err := unix.Socket(unix.AF_INET, unix.SOCK_DGRAM|unix.SOCK_CLOEXEC, 0)
	if err != nil {
		return nil
	}
	defer unix.Close(fd)

	ifr, err := unix.NewIfreq("lo")
	if err != nil {
		return nil
	}
	if err := unix.IoctlIfreq(fd, unix.SIOCGIFFLAGS, ifr); err != nil {
		return nil
	}
	ifr.SetUint16(ifr.Uint16() | uint16(unix.IFF_UP))
	_ = unix.IoctlIfreq(fd, unix.SIOCSIFFLAGS, ifr)
	return nil
}

func setupMountNamespace(cfg Config) error {
	if err := unix.Mount("none", "/", "", unix.MS_REC|unix.MS_PRIVATE, ""); err != nil {
		return fmt.Errorf("make root mount private: %w", err)
	}
	if err := bindMount(cfg.RootFS, cfg.RootFS, true); err != nil {
		return fmt.Errorf("make rootfs a mount point: %w", err)
	}

	dirs := []string{
		"proc",
		"sys",
		"dev",
		"run",
		"tmp",
	}
	for _, dir := range dirs {
		if err := ensureDir(filepath.Join(cfg.RootFS, dir), 0o755); err != nil {
			return err
		}
	}

	if err := mountOnce("proc", filepath.Join(cfg.RootFS, "proc"), "proc", unix.MS_NOSUID|unix.MS_NOEXEC|unix.MS_NODEV, ""); err != nil {
		return err
	}
	if err := mountOnce("sysfs", filepath.Join(cfg.RootFS, "sys"), "sysfs", unix.MS_RDONLY|unix.MS_NOSUID|unix.MS_NOEXEC|unix.MS_NODEV, ""); err != nil {
		return err
	}
	if err := mountOnce("tmpfs", filepath.Join(cfg.RootFS, "dev"), "tmpfs", unix.MS_NOSUID|unix.MS_STRICTATIME, "mode=755,size=64m"); err != nil {
		return err
	}
	for _, dir := range []string{"dev/pts", "dev/shm"} {
		if err := ensureDir(filepath.Join(cfg.RootFS, dir), 0o755); err != nil {
			return err
		}
	}
	if err := mountOnce("devpts", filepath.Join(cfg.RootFS, "dev/pts"), "devpts", unix.MS_NOSUID|unix.MS_NOEXEC, "newinstance,ptmxmode=0666,mode=0620,gid=5"); err != nil {
		return err
	}
	_ = os.Remove(filepath.Join(cfg.RootFS, "dev/ptmx"))
	if err := os.Symlink("pts/ptmx", filepath.Join(cfg.RootFS, "dev/ptmx")); err != nil && !errors.Is(err, os.ErrExist) {
		return fmt.Errorf("create /dev/ptmx symlink: %w", err)
	}
	if err := mountOnce("tmpfs", filepath.Join(cfg.RootFS, "dev/shm"), "tmpfs", unix.MS_NOSUID|unix.MS_NOEXEC|unix.MS_NODEV, "mode=1777,size=64m"); err != nil {
		return err
	}
	if err := mountOnce("tmpfs", filepath.Join(cfg.RootFS, "run"), "tmpfs", unix.MS_NOSUID|unix.MS_NOEXEC|unix.MS_NODEV, "mode=755,size=64m"); err != nil {
		return err
	}
	if err := mountOnce("tmpfs", filepath.Join(cfg.RootFS, "tmp"), "tmpfs", unix.MS_NOSUID|unix.MS_NODEV, "mode=1777,size=128m"); err != nil {
		return err
	}

	if err := bindCoreDevices(cfg.RootFS); err != nil {
		return err
	}

	// Đảm bảo /data tồn tại và bind mount nó vào chính nó để tạo mount point riêng rẽ.
	// Nhờ vậy khi Android init remount / thành read-only, /data vẫn giữ được quyền read-write.
	dataPath := filepath.Join(cfg.RootFS, "data")
	if err := ensureDir(dataPath, 0o777); err != nil {
		return err
	}
	if err := bindMount(dataPath, dataPath, true); err != nil {
		return fmt.Errorf("bind data: %w", err)
	}

	// Bind mount the shared host folder for user file access
	sharedPath := "/home/dang-nhat-phi/AeroAPK/shared"
	_ = os.MkdirAll(sharedPath, 0o777)
	_ = os.Chmod(sharedPath, 0o777)

	downloadPath := filepath.Join(cfg.RootFS, "data/media/0/Download")
	if err := os.MkdirAll(downloadPath, 0o777); err == nil {
		_ = os.Chmod(downloadPath, 0o777)
		if err := bindMount(sharedPath, downloadPath, true); err != nil {
			fmt.Fprintf(os.Stderr, "phishadowd: warning: failed to bind shared folder: %v\n", err)
		}
	}

	if err := setupBinderFS(filepath.Join(cfg.RootFS, "dev")); err != nil {
		return err
	}
	if err := bindDRI(cfg.RootFS); err != nil {
		return err
	}
	if err := bindWaylandSocket(cfg.RootFS, cfg.WaylandSocket); err != nil {
		return err
	}

	return nil
}

func bindCoreDevices(rootfs string) error {
	for _, name := range []string{"tty", "fuse"} {
		src := filepath.Join("/dev", name)
		dst := filepath.Join(rootfs, "dev", name)
		if err := bindMountFile(src, dst); err != nil {
			return err
		}
	}
	if _, err := os.Stat("/dev/dma_heap"); err == nil {
		target := filepath.Join(rootfs, "dev/dma_heap")
		if err := ensureDir(target, 0o755); err != nil {
			return err
		}
		if err := bindMount("/dev/dma_heap", target, true); err != nil {
			return fmt.Errorf("bind /dev/dma_heap: %w", err)
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("stat /dev/dma_heap: %w", err)
	}
	return nil
}

func setupBinderFS(devRoot string) error {
	mountPoint := filepath.Join(devRoot, "binderfs")
	if err := ensureDir(mountPoint, 0o755); err != nil {
		return err
	}
	if err := mountOnce("binder", mountPoint, "binder", unix.MS_NOSUID|unix.MS_NOEXEC, "max=3"); err != nil {
		return fmt.Errorf("mount binderfs at %s: %w", mountPoint, err)
	}
	for _, name := range []string{"binder", "hwbinder", "vndbinder"} {
		if err := addBinderDevice(mountPoint, name); err != nil {
			return err
		}
		src := filepath.Join(mountPoint, name)
		dst := filepath.Join(devRoot, name)
		if err := bindMountFile(src, dst); err != nil {
			return err
		}
	}
	return nil
}

func addBinderDevice(mountPoint string, name string) error {
	control := filepath.Join(mountPoint, "binder-control")
	fd, err := unix.Open(control, unix.O_RDONLY|unix.O_CLOEXEC, 0)
	if err != nil {
		return fmt.Errorf("open %s: %w", control, err)
	}
	defer unix.Close(fd)

	var dev binderfsDevice
	copy(dev.Name[:], name)
	request := ioctlIOWR('b', 1, unsafe.Sizeof(dev))
	_, _, errno := unix.Syscall(unix.SYS_IOCTL, uintptr(fd), request, uintptr(unsafe.Pointer(&dev)))
	if errno != 0 {
		if errno != unix.EEXIST {
			return fmt.Errorf("BINDER_CTL_ADD %s: %w", name, errno)
		}
	}

	createdDev := filepath.Join(mountPoint, name)
	if err := os.Chmod(createdDev, 0666); err != nil {
		return fmt.Errorf("chmod 0666 %s: %w", createdDev, err)
	}

	return nil
}

func ioctlIOWR(typ byte, nr byte, size uintptr) uintptr {
	const (
		iocNRBits    = 8
		iocTypeBits  = 8
		iocSizeBits  = 14
		iocNRShift   = 0
		iocTypeShift = iocNRShift + iocNRBits
		iocSizeShift = iocTypeShift + iocTypeBits
		iocDirShift  = iocSizeShift + iocSizeBits
		iocWrite     = 1
		iocRead      = 2
	)
	return uintptr((iocRead|iocWrite)<<iocDirShift) |
		(uintptr(typ) << iocTypeShift) |
		(uintptr(nr) << iocNRShift) |
		(size << iocSizeShift)
}

func bindDRI(rootfs string) error {
	info, err := os.Stat("/dev/dri")
	if err != nil {
		return fmt.Errorf("stat /dev/dri: %w", err)
	}
	if !info.IsDir() {
		return errors.New("/dev/dri is not a directory")
	}
	target := filepath.Join(rootfs, "dev/dri")
	if err := ensureDir(target, 0o755); err != nil {
		return err
	}
	entries, err := os.ReadDir("/dev/dri")
	if err != nil {
		return fmt.Errorf("read /dev/dri: %w", err)
	}

	hasCard0 := false
	var firstCardSrc string
	var firstCardRdev int

	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		name := entry.Name()
		if !isDRIDeviceNode(name) {
			continue
		}
		if name == "card0" {
			hasCard0 = true
		}
		src := filepath.Join("/dev/dri", name)
		dst := filepath.Join(target, name)
		var st unix.Stat_t
		if err := unix.Stat(src, &st); err != nil {
			return fmt.Errorf("stat %s: %w", src, err)
		}
		if st.Mode&unix.S_IFMT != unix.S_IFCHR {
			continue
		}
		if strings.HasPrefix(name, "card") && firstCardSrc == "" {
			firstCardSrc = src
			firstCardRdev = int(st.Rdev)
		}
		_ = os.Remove(dst)
		if err := unix.Mknod(dst, uint32(unix.S_IFCHR|0o666), int(st.Rdev)); err != nil {
			return fmt.Errorf("mknod %s from %s: %w", dst, src, err)
		}
		if err := os.Chown(dst, 0, 0); err != nil {
			return fmt.Errorf("chown %s: %w", dst, err)
		}
		if err := os.Chmod(dst, 0o666); err != nil {
			return fmt.Errorf("chmod %s: %w", dst, err)
		}
	}

	if !hasCard0 && firstCardSrc != "" {
		dst := filepath.Join(target, "card0")
		_ = os.Remove(dst)
		if err := unix.Mknod(dst, uint32(unix.S_IFCHR|0o666), firstCardRdev); err != nil {
			return fmt.Errorf("mknod %s from %s: %w", dst, firstCardSrc, err)
		}
		_ = os.Chown(dst, 0, 0)
		_ = os.Chmod(dst, 0o666)
	}

	return nil
}

func isDRIDeviceNode(name string) bool {
	return strings.HasPrefix(name, "card") || strings.HasPrefix(name, "renderD")
}

func bindWaylandSocket(rootfs string, socketPath string) error {
	if err := validateSocket(socketPath); err != nil {
		return err
	}

	runtimeDir := filepath.Dir(socketPath)
	for _, displayName := range waylandSocketAliases(rootfs, socketPath) {
		target := waylandSocketTarget(rootfs, runtimeDir, displayName)
		if err := ensureFileMountPoint(target, 0o600); err != nil {
			return err
		}
		if err := bindMount(socketPath, target, false); err != nil {
			return fmt.Errorf("bind wayland socket %s to %s: %w", socketPath, target, err)
		}
	}
	return nil
}

func waylandSocketAliases(rootfs string, socketPath string) []string {
	seen := make(map[string]bool, 6)
	aliases := make([]string, 0, 6)
	add := func(name string) {
		name = cleanWaylandDisplayName(name)
		if name == "" || seen[name] {
			return
		}
		seen[name] = true
		aliases = append(aliases, name)
	}

	add("wayland-0")
	add(filepath.Base(socketPath))
	add("wayland-aero")
	for _, value := range readRootFSPropertyValues(rootfs, "wayland.display") {
		add(value)
	}
	return aliases
}

func cleanWaylandDisplayName(name string) string {
	name = strings.TrimSpace(name)
	name = strings.Trim(name, `"'`)
	if name == "" || strings.ContainsAny(name, "\x00\r\n") {
		return ""
	}
	if filepath.IsAbs(name) {
		return filepath.Clean(name)
	}
	name = filepath.Clean(name)
	if name == "." || name == ".." || strings.HasPrefix(name, "../") {
		return ""
	}
	return name
}

func waylandSocketTarget(rootfs string, runtimeDir string, displayName string) string {
	if filepath.IsAbs(displayName) {
		return pathInsideRoot(rootfs, displayName)
	}
	return pathInsideRoot(rootfs, filepath.Join(runtimeDir, displayName))
}

func readRootFSPropertyValues(rootfs string, key string) []string {
	files := []string{
		"system/build.prop",
		"system/product/etc/build.prop",
		"system/system_ext/etc/build.prop",
		"vendor/build.prop",
		"vendor/odm/etc/build.prop",
		"vendor/waydroid.prop",
	}
	values := make([]string, 0, 2)
	prefix := key + "="
	for _, rel := range files {
		data, err := os.ReadFile(filepath.Join(rootfs, rel))
		if err != nil {
			continue
		}
		for _, line := range strings.Split(string(data), "\n") {
			line = strings.TrimSpace(line)
			if line == "" || strings.HasPrefix(line, "#") || !strings.HasPrefix(line, prefix) {
				continue
			}
			values = append(values, strings.TrimSpace(strings.TrimPrefix(line, prefix)))
		}
	}
	return values
}

func bindMountFile(source string, target string) error {
	info, err := os.Stat(source)
	if err != nil {
		return fmt.Errorf("stat %s: %w", source, err)
	}
	if info.IsDir() {
		if err := ensureDir(target, 0o755); err != nil {
			return err
		}
		return bindMount(source, target, true)
	}
	if err := ensureFileMountPoint(target, 0o600); err != nil {
		return err
	}
	return bindMount(source, target, false)
}

func bindMount(source string, target string, recursive bool) error {
	flags := uintptr(unix.MS_BIND)
	if recursive {
		flags |= unix.MS_REC
	}
	if err := unix.Mount(source, target, "", flags, ""); err != nil {
		return fmt.Errorf("mount --bind %s %s: %w", source, target, err)
	}
	return nil
}

func mountOnce(source string, target string, fstype string, flags uintptr, data string) error {
	if err := unix.Mount(source, target, fstype, flags, data); err != nil {
		if errors.Is(err, unix.EBUSY) {
			return nil
		}
		return fmt.Errorf("mount %s type %s at %s: %w", source, fstype, target, err)
	}
	return nil
}

func ensureDir(path string, mode os.FileMode) error {
	info, err := os.Stat(path)
	if err == nil {
		if !info.IsDir() {
			return fmt.Errorf("%s exists and is not a directory", path)
		}
		return nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("stat %s: %w", path, err)
	}
	if err := os.MkdirAll(path, mode); err != nil {
		return fmt.Errorf("mkdir %s: %w", path, err)
	}
	return nil
}

func ensureFileMountPoint(path string, mode os.FileMode) error {
	if err := ensureDir(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	info, err := os.Lstat(path)
	if err == nil {
		if info.IsDir() {
			return fmt.Errorf("%s exists and is a directory", path)
		}
		return nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("lstat %s: %w", path, err)
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_RDWR, mode)
	if err != nil {
		return fmt.Errorf("create mount point %s: %w", path, err)
	}
	return file.Close()
}

func pathInsideRoot(rootfs string, absPath string) string {
	clean := filepath.Clean(absPath)
	clean = strings.TrimPrefix(clean, string(filepath.Separator))
	return filepath.Join(rootfs, clean)
}

func pivotIntoRoot(rootfs string) error {
	putOld := filepath.Join(rootfs, ".pivot_old")
	if err := ensureDir(putOld, 0o700); err != nil {
		return err
	}
	if err := unix.PivotRoot(rootfs, putOld); err != nil {
		return fmt.Errorf("pivot_root %s: %w", rootfs, err)
	}
	if err := unix.Chdir("/"); err != nil {
		return fmt.Errorf("chdir / after pivot_root: %w", err)
	}
	if err := unix.Unmount("/.pivot_old", unix.MNT_DETACH); err != nil {
		return fmt.Errorf("unmount old root: %w", err)
	}
	if err := os.RemoveAll("/.pivot_old"); err != nil {
		return fmt.Errorf("remove old root: %w", err)
	}
	return nil
}

func buildContainerEnv(hostEnv []string, cfg Config) []string {
	out := make([]string, 0, len(hostEnv)+4)
	runtimeDir := filepath.Dir(cfg.WaylandSocket)
	for _, kv := range hostEnv {
		if strings.HasPrefix(kv, "XDG_RUNTIME_DIR=") ||
			strings.HasPrefix(kv, "WAYLAND_DISPLAY=") ||
			strings.HasPrefix(kv, "DISPLAY=") {
			continue
		}
		out = append(out, kv)
	}
	out = append(out,
		"container=phishadow-droid",
		"XDG_RUNTIME_DIR="+runtimeDir,
		"WAYLAND_DISPLAY=wayland-0",
		"ANDROID_BINDER_DEVICES=/dev/binder,/dev/hwbinder,/dev/vndbinder",
	)
	return out
}

func resolveExecutable(name string, env []string) (string, error) {
	if strings.ContainsRune(name, '/') {
		return name, nil
	}
	pathValue := "/system/bin:/vendor/bin:/odm/bin:/bin:/usr/bin:/sbin:/usr/sbin"
	for _, kv := range env {
		if strings.HasPrefix(kv, "PATH=") {
			pathValue = strings.TrimPrefix(kv, "PATH=")
			break
		}
	}
	for _, dir := range filepath.SplitList(pathValue) {
		if dir == "" {
			continue
		}
		candidate := filepath.Join(dir, name)
		info, err := os.Stat(candidate)
		if err != nil || info.IsDir() || info.Mode()&0o111 == 0 {
			continue
		}
		return candidate, nil
	}
	return "", fmt.Errorf("executable %q not found in PATH", name)
}

func hasEffectiveCapability(bit uint) bool {
	data, err := os.ReadFile("/proc/self/status")
	if err != nil {
		return false
	}
	for _, line := range strings.Split(string(data), "\n") {
		if !strings.HasPrefix(line, "CapEff:") {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) != 2 {
			return false
		}
		value, err := strconv.ParseUint(fields[1], 16, 64)
		if err != nil {
			return false
		}
		return value&(uint64(1)<<bit) != 0
	}
	return false
}
