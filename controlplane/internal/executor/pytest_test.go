package executor

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/newrohansinha/ML-CI-Test-Conflict-Detection-Scheduler/controlplane/internal/types"
)

type failingSink struct{}

func (failingSink) SaveExecution(context.Context, types.ExecutionResult) error {
	return errors.New("storage unavailable")
}

func TestStableIDIsDeterministicAndNodeSpecific(t *testing.T) {
	left := stableID("test.py::test_a")
	if left != stableID("test.py::test_a") {
		t.Fatal("stable ID changed for identical node ID")
	}
	if left == stableID("test.py::test_b") {
		t.Fatal("different node IDs received the same stable ID")
	}
	if !strings.HasPrefix(left, "test_") || len(left) != 29 {
		t.Fatalf("unexpected stable ID shape: %s", left)
	}
}

func TestWaitForPlannedStartHonorsOffsetAndCancellation(t *testing.T) {
	started := time.Now()
	if err := waitForPlannedStart(context.Background(), started, .03); err != nil {
		t.Fatal(err)
	}
	if time.Since(started) < 25*time.Millisecond {
		t.Fatal("planned idle time was skipped")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := waitForPlannedStart(ctx, time.Now(), 10); err == nil {
		t.Fatal("cancelled wait did not stop")
	}
}

func TestLimitedBufferPreservesWriterContractAndMarksTruncation(t *testing.T) {
	buffer := newLimitedBuffer(5)
	written, err := buffer.Write([]byte("abcdefgh"))
	if err != nil || written != 8 {
		t.Fatalf("Write returned (%d, %v)", written, err)
	}
	if value := buffer.String(); !strings.HasPrefix(value, "abcde") || !strings.Contains(value, "truncated") {
		t.Fatalf("unexpected buffer: %q", value)
	}
	if written, err = buffer.Write([]byte("more")); err != nil || written != 4 {
		t.Fatalf("discarded write returned (%d, %v)", written, err)
	}
}

func TestFailureSummaryChoosesUsefulAndBoundedLine(t *testing.T) {
	if got := failureSummary("hello\nFAILED assertion\nafter", ""); got != "FAILED assertion" {
		t.Fatalf("unexpected summary: %q", got)
	}
	if got := failureSummary("", ""); got != "test process failed without output" {
		t.Fatalf("unexpected empty summary: %q", got)
	}
	long := "ERROR " + strings.Repeat("x", 2000)
	if got := failureSummary(long, ""); len(got) != 1000 {
		t.Fatalf("summary length %d, want 1000", len(got))
	}
}

func TestExecuteDrainsMultipleErrorsPerWorker(t *testing.T) {
	directory := t.TempDir()
	runner := filepath.Join(directory, "failing-python")
	if err := os.WriteFile(runner, []byte("#!/bin/sh\nexit 1\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	tests := make([]types.ScheduledTest, 3)
	for index := range tests {
		tests[index] = types.ScheduledTest{
			TestID:            string(rune('a' + index)),
			NodeID:            "test_sample.py::test_failure",
			Worker:            0,
			EstimatedDuration: 0.01,
			EstimatedEnd:      0.01,
		}
	}
	schedule := types.Schedule{
		ID: "schedule", RunID: "run", Workers: 1, Policy: types.Balanced, Tests: tests,
	}
	executor := Executor{
		Adapter: &Pytest{Python: runner, Directory: directory, Timeout: time.Second},
		Sink:    failingSink{},
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		results, err := executor.Execute(context.Background(), schedule, false)
		if err == nil || len(results) != len(tests) {
			t.Errorf("Execute returned %d results and error %v", len(results), err)
		}
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("executor deadlocked while reporting repeated failures")
	}
}
