package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"phishadow-droid/container"
)

func main() {
	if len(os.Args) > 1 && os.Args[1] == container.InternalInitArg {
		if err := runInternalInit(os.Args[2:]); err != nil {
			fmt.Fprintf(os.Stderr, "container init failed: %v\n", err)
			os.Exit(1)
		}
		return
	}

	if len(os.Args) < 2 {
		printUsage()
		os.Exit(2)
	}

	switch os.Args[1] {
	case "run":
		if err := runContainer(os.Args[2:]); err != nil {
			exitWithError(err)
		}
	case "stop":
		if err := runStop(os.Args[2:]); err != nil {
			exitWithError(err)
		}
	case "install":
		if err := runInstall(os.Args[2:]); err != nil {
			exitWithError(err)
		}
	case "launch":
		if err := runLaunch(os.Args[2:]); err != nil {
			exitWithError(err)
		}
	case "exec":
		if err := runExec(os.Args[2:]); err != nil {
			exitWithError(err)
		}
	case "uninstall":
		if err := runUninstall(os.Args[2:]); err != nil {
			exitWithError(err)
		}
	case "status":
		if err := runStatus(os.Args[2:]); err != nil {
			exitWithError(err)
		}
	case "help", "-h", "--help":
		printUsage()
	default:
		fmt.Fprintf(os.Stderr, "unknown command: %s\n\n", os.Args[1])
		printUsage()
		os.Exit(2)
	}
}

func runInternalInit(args []string) error {
	fs := flag.NewFlagSet(container.InternalInitArg, flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	configPath := fs.String("config", "", "absolute path to internal container config")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *configPath == "" {
		return errors.New("missing --config")
	}
	return container.RunInitFromConfig(*configPath)
}

func runContainer(args []string) error {
	fs := flag.NewFlagSet("run", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)

	var (
		cfg       container.Config
		memLimit  string
		stateBase string
	)

	fs.StringVar(&cfg.ID, "name", "", "container name; default phishadow-<pid>")
	fs.StringVar(&cfg.RootFS, "rootfs", "/var/lib/phishadow/aosp-rootfs", "AOSP root filesystem directory")
	fs.StringVar(&cfg.Hostname, "hostname", "phishadow-droid", "UTS hostname inside the container")
	fs.StringVar(&cfg.WaylandSocket, "wayland-socket", "", "Wayland socket path; default $XDG_RUNTIME_DIR/wayland-0")
	fs.StringVar(&cfg.DisplayMode, "display-mode", "", "display mode: auto, nested-wayland, nested-x11, headless, native")
	fs.StringVar(&stateBase, "state-dir", "/run/phishadowd", "daemon runtime state directory")
	fs.StringVar(&memLimit, "memory", "3072M", "cgroup v2 hard memory limit, e.g. 768M, 1024M, 2G, 3G")
	fs.Int64Var(&cfg.CPUQuotaMicros, "cpu-quota", 200000, "cgroup v2 CPU quota in microseconds per period")
	fs.Int64Var(&cfg.CPUPeriodMicros, "cpu-period", 100000, "cgroup v2 CPU period in microseconds")
	fs.Int64Var(&cfg.PIDsMax, "pids-max", 512, "maximum number of tasks in the container cgroup")

	if err := fs.Parse(args); err != nil {
		return err
	}

	memBytes, err := parseByteSize(memLimit)
	if err != nil {
		return fmt.Errorf("invalid --memory: %w", err)
	}
	cfg.MemoryMaxBytes = memBytes
	cfg.Command = fs.Args()
	if len(cfg.Command) == 0 {
		cfg.Command = []string{"/init"}
	}
	if cfg.ID == "" {
		cfg.ID = fmt.Sprintf("phishadow-%d", os.Getpid())
	}
	if stateBase != "" {
		cfg.StateDir = strings.TrimRight(stateBase, "/") + "/" + cfg.ID
	}

	runner := container.Runner{Config: cfg}
	return runner.Run(context.Background())
}

func parseByteSize(s string) (int64, error) {
	raw := strings.TrimSpace(s)
	if raw == "" {
		return 0, errors.New("empty size")
	}

	upper := strings.ToUpper(raw)
	units := []struct {
		suffix string
		mult   int64
	}{
		{"KIB", 1024},
		{"MIB", 1024 * 1024},
		{"GIB", 1024 * 1024 * 1024},
		{"KB", 1000},
		{"MB", 1000 * 1000},
		{"GB", 1000 * 1000 * 1000},
		{"K", 1024},
		{"M", 1024 * 1024},
		{"G", 1024 * 1024 * 1024},
		{"B", 1},
	}

	mult := int64(1)
	num := upper
	for _, unit := range units {
		if strings.HasSuffix(upper, unit.suffix) {
			mult = unit.mult
			num = strings.TrimSpace(strings.TrimSuffix(upper, unit.suffix))
			break
		}
	}
	if num == "" {
		return 0, fmt.Errorf("%q has no numeric value", s)
	}
	value, err := strconv.ParseInt(num, 10, 64)
	if err != nil {
		return 0, err
	}
	if value <= 0 {
		return 0, errors.New("size must be positive")
	}
	if value > (1<<63-1)/mult {
		return 0, errors.New("size overflows int64")
	}
	return value * mult, nil
}

func exitWithError(err error) {
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		if ws, ok := exitErr.Sys().(syscall.WaitStatus); ok {
			if ws.Signaled() {
				os.Exit(128 + int(ws.Signal()))
			}
			os.Exit(ws.ExitStatus())
		}
	}
	fmt.Fprintf(os.Stderr, "phishadowd: %v\n", err)
	os.Exit(1)
}

