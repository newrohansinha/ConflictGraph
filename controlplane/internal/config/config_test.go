package config

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/newrohansinha/ML-CI-Test-Conflict-Detection-Scheduler/controlplane/internal/types"
)

const validConfig = `
version: 1
workers: 4
risk_policy: balanced
seed: 42
artifact_dir: artifacts
database_url: postgresql://localhost/conflictgraph
api_address: 0.0.0.0:8080
tracing:
  mode: disabled
  minimum_quality: 0.8
scheduler:
  risk_weight: 2
  hard_threshold: 0.9
  refinement_rounds: 4
model: {artifact: artifacts/model, default_no_edge_risk: 0.001}
`

func writeConfig(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "config.yaml")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestLoadAppliesDefaultsAndParsesValues(t *testing.T) {
	settings, err := Load(writeConfig(t, validConfig))
	if err != nil {
		t.Fatal(err)
	}
	if settings.Workers != 4 || settings.RiskPolicy != types.Balanced {
		t.Fatalf("unexpected settings: %#v", settings)
	}
	if settings.Tracing.CgroupRoot != "/sys/fs/cgroup/conflictgraph" || settings.Tracing.ControlSocket == "" {
		t.Fatalf("tracing defaults missing: %#v", settings.Tracing)
	}
}

func TestLoadRejectsUnknownFields(t *testing.T) {
	_, err := Load(writeConfig(t, validConfig+"unknown_field: true\n"))
	if err == nil || !strings.Contains(err.Error(), "field unknown_field") {
		t.Fatalf("expected strict YAML error, got %v", err)
	}
}

func TestEnvironmentOverrides(t *testing.T) {
	t.Setenv("CONFLICTGRAPH_DATABASE_URL", "postgresql://override/db")
	t.Setenv("CONFLICTGRAPH_API_ADDRESS", "127.0.0.1:9999")
	t.Setenv("CONFLICTGRAPH_WORKERS", "9")
	t.Setenv("CONFLICTGRAPH_HASH_SALT", "environment-salt")
	settings, err := Load(writeConfig(t, validConfig))
	if err != nil {
		t.Fatal(err)
	}
	if settings.DatabaseURL != "postgresql://override/db" || settings.APIAddress != "127.0.0.1:9999" || settings.Workers != 9 || settings.Tracing.HashSalt != "environment-salt" {
		t.Fatalf("environment was not applied: %#v", settings)
	}
}

func TestInvalidWorkerEnvironmentIsExplicit(t *testing.T) {
	t.Setenv("CONFLICTGRAPH_WORKERS", "many")
	_, err := Load(writeConfig(t, validConfig))
	if err == nil || !strings.Contains(err.Error(), "must be an integer") {
		t.Fatalf("expected invalid environment error, got %v", err)
	}
}

func TestValidationRejectsInvalidSettings(t *testing.T) {
	base := Config{
		Version: 1, Workers: 4, RiskPolicy: types.Balanced, ArtifactDir: "artifacts", DatabaseURL: "postgresql://db", APIAddress: "127.0.0.1:8080",
		Tracing:   Tracing{Mode: "disabled", MinimumQuality: .8},
		Scheduler: Scheduler{RiskWeight: 1, HardThreshold: .9, RefinementRounds: 4},
		Model:     Model{Artifact: "artifacts/model", DefaultNoEdgeRisk: .001},
	}
	cases := []struct {
		name string
		edit func(*Config)
	}{
		{"version", func(value *Config) { value.Version = 2 }},
		{"workers low", func(value *Config) { value.Workers = 0 }},
		{"workers high", func(value *Config) { value.Workers = 1025 }},
		{"policy", func(value *Config) { value.RiskPolicy = "unknown" }},
		{"trace mode", func(value *Config) { value.Tracing.Mode = "unknown" }},
		{"redaction salt", func(value *Config) { value.Tracing.RedactPaths = true }},
		{"quality low", func(value *Config) { value.Tracing.MinimumQuality = -.1 }},
		{"quality high", func(value *Config) { value.Tracing.MinimumQuality = 1.1 }},
		{"risk", func(value *Config) { value.Scheduler.RiskWeight = -1 }},
		{"threshold", func(value *Config) { value.Scheduler.HardThreshold = 2 }},
		{"refinement rounds", func(value *Config) { value.Scheduler.RefinementRounds = -1 }},
		{"artifact directory", func(value *Config) { value.ArtifactDir = "" }},
		{"model artifact", func(value *Config) { value.Model.Artifact = "" }},
		{"model floor", func(value *Config) { value.Model.DefaultNoEdgeRisk = 2 }},
		{"database", func(value *Config) { value.DatabaseURL = "" }},
		{"API address", func(value *Config) { value.APIAddress = "" }},
	}
	if runtime.GOOS != "linux" {
		cases = append(cases, struct {
			name string
			edit func(*Config)
		}{"ebpf platform", func(value *Config) { value.Tracing.Mode = "ebpf" }})
	}
	for _, item := range cases {
		t.Run(item.name, func(t *testing.T) {
			candidate := base
			item.edit(&candidate)
			if candidate.Validate() == nil {
				t.Fatal("invalid configuration was accepted")
			}
		})
	}
}
