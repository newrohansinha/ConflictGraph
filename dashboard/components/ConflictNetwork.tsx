"use client";

import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useMemo, useState } from "react";
import type { GraphData, Prediction } from "@/lib/types";

function TestNode({ data }: NodeProps<Node<{ label: string; risk: number }>>) {
  return (
    <div
      className="graphNode"
      data-risk={data.risk > 0.8 ? "high" : data.risk > 0.5 ? "medium" : "low"}
    >
      <Handle type="target" position={Position.Left} />
      <span>{data.label}</span>
      <small>{Math.round(data.risk * 100)}% peak risk</small>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes = { test: TestNode };

export function ConflictNetwork({ graph }: { graph: GraphData }) {
  const [threshold, setThreshold] = useState(0.4);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Prediction | null>(null);
  const filtered = graph.predictions
    .filter(
      (item) =>
        item.probability >= threshold &&
        (!query ||
          `${item.test_a} ${item.test_b}`
            .toLowerCase()
            .includes(query.toLowerCase())),
    )
    .slice(0, 1000);
  const elements = useMemo(() => {
    const ids = Array.from(
      new Set(
        filtered.flatMap((item) => [
          item.test_a ?? item.test_a_id!,
          item.test_b ?? item.test_b_id!,
        ]),
      ),
    );
    const columns = Math.max(1, Math.ceil(Math.sqrt(ids.length)));
    const peak = new Map(
      ids.map((id) => [
        id,
        Math.max(
          ...filtered
            .filter(
              (item) =>
                item.test_a === id ||
                item.test_b === id ||
                item.test_a_id === id ||
                item.test_b_id === id,
            )
            .map((item) => item.probability),
        ),
      ]),
    );
    const nodes: Node[] = ids.map((id, index) => ({
      id,
      type: "test",
      position: {
        x: (index % columns) * 245,
        y: Math.floor(index / columns) * 105,
      },
      data: {
        label:
          graph.tests
            .find((test) => test.id === id)
            ?.node_id?.split("::")
            .at(-1) ?? id,
        risk: peak.get(id) ?? 0,
      },
    }));
    const edges: Edge[] = filtered.map((item, index) => ({
      id: `p-${index}`,
      source: item.test_a ?? item.test_a_id!,
      target: item.test_b ?? item.test_b_id!,
      animated: item.probability > 0.85,
      style: {
        stroke:
          item.probability > 0.8
            ? "#f05a47"
            : item.probability > 0.55
              ? "#d99b32"
              : "#748599",
        strokeWidth: 1 + item.probability * 3,
      },
      data: { prediction: item },
    }));
    return { nodes, edges };
  }, [filtered, graph.tests]);
  return (
    <div className="networkPanel">
      <div className="networkTools">
        <label>
          Search
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="test name"
          />
        </label>
        <label>
          Minimum risk <strong>{Math.round(threshold * 100)}%</strong>
          <input
            type="range"
            min="0"
            max="1"
            step=".05"
            value={threshold}
            onChange={(event) => setThreshold(Number(event.target.value))}
          />
        </label>
        <span>{filtered.length} edges</span>
      </div>
      <div className="networkCanvas">
        <ReactFlow
          nodes={elements.nodes}
          edges={elements.edges}
          nodeTypes={nodeTypes}
          fitView
          minZoom={0.08}
          maxZoom={2}
          onEdgeClick={(_, edge) =>
            setSelected((edge.data as { prediction: Prediction }).prediction)
          }
        >
          <Background color="#233044" gap={22} />
          <Controls />
        </ReactFlow>
      </div>
      {selected && (
        <aside className="pairDrawer">
          <button onClick={() => setSelected(null)}>Close</button>
          <p className="eyebrow">Pair evidence</p>
          <h2>{Math.round(selected.probability * 100)}% predicted risk</h2>
          <span className="badge">{selected.cause}</span>
          <p>{selected.explanation}</p>
          <h3>Shared resources</h3>
          {selected.shared_resources?.length ? (
            <ul>
              {selected.shared_resources.map((resource) => (
                <li key={resource}>
                  <code>{resource}</code>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">No resource evidence recorded.</p>
          )}
          <small>Model {selected.model_version}</small>
        </aside>
      )}
    </div>
  );
}
