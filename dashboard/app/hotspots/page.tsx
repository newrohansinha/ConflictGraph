import { EmptyState, ErrorState, PageHeader } from "@/components/Shell";
import { api } from "@/lib/api";

export default async function HotspotsPage() {
  const loaded = await api.graph(0).then(
    (graph) => ({ ok: true as const, graph }),
    (error: unknown) => ({ ok: false as const, error }),
  );
  if (!loaded.ok) {
    return (
      <>
        <PageHeader
          eyebrow="Shared resources"
          title="Resource hotspots"
          description="Shared-resource rankings."
        />
        <ErrorState error={loaded.error} />
      </>
    );
  }
  const byResource = loaded.graph.resources
      .map((resource) => {
        const edges = loaded.graph.edges.filter(
          (edge) => edge.resource_id === resource.id,
        );
        const reads = edges.reduce(
          (total, edge) => total + (edge.counts.READ ?? 0),
          0,
        );
        const writes = edges.reduce(
          (total, edge) =>
            total +
            Object.entries(edge.counts)
              .filter(([key]) => key !== "READ")
              .reduce((sum, [, value]) => sum + value, 0),
          0,
        );
        const tests = new Set(edges.map((edge) => edge.test_id));
        const pairRisk = loaded.graph.predictions.filter((item) =>
          item.shared_resources?.includes(resource.identifier),
        );
        return {
          ...resource,
          reads,
          writes,
          tests: tests.size,
          riskyPairs: pairRisk.length,
          maximumRisk: Math.max(0, ...pairRisk.map((item) => item.probability)),
        };
      })
      .sort((a, b) => b.maximumRisk - a.maximumRisk || b.writes - a.writes)
      .slice(0, 200);
  return (
    <>
        <PageHeader
          eyebrow="Shared resources"
          title="Resource hotspots"
          description="Rank files, ports, sockets, databases, and logical keys by observed mutation and pair risk."
        />
        {!byResource.length ? (
          <EmptyState
            title="No resource data"
            detail="Hotspots appear after normalized trace events are ingested."
          />
        ) : (
          <section className="panel">
            <div className="tableWrap">
              <table>
                <thead>
                  <tr>
                    <th>Resource</th>
                    <th>Type</th>
                    <th>Tests</th>
                    <th>Reads</th>
                    <th>Mutations</th>
                    <th>Risky pairs</th>
                    <th>Peak risk</th>
                  </tr>
                </thead>
                <tbody>
                  {byResource.map((resource) => (
                    <tr key={resource.id}>
                      <td>
                        <code>{resource.identifier}</code>
                      </td>
                      <td>
                        <span className="badge">{resource.type}</span>
                      </td>
                      <td>{resource.tests}</td>
                      <td>{resource.reads}</td>
                      <td>{resource.writes}</td>
                      <td>{resource.riskyPairs}</td>
                      <td>
                        <strong>
                          {Math.round(resource.maximumRisk * 100)}%
                        </strong>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}
    </>
  );
}
