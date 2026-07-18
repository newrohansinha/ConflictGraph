package store

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/newrohansinha/ML-CI-Test-Conflict-Detection-Scheduler/controlplane/internal/types"
)

type Postgres struct{ pool *pgxpool.Pool }

func Open(ctx context.Context, url string) (*Postgres, error) {
	configuration, err := pgxpool.ParseConfig(url)
	if err != nil {
		return nil, fmt.Errorf("parse database URL: %w", err)
	}
	configuration.MaxConns = 16
	configuration.MinConns = 1
	pool, err := pgxpool.NewWithConfig(ctx, configuration)
	if err != nil {
		return nil, fmt.Errorf("open PostgreSQL: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping PostgreSQL: %w", err)
	}
	return &Postgres{pool}, nil
}
func (s *Postgres) Close() { s.pool.Close() }
func (s *Postgres) CreateRun(ctx context.Context, run types.Run) error {
	_, err := s.pool.Exec(ctx, `INSERT INTO runs(id,status,scheduler_policy,worker_count,seed,source_revision,trace_mode,model_version,metadata) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)`, run.ID, run.Status, run.Policy, run.WorkerCount, run.Seed, run.SourceRevision, run.TraceMode, run.ModelVersion, run.Metadata)
	return err
}
func (s *Postgres) SaveTests(ctx context.Context, tests []types.Test) error {
	batch := &pgxBatch{}
	for _, test := range tests {
		batch.queries = append(batch.queries, batchQuery{`INSERT INTO tests(id,node_id,repository,suite,framework,test_file,test_class,test_function,parameters,source_revision,duration_ema,duration_median,failure_rate,execution_count) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14) ON CONFLICT(id) DO UPDATE SET node_id=excluded.node_id,source_revision=excluded.source_revision,updated_at=now()`, []any{test.ID, test.NodeID, test.Repository, test.Suite, test.Framework, test.File, test.Class, test.Function, test.Parameters, test.SourceRevision, test.DurationEMA, test.DurationMedian, test.FailureRate, test.ExecutionCount}})
	}
	return batch.execute(ctx, s.pool)
}
func (s *Postgres) SavePredictions(ctx context.Context, runID string, predictions []types.Prediction) error {
	batch := &pgxBatch{}
	for _, prediction := range predictions {
		predictedAt := prediction.PredictedAt
		if predictedAt.IsZero() {
			predictedAt = time.Now()
		}
		batch.queries = append(batch.queries, batchQuery{`INSERT INTO predictions(run_id,test_a_id,test_b_id,probability,cause,model_version,shared_resources,explanation,predicted_at) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)`, []any{runID, prediction.TestA, prediction.TestB, prediction.Probability, prediction.Cause, prediction.ModelVersion, prediction.SharedResources, prediction.Explanation, predictedAt}})
	}
	return batch.execute(ctx, s.pool)
}

type batchQuery struct {
	sql  string
	args []any
}
type pgxBatch struct{ queries []batchQuery }

func (b *pgxBatch) execute(ctx context.Context, pool *pgxpool.Pool) error {
	transaction, err := pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer transaction.Rollback(ctx)
	for _, query := range b.queries {
		if _, err := transaction.Exec(ctx, query.sql, query.args...); err != nil {
			return err
		}
	}
	return transaction.Commit(ctx)
}
func (s *Postgres) SaveSchedule(ctx context.Context, schedule types.Schedule) error {
	plan, err := json.Marshal(schedule.Tests)
	if err != nil {
		return err
	}
	_, err = s.pool.Exec(ctx, `INSERT INTO schedules(id,run_id,policy,worker_count,expected_makespan,expected_risk,latency_ms,seed,plan) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)`, schedule.ID, schedule.RunID, schedule.Policy, schedule.Workers, schedule.ExpectedMakespan, schedule.ExpectedRisk, schedule.SchedulerLatencyMS, schedule.Seed, plan)
	return err
}
func (s *Postgres) SaveExecution(ctx context.Context, result types.ExecutionResult) error {
	_, err := s.pool.Exec(ctx, `INSERT INTO executions(id,run_id,test_id,worker_id,status,started_at,ended_at,duration_seconds,exit_code,stdout,stderr,failure_message,timed_out)VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)`, result.ExecutionID, result.RunID, result.TestID, result.Worker, result.Status, result.StartedAt, result.EndedAt, result.Duration, result.ExitCode, result.Stdout, result.Stderr, result.FailureMessage, result.TimedOut)
	return err
}
func (s *Postgres) FinishRun(ctx context.Context, runID, status, errorMessage string, quality *float64) error {
	_, err := s.pool.Exec(ctx, `UPDATE runs SET status=$2,error=$3,trace_quality=$4,ended_at=now() WHERE id=$1`, runID, status, errorMessage, quality)
	return err
}
func (s *Postgres) RecentRuns(ctx context.Context, limit int) ([]types.Run, error) {
	if limit < 1 {
		limit = 1
	}
	if limit > 500 {
		limit = 500
	}
	rows, err := s.pool.Query(ctx, `SELECT id,status,scheduler_policy,worker_count,seed,source_revision,trace_mode,trace_quality,coalesce(model_version,''),started_at,ended_at,error,metadata FROM runs ORDER BY started_at DESC LIMIT $1`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	output := []types.Run{}
	for rows.Next() {
		var run types.Run
		if err := rows.Scan(&run.ID, &run.Status, &run.Policy, &run.WorkerCount, &run.Seed, &run.SourceRevision, &run.TraceMode, &run.TraceQuality, &run.ModelVersion, &run.StartedAt, &run.EndedAt, &run.Error, &run.Metadata); err != nil {
			return nil, err
		}
		output = append(output, run)
	}
	return output, rows.Err()
}
func (s *Postgres) Health(ctx context.Context) error {
	healthCtx, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	return s.pool.Ping(healthCtx)
}
