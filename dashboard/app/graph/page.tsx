import { ConflictNetwork } from "@/components/ConflictNetwork";
import { EmptyState, ErrorState, PageHeader } from "@/components/Shell";
import { api } from "@/lib/api";

export default async function GraphPage() {
  const loaded = await api.graph(0).then(
    (graph) => ({ ok: true as const, graph }),
    (error: unknown) => ({ ok: false as const, error }),
  );
  if (!loaded.ok) {
    return (
      <>
        <PageHeader
          eyebrow="Predictions"
          title="Conflict graph"
          description="Weighted test-pair relationships."
        />
        <ErrorState error={loaded.error} />
      </>
    );
  }
  const graph = loaded.graph;
  return (
    <>
        <PageHeader
          eyebrow="Predictions"
          title="Conflict graph"
          description="Filter test-pair risk and inspect the shared-resource evidence behind each edge."
        />
        {graph.empty || !graph.predictions.length ? (
          <EmptyState
            title="No graph observations"
            detail="Replay a trace or complete a traced run to construct the test-resource graph."
          />
        ) : (
          <ConflictNetwork graph={graph} />
        )}
    </>
  );
}
