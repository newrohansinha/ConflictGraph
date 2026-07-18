package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/newrohansinha/ML-CI-Test-Conflict-Detection-Scheduler/controlplane/internal/store"
)

func testServer(t *testing.T) *Server {
	t.Helper()
	server, err := New("127.0.0.1:0", &store.Postgres{}, nil, "test-version")
	if err != nil {
		t.Fatal(err)
	}
	return server
}

func request(t *testing.T, server *Server, method, target, origin string) *httptest.ResponseRecorder {
	t.Helper()
	recorder := httptest.NewRecorder()
	req := httptest.NewRequest(method, target, nil)
	if origin != "" {
		req.Header.Set("Origin", origin)
	}
	server.server.Handler.ServeHTTP(recorder, req)
	return recorder
}

func TestNewValidatesDependencies(t *testing.T) {
	if _, err := New("", &store.Postgres{}, nil, "v"); err == nil {
		t.Fatal("empty address was accepted")
	}
	if _, err := New("127.0.0.1:0", nil, nil, "v"); err == nil {
		t.Fatal("nil database was accepted")
	}
}

func TestServersOwnIndependentMetricRegistries(t *testing.T) {
	first := testServer(t)
	second := testServer(t)
	if first.requests == second.requests || first.duration == second.duration {
		t.Fatal("server factories unexpectedly share mutable metric collectors")
	}
}

func TestVersionEndpointAndSecurityHeaders(t *testing.T) {
	response := request(t, testServer(t), http.MethodGet, "/api/v1/version", "")
	if response.Code != http.StatusOK {
		t.Fatalf("status %d", response.Code)
	}
	var value map[string]string
	if err := json.Unmarshal(response.Body.Bytes(), &value); err != nil {
		t.Fatal(err)
	}
	if value["version"] != "test-version" {
		t.Fatalf("unexpected body: %#v", value)
	}
	if response.Header().Get("X-Content-Type-Options") != "nosniff" || response.Header().Get("X-Frame-Options") != "DENY" || response.Header().Get("Referrer-Policy") != "no-referrer" {
		t.Fatalf("security headers missing: %#v", response.Header())
	}
}

func TestUnknownRouteIs404(t *testing.T) {
	response := request(t, testServer(t), http.MethodGet, "/missing", "")
	if response.Code != http.StatusNotFound {
		t.Fatalf("status %d", response.Code)
	}
}

func TestWriteErrorUsesStableJSONContract(t *testing.T) {
	recorder := httptest.NewRecorder()
	writeError(recorder, http.StatusBadRequest, "invalid", "bad input")
	if recorder.Code != http.StatusBadRequest || recorder.Header().Get("Content-Type") != "application/json" {
		t.Fatalf("unexpected response: %#v", recorder.Result())
	}
	var value map[string]string
	if err := json.Unmarshal(recorder.Body.Bytes(), &value); err != nil {
		t.Fatal(err)
	}
	if value["error"] != "invalid" || value["detail"] != "bad input" {
		t.Fatalf("unexpected body: %#v", value)
	}
}

func TestStatusWriterRetainsWrittenStatus(t *testing.T) {
	recorder := httptest.NewRecorder()
	writer := &statusWriter{ResponseWriter: recorder, status: http.StatusOK}
	writer.WriteHeader(http.StatusTeapot)
	if writer.status != http.StatusTeapot || recorder.Code != http.StatusTeapot {
		t.Fatalf("status not retained: %d / %d", writer.status, recorder.Code)
	}
}
