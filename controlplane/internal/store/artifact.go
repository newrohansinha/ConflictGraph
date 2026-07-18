package store

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"

	"github.com/newrohansinha/ML-CI-Test-Conflict-Detection-Scheduler/controlplane/internal/types"
)

type graphArtifact struct {
	Predictions []types.Prediction `json:"predictions"`
}

// LoadPredictions reads the graph artifact produced by `conflictgraph trace replay`.
// A missing artifact is a valid duration-only scheduling state; malformed content is not.
func LoadPredictions(path string) ([]types.Prediction, error) {
	content, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read prediction artifact: %w", err)
	}
	var artifact graphArtifact
	if err := json.Unmarshal(content, &artifact); err != nil {
		return nil, fmt.Errorf("parse prediction artifact: %w", err)
	}
	deduplicated := make(map[string]types.Prediction, len(artifact.Predictions))
	for index := range artifact.Predictions {
		prediction := artifact.Predictions[index]
		if err := prediction.Normalize(); err != nil {
			return nil, fmt.Errorf("invalid prediction %d: %w", index, err)
		}
		if prediction.Cause == "" {
			prediction.Cause = "UNKNOWN"
		}
		if prediction.ModelVersion == "" {
			prediction.ModelVersion = "unknown"
		}
		key := types.PairKey(prediction.TestA, prediction.TestB)
		if current, exists := deduplicated[key]; !exists || prediction.Probability > current.Probability {
			deduplicated[key] = prediction
		}
	}
	result := make([]types.Prediction, 0, len(deduplicated))
	for _, prediction := range deduplicated {
		result = append(result, prediction)
	}
	sort.Slice(result, func(i, j int) bool {
		if result[i].TestA == result[j].TestA {
			return result[i].TestB < result[j].TestB
		}
		return result[i].TestA < result[j].TestA
	})
	return result, nil
}
