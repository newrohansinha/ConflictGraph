import { EmptyState, ErrorState, PageHeader } from "@/components/Shell";
import { Duration, Percent } from "@/components/Metrics";
import { api } from "@/lib/api";

export default async function BenchmarksPage() {
  const loaded = await api.benchmarks().then(
    (reports) => ({ ok: true as const, reports }),
    (error: unknown) => ({ ok: false as const, error }),
  );
  if (!loaded.ok) {
    return (
      <>
        <PageHeader
          eyebrow="Replay evaluation"
          title="Scheduler benchmark"
          description="Cross-policy comparison."
        />
        <ErrorState error={loaded.error} />
      </>
    );
  }
  const report = loaded.reports[0];
  return (
    <>
        <PageHeader
          eyebrow="Replay evaluation"
          title="Scheduler benchmark"
          description="Compare failure rate, makespan, and utilization under identical synthetic replay inputs."
        />
        {!report ? (
          <EmptyState
            title="No benchmark reports"
            detail="Run `conflictgraph benchmark --profile quick` to generate the first measured comparison."
          />
        ) : (
          <>
            <div className="callout">
              <span className="badge">
                {report.replayed ? "offline replay" : "executed"}
              </span>
              <p>{report.impact_summary}</p>
              <small>
                {report.total_test_executions.toLocaleString()} executions ·{" "}
                {report.profile} · {report.workers} workers
              </small>
            </div>
            <section className="benchmarkGrid">
              {report.results.map((result) => (
                <article className="benchmarkCard" key={result.scheduler}>
                  <p className="eyebrow">{result.scheduler}</p>
                  <h2>
                    <Percent value={result.flake_rate} />
                  </h2>
                  <span>flake rate</span>
                  <div className="bar">
                    <i
                      style={{
                        width: `${Math.min(100, result.flake_rate * 1000)}%`,
                      }}
                    />
                  </div>
                  <dl className="definition">
                    <div>
                      <dt>Makespan</dt>
                      <dd>
                        <Duration seconds={result.mean_makespan_seconds} />
                      </dd>
                    </div>
                    <div>
                      <dt>Utilization</dt>
                      <dd>
                        <Percent value={result.worker_utilization} />
                      </dd>
                    </div>
                    <div>
                      <dt>Conflict failures</dt>
                      <dd>{result.conflict_failures}</dd>
                    </div>
                    <div>
                      <dt>Scheduler</dt>
                      <dd>{result.mean_scheduler_overhead_ms.toFixed(2)}ms</dd>
                    </div>
                  </dl>
                </article>
              ))}
            </section>
            <p className="provenance">
              Generated {new Date(report.created_at).toLocaleString()}. Replay
              data is labeled separately from executed kernel-traced
              measurements.
            </p>
          </>
        )}
    </>
  );
}
