// SPDX-License-Identifier: Apache-2.0
//
// main.go — Hardware Fault Prometheus Exporter
//
// Scrapes kernel hardware error counters from sysfs and exposes them as
// Prometheus metrics. Three collectors cover the three main Linux hardware
// error reporting subsystems:
//
//   - EDAC (Error Detection And Correction): /sys/devices/system/edac/
//   - MCE (Machine Check Events): /sys/firmware/acpi/errors/ or /dev/mcelog
//   - PCIe AER: /sys/bus/pci/devices/*/aer_dev_*
//
// All metrics are Prometheus Counter type (cumulative, monotonically increasing),
// which is the correct type for hardware error event counts — the kernel never
// resets them without a reboot.
//
// Author: Hanna Hawa <hhhawa@gmail.com>

package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/hannahawagmail/fault-oracle/exporter/collectors"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/prometheus/common/expfmt"
	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
)

const (
	defaultListenAddr  = ":9101"
	defaultSysfsRoot   = "/sys"
	defaultMetricsPath = "/metrics"
	defaultScrapeTimeout = 10 * time.Second
	exporterVersion    = "1.0.0"
)

func main() {
	// ----- CLI flags -------------------------------------------------------

	listenAddr := flag.String("listen-addr", defaultListenAddr,
		"Address to listen on for Prometheus scrapes (e.g. :9101)")
	sysfsRoot := flag.String("sysfs-root", defaultSysfsRoot,
		"Root of the sysfs filesystem. Override for testing (e.g. /tmp/mock-sysfs)")
	metricsPath := flag.String("metrics-path", defaultMetricsPath,
		"HTTP path for Prometheus metrics endpoint")
	logLevel := flag.String("log-level", "info",
		"Log level: debug, info, warn, error")
	scrapeTimeout := flag.Duration("scrape-timeout", defaultScrapeTimeout,
		"Maximum time allowed for a single scrape cycle")
	disableEDAC := flag.Bool("no-edac", false, "Disable the EDAC collector")
	disableMCE  := flag.Bool("no-mce",  false, "Disable the MCE collector")
	disableAER  := flag.Bool("no-aer",  false, "Disable the PCIe AER collector")
	onceMode    := flag.Bool("once", false, "Single-shot: scrape once, print metrics to stdout, then exit")
	showVersion := flag.Bool("version", false, "Print version and exit")

	// mTLS flags — all three must be set together to enable mutual TLS.
	tlsCert := flag.String("tls-cert", "", "Path to server TLS certificate (PEM)")
	tlsKey  := flag.String("tls-key",  "", "Path to server TLS private key (PEM)")
	tlsCA   := flag.String("tls-ca",   "", "Path to CA certificate for client verification (enables mTLS)")

	flag.Parse()

	if *showVersion {
		fmt.Printf("hw-fault-exporter version %s\n", exporterVersion)
		os.Exit(0)
	}

	// ----- Logger ----------------------------------------------------------

	logger := buildLogger(*logLevel)
	defer logger.Sync() //nolint:errcheck

	logger.Info("Hardware Fault Prometheus Exporter starting",
		zap.String("version", exporterVersion),
		zap.String("listen", *listenAddr),
		zap.String("sysfs_root", *sysfsRoot),
		zap.String("metrics_path", *metricsPath),
	)

	// ----- Prometheus registry ---------------------------------------------

	// Use a custom registry (not the global default) so we control exactly
	// which metrics are exposed and avoid accidental Go runtime metric leakage.
	registry := prometheus.NewRegistry()

	// ready flips to 1 after the first successful /metrics scrape.
	var ready atomic.Int32

	// Always register the exporter's own health metrics.
	selfCollector := collectors.NewSelfCollector(exporterVersion)
	registry.MustRegister(selfCollector)

	// ----- Register subsystem collectors -----------------------------------

	opts := collectors.Options{
		SysfsRoot:     *sysfsRoot,
		ScrapeTimeout: *scrapeTimeout,
		Logger:        logger,
	}

	if !*disableEDAC {
		edacCollector := collectors.NewEDACCollector(opts)
		registry.MustRegister(edacCollector)
		logger.Info("EDAC collector registered")
	} else {
		logger.Warn("EDAC collector disabled via --no-edac")
	}

	if !*disableMCE {
		mceCollector := collectors.NewMCECollector(opts)
		registry.MustRegister(mceCollector)
		logger.Info("MCE collector registered")
	} else {
		logger.Warn("MCE collector disabled via --no-mce")
	}

	if !*disableAER {
		aerCollector := collectors.NewAERCollector(opts)
		registry.MustRegister(aerCollector)
		logger.Info("AER collector registered")
	} else {
		logger.Warn("AER collector disabled via --no-aer")
	}

	// ----- --once mode (cron / textfile collector) -------------------------

	if *onceMode {
		mfs, err := registry.Gather()
		if err != nil {
			logger.Error("Once mode: gather failed", zap.Error(err))
			os.Exit(1)
		}
		var buf strings.Builder
		for _, mf := range mfs {
			if _, err := expfmt.MetricFamilyToText(&buf, mf); err != nil {
				logger.Warn("Once mode: encode error", zap.Error(err))
			}
		}
		fmt.Print(buf.String())
		os.Exit(0)
	}

	// ----- HTTP server -----------------------------------------------------

	mux := http.NewServeMux()

	// Metrics endpoint — flips the readiness flag after first successful gather.
	baseHandler := promhttp.HandlerFor(registry, promhttp.HandlerOpts{
		ErrorHandling: promhttp.ContinueOnError,
		ErrorLog:      &zapErrorLogger{logger},
		Registry:      registry,
	})
	mux.HandleFunc(*metricsPath, func(w http.ResponseWriter, r *http.Request) {
		baseHandler.ServeHTTP(w, r)
		ready.Store(1)
	})

	// Liveness probe — always 200 while the process is running.
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		w.WriteHeader(http.StatusOK)
		fmt.Fprintf(w, "ok\n")
	})

	// Readiness probe — 503 until the first /metrics scrape succeeds.
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		if ready.Load() == 1 {
			w.WriteHeader(http.StatusOK)
			fmt.Fprintf(w, "ready\n")
		} else {
			w.WriteHeader(http.StatusServiceUnavailable)
			fmt.Fprintf(w, "not ready\n")
		}
	})

	// Landing page
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprintf(w, `<!DOCTYPE html>
<html><head><title>Hardware Fault Exporter</title></head>
<body>
<h1>Hardware Fault Prometheus Exporter v%s</h1>
<p>Scrapes kernel hardware error counters (EDAC, MCE, PCIe AER) for ARM Linux servers.</p>
<p><a href="%s">Metrics</a> | <a href="/healthz">Liveness</a> | <a href="/readyz">Readiness</a></p>
<h2>Active Collectors</h2>
<p>Each collector emits <code>fault_resilience_collector_up{collector="..."}</code> (1=up, 0=absent) for alerting on subsystem availability.</p>
<ul>
%s%s%s</ul>
</body></html>`,
			exporterVersion,
			*metricsPath,
			collectorLI("EDAC", !*disableEDAC, "EDAC memory error counters (/sys/devices/system/edac/)"),
			collectorLI("MCE", !*disableMCE, "Machine check event counters"),
			collectorLI("PCIe AER", !*disableAER, "PCIe Advanced Error Reporting counters (/sys/bus/pci/devices/)"),
		)
	})

	server := &http.Server{
		Addr:         *listenAddr,
		Handler:      mux,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	// ----- mTLS configuration ----------------------------------------------

	tlsEnabled := *tlsCert != "" && *tlsKey != ""
	if tlsEnabled {
		tlsCfg, err := buildTLSConfig(*tlsCert, *tlsKey, *tlsCA, logger)
		if err != nil {
			logger.Fatal("TLS configuration failed", zap.Error(err))
		}
		server.TLSConfig = tlsCfg
		logger.Info("mTLS enabled",
			zap.String("cert", *tlsCert),
			zap.String("ca", *tlsCA),
		)
	}

	// ----- Graceful shutdown -----------------------------------------------

	ctx, stop := signal.NotifyContext(context.Background(),
		syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		logger.Info("HTTP server listening", zap.String("addr", *listenAddr))
		var err error
		if tlsEnabled {
			err = server.ListenAndServeTLS(*tlsCert, *tlsKey)
		} else {
			err = server.ListenAndServe()
		}
		if err != nil && err != http.ErrServerClosed {
			logger.Fatal("HTTP server error", zap.Error(err))
		}
	}()

	<-ctx.Done()
	logger.Info("Shutdown signal received")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := server.Shutdown(shutdownCtx); err != nil {
		logger.Error("Graceful shutdown failed", zap.Error(err))
	}
	logger.Info("Exporter stopped")
}

