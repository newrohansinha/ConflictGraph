import type { ReactNode } from "react";

export function Metric({
  label,
  value,
  foot,
  icon,
}: {
  label: string;
  value: ReactNode;
  foot?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <article className="metric">
      <div className="metricTop">
        <span>{label}</span>
        {icon}
      </div>
      <strong>{value}</strong>
      {foot && <small>{foot}</small>}
    </article>
  );
}

export function Percent({
  value,
  digits = 1,
}: {
  value?: number;
  digits?: number;
}) {
  return (
    <>
      {value === undefined || Number.isNaN(value)
        ? "—"
        : `${(value * 100).toFixed(digits)}%`}
    </>
  );
}

export function Duration({ seconds }: { seconds?: number }) {
  if (seconds === undefined) return <>—</>;
  if (seconds < 1) return <>{Math.round(seconds * 1000)}ms</>;
  if (seconds < 60) return <>{seconds.toFixed(1)}s</>;
  return (
    <>
      {Math.floor(seconds / 60)}m {Math.round(seconds % 60)}s
    </>
  );
}

export function StatusBadge({ value }: { value: string }) {
  const normalized = value.toLowerCase().replaceAll("_", "-");
  return (
    <span className={`badge badge-${normalized}`}>
      {value.replaceAll("_", " ")}
    </span>
  );
}
