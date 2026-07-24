export type RunStatus =
  "PENDING" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED";

export interface RunSummary {
  id: string;
  status: RunStatus;
  started_at: string;
  ended_at?: string;
  scheduler_policy: string;
  worker_count: number;
  trace_mode: string;
  trace_quality?: number;
  tests?: number;
  passed?: number;
  failed?: number;
  test_seconds?: number;
  model_version?: string;
}

export interface Execution {
  execution_id?: string;
  id?: string;
  test_id: string;
  node_id?: string;
  worker: number;
  worker_id?: number;
  status: string;
  started_at: string;
  ended_at: string;
  duration_seconds: number;
  failure_message?: string;
}

export interface Prediction {
  test_a: string;
  test_b: string;
  test_a_id?: string;
  test_b_id?: string;
  probability: number;
  cause: string;
  model_version: string;
  explanation: string;
  shared_resources: string[];
}

export interface RunDetail extends RunSummary {
  executions: Execution[];
  predictions: Prediction[];
}

export interface GraphTest {
  id: string;
  node_id: string;
  test_file?: string;
  test_function?: string;
}
export interface GraphResource {
  id: string;
  type: string;
  identifier: string;
}
export interface ResourceEdge {
  test_id: string;
  resource_id: string;
  counts: Record<string, number>;
  modes: string[];
  first_ns: number;
  last_ns: number;
}
export interface GraphData {
  tests: GraphTest[];
  resources: GraphResource[];
  edges: ResourceEdge[];
  predictions: Prediction[];
  empty?: boolean;
}

export interface ClassificationMetrics {
  pr_auc: number;
  roc_auc: number;
  precision: number;
  recall: number;
  f1: number;
  brier_score: number;
  expected_calibration_error: number;
  confusion_matrix: number[][];
}

export interface ModelArtifact {
  version: string;
  model_type: string;
  created_at: string;
  dataset_hash: string;
  validation_metrics: ClassificationMetrics;
  test_metrics: ClassificationMetrics;
  training_config: Record<string, unknown>;
  calibration: Record<string, unknown>;
}

export interface AggregateBenchmark {
  scheduler: string;
  trials: number;
  executions: number;
  failures: number;
  conflict_failures: number;
  flake_rate: number;
  flake_rate_ci95: [number, number];
  mean_makespan_seconds: number;
  worker_utilization: number;
  mean_scheduler_overhead_ms: number;
}

export interface BenchmarkReport {
  created_at: string;
  profile: string;
  workers: number;
  total_test_executions: number;
  impact_summary: string;
  results: AggregateBenchmark[];
  replayed: boolean;
}
