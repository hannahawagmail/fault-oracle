package collectors

import (
	"fmt"
	"sort"
	"strings"
	"sync"

	"github.com/prometheus/client_golang/prometheus"
)

var (
	droppedTotal = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "hw_fault_exporter_cardinality_dropped_total",
		Help: "Dropped series due to cardinality limit.",
	}, []string{"metric"})
	currentGauge = prometheus.NewGaugeVec(prometheus.GaugeOpts{
		Name: "hw_fault_exporter_cardinality_current",
		Help: "Current unique series count per metric.",
	}, []string{"metric"})
)

func init() { prometheus.MustRegister(droppedTotal, currentGauge) }

type CardinalityGuard struct {
	mu      sync.RWMutex
	max     int
	seen    map[string]map[string]struct{}
	dropped map[string]int
}

func NewCardinalityGuard(max int) *CardinalityGuard {
	if max <= 0 {
		max = 1000
	}
	return &CardinalityGuard{max: max, seen: make(map[string]map[string]struct{}), dropped: make(map[string]int)}
}

func (g *CardinalityGuard) AllowLabels(metricName string, labels prometheus.Labels) bool {
	fp := fingerprint(labels)
	g.mu.RLock()
	if s, ok := g.seen[metricName]; ok {
		if _, exists := s[fp]; exists {
			g.mu.RUnlock()
			return true
		}
	}
	g.mu.RUnlock()
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.seen[metricName] == nil {
		g.seen[metricName] = make(map[string]struct{})
	}
	if _, exists := g.seen[metricName][fp]; exists {
		return true
	}
	if len(g.seen[metricName]) >= g.max {
		g.dropped[metricName]++
		droppedTotal.WithLabelValues(metricName).Inc()
		return false
	}
	g.seen[metricName][fp] = struct{}{}
	currentGauge.WithLabelValues(metricName).Set(float64(len(g.seen[metricName])))
	return true
}

func fingerprint(labels prometheus.Labels) string {
	keys := make([]string, 0, len(labels))
	for k := range labels {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	for _, k := range keys {
		fmt.Fprintf(&b, "%s=%s,", k, labels[k])
	}
	return b.String()
}
