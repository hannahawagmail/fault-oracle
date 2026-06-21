// SPDX-License-Identifier: Apache-2.0
//
// operator/main.go — Self-healing operator for hw-fault-exporter.
//
// Polls Prometheus every 60 seconds for fault_resilience_collector_up == 0.
// When a collector is found down, deletes the DaemonSet pod on the affected
// node to trigger a restart.  Safety limits:
//   - Max 3 restarts per (node, collector) per hour.
//   - 30s cooldown between any pod deletion.
//
// Flags:
//   --prometheus-url   Prometheus endpoint (default: http://prometheus:9090)
//   --namespace        Namespace for the DaemonSet (default: monitoring)
//   --daemonset        DaemonSet name (default: hw-fault-exporter)
//   --poll-interval    How often to poll Prometheus (default: 60s)
//   --max-restarts     Max pod restarts per node/collector per hour (default: 3)
//   --dry-run          Log what would be done without deleting pods
//   --log-level        debug/info/warn/error (default: info)

package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

const (
	defaultPromURL      = "http://prometheus-operated:9090"
	defaultNamespace    = "monitoring"
	defaultDaemonSet    = "hw-fault-exporter"
	defaultPollInterval = 60 * time.Second
	defaultMaxRestarts  = 3
	cooldownDuration    = 30 * time.Second
)

// restartKey uniquely identifies a (node, collector) pair for rate-limiting.
type restartKey struct {
	node      string
	collector string
}

// restartRecord tracks restart events for rate-limiting.
type restartRecord struct {
	mu        sync.Mutex
	events    map[restartKey][]time.Time
	lastGlobal time.Time
}

func newRestartRecord() *restartRecord {
	return &restartRecord{events: make(map[restartKey][]time.Time)}
}

// allowed returns true if a restart is permitted and records the event.
func (r *restartRecord) allowed(key restartKey, maxPerHour int) bool {
	r.mu.Lock()
	defer r.mu.Unlock()

	// Global cooldown
	if time.Since(r.lastGlobal) < cooldownDuration {
		return false
	}

	// Per-key hourly limit
	cutoff := time.Now().Add(-time.Hour)
	var recent []time.Time
	for _, t := range r.events[key] {
		if t.After(cutoff) {
			recent = append(recent, t)
		}
	}
	if len(recent) >= maxPerHour {
		return false
	}

	recent = append(recent, time.Now())
	r.events[key] = recent
	r.lastGlobal = time.Now()
	return true
}

// prometheusResult represents one time-series from a Prometheus instant query.
type prometheusResult struct {
	Metric map[string]string `json:"metric"`
	Value  [2]interface{}    `json:"value"`
}

type prometheusResponse struct {
	Status string `json:"status"`
	Data   struct {
		ResultType string             `json:"resultType"`
		Result     []prometheusResult `json:"result"`
	} `json:"data"`
}

