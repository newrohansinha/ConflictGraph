import { Clock3, GitFork, ShieldAlert, TestTube2 } from "lucide-react";
import { Duration, Metric, Percent, StatusBadge } from "@/components/Metrics";
import { RunTimeline } from "@/components/RunTimeline";
import { ErrorState, PageHeader } from "@/components/Shell";
import { api } from "@/lib/api";

export default async function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const loaded = await api.run(id).then(
    (run) => ({ ok: true as const, run }),
    (error: unknown) => ({ ok: false as const, error }),
  );
  if (!loaded.ok) {
    return (
      <>
        <PageHeader
          eyebrow="Run detail"
          title="Run details"
          description="Run data could not be loaded."
        />
        <ErrorState error={loaded.error} />
      </>
    );
  }
  const run = loaded.run;
  const executions = run.executions ?? [];
  const failed = executions.filter((item) => item.status !== "PASSED").length;
  const start = executions.length
    ? Math.min(...executions.map((item) => new Date(item.started_at).getTime()))
    : 0;
  const end = executions.length
    ? Math.max(...executions.map((item) => new Date(item.ended_at).getTime()))
    : 0;
  const highRisk = (run.predictions ?? []).filter(
    (item) => item.probability >= 0.7,
  );
  return (
    <>
        <PageHeader
          eyebrow={`Run ${run.id.slice(0, 12)}`}
          title="Run details"
          description="Worker timing, first-attempt outcomes, and predicted conflicts for this run."
          actions={<StatusBadge value={run.status} />}
        />
        <section className="metricGrid">
          <Metric
            label="Tests"
            value={executions.length}
            foot={`${executions.length - failed} passed`}
            icon={<TestTube2 size={18} />}
          />
          <Metric
            label="Wall time"
            value={<Duration seconds={(end - start) / 1000} />}
            foot={`${run.worker_count} workers`}
            icon={<Clock3 size={18} />}
          />
          <Metric
            label="Failure rate"
            value={
              <Percent
                value={executions.length ? failed / executions.length : 0}
              />
            }
            foot={`${failed} first-attempt failures`}
            icon={<ShieldAlert size={18} />}
          />
          <Metric
            label="High-risk pairs"
            value={highRisk.length}
            foot={`Policy: ${run.scheduler_policy}`}
            icon={<GitFork size={18} />}
          />
        </section>
        <section className="panel">
          <div className="panelHeading">
            <div>
              <p className="eyebrow">Execution</p>
              <h2>Worker timeline</h2>
            </div>
            <span className="legend">
              <i className="passSwatch" />
              passed <i className="failSwatch" />
              failed <i className="riskSwatch" />
              high risk
            </span>
          </div>
          <RunTimeline
            executions={executions}
            predictions={run.predictions ?? []}
          />
        </section>
        {failed > 0 && (
          <section className="panel">
            <div className="panelHeading">
              <div>
                <p className="eyebrow">Failure detail</p>
                <h2>First-attempt failures</h2>
              </div>
            </div>
            <div className="failureList">
              {executions
                .filter((item) => item.status !== "PASSED")
                .map((item) => (
                  <article key={item.execution_id ?? item.id}>
                    <StatusBadge value={item.status} />
                    <strong>{item.node_id ?? item.test_id}</strong>
                    <pre>
                      {item.failure_message ||
                        "No failure message was captured."}
                    </pre>
                  </article>
                ))}
            </div>
          </section>
        )}
    </>
  );
}