func runStop(args []string) error {
	fs := flag.NewFlagSet("stop", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	name := fs.String("name", "default-android", "container name to stop")
	if err := fs.Parse(args); err != nil {
		return err
	}
	return container.RemoveStaleCgroup(containerCgroupPath(*name))
}

func runInstall(args []string) error {
	fs := flag.NewFlagSet("install", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	name := fs.String("name", "default-android", "container name")
	replace := fs.Bool("replace", true, "replace an existing installed package")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() < 1 {
		return errors.New("missing apk path")
	}
	apkPath := fs.Arg(0)

	info, err := os.Stat(apkPath)
	if err != nil {
		return fmt.Errorf("stat apk: %w", err)
	}
	if info.IsDir() {
		return fmt.Errorf("apk path is a directory: %s", apkPath)
	}
	if err := waitForAndroidReady(*name, 180*time.Second); err != nil {
		return err
	}

	f, err := os.Open(apkPath)
	if err != nil {
		return fmt.Errorf("open apk: %w", err)
	}
	defer f.Close()

	sizeStr := strconv.FormatInt(info.Size(), 10)
	pmArgs := []string{"/system/bin/pm", "install", "-g"}
	if *replace {
		pmArgs = append(pmArgs, "-r")
	}
	pmArgs = append(pmArgs, "-S", sizeStr)

	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()

	cmd, err := commandInContainerContext(ctx, *name, pmArgs...)
	if err != nil {
		return err
	}
	cmd.Stdin = f
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr

	if err := cmd.Run(); err != nil {
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			return errors.New("apk install timed out after 120s")
		}
		return err
	}
	return nil
}

func runLaunch(args []string) error {
	fs := flag.NewFlagSet("launch", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	name := fs.String("name", "default-android", "container name")
	packageName := fs.String("package", "", "Android package name")
	activity := fs.String("activity", "", "fully qualified or package-relative Activity name")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *packageName == "" {
		return errors.New("missing --package")
	}
	if err := waitForAndroidReady(*name, 180*time.Second); err != nil {
		return err
	}
	if err := setWaydroidActiveApp(*name, *packageName); err != nil {
		return err
	}

	var launchArgs []string
	if *activity != "" {
		launchArgs = []string{
			"/system/bin/am", "start",
			"-a", "android.intent.action.MAIN",
			"-c", "android.intent.category.LAUNCHER",
			"-n", componentName(*packageName, *activity),
		}
	} else {
		launchArgs = []string{
			"/system/bin/monkey",
			"-p", *packageName,
			"-c", "android.intent.category.LAUNCHER",
			"1",
		}
	}
	return runInContainer(*name, launchArgs...)
}

func setWaydroidActiveApp(name string, packageName string) error {
	if err := runInContainer(name, "/system/bin/setprop", "waydroid.active_apps", packageName); err != nil {
		return fmt.Errorf("set waydroid.active_apps=%s: %w", packageName, err)
	}
	return nil
}

func runUninstall(args []string) error {
	fs := flag.NewFlagSet("uninstall", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	name := fs.String("name", "default-android", "container name")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() < 1 {
		return errors.New("missing package name")
	}
	if err := waitForAndroidReady(*name, 180*time.Second); err != nil {
		return err
	}
	return runInContainer(*name, "/system/bin/pm", "uninstall", fs.Arg(0))
}

func runExec(args []string) error {
	fs := flag.NewFlagSet("exec", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	name := fs.String("name", "default-android", "container name")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() < 1 {
		return errors.New("missing command")
	}
	return runInContainer(*name, fs.Args()...)
}

func runStatus(args []string) error {
	fs := flag.NewFlagSet("status", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	name := fs.String("name", "default-android", "container name")
	if err := fs.Parse(args); err != nil {
		return err
	}
	pid, err := containerPID(*name)
	if err != nil {
		fmt.Println("STOPPED")
		return nil
	}
	fmt.Printf("RUNNING pid=%s\n", pid)
	return nil
}

func runInContainer(name string, args ...string) error {
	cmd, err := commandInContainer(name, args...)
	if err != nil {
		return err
	}
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

func commandInContainer(name string, args ...string) (*exec.Cmd, error) {
	return commandInContainerContext(context.Background(), name, args...)
}

func commandInContainerContext(ctx context.Context, name string, args ...string) (*exec.Cmd, error) {
	pid, err := containerPID(name)
	if err != nil {
		return nil, err
	}
	nsenterArgs := append([]string{"-t", pid, "-m", "-p", "-u", "-i", "-n", "--"}, args...)
	cmd := exec.CommandContext(ctx, "nsenter", nsenterArgs...)
	if os.Geteuid() != 0 {
		cmd.SysProcAttr = &syscall.SysProcAttr{
			Credential: &syscall.Credential{Uid: 0, Gid: 0},
		}
	}
	return cmd, nil
}

func waitForAndroidReady(name string, timeout time.Duration) error {
	requiredServices := []string{"activity", "package", "mount"}
	deadline := time.Now().Add(timeout)
	var lastDetail string
	for {
		missing := make([]string, 0, len(requiredServices)+1)
		details := make([]string, 0, len(requiredServices)+1)

		for _, service := range requiredServices {
			ready, output, err := androidServiceReady(name, service)
			if err != nil {
				return err
			}
			if !ready {
				missing = append(missing, service)
				details = append(details, fmt.Sprintf("%s=%s", service, emptyAs(output, "no response")))
			}
		}

		bootCompleted, err := androidGetProp(name, "sys.boot_completed")
		if err != nil {
			return err
		}
		if bootCompleted != "1" {
			missing = append(missing, "sys.boot_completed")
			details = append(details, fmt.Sprintf("sys.boot_completed=%s", emptyAs(bootCompleted, "empty")))
		}

		if len(missing) == 0 {
			return nil
		}
		lastDetail = strings.Join(details, "; ")
		if time.Now().After(deadline) {
			if lastDetail == "" {
				lastDetail = "no response"
			}
			return fmt.Errorf("android framework not ready after %s: missing %s (%s)", timeout, strings.Join(missing, ", "), lastDetail)
		}
		time.Sleep(2 * time.Second)
	}
}

func androidServiceReady(name string, service string) (bool, string, error) {
	cmd, err := commandInContainer(name, "/system/bin/service", "check", service)
	if err != nil {
		return false, "", err
	}
	out, err := cmd.CombinedOutput()
	output := strings.TrimSpace(string(out))
	if err != nil {
		return false, output, nil
	}
	return strings.Contains(output, ": found"), output, nil
}

func androidGetProp(name string, prop string) (string, error) {
	cmd, err := commandInContainer(name, "/system/bin/getprop", prop)
	if err != nil {
		return "", err
	}
	out, err := cmd.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("read android property %q: %w: %s", prop, err, strings.TrimSpace(string(out)))
	}
	return strings.TrimSpace(string(out)), nil
}

func emptyAs(value string, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}

func containerPID(name string) (string, error) {
	procsFile := filepath.Join(containerCgroupPath(name), "cgroup.procs")
	procsData, err := os.ReadFile(procsFile)
	if err != nil {
		return "", fmt.Errorf("container not running (cannot read %s): %w", procsFile, err)
	}

	for _, line := range strings.Split(string(procsData), "\n") {
		pid := strings.TrimSpace(line)
		if pid != "" {
			return pid, nil
		}
	}
	return "", errors.New("container has no processes")
}

func containerCgroupPath(name string) string {
	return filepath.Join("/sys/fs/cgroup/phishadowd", name)
}

func componentName(packageName string, activity string) string {
	if strings.Contains(activity, "/") {
		return activity
	}
	if strings.HasPrefix(activity, ".") {
		return packageName + "/" + packageName + activity
	}
	if strings.Contains(activity, ".") {
		return packageName + "/" + activity
	}
	return packageName + "/" + packageName + "." + activity
}

func printUsage() {
	fmt.Fprintf(os.Stderr, `PhiShadow Droid container daemon

Usage:
  phishadowd run [options] -- [/init args...]
  phishadowd stop [--name NAME]
  phishadowd status [--name NAME]
  phishadowd install [--name NAME] [--replace=true] <apk_path>
  phishadowd launch [--name NAME] --package PACKAGE [--activity ACTIVITY]
  phishadowd uninstall [--name NAME] PACKAGE
  phishadowd exec [--name NAME] -- COMMAND [ARGS...]

Options (run):
  --rootfs PATH              AOSP root filesystem directory
  --name NAME                container name
  --hostname NAME            UTS hostname inside container
  --wayland-socket PATH      default: $XDG_RUNTIME_DIR/wayland-0
  --display-mode MODE        auto, nested-wayland, nested-x11, headless, native
  --state-dir PATH           default: /run/phishadowd
  --memory SIZE              hard memory limit, default: 3072M
  --cpu-quota MICROS         default: 200000
  --cpu-period MICROS        default: 100000
  --pids-max N               default: 512
`)
}
