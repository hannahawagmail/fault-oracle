"""Leader election via Kubernetes Lease objects for HA remediation controllers."""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.request
from datetime import datetime, timezone


_SA_TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"
_SA_CA = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"


def _in_cluster() -> bool:
    return os.path.isfile(_SA_TOKEN)


class LeaderElector:
    def __init__(self, namespace: str, lease_name: str, identity: str, duration_s: int = 15):
        self.namespace = namespace
        self.lease_name = lease_name
        self.identity = identity
        self.duration_s = duration_s
        self._leader = not _in_cluster()  # fallback: single-instance mode
        self._k8s = _in_cluster()

    def _headers(self) -> dict[str, str]:
        with open(_SA_TOKEN) as f:
            token = f.read().strip()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _url(self) -> str:
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        return f"https://{host}:{port}/apis/coordination.k8s.io/v1/namespaces/{self.namespace}/leases/{self.lease_name}"

    def _ssl_ctx(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(cafile=_SA_CA if os.path.isfile(_SA_CA) else None)
        return ctx

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    def _lease_body(self) -> bytes:
        return json.dumps({
            "apiVersion": "coordination.k8s.io/v1",
            "kind": "Lease",
            "metadata": {"name": self.lease_name, "namespace": self.namespace},
            "spec": {
                "holderIdentity": self.identity,
                "leaseDurationSeconds": self.duration_s,
                "acquireTime": self._now_iso(),
                "renewTime": self._now_iso(),
            },
        }).encode()

    def try_acquire(self) -> bool:
        if not self._k8s:
            self._leader = True
            return True
        try:
            req = urllib.request.Request(self._url(), data=self._lease_body(),
                                        headers=self._headers(), method="PUT")
            urllib.request.urlopen(req, context=self._ssl_ctx())
            self._leader = True
        except urllib.error.HTTPError as e:
            if e.code == 409:
                self._leader = False
            else:
                self._leader = False
        except OSError:
            self._leader = False
        return self._leader

    def is_leader(self) -> bool:
        return self._leader

    def release(self) -> None:
        self._leader = False
