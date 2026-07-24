import type {
  BenchmarkReport,
  GraphData,
  ModelArtifact,
  RunDetail,
  RunSummary,
} from "./types";

const base = process.env.CONFLICTGRAPH_API_URL ?? "http://localhost:8090";

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    ...options,
    headers: { Accept: "application/json", ...options.headers },
    next: { revalidate: 10 },
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(
      `ConflictGraph API ${response.status}: ${body.slice(0, 300)}`,
    );
  }
  return response.json() as Promise<T>;
}

export const api = {
  runs: (limit = 50) => request<RunSummary[]>(`/api/v1/runs?limit=${limit}`),
  run: (id: string) =>
    request<RunDetail>(`/api/v1/runs/${encodeURIComponent(id)}`),
  graph: (minimumRisk = 0.25) =>
    request<GraphData>(`/api/v1/graph?min_risk=${minimumRisk}`),
  models: () => request<ModelArtifact[]>("/api/v1/models"),
  benchmarks: () => request<BenchmarkReport[]>("/api/v1/benchmarks"),
  health: () => request<Record<string, string>>("/api/v1/health"),
};