// queryPrometheus returns all time-series for the given PromQL expression.
func queryPrometheus(ctx context.Context, promURL, expr string) ([]prometheusResult, error) {
	u, err := url.Parse(promURL + "/api/v1/query")
	if err != nil {
		return nil, err
	}
	q := u.Query()
	q.Set("query", expr)
	u.RawQuery = q.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, err
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("prometheus query failed: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	var pr prometheusResponse
	if err := json.Unmarshal(body, &pr); err != nil {
		return nil, fmt.Errorf("json decode: %w", err)
	}
	if pr.Status != "success" {
		return nil, fmt.Errorf("prometheus returned status: %s", pr.Status)
	}
	return pr.Data.Result, nil
}

// findPodOnNode returns the name of the DaemonSet pod running on a given node.
func findPodOnNode(ctx context.Context, kube kubernetes.Interface,
	namespace, daemonSet, node string) (string, error) {

	pods, err := kube.CoreV1().Pods(namespace).List(ctx, metav1.ListOptions{
		LabelSelector: fmt.Sprintf("app.kubernetes.io/name=%s", daemonSet),
	})
	if err != nil {
		return "", err
	}
	for _, pod := range pods.Items {
		if pod.Spec.NodeName == node {
			return pod.Name, nil
		}
	}
	return "", fmt.Errorf("no pod for daemonset %s on node %s", daemonSet, node)
}

func buildLogger(level string) *zap.Logger {
	lvl := zapcore.InfoLevel
	switch level {
	case "debug":
		lvl = zapcore.DebugLevel
	case "warn":
		lvl = zapcore.WarnLevel
	case "error":
		lvl = zapcore.ErrorLevel
	}
	cfg := zap.NewProductionConfig()
	cfg.Level = zap.NewAtomicLevelAt(lvl)
	cfg.EncoderConfig.TimeKey = "ts"
	cfg.EncoderConfig.EncodeTime = zapcore.ISO8601TimeEncoder
	logger, _ := cfg.Build()
	return logger
}

func main() {
	promURL      := flag.String("prometheus-url", defaultPromURL, "Prometheus API URL")
	namespace    := flag.String("namespace", defaultNamespace, "Kubernetes namespace")
	daemonSet    := flag.String("daemonset", defaultDaemonSet, "DaemonSet name")
	pollInterval := flag.Duration("poll-interval", defaultPollInterval, "Prometheus poll interval")
	maxRestarts  := flag.Int("max-restarts", defaultMaxRestarts, "Max pod restarts per node/collector per hour")
	dryRun       := flag.Bool("dry-run", false, "Log actions without deleting pods")
	logLevel     := flag.String("log-level", "info", "Log level: debug/info/warn/error")
	kubeconfig   := flag.String("kubeconfig", "", "Path to kubeconfig (empty = in-cluster)")
	flag.Parse()

	logger := buildLogger(*logLevel)
	defer logger.Sync() //nolint:errcheck

	logger.Info("hw-fault-exporter self-healing operator starting",
		zap.String("prometheus", *promURL),
		zap.String("namespace", *namespace),
		zap.String("daemonset", *daemonSet),
		zap.Duration("poll_interval", *pollInterval),
		zap.Int("max_restarts_per_hour", *maxRestarts),
		zap.Bool("dry_run", *dryRun),
	)

	// Build Kubernetes client
	var cfg *rest.Config
	var err error
	if *kubeconfig != "" {
		cfg, err = clientcmd.BuildConfigFromFlags("", *kubeconfig)
	} else {
		cfg, err = rest.InClusterConfig()
	}
	if err != nil {
		logger.Fatal("Kubernetes config failed", zap.Error(err))
	}

	kube, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		logger.Fatal("Kubernetes client failed", zap.Error(err))
	}

	records := newRestartRecord()

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	ticker := time.NewTicker(*pollInterval)
	defer ticker.Stop()

	logger.Info("Operator running — polling for collector_up == 0")

	for {
		select {
		case <-ctx.Done():
			logger.Info("Operator shutting down")
			return
		case <-ticker.C:
			results, err := queryPrometheus(ctx,
				*promURL,
				`fault_resilience_collector_up == 0`,
			)
			if err != nil {
				logger.Warn("Prometheus query failed", zap.Error(err))
				continue
			}

			for _, r := range results {
				node := r.Metric["node"]
				collector := r.Metric["collector"]
				if node == "" || collector == "" {
					continue
				}

				key := restartKey{node: node, collector: collector}
				logger.Warn("Collector down",
					zap.String("node", node),
					zap.String("collector", collector),
				)

				if !records.allowed(key, *maxRestarts) {
					logger.Warn("Restart rate-limited — skipping",
						zap.String("node", node),
						zap.String("collector", collector),
					)
					continue
				}

				podName, err := findPodOnNode(ctx, kube, *namespace, *daemonSet, node)
				if err != nil {
					logger.Error("Could not find pod on node",
						zap.String("node", node), zap.Error(err))
					continue
				}

				if *dryRun {
					logger.Info("DRY-RUN: would delete pod",
						zap.String("pod", podName),
						zap.String("node", node),
						zap.String("collector", collector),
					)
					continue
				}

				err = kube.CoreV1().Pods(*namespace).Delete(ctx, podName, metav1.DeleteOptions{})
				if err != nil {
					logger.Error("Pod delete failed",
						zap.String("pod", podName), zap.Error(err))
				} else {
					logger.Info("Pod deleted for restart",
						zap.String("pod", podName),
						zap.String("node", node),
						zap.String("collector", collector),
					)
				}
			}
		}
	}
}
