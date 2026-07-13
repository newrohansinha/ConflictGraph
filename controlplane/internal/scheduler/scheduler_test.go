package scheduler

import (
	"math/rand"
	"testing"

	"github.com/newrohansinha/ML-CI-Test-Conflict-Detection-Scheduler/controlplane/internal/types"
)

func testSuite(count int) ([]types.Test, map[string]types.Test) {
	tests := make([]types.Test, 0, count)
	index := make(map[string]types.Test, count)
	for number := 0; number < count; number++ {
		id := string(rune('a' + number))
		test := types.Test{
			ID: id, NodeID: "test.py::test_" + id, Repository: "test",
			Suite: "pytest", Framework: "pytest", File: "test.py",
			Function: "test_" + id, SourceRevision: "revision",
			DurationEMA: float64(number%3 + 1), DurationMedian: float64(number%3 + 1),
		}
		tests = append(tests, test)
		index[id] = test
	}
	return tests, index
}

func findScheduled(t *testing.T, schedule types.Schedule, id string) types.ScheduledTest {
	t.Helper()
	for _, test := range schedule.Tests {
		if test.TestID == id {
			return test
		}
	}
	t.Fatalf("test %s was not scheduled", id)
	return types.ScheduledTest{}
}

func overlap(left, right types.ScheduledTest) bool {
	if left.Worker == right.Worker {
		return false
	}
	return left.EstimatedStart < right.EstimatedEnd && right.EstimatedStart < left.EstimatedEnd
}

func TestRiskIndexNormalizesPairs(t *testing.T) {
	index, err := NewRiskIndex([]types.Prediction{{TestA: "z", TestB: "a", Probability: .75}})
	if err != nil {
		t.Fatal(err)
	}
	if value := index.Probability("a", "z"); value != .75 {
		t.Fatalf("expected .75, got %f", value)
	}
	if value := index.Probability("z", "a"); value != .75 {
		t.Fatalf("reverse lookup expected .75, got %f", value)
	}
}

func TestRiskIndexRejectsInvalidPrediction(t *testing.T) {
	_, err := NewRiskIndex([]types.Prediction{{TestA: "a", TestB: "a", Probability: .5}})
	if err == nil {
		t.Fatal("expected invalid self-pair to fail")
	}
	_, err = NewRiskIndex([]types.Prediction{{TestA: "a", TestB: "b", Probability: 2}})
	if err == nil {
		t.Fatal("expected out-of-range probability to fail")
	}
}

func TestSchedulerRejectsInvalidWorkerCount(t *testing.T) {
	if _, err := New(0, types.Balanced, 42); err == nil {
		t.Fatal("zero workers should fail")
	}
}

func TestSchedulerRejectsUnknownPolicy(t *testing.T) {
	if _, err := New(4, types.RiskPolicy("unknown"), 42); err == nil {
		t.Fatal("unknown policy should fail")
	}
}

func TestPoliciesHaveIncreasingRiskAversion(t *testing.T) {
	aggressive := ParametersFor(types.Aggressive)
	balanced := ParametersFor(types.Balanced)
	safe := ParametersFor(types.Safe)
	if !(aggressive.RiskWeight < balanced.RiskWeight && balanced.RiskWeight < safe.RiskWeight) {
		t.Fatalf("risk weights are not ordered: %#v %#v %#v", aggressive, balanced, safe)
	}
	if !(aggressive.HardThreshold > balanced.HardThreshold && balanced.HardThreshold > safe.HardThreshold) {
		t.Fatalf("hard thresholds are not ordered: %#v %#v %#v", aggressive, balanced, safe)
	}
}

