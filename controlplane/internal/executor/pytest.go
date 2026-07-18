package executor

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"regexp"
	"runtime"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/google/uuid"
	"github.com/newrohansinha/ML-CI-Test-Conflict-Detection-Scheduler/controlplane/internal/types"
)

var nodeIDPattern = regexp.MustCompile(`^[^\s]+::[^\s]+$`)

type Pytest struct {
	Python      string
	Directory   string
	Timeout     time.Duration
	ExtraArgs   []string
	Environment []string
	OutputLimit int64
}

func NewPytest(directory string) *Pytest {
	return &Pytest{"python3", directory, 5 * time.Minute, nil, nil, 1_000_000}
}

func stableID(nodeID string) string {
	digest := sha256.Sum256([]byte("local\x00pytest\x00" + nodeID))
	return "test_" + hex.EncodeToString(digest[:12])
}

func (p *Pytest) Collect(ctx context.Context, targets []string) ([]types.Test, error) {
	arguments := append([]string{"-m", "pytest", "--collect-only", "-q"}, targets...)
	arguments = append(arguments, p.ExtraArgs...)
	command := exec.CommandContext(ctx, p.Python, arguments...)
	command.Dir = p.Directory
	command.Env = p.environment(nil)
	var stdout, stderr bytes.Buffer
	command.Stdout = &stdout
	command.Stderr = &stderr
	if err := command.Run(); err != nil {
		var exit *exec.ExitError
		if !errors.As(err, &exit) || exit.ExitCode() != 5 {
			return nil, fmt.Errorf("pytest collection failed: %w: %s", err, strings.TrimSpace(stderr.String()))
		}
	}
	tests := []types.Test{}
	scanner := bufio.NewScanner(&stdout)
	for scanner.Scan() {
		node := strings.TrimSpace(scanner.Text())
		if !nodeIDPattern.MatchString(node) {
			continue
		}
		parts := strings.Split(node, "::")
		function := parts[len(parts)-1]
		parameters := ""
		if start := strings.IndexByte(function, '['); start >= 0 && strings.HasSuffix(function, "]") {
			parameters = function[start+1 : len(function)-1]
			function = function[:start]
		}
		class := ""
		if len(parts) > 2 {
			class = parts[1]
		}
		tests = append(tests, types.Test{ID: stableID(node), NodeID: node, Repository: "local", Suite: "pytest", Framework: "pytest", File: parts[0], Class: class, Function: function, Parameters: parameters, SourceRevision: "unknown", DurationEMA: 1, DurationMedian: 1})
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("read pytest collection: %w", err)
	}
	if len(tests) == 0 {
		return nil, errors.New("pytest collected no stable node IDs")
	}
	return tests, nil
}

func (p *Pytest) environment(extra map[string]string) []string {
	values := append([]string{}, os.Environ()...)
	values = append(values, p.Environment...)
	values = append(values, "PYTHONHASHSEED=0")
	for key, value := range extra {
		values = append(values, key+"="+value)
	}
	return values
}

type ResultSink interface {
	SaveExecution(context.Context, types.ExecutionResult) error
}
type Tracer interface {
	Register(context.Context, string, string, int) (func(context.Context) error, error)
}

type Executor struct {
	Adapter *Pytest
	Sink    ResultSink
	Tracer  Tracer
	active  atomic.Int64
}

func (e *Executor) Execute(ctx context.Context, schedule types.Schedule, failFast bool) ([]types.ExecutionResult, error) {
	if err := schedule.Validate(); err != nil {
		return nil, err
	}
	groups := schedule.ByWorker()
	scheduleStarted := time.Now()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	results := make(chan types.ExecutionResult, len(schedule.Tests))
	errorsChannel := make(chan error, len(schedule.Tests)*2)
	var wait sync.WaitGroup
	for worker := 0; worker < schedule.Workers; worker++ {
		wait.Add(1)
		go func(worker int) {
			defer wait.Done()
			for _, test := range groups[worker] {
				if ctx.Err() != nil {
					return
				}
				if err := waitForPlannedStart(ctx, scheduleStarted, test.EstimatedStart); err != nil {
					return
				}
				result, err := e.executeOne(ctx, schedule.RunID, test)
				if result.ExecutionID != "" {
					results <- result
				}
				if result.ExecutionID != "" && e.Sink != nil {
					if err := e.Sink.SaveExecution(ctx, result); err != nil {
						errorsChannel <- fmt.Errorf("persist execution %s: %w", result.ExecutionID, err)
					}
				}
				if err != nil {
					errorsChannel <- err
					if failFast {
						cancel()
					}
					continue
				}
				if failFast && result.Status != types.Passed {
					cancel()
					return
				}
			}
		}(worker)
	}
	done := make(chan struct{})
	go func() { wait.Wait(); close(done) }()
	select {
	case <-done:
	case <-ctx.Done():
		<-done
	}
	close(results)
	close(errorsChannel)
	output := make([]types.ExecutionResult, 0, len(schedule.Tests))
	for result := range results {
		output = append(output, result)
	}
	sort.Slice(output, func(i, j int) bool { return output[i].StartedAt.Before(output[j].StartedAt) })
	var gathered []error
	for err := range errorsChannel {
		gathered = append(gathered, err)
	}
	return output, errors.Join(gathered...)
}

