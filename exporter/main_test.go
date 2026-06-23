package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"os"
	"path/filepath"
	"testing"
	"time"

	"go.uber.org/zap"
)

func TestBuildLogger_InfoLevel(t *testing.T) {
	l := buildLogger("info")
	if l == nil {
		t.Fatal("expected non-nil logger for info level")
	}
}

func TestBuildLogger_DebugLevel(t *testing.T) {
	l := buildLogger("debug")
	if l == nil {
		t.Fatal("expected non-nil logger for debug level")
	}
}

func TestBuildLogger_InvalidLevel(t *testing.T) {
	l := buildLogger("invalid")
	if l == nil {
		t.Fatal("expected non-nil logger even for unknown level (falls back to info)")
	}
}

func TestBuildTLSConfig_MissingFiles(t *testing.T) {
	logger := zap.NewNop()
	_, err := buildTLSConfig("/nonexistent/cert.pem", "/nonexistent/key.pem", "", logger)
	if err == nil {
		t.Fatal("expected error for missing cert/key files")
	}
}

func TestBuildTLSConfig_ValidCerts(t *testing.T) {
	dir := t.TempDir()
	certFile := filepath.Join(dir, "cert.pem")
	keyFile := filepath.Join(dir, "key.pem")
	caFile := filepath.Join(dir, "ca.pem")

	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: "test"},
		NotBefore:    time.Now(),
		NotAfter:     time.Now().Add(time.Hour),
		IsCA:         true,
		KeyUsage:     x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
	}
	certDER, _ := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	keyDER, _ := x509.MarshalECPrivateKey(key)

	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: certDER})
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER})

	os.WriteFile(certFile, certPEM, 0600)
	os.WriteFile(keyFile, keyPEM, 0600)
	os.WriteFile(caFile, certPEM, 0600)

	logger := zap.NewNop()
	cfg, err := buildTLSConfig(certFile, keyFile, caFile, logger)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if cfg == nil {
		t.Fatal("expected non-nil TLS config")
	}
	if cfg.ClientCAs == nil {
		t.Fatal("expected ClientCAs to be set when CA file provided")
	}
}
