# Machine learning methodology

Dataset rows represent unordered stable test pairs. Features describe shared resource semantics, resource rarity, Jaccard overlap, access windows, duration and failure history, and prior concurrent outcomes. Synthetic labels carry explicit causes; inference datasets retain all generated candidates.

Pairs are assigned as indivisible groups during train, validation, and test splitting so repeated observations of one pair cannot cross partitions. Temperature calibration uses validation predictions only. Test metrics are reported after model selection.

Implemented predictors are:

1. A semantic shared-resource heuristic used for cold start and fallback.
2. Scaled logistic regression and histogram gradient boosting baselines.
3. A hybrid GraphSAGE model combining graph embeddings with explicit pair features.

Training uses weighted binary cross-entropy and retains the checkpoint with the best validation objective. Reports emphasize PR-AUC, precision, recall, F1, Brier score, expected calibration error, and confusion matrices because class imbalance makes accuracy uninformative.

Saved GNN artifacts include schema and feature versions, training configuration, dataset hash, seed, validation and test metrics, calibration parameters, normalization values, and a SHA-256 checksum of the weights. Inference verifies the schema, feature list, and checksum before loading the state dictionary.

Model metrics and scheduler outcomes are separate. A useful predictor still needs execution-level evaluation for failures, makespan, utilization, and unnecessary serialization.
