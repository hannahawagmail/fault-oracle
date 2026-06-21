// SPDX-License-Identifier: Apache-2.0
// otel/tracing.go — OpenTelemetry trace setup and scrape-cycle instrumentation.
//
// Each /metrics scrape creates a root span "scrape_cycle" with child spans
// per collector. Traces are exported via OTLP/gRPC to an OTel Collector
// DaemonSet sidecar (default: localhost:4317).
//
// W3C traceparent header is injected into the /metrics HTTP response so
// callers (e.g. Prometheus itself) can propagate the trace context.
//
// Environment variables:
//   OTEL_EXPORTER_OTLP_ENDPOINT — gRPC endpoint (default: localhost:4317)
//   OTEL_SERVICE_NAME           — service name (default: hw-fault-exporter)
//   OTEL_TRACE_ENABLED          — set to "false" to disable tracing at runtime
package otel

import (
	"context"
	"fmt"
	"os"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.21.0"
	"go.opentelemetry.io/otel/trace"
)

const (
	defaultOTLPEndpoint = "localhost:4317"
	defaultServiceName  = "hw-fault-exporter"
	tracerName          = "hw-fault-resilience"
)

// Tracer is the package-level tracer; nil when tracing is disabled.
var Tracer trace.Tracer

// TracingEnabled returns true if tracing is active.
func TracingEnabled() bool {
	return os.Getenv("OTEL_TRACE_ENABLED") != "false" && Tracer != nil
}

// InitTracing sets up the OTLP gRPC exporter and installs the global
// TraceProvider. Call Shutdown(ctx) on the returned function to flush.
func InitTracing(ctx context.Context, version string) (shutdown func(context.Context) error, err error) {
	if os.Getenv("OTEL_TRACE_ENABLED") == "false" {
		return func(_ context.Context) error { return nil }, nil
	}

	endpoint := os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
	if endpoint == "" {
		endpoint = defaultOTLPEndpoint
	}
	svcName := os.Getenv("OTEL_SERVICE_NAME")
	if svcName == "" {
		svcName = defaultServiceName
	}

	exporter, err := otlptracegrpc.New(ctx,
		otlptracegrpc.WithEndpoint(endpoint),
		otlptracegrpc.WithInsecure(),
		otlptracegrpc.WithTimeout(5*time.Second),
	)
	if err != nil {
		return nil, fmt.Errorf("OTLP exporter: %w", err)
	}

	res, _ := resource.Merge(
		resource.Default(),
		resource.NewWithAttributes(
			semconv.SchemaURL,
			semconv.ServiceName(svcName),
			semconv.ServiceVersion(version),
		),
	)

	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exporter),
		sdktrace.WithResource(res),
		sdktrace.WithSampler(sdktrace.AlwaysSample()),
	)
	otel.SetTracerProvider(tp)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{},
		propagation.Baggage{},
	))

	Tracer = otel.Tracer(tracerName)
	return tp.Shutdown, nil
}

// StartScrapeSpan starts a root span for a scrape cycle.
// The returned context carries the span; call span.End() when done.
func StartScrapeSpan(ctx context.Context) (context.Context, trace.Span) {
	if !TracingEnabled() {
		return ctx, trace.SpanFromContext(ctx) // noop span
	}
	return Tracer.Start(ctx, "scrape_cycle",
		trace.WithSpanKind(trace.SpanKindServer),
	)
}

// StartCollectorSpan starts a child span for a single collector within
// an ongoing scrape cycle. Must be called with the context from StartScrapeSpan.
func StartCollectorSpan(ctx context.Context, collectorName string) (context.Context, trace.Span) {
	if !TracingEnabled() {
		return ctx, trace.SpanFromContext(ctx)
	}
	return Tracer.Start(ctx, "collect:"+collectorName,
		trace.WithSpanKind(trace.SpanKindInternal),
		trace.WithAttributes(
			attribute.String("collector.name", collectorName),
		),
	)
}

// RecordCollectorResult sets span attributes for a completed collector.
func RecordCollectorResult(span trace.Span, collectorName string, metricCount int, up bool, err error) {
	if !TracingEnabled() {
		return
	}
	span.SetAttributes(
		attribute.String("collector.name", collectorName),
		attribute.Int("collector.metric_count", metricCount),
		attribute.Bool("collector.up", up),
	)
	if err != nil {
		span.RecordError(err)
	}
}

// InjectW3CTraceparent writes the W3C traceparent header into an HTTP
// response header so Prometheus can propagate the trace context.
func InjectW3CTraceparent(ctx context.Context, headers map[string][]string) {
	carrier := propagation.MapCarrier(map[string]string{})
	otel.GetTextMapPropagator().Inject(ctx, carrier)
	if tp, ok := carrier["traceparent"]; ok {
		headers["Traceparent"] = []string{tp}
	}
}
