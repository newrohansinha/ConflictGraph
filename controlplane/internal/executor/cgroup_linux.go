//go:build linux

package executor

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

// CgroupTracer gives every test execution a cgroup-v2 identity and registers
// both that identity and the initial PID with the Rust tracer. The PID is a
// fallback for the short interval between process start and cgroup migration;
// fork attribution in the tracer carries that identity into child processes.
type CgroupTracer struct {
	Root          string
	ControlSocket string
	DialTimeout   time.Duration
}

type tracerControlMessage struct {
	Action      string `json:"action"`
	ExecutionID string `json:"execution_id"`
	TestID      string `json:"test_id,omitempty"`
	CgroupID    uint64 `json:"cgroup_id,omitempty"`
	RootPID     uint32 `json:"root_pid,omitempty"`
}

func NewCgroupTracer(root, controlSocket string) (*CgroupTracer, error) {
	if root == "" || controlSocket == "" {
		return nil, errors.New("cgroup root and tracer control socket are required")
	}
	if _, err := os.Stat("/sys/fs/cgroup/cgroup.controllers"); err != nil {
		return nil, fmt.Errorf("cgroup v2 is not mounted: %w", err)
	}
	if err := os.MkdirAll(root, 0o755); err != nil {
		return nil, fmt.Errorf("create ConflictGraph cgroup root: %w", err)
	}
	return &CgroupTracer{Root: root, ControlSocket: controlSocket, DialTimeout: 2 * time.Second}, nil
}

func (tracer *CgroupTracer) Register(ctx context.Context, executionID, testID string, pid int) (func(context.Context) error, error) {
	if pid <= 0 || executionID == "" || testID == "" {
		return nil, errors.New("execution ID, test ID, and a positive PID are required")
	}
	name := "execution-" + safeCgroupName(executionID)
	path := filepath.Join(tracer.Root, name)
	if err := os.Mkdir(path, 0o755); err != nil {
		return nil, fmt.Errorf("create execution cgroup: %w", err)
	}
	cgroupID, err := cgroupInode(path)
	if err != nil {
		_ = os.Remove(path)
		return nil, err
	}
	register := tracerControlMessage{Action: "register", ExecutionID: executionID, TestID: testID, CgroupID: cgroupID, RootPID: uint32(pid)}
	if err := tracer.send(ctx, register); err != nil {
		_ = os.Remove(path)
		return nil, fmt.Errorf("register execution with tracer: %w", err)
	}
	if err := os.WriteFile(filepath.Join(path, "cgroup.procs"), []byte(fmt.Sprintf("%d\n", pid)), 0o644); err != nil {
		_ = tracer.send(context.Background(), tracerControlMessage{Action: "unregister", ExecutionID: executionID})
		_ = os.Remove(path)
		return nil, fmt.Errorf("move process %d into execution cgroup: %w", pid, err)
	}
	return func(cleanupContext context.Context) error {
		unregisterErr := tracer.send(cleanupContext, tracerControlMessage{Action: "unregister", ExecutionID: executionID})
		removeErr := os.Remove(path)
		if errors.Is(removeErr, os.ErrNotExist) {
			removeErr = nil
		}
		return errors.Join(unregisterErr, removeErr)
	}, nil
}

func (tracer *CgroupTracer) send(ctx context.Context, message tracerControlMessage) error {
	timeout := tracer.DialTimeout
	if timeout <= 0 {
		timeout = 2 * time.Second
	}
	dialer := net.Dialer{Timeout: timeout}
	connection, err := dialer.DialContext(ctx, "unix", tracer.ControlSocket)
	if err != nil {
		return err
	}
	defer connection.Close()
	deadline, hasDeadline := ctx.Deadline()
	if !hasDeadline {
		deadline = time.Now().Add(timeout)
	}
	if err := connection.SetWriteDeadline(deadline); err != nil {
		return err
	}
	writer := bufio.NewWriter(connection)
	if err := json.NewEncoder(writer).Encode(message); err != nil {
		return err
	}
	return writer.Flush()
}

func cgroupInode(path string) (uint64, error) {
	info, err := os.Stat(path)
	if err != nil {
		return 0, fmt.Errorf("stat execution cgroup: %w", err)
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Ino == 0 {
		return 0, errors.New("kernel did not expose a cgroup inode")
	}
	return stat.Ino, nil
}

func safeCgroupName(value string) string {
	value = strings.ToLower(value)
	var result strings.Builder
	result.Grow(len(value))
	for _, character := range value {
		if character >= 'a' && character <= 'z' || character >= '0' && character <= '9' || character == '-' || character == '_' {
			result.WriteRune(character)
		} else {
			result.WriteByte('-')
		}
	}
	if result.Len() == 0 {
		return "unknown"
	}
	return result.String()
}
