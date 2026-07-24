"use client";

import { useMemo, useState } from "react";
import type { Execution, Prediction } from "@/lib/types";

function timestamp(value: string): number {
  return new Date(value).getTime();
}

export function RunTimeline({
  executions,
  predictions,
}: {
  executions: Execution[];
  predictions: Prediction[];
}) {
  const [selected, setSelected] = useState<Execution | null>(null);
  const data = useMemo(() => {
    if (!executions.length) return null;
    const start = Math.min(
      ...executions.map((item) => timestamp(item.started_at)),
    );
    const end = Math.max(...executions.map((item) => timestamp(item.ended_at)));
    const span = Math.max(1, end - start);
    const workers = Array.from(
      new Set(executions.map((item) => item.worker ?? item.worker_id ?? 0)),
    ).sort((a, b) => a - b);
    const highRisk = new Set<string>();
    for (const prediction of predictions.filter(
      (item) => item.probability >= 0.7,
    )) {
      highRisk.add(prediction.test_a ?? prediction.test_a_id ?? "");
      highRisk.add(prediction.test_b ?? prediction.test_b_id ?? "");
    }
    return { start, end, span, workers, highRisk };
  }, [executions, predictions]);
  if (!data)
    return <p className="muted">No execution timeline is available.</p>;
  return (
    <div className="timelineWrap">
      <div className="timelineAxis">
        <span>0s</span>
        <span>{((data.end - data.start) / 2000).toFixed(1)}s</span>
        <span>{((data.end - data.start) / 1000).toFixed(1)}s</span>
      </div>
      {data.workers.map((worker) => (
        <div className="timelineLane" key={worker}>
          <span className="laneLabel">W{worker + 1}</span>
          <div className="laneTrack">
            {executions
              .filter((item) => (item.worker ?? item.worker_id ?? 0) === worker)
              .map((item) => {
                const left =
                  ((timestamp(item.started_at) - data.start) / data.span) * 100;
                const width = Math.max(
                  0.7,
                  ((timestamp(item.ended_at) - timestamp(item.started_at)) /
                    data.span) *
                    100,
                );
                const risky = data.highRisk.has(item.test_id);
                return (
                  <button
                    key={item.execution_id ?? item.id}
                    className={`timelineBar ${item.status.toLowerCase()} ${risky ? "risky" : ""}`}
                    style={{ left: `${left}%`, width: `${width}%` }}
                    title={`${item.node_id ?? item.test_id} · ${item.duration_seconds.toFixed(2)}s`}
                    onClick={() => setSelected(item)}
                  >
                    <span>
                      {item.node_id?.split("::").at(-1) ?? item.test_id}
                    </span>
                  </button>
                );
              })}
          </div>
        </div>
      ))}
      {selected && (
        <div className="selection">
          <button onClick={() => setSelected(null)} aria-label="Close detail">
            ×
          </button>
          <strong>{selected.node_id ?? selected.test_id}</strong>
          <span>
            {selected.status} · {selected.duration_seconds.toFixed(3)} seconds
          </span>
          {selected.failure_message && <pre>{selected.failure_message}</pre>}
        </div>
      )}
    </div>
  );
}
