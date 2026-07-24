import { BrainCircuit, Crosshair, Gauge, Target } from "lucide-react";
import { Metric, Percent } from "@/components/Metrics";
import { EmptyState, ErrorState, PageHeader } from "@/components/Shell";
import { api } from "@/lib/api";

export default async function ModelsPage() {
  const loaded = await api.models().then(
    (models) => ({ ok: true as const, models }),
    (error: unknown) => ({ ok: false as const, error }),
  );
  if (!loaded.ok) {
    return (
      <>
        <PageHeader
          eyebrow="Model artifact"
          title="Prediction model"
          description="Model metrics and artifact state."
        />
        <ErrorState error={loaded.error} />
      </>
    );
  }
  const model = loaded.models[0];
  return (
    <>
        <PageHeader
          eyebrow="Model artifact"
          title="Prediction model"
          description="Review held-out metrics, calibration, and the model artifact available to trace replay."
        />
        {!model ? (
          <EmptyState
            title="Heuristic fallback is active"
            detail="No trained artifact is installed. Run `conflictgraph model train` to produce a calibrated graph model."
          />
        ) : (
          <>
            <section className="modelHero">
              <div>
                <span className="badge badge-passed">active</span>
                <p className="eyebrow">{model.model_type}</p>
                <h2>{model.version}</h2>
                <p>
                  Trained {new Date(model.created_at).toLocaleString()} ·
                  dataset <code>{model.dataset_hash.slice(0, 12)}</code>
                </p>
              </div>
              <BrainCircuit size={58} strokeWidth={1.1} />
            </section>
            <section className="metricGrid">
              <Metric
                label="Test PR-AUC"
                value={<Percent value={model.test_metrics.pr_auc} />}
                foot="Primary rare-positive metric"
                icon={<Target size={18} />}
              />
              <Metric
                label="ROC-AUC"
                value={<Percent value={model.test_metrics.roc_auc} />}
                foot="Pair-disjoint holdout"
                icon={<Crosshair size={18} />}
              />
              <Metric
                label="F1"
                value={<Percent value={model.test_metrics.f1} />}
                foot={`Precision ${(model.test_metrics.precision * 100).toFixed(1)}%`}
                icon={<Gauge size={18} />}
              />
              <Metric
                label="Brier score"
                value={model.test_metrics.brier_score.toFixed(3)}
                foot={`ECE ${model.test_metrics.expected_calibration_error.toFixed(3)}`}
                icon={<BrainCircuit size={18} />}
              />
            </section>
            <section className="splitPanel">
              <article className="panel">
                <p className="eyebrow">Confusion matrix</p>
                <h2>Pair-disjoint test split</h2>
                <div className="matrix">
                  <span>
                    TN
                    <br />
                    <strong>{model.test_metrics.confusion_matrix[0][0]}</strong>
                  </span>
                  <span>
                    FP
                    <br />
                    <strong>{model.test_metrics.confusion_matrix[0][1]}</strong>
                  </span>
                  <span>
                    FN
                    <br />
                    <strong>{model.test_metrics.confusion_matrix[1][0]}</strong>
                  </span>
                  <span>
                    TP
                    <br />
                    <strong>{model.test_metrics.confusion_matrix[1][1]}</strong>
                  </span>
                </div>
              </article>
              <article className="panel">
                <p className="eyebrow">Calibration</p>
                <h2>{String(model.calibration.method)}</h2>
                <dl className="definition">
                  <div>
                    <dt>Temperature</dt>
                    <dd>{Number(model.calibration.temperature).toFixed(3)}</dd>
                  </div>
                  <div>
                    <dt>Validation PR-AUC</dt>
                    <dd>
                      {(model.validation_metrics.pr_auc * 100).toFixed(1)}%
                    </dd>
                  </div>
                  <div>
                    <dt>Validation Brier</dt>
                    <dd>{model.validation_metrics.brier_score.toFixed(3)}</dd>
                  </div>
                </dl>
              </article>
            </section>
          </>
        )}
    </>
  );
}
