// SPDX-License-Identifier: Apache-2.0
// collectors/shared.go — Descriptors shared across all collectors.
//
// Prometheus panics with "duplicate metrics collector registration attempted"
// when two distinct *prometheus.Desc objects carry the same fully-qualified
// metric name and the same label set. All collectors that emit
// fault_resilience_collector_up must therefore reference the *same* Desc
// pointer; this file provides that singleton.
package collectors

import "github.com/prometheus/client_golang/prometheus"

// collectorUpDesc is the single shared descriptor for the
// fault_resilience_collector_up gauge. All collectors must use this variable
// instead of calling prometheus.NewDesc with the same metric name, to avoid a
// duplicate-descriptor panic during MustRegister.
var collectorUpDesc = prometheus.NewDesc(
	"fault_resilience_collector_up",
	"1 if the collector is running and producing metrics, 0 if disabled or hardware absent",
	[]string{"collector"},
	nil,
)
