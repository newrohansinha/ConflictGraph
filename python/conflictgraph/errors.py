class ConflictGraphError(Exception):
    """Base exception for actionable user-facing failures."""


class ConfigurationError(ConflictGraphError):
    """Configuration is invalid or incomplete."""


class TraceQualityError(ConflictGraphError):
    """A trace cannot be used safely for model training."""


class ArtifactError(ConflictGraphError):
    """A model or dataset artifact is invalid or incompatible."""


class ExecutionError(ConflictGraphError):
    """Test collection or execution failed at the infrastructure layer."""