func TestBuildSchedulesEveryTestExactlyOnce(t *testing.T) {
	tests, _ := testSuite(20)
	engine, err := New(4, types.Balanced, 42)
	if err != nil {
		t.Fatal(err)
	}
	schedule, err := engine.Build("run", tests, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(schedule.Tests) != len(tests) {
		t.Fatalf("scheduled %d of %d tests", len(schedule.Tests), len(tests))
	}
	seen := make(map[string]bool)
	for _, item := range schedule.Tests {
		if seen[item.TestID] {
			t.Fatalf("test %s scheduled twice", item.TestID)
		}
		seen[item.TestID] = true
		if item.Worker < 0 || item.Worker >= 4 {
			t.Fatalf("worker out of range: %d", item.Worker)
		}
	}
}

func TestBuildIsDeterministicForSameSeed(t *testing.T) {
	tests, _ := testSuite(20)
	leftEngine, _ := New(4, types.Balanced, 99)
	rightEngine, _ := New(4, types.Balanced, 99)
	left, err := leftEngine.Build("left", tests, nil)
	if err != nil {
		t.Fatal(err)
	}
	right, err := rightEngine.Build("right", tests, nil)
	if err != nil {
		t.Fatal(err)
	}
	for index := range left.Tests {
		if left.Tests[index].TestID != right.Tests[index].TestID ||
			left.Tests[index].Worker != right.Tests[index].Worker ||
			left.Tests[index].EstimatedStart != right.Tests[index].EstimatedStart {
			t.Fatalf("plans differ at index %d", index)
		}
	}
}

func TestHighRiskPairDoesNotOverlap(t *testing.T) {
	tests, _ := testSuite(4)
	engine, _ := New(2, types.Balanced, 3)
	schedule, err := engine.Build("run", tests, []types.Prediction{{
		TestA: "a", TestB: "b", Probability: .99, Cause: "PORT_COLLISION",
	}})
	if err != nil {
		t.Fatal(err)
	}
	left := findScheduled(t, schedule, "a")
	right := findScheduled(t, schedule, "b")
	if overlap(left, right) {
		t.Fatalf("high-risk pair overlaps: %#v %#v", left, right)
	}
}

func TestLowRiskPairRetainsParallelism(t *testing.T) {
	tests, _ := testSuite(2)
	tests[0].DurationEMA, tests[1].DurationEMA = 2, 2
	engine, _ := New(2, types.Balanced, 3)
	schedule, err := engine.Build("run", tests, []types.Prediction{{
		TestA: "a", TestB: "b", Probability: .01,
	}})
	if err != nil {
		t.Fatal(err)
	}
	if !overlap(findScheduled(t, schedule, "a"), findScheduled(t, schedule, "b")) {
		t.Fatal("low-risk pair was unnecessarily serialized")
	}
}

func TestLongestTestsStartAtTimeZero(t *testing.T) {
	tests, _ := testSuite(9)
	engine, _ := New(3, types.Balanced, 5)
	schedule, err := engine.Build("run", tests, nil)
	if err != nil {
		t.Fatal(err)
	}
	started := 0
	for _, item := range schedule.Tests {
		if item.EstimatedStart == 0 {
			started++
			if item.EstimatedDuration != 3 {
				t.Fatalf("short test started before long tail: %#v", item)
			}
		}
	}
	if started != 3 {
		t.Fatalf("expected three initial tests, got %d", started)
	}
}

func TestScheduleExpectedRiskTracksOverlap(t *testing.T) {
	tests, _ := testSuite(2)
	tests[0].DurationEMA, tests[1].DurationEMA = 1, 1
	engine, _ := New(2, types.Aggressive, 8)
	schedule, err := engine.Build("run", tests, []types.Prediction{{
		TestA: "a", TestB: "b", Probability: .2,
	}})
	if err != nil {
		t.Fatal(err)
	}
	if schedule.ExpectedRisk != .2 {
		t.Fatalf("expected duration-weighted risk .2, got %f", schedule.ExpectedRisk)
	}
}

func BenchmarkBuildThousandTests(b *testing.B) {
	tests := make([]types.Test, 1000)
	for index := range tests {
		tests[index] = types.Test{
			ID: string(rune(index + 1)), NodeID: "node", Repository: "repo",
			Suite: "pytest", Framework: "pytest", File: "test.py", Function: "test",
			SourceRevision: "revision", DurationEMA: float64(index%10+1) / 10,
			DurationMedian: float64(index%10+1) / 10,
		}
	}
	randomizer := rand.New(rand.NewSource(42))
	predictions := make([]types.Prediction, 3000)
	for index := range predictions {
		left := randomizer.Intn(len(tests))
		right := randomizer.Intn(len(tests))
		if left == right {
			right = (right + 1) % len(tests)
		}
		predictions[index] = types.Prediction{
			TestA: tests[left].ID, TestB: tests[right].ID,
			Probability: randomizer.Float64() * .5,
		}
	}
	engine, _ := New(16, types.Balanced, 42)
	b.ResetTimer()
	for iteration := 0; iteration < b.N; iteration++ {
		if _, err := engine.Build("benchmark", tests, predictions); err != nil {
			b.Fatal(err)
		}
	}
}
