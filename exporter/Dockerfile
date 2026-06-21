# SPDX-License-Identifier: Apache-2.0
#
# Multi-stage build for the hardware fault Prometheus exporter.
#
# Stage 1 (builder): compile the Go binary with CGO disabled so the resulting
#   binary is fully static and can run in a distroless container.
#
# Stage 2 (runtime): gcr.io/distroless/static — contains only CA certificates,
#   /etc/passwd, and /etc/nsswitch.conf. No shell, no package manager, no
#   libc. Attack surface is minimal.
#
# Build:
#   docker build -t hw-fault-exporter:latest .
#
# Cross-build for ARM64 from an x86_64 host:
#   docker buildx build --platform linux/arm64 -t hw-fault-exporter:latest .

# ── Stage 1: builder ──────────────────────────────────────────────────────
FROM golang:1.21-alpine AS builder

# Install git so `go mod download` can fetch from VCS if needed.
RUN apk add --no-cache git ca-certificates tzdata

WORKDIR /build

# Cache dependency downloads separately from source compilation.
COPY go.mod go.sum ./
RUN go mod download

# Copy source and build a fully static binary.
COPY . .
RUN CGO_ENABLED=0 GOOS=linux \
    go build \
    -ldflags="-s -w -extldflags=-static" \
    -trimpath \
    -o hw-fault-exporter \
    .

# ── Stage 2: runtime (distroless) ─────────────────────────────────────────
# gcr.io/distroless/static contains:
#   - CA certificates (for HTTPS egress if needed)
#   - /etc/passwd and /etc/nsswitch.conf (for non-root user support)
#   - No shell, no curl, no package manager
FROM gcr.io/distroless/static:nonroot

# Copy the static binary from builder.
COPY --from=builder /build/hw-fault-exporter /usr/local/bin/hw-fault-exporter

# Run as non-root user (uid 65532 = "nonroot" in distroless).
USER nonroot:nonroot

# Prometheus scrape port.
EXPOSE 9101

ENTRYPOINT ["/usr/local/bin/hw-fault-exporter"]
CMD ["--listen-addr=:9101", "--sysfs-root=/sys"]

# Image labels (OCI spec)
LABEL org.opencontainers.image.title="hw-fault-exporter" \
      org.opencontainers.image.description="Prometheus exporter for ARM Linux hardware fault counters (EDAC, MCE, PCIe AER)" \
      org.opencontainers.image.source="https://github.com/hanna-hawa/arm-linux-fault-resilience" \
      org.opencontainers.image.licenses="Apache-2.0"
