package api

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/newrohansinha/ML-CI-Test-Conflict-Detection-Scheduler/controlplane/internal/store"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

type Server struct {
	database     *store.Postgres
	logger       *slog.Logger
	server       *http.Server
	version      string
	requests     *prometheus.CounterVec
	duration     *prometheus.HistogramVec
	mu           sync.RWMutex
	shuttingDown bool
}

func New(address string, database *store.Postgres, logger *slog.Logger, version string) (*Server, error) {
	if address == "" {
		return nil, errors.New("API address is required")
	}
	if database == nil {
		return nil, errors.New("database store is required")
	}
	if logger == nil {
		logger = slog.Default()
	}
	registry := prometheus.NewRegistry()
	requests := prometheus.NewCounterVec(prometheus.CounterOpts{Name: "conflictgraph_api_requests_total", Help: "HTTP API requests."}, []string{"method", "route", "status"})
	duration := prometheus.NewHistogramVec(prometheus.HistogramOpts{Name: "conflictgraph_api_request_duration_seconds", Help: "HTTP API request duration.", Buckets: prometheus.DefBuckets}, []string{"method", "route"})
	registry.MustRegister(requests, duration)
	result := &Server{database: database, logger: logger, version: version, requests: requests, duration: duration}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v1/health", result.health)
	mux.HandleFunc("GET /api/v1/runs", result.runs)
	mux.HandleFunc("GET /api/v1/version", result.buildVersion)
	mux.Handle("GET /metrics", promhttp.HandlerFor(registry, promhttp.HandlerOpts{}))
	handler := result.instrument(securityHeaders(mux))
	result.server = &http.Server{Addr: address, Handler: handler, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, WriteTimeout: 30 * time.Second, IdleTimeout: 90 * time.Second}
	return result, nil
}

func (s *Server) ListenAndServe() error {
	s.logger.Info("API listening", "address", s.server.Addr)
	err := s.server.ListenAndServe()
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}
func (s *Server) Shutdown(ctx context.Context) error {
	s.mu.Lock()
	s.shuttingDown = true
	s.mu.Unlock()
	return s.server.Shutdown(ctx)
}
func (s *Server) health(response http.ResponseWriter, request *http.Request) {
	s.mu.RLock()
	stopping := s.shuttingDown
	s.mu.RUnlock()
	status := "ok"
	code := http.StatusOK
	if stopping {
		status = "stopping"
		code = http.StatusServiceUnavailable
	}
	database := "connected"
	if err := s.database.Health(request.Context()); err != nil {
		database = "unavailable"
		status = "degraded"
		code = http.StatusServiceUnavailable
	}
	writeJSON(response, code, map[string]any{"status": status, "database": database, "version": s.version})
}
func (s *Server) buildVersion(response http.ResponseWriter, _ *http.Request) {
	writeJSON(response, http.StatusOK, map[string]string{"version": s.version})
}
func (s *Server) runs(response http.ResponseWriter, request *http.Request) {
	limit := 50
	if raw := request.URL.Query().Get("limit"); raw != "" {
		parsed, err := strconv.Atoi(raw)
		if err != nil || parsed < 1 || parsed > 500 {
			writeError(response, http.StatusBadRequest, "invalid_limit", "limit must be an integer between 1 and 500")
			return
		}
		limit = parsed
	}
	runs, err := s.database.RecentRuns(request.Context(), limit)
	if err != nil {
		s.logger.Error("list runs", "error", err)
		writeError(response, http.StatusServiceUnavailable, "storage_unavailable", "run history is temporarily unavailable")
		return
	}
	writeJSON(response, http.StatusOK, runs)
}
func (s *Server) instrument(next http.Handler) http.Handler {
	return http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		started := time.Now()
		wrapped := &statusWriter{ResponseWriter: response, status: 200}
		next.ServeHTTP(wrapped, request)
		route := request.URL.Path
		if strings.HasPrefix(route, "/api/v1/runs/") {
			route = "/api/v1/runs/{id}"
		}
		s.requests.WithLabelValues(request.Method, route, strconv.Itoa(wrapped.status)).Inc()
		s.duration.WithLabelValues(request.Method, route).Observe(time.Since(started).Seconds())
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(status int) {
	w.status = status
	w.ResponseWriter.WriteHeader(status)
}
func writeJSON(response http.ResponseWriter, status int, value any) {
	response.Header().Set("Content-Type", "application/json")
	response.WriteHeader(status)
	if err := json.NewEncoder(response).Encode(value); err != nil {
		slog.Error("encode API response", "error", err)
	}
}
func writeError(response http.ResponseWriter, status int, code, detail string) {
	writeJSON(response, status, map[string]string{"error": code, "detail": detail})
}
func securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		response.Header().Set("X-Content-Type-Options", "nosniff")
		response.Header().Set("X-Frame-Options", "DENY")
		response.Header().Set("Referrer-Policy", "no-referrer")
		next.ServeHTTP(response, request)
	})
}
