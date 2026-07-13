package config

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"runtime"
	"strconv"

	"github.com/newrohansinha/ML-CI-Test-Conflict-Detection-Scheduler/controlplane/internal/types"
	"gopkg.in/yaml.v3"
)

type Tracing struct {
	Mode            string   `yaml:"mode"`
	RedactPaths     bool     `yaml:"redact_paths"`
	HashSalt        string   `yaml:"hash_salt"`
	ExcludePrefixes []string `yaml:"exclude_prefixes"`
	MinimumQuality  float64  `yaml:"minimum_quality"`
	CgroupRoot      string   `yaml:"cgroup_root"`
	ControlSocket   string   `yaml:"control_socket"`
}

type Scheduler struct {
	RiskWeight       float64 `yaml:"risk_weight"`
	HardThreshold    float64 `yaml:"hard_threshold"`
	RefinementRounds int     `yaml:"refinement_rounds"`
}

type Model struct {
	Artifact          string  `yaml:"artifact"`
	DefaultNoEdgeRisk float64 `yaml:"default_no_edge_risk"`
}

type Config struct {
	Version     int              `yaml:"version"`
	Workers     int              `yaml:"workers"`
	RiskPolicy  types.RiskPolicy `yaml:"risk_policy"`
	Seed        int64            `yaml:"seed"`
	ArtifactDir string           `yaml:"artifact_dir"`
	DatabaseURL string           `yaml:"database_url"`
	APIAddress  string           `yaml:"api_address"`
	Tracing     Tracing          `yaml:"tracing"`
	Scheduler   Scheduler        `yaml:"scheduler"`
	Model       Model            `yaml:"model"`
}

func Load(path string) (Config, error) {
	content, err := os.ReadFile(path)
	if err != nil {
		return Config{}, fmt.Errorf("read configuration: %w", err)
	}
	var result Config
	decoder := yaml.NewDecoder(bytes.NewReader(content))
	decoder.KnownFields(true)
	if err := decoder.Decode(&result); err != nil {
		return Config{}, fmt.Errorf("parse configuration: %w", err)
	}
	result.defaults()
	if err := result.environment(); err != nil {
		return Config{}, err
	}
	if err := result.Validate(); err != nil {
		return Config{}, err
	}
	return result, nil
}

func (c *Config) defaults() {
	if c.Tracing.CgroupRoot == "" {
		c.Tracing.CgroupRoot = "/sys/fs/cgroup/conflictgraph"
	}
	if c.Tracing.ControlSocket == "" {
		c.Tracing.ControlSocket = "/run/conflictgraph/tracer.sock"
	}
}

func (c *Config) environment() error {
	if value := os.Getenv("CONFLICTGRAPH_DATABASE_URL"); value != "" {
		c.DatabaseURL = value
	}
	if value := os.Getenv("CONFLICTGRAPH_API_ADDRESS"); value != "" {
		c.APIAddress = value
	}
	if value := os.Getenv("CONFLICTGRAPH_WORKERS"); value != "" {
		parsed, err := strconv.Atoi(value)
		if err != nil {
			return fmt.Errorf("CONFLICTGRAPH_WORKERS must be an integer: %w", err)
		}
		c.Workers = parsed
	}
	if value := os.Getenv("CONFLICTGRAPH_HASH_SALT"); value != "" {
		c.Tracing.HashSalt = value
	}
	return nil
}

func (c Config) Validate() error {
	var problems []error
	if c.Version != 1 {
		problems = append(problems, fmt.Errorf("unsupported configuration version %d", c.Version))
	}
	if c.Workers <= 0 || c.Workers > 1024 {
		problems = append(problems, errors.New("workers must be between 1 and 1024"))
	}
	if !c.RiskPolicy.Valid() {
		problems = append(problems, fmt.Errorf("invalid risk policy %q", c.RiskPolicy))
	}
	if c.Tracing.Mode != "ebpf" && c.Tracing.Mode != "replay" && c.Tracing.Mode != "disabled" {
		problems = append(problems, errors.New("tracing mode must be ebpf, replay, or disabled"))
	}
	if c.Tracing.Mode == "ebpf" && runtime.GOOS != "linux" {
		problems = append(problems, errors.New("eBPF tracing requires Linux"))
	}
	if c.Tracing.RedactPaths && c.Tracing.HashSalt == "" {
		problems = append(problems, errors.New("path hashing requires a non-empty salt"))
	}
	if c.Tracing.MinimumQuality < 0 || c.Tracing.MinimumQuality > 1 {
		problems = append(problems, errors.New("minimum trace quality must be in [0,1]"))
	}
	if c.Scheduler.RiskWeight < 0 || c.Scheduler.HardThreshold < 0 || c.Scheduler.HardThreshold > 1 {
		problems = append(problems, errors.New("invalid scheduler risk parameters"))
	}
	if c.Scheduler.RefinementRounds < 0 {
		problems = append(problems, errors.New("scheduler refinement rounds cannot be negative"))
	}
	if c.ArtifactDir == "" {
		problems = append(problems, errors.New("artifact directory is required"))
	}
	if c.Model.Artifact == "" {
		problems = append(problems, errors.New("model artifact path is required"))
	}
	if c.Model.DefaultNoEdgeRisk < 0 || c.Model.DefaultNoEdgeRisk > 1 {
		problems = append(problems, errors.New("default no-edge risk must be in [0,1]"))
	}
	if c.DatabaseURL == "" {
		problems = append(problems, errors.New("database URL is required"))
	}
	if c.APIAddress == "" {
		problems = append(problems, errors.New("API address is required"))
	}
	return errors.Join(problems...)
}
