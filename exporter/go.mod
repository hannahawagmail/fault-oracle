// Run `cd exporter && go mod tidy` to generate go.sum after cloning.
// Do NOT hand-edit this file; let the Go toolchain manage dependencies.
module github.com/hannahawagmail/fault-oracle/exporter

go 1.21

require (
	github.com/prometheus/client_golang v1.17.0
	github.com/prometheus/common v0.45.0
	go.uber.org/zap v1.26.0
)

require (
	github.com/beorn7/perks v1.0.1 // indirect
	github.com/cespare/xxhash/v2 v2.2.0 // indirect
	github.com/prometheus/client_model v0.5.0 // indirect
	github.com/prometheus/procfs v0.12.0 // indirect
	go.uber.org/multierr v1.11.0 // indirect
	golang.org/x/sys v0.15.0 // indirect
	google.golang.org/protobuf v1.31.0 // indirect
)
