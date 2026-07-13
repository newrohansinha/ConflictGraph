package scheduler

import (
	"fmt"
	"math"
	"math/rand"
	"sort"
	"time"

	"github.com/google/uuid"
	"github.com/newrohansinha/ML-CI-Test-Conflict-Detection-Scheduler/controlplane/internal/types"
)

type Parameters struct {
	RiskWeight       float64
	HardThreshold    float64
	UtilizationBias  float64
	Lookahead        int
	RefinementRounds int
}

func ParametersFor(policy types.RiskPolicy) Parameters {
	switch policy {
	case types.Aggressive:
		return Parameters{0.55, 0.985, 1.5, 8, 2}
	case types.Safe:
		return Parameters{7.5, 0.68, 0.5, 32, 6}
	default:
		return Parameters{2, 0.90, 1, 16, 4}
	}
}

type RiskIndex struct{ predictions map[string]types.Prediction }

func NewRiskIndex(predictions []types.Prediction) (*RiskIndex, error) {
	index := &RiskIndex{predictions: make(map[string]types.Prediction, len(predictions))}
	for _, prediction := range predictions {
		if err := prediction.Normalize(); err != nil {
			return nil, err
		}
		key := types.PairKey(prediction.TestA, prediction.TestB)
		if current, exists := index.predictions[key]; !exists || prediction.Probability > current.Probability {
			index.predictions[key] = prediction
		}
	}
	return index, nil
}

func (r *RiskIndex) Probability(a, b string) float64 {
	if a == b {
		return 1
	}
	return r.predictions[types.PairKey(a, b)].Probability
}

type Scheduler struct {
	Workers    int
	Policy     types.RiskPolicy
	Seed       int64
	Parameters Parameters
}

func New(workers int, policy types.RiskPolicy, seed int64) (*Scheduler, error) {
	if workers <= 0 {
		return nil, fmt.Errorf("worker count must be positive")
	}
	if !policy.Valid() {
		return nil, fmt.Errorf("invalid risk policy %q", policy)
	}
	return &Scheduler{workers, policy, seed, ParametersFor(policy)}, nil
}

type lane struct {
	tests []types.ScheduledTest
	end   float64
}

func (s *Scheduler) Build(runID string, tests []types.Test, predictions []types.Prediction) (types.Schedule, error) {
	started := time.Now()
	risk, err := NewRiskIndex(predictions)
	if err != nil {
		return types.Schedule{}, err
	}
	if runID == "" {
		runID = uuid.NewString()
	}
	remaining := make(map[string]types.Test, len(tests))
	tie := make(map[string]float64, len(tests))
	randomizer := rand.New(rand.NewSource(s.Seed))
	for _, test := range tests {
		if err := test.Validate(); err != nil {
			return types.Schedule{}, fmt.Errorf("invalid test %s: %w", test.ID, err)
		}
		if _, exists := remaining[test.ID]; exists {
			return types.Schedule{}, fmt.Errorf("duplicate test ID %s", test.ID)
		}
		if test.DurationEMA <= 0 {
			test.DurationEMA = 1
		}
		remaining[test.ID], tie[test.ID] = test, randomizer.Float64()
	}
	lanes := make([]lane, s.Workers)
	for len(remaining) > 0 {
		worker := earliestLane(lanes)
		start := lanes[worker].end
		candidates := make([]types.Test, 0, len(remaining))
		for _, test := range remaining {
			candidates = append(candidates, test)
		}
		sort.Slice(candidates, func(i, j int) bool {
			if candidates[i].DurationEMA == candidates[j].DurationEMA {
				return tie[candidates[i].ID] < tie[candidates[j].ID]
			}
			return candidates[i].DurationEMA > candidates[j].DurationEMA
		})
		if len(candidates) > s.Parameters.Lookahead {
			candidates = candidates[:s.Parameters.Lookahead]
		}
		best, bestCost := candidates[0], math.Inf(1)
		for _, candidate := range candidates {
			probability, _ := placementRisk(candidate.ID, start, start+candidate.DurationEMA, lanes, worker, risk)
			cost := start - s.Parameters.UtilizationBias*candidate.DurationEMA + s.Parameters.RiskWeight*probability*candidate.DurationEMA
			if probability >= s.Parameters.HardThreshold {
				cost += 1000
			}
			if cost < bestCost {
				best, bestCost = candidate, cost
			}
		}
		probability, reasons := placementRisk(best.ID, start, start+best.DurationEMA, lanes, worker, risk)
		if probability >= s.Parameters.HardThreshold {
			if next, ok := earliestConflictingEnd(best.ID, start, lanes, worker, risk, s.Parameters.HardThreshold); ok {
				start = next
				probability, reasons = placementRisk(best.ID, start, start+best.DurationEMA, lanes, worker, risk)
			}
		}
		scheduled := types.ScheduledTest{
			TestID: best.ID, NodeID: best.NodeID, Worker: worker,
			EstimatedStart: start, EstimatedEnd: start + best.DurationEMA,
			EstimatedDuration: best.DurationEMA, RiskCost: probability, Reasons: reasons,
		}
		lanes[worker].tests = append(lanes[worker].tests, scheduled)
		lanes[worker].end = scheduled.EstimatedEnd
		delete(remaining, best.ID)
	}
	if len(tests) <= 64 {
		for round := 0; round < s.Parameters.RefinementRounds; round++ {
			if !refine(lanes, risk, s.Parameters.RiskWeight) {
				break
			}
		}
	}
	flattened := make([]types.ScheduledTest, 0, len(tests))
	makespan := 0.0
	for _, lane := range lanes {
		flattened = append(flattened, lane.tests...)
		makespan = math.Max(makespan, lane.end)
	}
	sort.Slice(flattened, func(i, j int) bool {
		if flattened[i].EstimatedStart == flattened[j].EstimatedStart {
			return flattened[i].Worker < flattened[j].Worker
		}
		return flattened[i].EstimatedStart < flattened[j].EstimatedStart
	})
	result := types.Schedule{
		ID: uuid.NewString(), RunID: runID, Workers: s.Workers, Policy: s.Policy,
		Tests: flattened, SchedulerLatencyMS: float64(time.Since(started).Microseconds()) / 1000,
		ExpectedMakespan: makespan, ExpectedRisk: scheduleRisk(flattened, risk), Seed: s.Seed,
	}
	return result, result.Validate()
}

