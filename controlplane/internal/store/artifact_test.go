package store

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoadPredictionsAllowsMissingArtifact(t *testing.T) {
	predictions, err := LoadPredictions(filepath.Join(t.TempDir(), "missing.json"))
	if err != nil || len(predictions) != 0 {
		t.Fatalf("expected duration-only fallback, got %#v, %v", predictions, err)
	}
}

func TestLoadPredictionsNormalizesAndDeduplicatesPairs(t *testing.T) {
	path := filepath.Join(t.TempDir(), "latest.json")
	content := `{"tests":[],"predictions":[
		{"test_a":"b","test_b":"a","probability":0.4},
		{"test_a":"a","test_b":"b","probability":0.8,"model_version":"v1"}
	]}`
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	predictions, err := LoadPredictions(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(predictions) != 1 || predictions[0].TestA != "a" || predictions[0].TestB != "b" || predictions[0].Probability != .8 {
		t.Fatalf("unexpected predictions: %#v", predictions)
	}
}

func TestLoadPredictionsRejectsMalformedContent(t *testing.T) {
	path := filepath.Join(t.TempDir(), "latest.json")
	for _, content := range []string{"{", `{"predictions":[{"test_a":"a","test_b":"a","probability":0.5}]}`, `{"predictions":[{"test_a":"a","test_b":"b","probability":2}]}`} {
		if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
		if _, err := LoadPredictions(path); err == nil {
			t.Fatalf("expected malformed artifact to fail: %s", content)
		}
	}
}
