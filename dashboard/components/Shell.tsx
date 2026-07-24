import Link from "next/link";
import {
  Activity,
  Beaker,
  Boxes,
  BrainCircuit,
  Flame,
  GitFork,
} from "lucide-react";
import type { ReactNode } from "react";

const navigation = [
  ["/", "Overview", Activity],
  ["/graph", "Conflict graph", GitFork],
  ["/hotspots", "Resource hotspots", Flame],
  ["/models", "Models", BrainCircuit],
  ["/benchmarks", "Benchmarks", Beaker],
] as const;

export function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="shell">
      <aside className="sidebar">
        <Link href="/" className="brand" aria-label="ConflictGraph home">
          <span className="brandMark">
            <Boxes size={19} />
          </span>
          <span>ConflictGraph</span>
        </Link>
        <nav aria-label="Primary navigation">
          {navigation.map(([href, label, Icon]) => (
            <Link href={href} key={href} className="navLink">
              <Icon size={17} />
              <span>{label}</span>
            </Link>
          ))}
        </nav>
      </aside>
      <main className="content">{children}</main>
    </div>
  );
}

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow: string;
  title: string;
  description: string;
  actions?: ReactNode;
}) {
  return (
    <header className="pageHeader">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p className="lede">{description}</p>
      </div>
      {actions && <div className="headerActions">{actions}</div>}
    </header>
  );
}

export function EmptyState({
  title,
  detail,
}: {
  title: string;
  detail: string;
}) {
  return (
    <div className="empty">
      <Boxes size={28} />
      <h2>{title}</h2>
      <p>{detail}</p>
    </div>
  );
}

export function ErrorState({ error }: { error: unknown }) {
  return (
    <div className="empty error">
      <h2>Data unavailable</h2>
      <p>
        {error instanceof Error
          ? error.message
          : "The analysis API could not be reached."}
      </p>
      <code>conflictgraph serve --port 8090</code>
    </div>
  );
}
