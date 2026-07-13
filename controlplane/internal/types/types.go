package types

import (
	"fmt"
	"sort"
	"time"
)

type RiskPolicy string

const (
	Aggressive RiskPolicy = "aggressive"
	Balanced   RiskPolicy = "balanced"
	Safe       RiskPolicy = "safe"
)

func (p RiskPolicy) Valid() bool { return p == Aggressive || p == Balanced || p == Safe }

type ExecutionStatus string

const (
	Pending    ExecutionStatus = "PENDING"
	Running    ExecutionStatus = "RUNNING"
	Passed     ExecutionStatus = "PASSED"
	Failed     ExecutionStatus = "FAILED"
	TimedOut   ExecutionStatus = "TIMED_OUT"
	Cancelled  ExecutionStatus = "CANCELLED"
	InfraError ExecutionStatus = "INFRA_ERROR"
)

type Test struct {
	ID             string  `json:"id"`
	NodeID         string  `json:"node_id"`
	Repository     string  `json:"repository"`
	Suite          string  `json:"suite"`
	Framework      string  `json:"framework"`
	File           string  `json:"test_file"`
	Class          string  `json:"test_class"`
	Function       string  `json:"test_function"`
	Parameters     string  `json:"parameters"`
	SourceRevision string  `json:"source_revision"`
	DurationEMA    float64 `json:"duration_ema"`
	DurationMedian float64 `json:"duration_median"`
	FailureRate    float64 `json:"failure_rate"`
	ExecutionCount int64   `json:"execution_count"`
}

func (t Test) Validate() error {
	if t.ID == "" || t.NodeID == "" {
		return fmt.Errorf("test ID and node ID are required")
	}
	if t.DurationEMA < 0 || t.DurationMedian < 0 {
		return fmt.Errorf("test durations cannot be negative")
	}
	if t.FailureRate < 0 || t.FailureRate > 1 {
		return fmt.Errorf("test failure rate must be in [0,1]")
	}
	return nil
}

type Prediction struct {
	TestA           string    `json:"test_a"`
	TestB           string    `json:"test_b"`
	Probability     float64   `json:"probability"`
	Cause           string    `json:"cause"`
	ModelVersion    string    `json:"model_version"`
	SharedResources []string  `json:"shared_resources"`
	Explanation     string    `json:"explanation"`
	PredictedAt     time.Time `json:"predicted_at"`
}

func (p *Prediction) Normalize() error {
	if p.TestA == "" || p.TestB == "" || p.TestA == p.TestB {
		return fmt.Errorf("prediction requires two distinct test IDs")
	}
	if p.TestB < p.TestA {
		p.TestA, p.TestB = p.TestB, p.TestA
	}
	if p.Probability < 0 || p.Probability > 1 {
		return fmt.Errorf("prediction probability must be in [0,1]")
	}
	return nil
}

func PairKey(a, b string) string {
	if b < a {
		a, b = b, a
	}
	return a + "\x00" + b
}

type ScheduledTest struct {
	TestID            string   `json:"test_id"`
	NodeID            string   `json:"node_id"`
	Worker            int      `json:"worker"`
	EstimatedStart    float64  `json:"estimated_start"`
	EstimatedEnd      float64  `json:"estimated_end"`
	EstimatedDuration float64  `json:"estimated_duration"`
	RiskCost          float64  `json:"risk_cost"`
	Reasons           []string `json:"reasons"`
}

type Schedule struct {
	ID                 string          `json:"id"`
	RunID              string          `json:"run_id"`
	Workers            int             `json:"workers"`
	Policy             RiskPolicy      `json:"policy"`
	Tests              []ScheduledTest `json:"tests"`
	SchedulerLatencyMS float64         `json:"scheduler_latency_ms"`
	ExpectedMakespan   float64         `json:"expected_makespan"`
	ExpectedRisk       float64         `json:"expected_risk"`
	Seed               int64           `json:"seed"`
}

func (s Schedule) Validate() error {
	if s.ID == "" || s.RunID == "" {
		return fmt.Errorf("schedule and run IDs are required")
	}
	if s.Workers <= 0 {
		return fmt.Errorf("worker count must be positive")
	}
	if !s.Policy.Valid() {
		return fmt.Errorf("invalid risk policy %q", s.Policy)
	}
	seen := make(map[string]struct{}, len(s.Tests))
	for _, test := range s.Tests {
		if test.Worker < 0 || test.Worker >= s.Workers {
			return fmt.Errorf("worker %d out of range", test.Worker)
		}
		if test.EstimatedStart < 0 || test.EstimatedEnd < test.EstimatedStart {
			return fmt.Errorf("invalid timing for %s", test.TestID)
		}
		if _, exists := seen[test.TestID]; exists {
			return fmt.Errorf("test %s scheduled more than once", test.TestID)
		}
		seen[test.TestID] = struct{}{}
	}
	return nil
}

func (s Schedule) ByWorker() map[int][]ScheduledTest {
	output := make(map[int][]ScheduledTest, s.Workers)
	for _, test := range s.Tests {
		output[test.Worker] = append(output[test.Worker], test)
	}
	for worker := range output {
		sort.Slice(output[worker], func(i, j int) bool { return output[worker][i].EstimatedStart < output[worker][j].EstimatedStart })
	}
	return output
}

type ExecutionResult struct {
	ExecutionID    string          `json:"execution_id"`
	RunID          string          `json:"run_id"`
	TestID         string          `json:"test_id"`
	NodeID         string          `json:"node_id"`
	Worker         int             `json:"worker"`
	Status         ExecutionStatus `json:"status"`
	StartedAt      time.Time       `json:"started_at"`
	EndedAt        time.Time       `json:"ended_at"`
	Duration       float64         `json:"duration_seconds"`
	ExitCode       int             `json:"exit_code"`
	Stdout         string          `json:"stdout"`
	Stderr         string          `json:"stderr"`
	FailureMessage string          `json:"failure_message"`
	TimedOut       bool            `json:"timed_out"`
}

type TraceQuality struct {
	Captured     uint64 `json:"captured"`
	Processed    uint64 `json:"processed"`
	Dropped      uint64 `json:"dropped"`
	Unattributed uint64 `json:"unattributed"`
	ParseErrors  uint64 `json:"parse_errors"`
}

func (q TraceQuality) Score() float64 {
	completeness := 1.0
	if q.Captured+q.Dropped > 0 {
		completeness = float64(q.Processed) / float64(q.Captured+q.Dropped)
	}
	attribution := 1.0
	if q.Processed > 0 {
		attribution = 1 - float64(q.Unattributed)/float64(q.Processed)
	}
	return completeness * attribution
}

type Run struct {
	ID             string         `json:"id"`
	Status         string         `json:"status"`
	Policy         RiskPolicy     `json:"scheduler_policy"`
	WorkerCount    int            `json:"worker_count"`
	Seed           int64          `json:"seed"`
	SourceRevision string         `json:"source_revision"`
	TraceMode      string         `json:"trace_mode"`
	TraceQuality   *float64       `json:"trace_quality,omitempty"`
	ModelVersion   string         `json:"model_version"`
	StartedAt      time.Time      `json:"started_at"`
	EndedAt        *time.Time     `json:"ended_at,omitempty"`
	Error          string         `json:"error"`
	Metadata       map[string]any `json:"metadata"`
}
