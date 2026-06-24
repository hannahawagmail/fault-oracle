package collectors

import (
	"fmt"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
)

func TestAllowFirst1000(t *testing.T) {
	g := NewCardinalityGuard(1000)
	for i := 0; i < 1000; i++ {
		labels := prometheus.Labels{"id": fmt.Sprintf("%d", i)}
		if !g.AllowLabels("test_metric", labels) {
			t.Fatalf("combo %d should be allowed", i)
		}
	}
}

func TestReject1001(t *testing.T) {
	g := NewCardinalityGuard(1000)
	for i := 0; i < 1000; i++ {
		g.AllowLabels("test_metric", prometheus.Labels{"id": fmt.Sprintf("%d", i)})
	}
	if g.AllowLabels("test_metric", prometheus.Labels{"id": "new"}) {
		t.Fatal("combo 1001 should be rejected")
	}
}

func TestRepeatedCombosNoop(t *testing.T) {
	g := NewCardinalityGuard(1000)
	labels := prometheus.Labels{"id": "same"}
	g.AllowLabels("m", labels)
	g.AllowLabels("m", labels)
	g.AllowLabels("m", labels)
	g.mu.RLock()
	count := len(g.seen["m"])
	g.mu.RUnlock()
	if count != 1 {
		t.Fatalf("expected 1 unique combo, got %d", count)
	}
}

func TestDroppedCounterIncrements(t *testing.T) {
	g := NewCardinalityGuard(2)
	g.AllowLabels("m", prometheus.Labels{"id": "a"})
	g.AllowLabels("m", prometheus.Labels{"id": "b"})
	g.AllowLabels("m", prometheus.Labels{"id": "c"})
	g.AllowLabels("m", prometheus.Labels{"id": "d"})
	g.mu.RLock()
	d := g.dropped["m"]
	g.mu.RUnlock()
	if d != 2 {
		t.Fatalf("expected 2 drops, got %d", d)
	}
}
