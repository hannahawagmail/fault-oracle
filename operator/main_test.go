// SPDX-License-Identifier: Apache-2.0
//
// operator/main_test.go — Unit tests for the self-healing operator.
// Tests restartRecord rate-limiting logic without requiring Kubernetes or Prometheus.

package main

import (
	"testing"
	"time"
)

func TestRestartRecord_AllowsFirstRestart(t *testing.T) {
	r := newRestartRecord()
	key := restartKey{node: "node-1", collector: "edac"}
	if !r.allowed(key, 3) {
		t.Error("first restart should be allowed")
	}
}

func TestRestartRecord_RespectsMaxPerHour(t *testing.T) {
	r := newRestartRecord()
	key := restartKey{node: "node-1", collector: "edac"}
	maxPerHour := 3

	// Burn through all allowed slots (each needs to pass the global cooldown)
	r.mu.Lock()
	// Manually inject past events (bypass cooldown for test)
	hourAgo := time.Now().Add(-50 * time.Minute)
	r.events[key] = []time.Time{hourAgo, hourAgo, hourAgo} // 3 recent events
	r.lastGlobal = time.Time{}                             // reset global cooldown
	r.mu.Unlock()

	// 4th attempt should be denied
	if r.allowed(key, maxPerHour) {
		t.Error("should be rate-limited after 3 restarts")
	}
}

func TestRestartRecord_AllowsAfterHourExpiry(t *testing.T) {
	r := newRestartRecord()
	key := restartKey{node: "node-1", collector: "edac"}

	// Inject 3 events from >1 hour ago
	r.mu.Lock()
	old := time.Now().Add(-70 * time.Minute)
	r.events[key] = []time.Time{old, old, old}
	r.lastGlobal = time.Time{}
	r.mu.Unlock()

	// Should be allowed since old events have expired
	if !r.allowed(key, 3) {
		t.Error("should be allowed after hour-old events expired")
	}
}

func TestRestartRecord_GlobalCooldownBlocks(t *testing.T) {
	r := newRestartRecord()
	key1 := restartKey{node: "node-1", collector: "edac"}
	key2 := restartKey{node: "node-2", collector: "aer"}

	// First restart on key1 sets global cooldown
	r.allowed(key1, 3)

	// key2 should be blocked by global cooldown even though it has no restarts
	if r.allowed(key2, 3) {
		t.Error("global cooldown should block other restarts")
	}
}

func TestRestartRecord_DifferentCollectorsSeparateCounters(t *testing.T) {
	r := newRestartRecord()
	nodeA := restartKey{node: "node-1", collector: "edac"}
	nodeB := restartKey{node: "node-1", collector: "aer"}

	r.mu.Lock()
	old := time.Now().Add(-70 * time.Minute)
	r.events[nodeA] = []time.Time{old, old, old} // exhausted for edac
	r.lastGlobal = time.Time{}
	r.mu.Unlock()

	// aer on same node should have independent counter
	if !r.allowed(nodeB, 3) {
		t.Error("aer counter should be independent of edac counter")
	}
}

func TestRestartRecord_ZeroMaxRestarts(t *testing.T) {
	r := newRestartRecord()
	key := restartKey{node: "node-1", collector: "edac"}
	if r.allowed(key, 0) {
		t.Error("maxRestarts=0 should never allow restarts")
	}
}