func earliestLane(lanes []lane) int {
	best := 0
	for i := 1; i < len(lanes); i++ {
		if lanes[i].end < lanes[best].end {
			best = i
		}
	}
	return best
}

func placementRisk(testID string, start, end float64, lanes []lane, worker int, risk *RiskIndex) (float64, []string) {
	safe := 1.0
	reasons := []string{}
	for index, current := range lanes {
		if index == worker {
			continue
		}
		for _, other := range current.tests {
			overlap := math.Max(0, math.Min(end, other.EstimatedEnd)-math.Max(start, other.EstimatedStart))
			if overlap == 0 {
				continue
			}
			probability := risk.Probability(testID, other.TestID)
			exposure := math.Min(1, overlap/math.Max(.001, math.Min(end-start, other.EstimatedDuration)))
			safe *= 1 - probability*exposure
			if probability >= .25 {
				reasons = append(reasons, fmt.Sprintf("%s: %.1f%% conflict risk", other.NodeID, probability*100))
			}
		}
	}
	return 1 - safe, reasons
}

func earliestConflictingEnd(testID string, now float64, lanes []lane, worker int, risk *RiskIndex, threshold float64) (float64, bool) {
	best := math.Inf(1)
	found := false
	for index, current := range lanes {
		if index == worker {
			continue
		}
		for _, other := range current.tests {
			if other.EstimatedStart <= now && now < other.EstimatedEnd && risk.Probability(testID, other.TestID) >= threshold {
				best = math.Min(best, other.EstimatedEnd)
				found = true
			}
		}
	}
	return best, found
}

func scheduleRisk(tests []types.ScheduledTest, risk *RiskIndex) float64 {
	total := 0.0
	for i, left := range tests {
		for _, right := range tests[i+1:] {
			if left.Worker == right.Worker {
				continue
			}
			overlap := math.Max(0, math.Min(left.EstimatedEnd, right.EstimatedEnd)-math.Max(left.EstimatedStart, right.EstimatedStart))
			total += overlap * risk.Probability(left.TestID, right.TestID)
		}
	}
	return total
}

func objective(lanes []lane, risk *RiskIndex, weight float64) float64 {
	all := []types.ScheduledTest{}
	makespan := 0.0
	for _, lane := range lanes {
		all = append(all, lane.tests...)
		makespan = math.Max(makespan, lane.end)
	}
	return makespan + weight*scheduleRisk(all, risk)
}
func reflow(target *lane, worker int) {
	cursor := 0.0
	for index := range target.tests {
		target.tests[index].Worker = worker
		target.tests[index].EstimatedStart = cursor
		cursor += target.tests[index].EstimatedDuration
		target.tests[index].EstimatedEnd = cursor
	}
	target.end = cursor
}
func refine(lanes []lane, risk *RiskIndex, weight float64) bool {
	changed := false
	baseline := objective(lanes, risk, weight)
	for worker := range lanes {
		for i := 0; i+1 < len(lanes[worker].tests); i++ {
			lanes[worker].tests[i], lanes[worker].tests[i+1] = lanes[worker].tests[i+1], lanes[worker].tests[i]
			reflow(&lanes[worker], worker)
			candidate := objective(lanes, risk, weight)
			if candidate+1e-9 < baseline {
				baseline = candidate
				changed = true
			} else {
				lanes[worker].tests[i], lanes[worker].tests[i+1] = lanes[worker].tests[i+1], lanes[worker].tests[i]
				reflow(&lanes[worker], worker)
			}
		}
	}
	return changed
}
