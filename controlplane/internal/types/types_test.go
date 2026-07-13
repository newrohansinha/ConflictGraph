package types

import (
	"strings"
	"testing"
)

func TestRiskPolicyValidation(t *testing.T) {
	for _, policy := range []RiskPolicy{Aggressive, Balanced, Safe} {
		if !policy.Valid() {
			t.Fatalf("expected %q to be valid", policy)
		}
	}
	if RiskPolicy("unknown").Valid() {
		t.Fatal("unknown policy must not be valid")
	}
}

func TestTestValidation(t *testing.T) {
	valid := Test{ID: "id", NodeID: "test.py::test_a", DurationEMA: 1, DurationMedian: 1}
	if err := valid.Validate(); err != nil {
		t.Fatalf("valid test rejected: %v", err)
	}
	cases := []struct {
		name string
		edit func(*Test)
	}{
		{"missing ID", func(value *Test) { value.ID = "" }},
		{"missing node ID", func(value *Test) { value.NodeID = "" }},
		{"negative EMA", func(value *Test) { value.DurationEMA = -1 }},
		{"negative median", func(value *Test) { value.DurationMedian = -1 }},
		{"negative failure rate", func(value *Test) { value.FailureRate = -.1 }},
		{"large failure rate", func(value *Test) { value.FailureRate = 1.1 }},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			candidate := valid
			item.edit(&candidate)
			if candidate.Validate() == nil {
				t.Fatal("invalid test was accepted")
			}
		})
	}
}

func TestPredictionNormalize(t *testing.T) {
	prediction := Prediction{TestA: "z", TestB: "a", Probability: .7}
	if err := prediction.Normalize(); err != nil {
		t.Fatal(err)
	}
	if prediction.TestA != "a" || prediction.TestB != "z" {
		t.Fatalf("pair was not normalized: %#v", prediction)
	}
	for _, invalid := range []Prediction{
		{TestA: "", TestB: "b", Probability: .5},
		{TestA: "a", TestB: "a", Probability: .5},
		{TestA: "a", TestB: "b", Probability: -.1},
		{TestA: "a", TestB: "b", Probability: 1.1},
	} {
		if invalid.Normalize() == nil {
			t.Fatalf("invalid prediction accepted: %#v", invalid)
		}
	}
}

func TestPairKeyIsOrderIndependentAndUnambiguous(t *testing.T) {
	if PairKey("z", "a") != PairKey("a", "z") {
		t.Fatal("pair key depends on argument order")
	}
	if !strings.ContainsRune(PairKey("a", "b"), '\x00') {
		t.Fatal("pair key lacks an unambiguous separator")
	}
}

func validSchedule() Schedule {
	return Schedule{
		ID: "schedule", RunID: "run", Workers: 2, Policy: Balanced,
		Tests: []ScheduledTest{
			{TestID: "later", NodeID: "x::later", Worker: 0, EstimatedStart: 2, EstimatedEnd: 3},
			{TestID: "early", NodeID: "x::early", Worker: 0, EstimatedStart: 0, EstimatedEnd: 1},
			{TestID: "other", NodeID: "x::other", Worker: 1, EstimatedStart: 0, EstimatedEnd: 1},
		},
	}
}

func TestScheduleValidationAndWorkerOrdering(t *testing.T) {
	schedule := validSchedule()
	if err := schedule.Validate(); err != nil {
		t.Fatal(err)
	}
	lanes := schedule.ByWorker()
	if lanes[0][0].TestID != "early" || lanes[0][1].TestID != "later" {
		t.Fatalf("worker lane is not time ordered: %#v", lanes[0])
	}
}

func TestScheduleRejectsInvalidShapes(t *testing.T) {
	cases := []struct {
		name string
		edit func(*Schedule)
	}{
		{"missing schedule ID", func(value *Schedule) { value.ID = "" }},
		{"missing run ID", func(value *Schedule) { value.RunID = "" }},
		{"zero workers", func(value *Schedule) { value.Workers = 0 }},
		{"unknown policy", func(value *Schedule) { value.Policy = "unknown" }},
		{"negative worker", func(value *Schedule) { value.Tests[0].Worker = -1 }},
		{"large worker", func(value *Schedule) { value.Tests[0].Worker = 2 }},
		{"negative start", func(value *Schedule) { value.Tests[0].EstimatedStart = -1 }},
		{"end before start", func(value *Schedule) { value.Tests[0].EstimatedEnd = 1 }},
		{"duplicate", func(value *Schedule) { value.Tests[1].TestID = value.Tests[0].TestID }},
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			candidate := validSchedule()
			item.edit(&candidate)
			if candidate.Validate() == nil {
				t.Fatal("invalid schedule was accepted")
			}
		})
	}
}

func TestTraceQualityScore(t *testing.T) {
	cases := []struct {
		quality TraceQuality
		want    float64
	}{
		{TraceQuality{}, 1},
		{TraceQuality{Captured: 10, Processed: 10}, 1},
		{TraceQuality{Captured: 10, Processed: 8, Dropped: 2}, 8.0 / 12.0},
		{TraceQuality{Captured: 10, Processed: 10, Unattributed: 2}, .8},
	}
	for _, item := range cases {
		if got := item.quality.Score(); got != item.want {
			t.Fatalf("score %f, want %f", got, item.want)
		}
	}
}