func waitForPlannedStart(ctx context.Context, scheduleStarted time.Time, offsetSeconds float64) error {
	delay := time.Until(scheduleStarted.Add(time.Duration(offsetSeconds * float64(time.Second))))
	if delay <= 0 {
		return nil
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (e *Executor) executeOne(parent context.Context, runID string, test types.ScheduledTest) (types.ExecutionResult, error) {
	executionID := uuid.NewString()
	timeout := e.Adapter.Timeout
	if timeout <= 0 {
		timeout = 5 * time.Minute
	}
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	arguments := append([]string{"-m", "pytest", "-q", "--tb=short", test.NodeID}, e.Adapter.ExtraArgs...)
	command := exec.CommandContext(ctx, e.Adapter.Python, arguments...)
	command.Dir = e.Adapter.Directory
	command.Env = e.Adapter.environment(map[string]string{"CONFLICTGRAPH_RUN_ID": runID, "CONFLICTGRAPH_EXECUTION_ID": executionID, "CONFLICTGRAPH_TEST_ID": test.TestID, "CONFLICTGRAPH_WORKER_ID": fmt.Sprint(test.Worker)})
	if runtime.GOOS != "windows" {
		command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
		command.Cancel = func() error {
			if command.Process == nil {
				return os.ErrProcessDone
			}
			if err := syscall.Kill(-command.Process.Pid, syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
				return err
			}
			return nil
		}
	}
	stdout := newLimitedBuffer(e.Adapter.OutputLimit)
	stderr := newLimitedBuffer(e.Adapter.OutputLimit)
	command.Stdout = stdout
	command.Stderr = stderr
	started := time.Now()
	e.active.Add(1)
	defer e.active.Add(-1)
	if err := command.Start(); err != nil {
		return types.ExecutionResult{}, fmt.Errorf("start %s: %w", test.NodeID, err)
	}
	cleanup := func(context.Context) error { return nil }
	if e.Tracer != nil {
		registered, err := e.Tracer.Register(ctx, executionID, test.TestID, command.Process.Pid)
		if err != nil {
			_ = command.Process.Kill()
			_ = command.Wait()
			return types.ExecutionResult{}, fmt.Errorf("register trace attribution: %w", err)
		}
		cleanup = registered
	}
	err := command.Wait()
	var descendantErr error
	if runtime.GOOS != "windows" {
		if killErr := syscall.Kill(-command.Process.Pid, syscall.SIGKILL); killErr != nil && !errors.Is(killErr, syscall.ESRCH) {
			descendantErr = fmt.Errorf("terminate descendants: %w", killErr)
		}
	}
	cleanupErr := cleanup(context.Background())
	ended := time.Now()
	status := types.Passed
	exitCode := 0
	timedOut := errors.Is(ctx.Err(), context.DeadlineExceeded)
	if timedOut {
		status = types.TimedOut
		exitCode = -1
	} else if errors.Is(ctx.Err(), context.Canceled) {
		status = types.Cancelled
		exitCode = -1
	} else if err != nil {
		status = types.Failed
		var exit *exec.ExitError
		if errors.As(err, &exit) {
			exitCode = exit.ExitCode()
		} else {
			status = types.InfraError
			exitCode = -1
		}
	}
	failure := ""
	if status != types.Passed {
		failure = failureSummary(stdout.String(), stderr.String())
	}
	result := types.ExecutionResult{
		ExecutionID: executionID, RunID: runID, TestID: test.TestID, NodeID: test.NodeID,
		Worker: test.Worker, Status: status, StartedAt: started, EndedAt: ended,
		Duration: ended.Sub(started).Seconds(), ExitCode: exitCode,
		Stdout: stdout.String(), Stderr: stderr.String(), FailureMessage: failure,
		TimedOut: timedOut,
	}
	if cleanupErr != nil || descendantErr != nil {
		var attributionErr error
		if cleanupErr != nil {
			attributionErr = fmt.Errorf("clean up trace attribution for %s: %w", test.NodeID, cleanupErr)
		}
		return result, errors.Join(descendantErr, attributionErr)
	}
	return result, nil
}

type limitedBuffer struct {
	buffer    bytes.Buffer
	remaining int64
	truncated bool
	lock      sync.Mutex
}

func newLimitedBuffer(limit int64) *limitedBuffer {
	if limit <= 0 {
		limit = 1_000_000
	}
	return &limitedBuffer{remaining: limit}
}
func (b *limitedBuffer) Write(value []byte) (int, error) {
	b.lock.Lock()
	defer b.lock.Unlock()
	original := len(value)
	if b.remaining <= 0 {
		b.truncated = true
		return original, nil
	}
	if int64(len(value)) > b.remaining {
		value = value[:b.remaining]
		b.truncated = true
	}
	_, err := b.buffer.Write(value)
	b.remaining -= int64(len(value))
	return original, err
}
func (b *limitedBuffer) String() string {
	b.lock.Lock()
	defer b.lock.Unlock()
	value := b.buffer.String()
	if b.truncated {
		value += "\n...[output truncated by ConflictGraph]"
	}
	return value
}
func failureSummary(stdout, stderr string) string {
	scanner := bufio.NewScanner(strings.NewReader(stdout + "\n" + stderr))
	last := "test process failed without output"
	important := ""
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line != "" {
			last = line
		}
		lower := strings.ToLower(line)
		if strings.Contains(lower, "failed") || strings.Contains(lower, "error") {
			important = line
		}
	}
	if important != "" {
		last = important
	}
	if len(last) > 1000 {
		return last[:1000]
	}
	return last
}
