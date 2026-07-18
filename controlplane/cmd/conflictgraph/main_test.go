package main

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/newrohansinha/ML-CI-Test-Conflict-Detection-Scheduler/controlplane/internal/config"
	"github.com/newrohansinha/ML-CI-Test-Conflict-Detection-Scheduler/controlplane/internal/types"
)

func TestPredictionsForTestsFiltersStalePairs(t *testing.T) {
	root := t.TempDir()
	graphDirectory := filepath.Join(root, "graphs")
	if err := os.Mkdir(graphDirectory, 0o755); err != nil {
		t.Fatal(err)
	}
	content := `{"predictions":[
		{"test_a":"a","test_b":"b","probability":0.9,"model_version":"v1"},
		{"test_a":"a","test_b":"stale","probability":1.0,"model_version":"old"}
	]}`
	if err := os.WriteFile(filepath.Join(graphDirectory, "latest.json"), []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	predictions, err := predictionsForTests(root, []types.Test{{ID: "a"}, {ID: "b"}})
	if err != nil {
		t.Fatal(err)
	}
	if len(predictions) != 1 || predictions[0].TestA != "a" || predictions[0].TestB != "b" {
		t.Fatalf("unexpected filtered predictions: %#v", predictions)
	}
}

func TestConfiguredSchedulerUsesRuntimeParameters(t *testing.T) {
	settings := config.Config{
		Workers: 6, RiskPolicy: types.Safe, Seed: 19,
		Scheduler: config.Scheduler{RiskWeight: 4.5, HardThreshold: .72, RefinementRounds: 7},
	}
	engine, err := configuredScheduler(settings)
	if err != nil {
		t.Fatal(err)
	}
	if engine.Workers != 6 || engine.Policy != types.Safe || engine.Seed != 19 {
		t.Fatalf("scheduler identity settings were lost: %#v", engine)
	}
	if engine.Parameters.RiskWeight != 4.5 || engine.Parameters.HardThreshold != .72 || engine.Parameters.RefinementRounds != 7 {
		t.Fatalf("scheduler runtime settings were lost: %#v", engine.Parameters)
	}
}

func TestPlanRejectsNegativeWorkerOverrideBeforeCollection(t *testing.T) {
	if err := plan([]string{"-workers", "-1"}); err == nil {
		t.Fatal("negative worker override was accepted")
	}
}
