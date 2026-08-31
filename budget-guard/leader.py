"""Kubernetes Lease leader election (active/standby).

Uses coordination.k8s.io/v1 Lease via CoordinationV1Api so we do not
depend on kubernetes.leaderelection.LeaseLock (not in all client versions).
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from state import detect_namespace

logger = logging.getLogger("budget-guard")

LEASE_DURATION_S = 15
RENEW_DEADLINE_S = 10
RETRY_PERIOD_S = 2


def election_enabled() -> bool:
    """On unless explicitly disabled; auto-on inside a cluster."""
    flag = (os.environ.get("BUDGET_GUARD_LEADER_ELECTION") or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    return bool(os.environ.get("KUBERNETES_SERVICE_HOST"))


def identity() -> str:
    return (
        (os.environ.get("POD_NAME") or "").strip()
        or (os.environ.get("HOSTNAME") or "").strip()
        or socket.gethostname()
    )


def lease_name() -> str:
    return (os.environ.get("BUDGET_GUARD_LEASE_NAME") or "budget-guard").strip()


def _api_status(exc: BaseException) -> int | None:
    status = getattr(exc, "status", None)
    return status if isinstance(status, int) else None


def _renew_time(lease: Any) -> datetime | None:
    spec = getattr(lease, "spec", None)
    if spec is None:
        return None
    raw = getattr(spec, "renew_time", None)
    if raw is None:
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=timezone.utc)
        return raw
    return None


def lease_is_expired(lease: Any, duration_s: int, now: datetime | None = None) -> bool:
    """True if the lease has no renewTime or renewTime is older than duration_s."""
    now = now or datetime.now(timezone.utc)
    renew = _renew_time(lease)
    if renew is None:
        return True
    return (now - renew).total_seconds() > duration_s


class LeaseElector:
    """Acquire / renew a Lease. Inject ``api`` in tests."""

    def __init__(
        self,
        name: str,
        namespace: str,
        identity: str,
        api: Any | None = None,
        lease_duration_s: int = LEASE_DURATION_S,
    ) -> None:
        self.name = name
        self.namespace = namespace
        self.identity = identity
        self.lease_duration_s = lease_duration_s
        self._api = api
        self._lease: Any = None

    def _client(self) -> Any:
        if self._api is not None:
            return self._api
        from kubernetes import client, config as kcfg

        try:
            kcfg.load_incluster_config()
        except Exception:
            kcfg.load_kube_config()
        self._api = client.CoordinationV1Api()
        return self._api

    def try_acquire_or_renew(self) -> bool:
        from kubernetes import client

        api = self._client()
        now = datetime.now(timezone.utc)
        try:
            lease = api.read_namespaced_lease(self.name, self.namespace)
        except Exception as exc:
            if _api_status(exc) != 404:
                logger.info("Lease read failed: %s", exc)
                return False
            body = client.V1Lease(
                metadata=client.V1ObjectMeta(name=self.name),
                spec=client.V1LeaseSpec(
                    holder_identity=self.identity,
                    lease_duration_seconds=self.lease_duration_s,
                    acquire_time=now,
                    renew_time=now,
                    lease_transitions=0,
                ),
            )
            try:
                self._lease = api.create_namespaced_lease(self.namespace, body)
                return True
            except Exception as create_exc:
                logger.info("Lease create failed: %s", create_exc)
                return False

        spec = lease.spec or client.V1LeaseSpec()
        holder = getattr(spec, "holder_identity", None) or ""
        ours = holder == self.identity
        if not ours and not lease_is_expired(lease, self.lease_duration_s, now):
            return False

        spec.holder_identity = self.identity
        spec.lease_duration_seconds = self.lease_duration_s
        spec.renew_time = now
        if not ours:
            spec.acquire_time = now
            spec.lease_transitions = (getattr(spec, "lease_transitions", 0) or 0) + 1
        lease.spec = spec
        try:
            self._lease = api.replace_namespaced_lease(
                self.name, self.namespace, lease,
            )
            return True
        except Exception as exc:
            logger.info("Lease update failed: %s", exc)
            return False


def run_election(
    on_started: Callable[[], None],
    on_stopped: Callable[[], None],
    elector: LeaseElector | None = None,
    retry_period_s: float = RETRY_PERIOD_S,
    renew_deadline_s: float = RENEW_DEADLINE_S,
    stop: threading.Event | None = None,
) -> None:
    """Block: acquire Lease, run on_started in a worker, renew until lost.

    After losing the lease, ``on_stopped`` is invoked and this function
    returns. The caller may loop to re-acquire.
    """
    if elector is None:
        ns = detect_namespace()
        ident = identity()
        name = lease_name()
        logger.info(
            "Leader election lease=%s namespace=%s identity=%s", name, ns, ident,
        )
        elector = LeaseElector(name, ns, ident)

    while True:
        if stop is not None and stop.is_set():
            return
        if elector.try_acquire_or_renew():
            break
        time.sleep(retry_period_s)

    logger.info("%s acquired lease", elector.identity)
    leading_failed = threading.Event()

    def _work() -> None:
        try:
            on_started()
        except Exception:
            logger.exception("onstarted_leading raised; stepping down")
            leading_failed.set()

    work = threading.Thread(target=_work, daemon=True, name="leader-work")
    work.start()

    try:
        while True:
            if stop is not None and stop.is_set():
                break
            if leading_failed.is_set():
                break
            deadline = time.monotonic() + renew_deadline_s
            ok = False
            while time.monotonic() < deadline:
                if elector.try_acquire_or_renew():
                    ok = True
                    break
                time.sleep(retry_period_s)
            if not ok:
                logger.warning("Failed to renew lease before deadline")
                break
            time.sleep(retry_period_s)
    finally:
        on_stopped()
        work.join(timeout=LEASE_DURATION_S + 5)
