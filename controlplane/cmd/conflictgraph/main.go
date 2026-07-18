package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/google/uuid"
	"github.com/newrohansinha/ML-CI-Test-Conflict-Detection-Scheduler/controlplane/internal/api"
	"github.com/newrohansinha/ML-CI-Test-Conflict-Detection-Scheduler/controlplane/internal/config"
	"github.com/newrohansinha/ML-CI-Test-Conflict-Detection-Scheduler/controlplane/internal/executor"
	"github.com/newrohansinha/ML-CI-Test-Conflict-Detection-Scheduler/controlplane/internal/scheduler"
	"github.com/newrohansinha/ML-CI-Test-Conflict-Detection-Scheduler/controlplane/internal/store"
	"github.com/newrohansinha/ML-CI-Test-Conflict-Detection-Scheduler/controlplane/internal/types"
)

var version = "development"

func main() {
	if err := run(os.Args[1:]); err != nil {
		slog.Error("ConflictGraph failed", "error", err)
		os.Exit(1)
	}
}
func run(arguments []string) error {
	if len(arguments) == 0 {
		return usage()
	}
	switch arguments[0] {
	case "serve":
		return serve(arguments[1:])
	case "collect":
		return collect(arguments[1:])
	case "plan":
		return plan(arguments[1:])
	case "run":
		return execute(arguments[1:])
	case "version":
		fmt.Println("ConflictGraph", version)
		return nil
	default:
		return usage()
	}
}
func usage() error {
	fmt.Fprintln(os.Stderr, "usage: conflictgraph <serve|collect|plan|run|version> [options]")
	return errors.New("a command is required")
}
func load(path string) (config.Config, error) { return config.Load(path) }
func serve(arguments []string) error {
	flags := flag.NewFlagSet("serve", flag.ContinueOnError)
	path := flags.String("config", "conflictgraph.yaml", "configuration path")
	if err := flags.Parse(arguments); err != nil {
		return err
	}
	settings, err := load(*path)
	if err != nil {
		return err
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	database, err := store.Open(ctx, settings.DatabaseURL)
	if err != nil {
		return err
	}
	defer database.Close()
	server, err := api.New(settings.APIAddress, database, slog.Default(), version)
	if err != nil {
		return err
	}
	failure := make(chan error, 1)
	go func() { failure <- server.ListenAndServe() }()
	select {
	case err := <-failure:
		return err
	case <-ctx.Done():
		shutdown, done := context.WithTimeout(context.Background(), 10*time.Second)
		defer done()
		return server.Shutdown(shutdown)
	}
}
func collect(arguments []string) error {
	flags := flag.NewFlagSet("collect", flag.ContinueOnError)
	directory := flags.String("directory", ".", "test repository")
	if err := flags.Parse(arguments); err != nil {
		return err
	}
	tests, err := executor.NewPytest(*directory).Collect(context.Background(), flags.Args())
	if err != nil {
		return err
	}
	for _, test := range tests {
		fmt.Printf("%s  %s\n", test.ID, test.NodeID)
	}
	fmt.Printf("Collected %d tests\n", len(tests))
	return nil
}
func plan(arguments []string) error {
	flags := flag.NewFlagSet("plan", flag.ContinueOnError)
	path := flags.String("config", "conflictgraph.yaml", "configuration path")
	directory := flags.String("directory", ".", "test repository")
	workers := flags.Int("workers", 0, "worker count (defaults to configuration)")
	policy := flags.String("policy", "", "risk policy (defaults to configuration)")
	if err := flags.Parse(arguments); err != nil {
		return err
	}
	if *workers < 0 {
		return errors.New("worker count cannot be negative")
	}
	settings, err := load(*path)
	if err != nil {
		return err
	}
	if *workers > 0 {
		settings.Workers = *workers
	}
	if *policy != "" {
		settings.RiskPolicy = types.RiskPolicy(*policy)
	}
	adapter := executor.NewPytest(*directory)
	tests, err := adapter.Collect(context.Background(), flags.Args())
	if err != nil {
		return err
	}
	predictions, err := predictionsForTests(settings.ArtifactDir, tests)
	if err != nil {
		return err
	}
	engine, err := configuredScheduler(settings)
	if err != nil {
		return err
	}
	schedule, err := engine.Build(uuid.NewString(), tests, predictions)
	if err != nil {
		return err
	}
	for worker, lane := range schedule.ByWorker() {
		fmt.Printf("Worker %d:\n", worker+1)
		for _, test := range lane {
			fmt.Printf("  %7.2fs  %s\n", test.EstimatedStart, test.NodeID)
		}
	}
	fmt.Printf("Expected makespan %.2fs; scheduling %.2fms\n", schedule.ExpectedMakespan, schedule.SchedulerLatencyMS)
	return nil
}
func execute(arguments []string) error {
	flags := flag.NewFlagSet("run", flag.ContinueOnError)
	path := flags.String("config", "conflictgraph.yaml", "configuration path")
	directory := flags.String("directory", ".", "test repository")
	if err := flags.Parse(arguments); err != nil {
		return err
	}
	settings, err := load(*path)
	if err != nil {
		return err
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	database, err := store.Open(ctx, settings.DatabaseURL)
	if err != nil {
		return err
	}
	defer database.Close()
	adapter := executor.NewPytest(*directory)
	tests, err := adapter.Collect(ctx, flags.Args())
	if err != nil {
		return err
	}
	predictions, err := predictionsForTests(settings.ArtifactDir, tests)
	if err != nil {
		return err
	}
	modelVersion := "duration-only"
	if len(predictions) > 0 {
		modelVersion = predictions[0].ModelVersion
		for _, prediction := range predictions[1:] {
			if prediction.ModelVersion != modelVersion {
				modelVersion = "mixed"
				break
			}
		}
	}
	runID := uuid.NewString()
	run := types.Run{ID: runID, Status: "RUNNING", Policy: settings.RiskPolicy, WorkerCount: settings.Workers, Seed: settings.Seed, SourceRevision: "unknown", TraceMode: settings.Tracing.Mode, ModelVersion: modelVersion, StartedAt: time.Now(), Metadata: map[string]any{"prediction_count": len(predictions)}}
	if err := database.SaveTests(ctx, tests); err != nil {
		return err
	}
	if err := database.CreateRun(ctx, run); err != nil {
		return err
	}
	engine, err := configuredScheduler(settings)
	if err != nil {
		return failRun(database, runID, err)
	}
	schedule, err := engine.Build(runID, tests, predictions)
	if err != nil {
		return failRun(database, runID, err)
	}
	if err := database.SavePredictions(ctx, runID, predictions); err != nil {
		return failRun(database, runID, fmt.Errorf("save predictions: %w", err))
	}
	if err := database.SaveSchedule(ctx, schedule); err != nil {
		return failRun(database, runID, fmt.Errorf("save schedule: %w", err))
	}
	runner := &executor.Executor{Adapter: adapter, Sink: database}
	if settings.Tracing.Mode == "ebpf" {
		tracer, err := executor.NewCgroupTracer(settings.Tracing.CgroupRoot, settings.Tracing.ControlSocket)
		if err != nil {
			return fmt.Errorf("initialize test attribution: %w", err)
		}
		runner.Tracer = tracer
	}
	results, runErr := runner.Execute(ctx, schedule, false)
	status := "COMPLETED"
	message := ""
	if runErr != nil {
		status = "FAILED"
		message = runErr.Error()
	}
	for _, result := range results {
		if result.Status != types.Passed {
			status = "FAILED"
			if message == "" {
				message = "one or more tests failed"
			}
		}
	}
	if err := database.FinishRun(context.Background(), runID, status, message, nil); err != nil {
		return errors.Join(runErr, err)
	}
	passed := 0
	for _, result := range results {
		if result.Status == types.Passed {
			passed++
		}
	}
	fmt.Printf("Run %s: %d/%d passed\n", runID, passed, len(results))
	if status == "FAILED" && runErr == nil {
		return errors.New(message)
	}
	return runErr
}

func configuredScheduler(settings config.Config) (*scheduler.Scheduler, error) {
	engine, err := scheduler.New(settings.Workers, settings.RiskPolicy, settings.Seed)
	if err != nil {
		return nil, err
	}
	engine.Parameters.RiskWeight = settings.Scheduler.RiskWeight
	engine.Parameters.HardThreshold = settings.Scheduler.HardThreshold
	engine.Parameters.RefinementRounds = settings.Scheduler.RefinementRounds
	return engine, nil
}

func predictionsForTests(artifactDir string, tests []types.Test) ([]types.Prediction, error) {
	predictions, err := store.LoadPredictions(filepath.Join(artifactDir, "graphs", "latest.json"))
	if err != nil {
		return nil, err
	}
	known := make(map[string]struct{}, len(tests))
	for _, test := range tests {
		known[test.ID] = struct{}{}
	}
	filtered := make([]types.Prediction, 0, len(predictions))
	for _, prediction := range predictions {
		_, left := known[prediction.TestA]
		_, right := known[prediction.TestB]
		if left && right {
			filtered = append(filtered, prediction)
		}
	}
	return filtered, nil
}

func failRun(database *store.Postgres, runID string, cause error) error {
	if err := database.FinishRun(context.Background(), runID, "FAILED", cause.Error(), nil); err != nil {
		return errors.Join(cause, err)
	}
	return cause
}