// ----- Helpers -------------------------------------------------------------

// buildLogger creates a zap production logger configured for the given level string.
// Accepts "debug", "info" (default), "warn", or "error". Unknown strings fall through
// to info level. Uses ISO8601 timestamps so log lines are human-readable in journald.
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

// buildTLSConfig returns a *tls.Config that enables mTLS when caPath is set.
func buildTLSConfig(certPath, keyPath, caPath string, logger *zap.Logger) (*tls.Config, error) {
	cert, err := tls.LoadX509KeyPair(certPath, keyPath)
	if err != nil {
		return nil, fmt.Errorf("load key pair: %w", err)
	}

	tlsCfg := &tls.Config{
		Certificates: []tls.Certificate{cert},
		MinVersion:   tls.VersionTLS13,
	}

	if caPath != "" {
		caPEM, err := os.ReadFile(caPath)
		if err != nil {
			return nil, fmt.Errorf("read CA cert %s: %w", caPath, err)
		}
		caPool := x509.NewCertPool()
		if !caPool.AppendCertsFromPEM(caPEM) {
			return nil, fmt.Errorf("no valid certificates found in CA file: %s", caPath)
		}
		tlsCfg.ClientCAs = caPool
		tlsCfg.ClientAuth = tls.RequireAndVerifyClientCert
		logger.Info("mTLS: client certificate verification enabled")
	} else {
		logger.Warn("mTLS: --tls-ca not set; server TLS only (no client verification)")
	}

	return tlsCfg, nil
}

// collectorLI renders an HTML list item for the exporter landing page.
func collectorLI(name string, enabled bool, desc string) string {
	status := "enabled"
	if !enabled {
		status = "disabled"
	}
	return fmt.Sprintf("<li><strong>%s</strong> (%s) — %s</li>\n", name, status, desc)
}

// zapErrorLogger adapts zap.Logger to the promhttp.Logger interface.
type zapErrorLogger struct{ l *zap.Logger }

func (z *zapErrorLogger) Println(v ...interface{}) {
	z.l.Error(fmt.Sprint(v...))
}
