import Link from "next/link";
import { Activity, Clock3, GitFork, ShieldCheck } from "lucide-react";
import { Duration, Metric, Percent, StatusBadge } from "@/components/Metrics";
import { EmptyState, ErrorState, PageHeader } from "@/components/Shell";
import { api } from "@/lib/api";

export default async function Overview() {
  const loaded = await Promise.all([api.runs(30), api.health()]).then(
    ([runs, health]) => ({ ok: true as const, runs, health }),
    (error: unknown) => ({ ok: false as const, error }),
  );
  if (!loaded.ok) {
    return (
      <>
        <PageHeader
          eyebrow="Run monitoring"
          title="CI run overview"
          description="Recent executions and scheduler state."
        />
        <ErrorState error={loaded.error} />
      </>
    );
  }
  const { runs, health } = loaded;
  const latest = runs[0];
  const failures = runs.reduce((total, run) => total + (run.failed ?? 0), 0);
  const tests = runs.reduce((total, run) => total + (run.tests ?? 0), 0);
  const meanDuration = runs.length
    ? runs.reduce((total, run) => total + (run.test_seconds ?? 0), 0) /
      runs.length
    : undefined;
  return (
    <>
        <PageHeader
          eyebrow="Run monitoring"
          title="CI run overview"
          description="Inspect recent executions, failures, trace quality, and active scheduling policy."
          actions={
            <span className="health">
              <span className="statusDot" />
              {health.model ?? "unknown model"}
            </span>
          }
        />
        {!runs.length ? (
          <EmptyState
            title="No runs yet"
            detail="Execute `conflictgraph run --workers 4 tests` to populate the local artifact view, or use the Go runner for PostgreSQL-backed history."
          />
        ) : (
          <>
            <section className="metricGrid">
              <Metric
                label="Recent test executions"
                value={tests.toLocaleString()}
                foot={`${runs.length} recorded runs`}
                icon={<Activity size={18} />}
              />
              <Metric
                label="Observed flake rate"
                value={<Percent value={tests ? failures / tests : 0} />}
                foot={`${failures} failed executions`}
                icon={<ShieldCheck size={18} />}
              />
              <Metric
                label="Mean test work / run"
                value={<Duration seconds={meanDuration} />}
                foot="Aggregate process time"
                icon={<Clock3 size={18} />}
              />
              <Metric
                label="Active scheduler"
                value={latest?.scheduler_policy ?? "—"}
                foot={`${latest?.worker_count ?? 0} workers`}
                icon={<GitFork size={18} />}
              />
            </section>
            <section className="panel">
              <div className="panelHeading">
                <div>
                  <p className="eyebrow">Run history</p>
                  <h2>Recent CI activity</h2>
                </div>
                <span className="muted">
                  Trace mode: {latest?.trace_mode ?? "unknown"}
                </span>
              </div>
              <div className="tableWrap">
                <table>
                  <thead>
                    <tr>
                      <th>Run</th>
                      <th>Status</th>
                      <th>Policy</th>
                      <th>Tests</th>
                      <th>Failures</th>
                      <th>Trace quality</th>
                      <th>Started</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((run) => (
                      <tr key={run.id}>
                        <td>
                          <Link className="runLink" href={`/runs/${run.id}`}>
                            {run.id.slice(0, 8)}
                          </Link>
                        </td>
                        <td>
                          <StatusBadge value={run.status} />
                        </td>
                        <td>{run.scheduler_policy}</td>
                        <td>{run.tests ?? "—"}</td>
                        <td
                          className={(run.failed ?? 0) > 0 ? "dangerText" : ""}
                        >
                          {run.failed ?? "—"}
                        </td>
                        <td>
                          <Percent value={run.trace_quality} />
                        </td>
                        <td>{new Date(run.started_at).toLocaleString()}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          </>
        )}
    </>
  );
}
