"""Compact state stored in a Kubernetes ConfigMap (etcd via the API)."""
from __future__ import annotations

import json
import logging
from typing import Any

from tracker import SpendTracker, utc_day_key

import state as persist

logger = logging.getLogger("budget-guard")

DATA_KEY = "state.json"


def _api_exception_status(exc: BaseException) -> int | None:
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return status
    return None


class ConfigMapStateStore:
    """Load/save compact JSON in ConfigMap.data['state.json'].

    Uses resourceVersion for optimistic concurrency. A missing ConfigMap
    is fail-open (empty snapshot, watermark unset).
    """

    def __init__(
        self,
        name: str,
        namespace: str,
        api: Any | None = None,
    ) -> None:
        self.name = name
        self.namespace = namespace
        self._api = api
        self._resource_version: str | None = None

    def _client(self) -> Any:
        if self._api is not None:
            return self._api
        from kubernetes import client, config as kcfg

        try:
            kcfg.load_incluster_config()
        except Exception:
            kcfg.load_kube_config()
        self._api = client.CoreV1Api()
        return self._api

    def load(self) -> dict[str, Any]:
        today = utc_day_key()
        try:
            cm = self._client().read_namespaced_config_map(self.name, self.namespace)
        except Exception as exc:
            if _api_exception_status(exc) == 404:
                logger.info(
                    "State ConfigMap %s/%s missing; starting fail-open",
                    self.namespace,
                    self.name,
                )
                return persist.empty_snapshot(today)
            logger.warning("Failed to read state ConfigMap: %s", exc)
            return persist.empty_snapshot(today)

        meta = getattr(cm, "metadata", None)
        self._resource_version = getattr(meta, "resource_version", None)
        raw = (getattr(cm, "data", None) or {}).get(DATA_KEY)
        if not raw:
            return persist.empty_snapshot(today)
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("state root must be a mapping")
            return persist.parse_snapshot(data)
        except Exception as exc:
            logger.warning("Corrupt state ConfigMap payload: %s", exc)
            return persist.empty_snapshot(today)

    def save(
        self,
        tracker: SpendTracker,
        blocked_projects: set[str],
        watermark_ms: int,
    ) -> bool:
        payload = persist.compact_snapshot(tracker, blocked_projects, watermark_ms)
        body_str = json.dumps(payload, sort_keys=True)
        try:
            return self._write(body_str)
        except Exception as exc:
            if _api_exception_status(exc) == 409:
                logger.info("State ConfigMap conflict; retrying once")
                try:
                    self.load()
                    return self._write(body_str)
                except Exception as retry_exc:
                    logger.warning("Failed to save state ConfigMap: %s", retry_exc)
                    return False
            logger.warning("Failed to save state ConfigMap: %s", exc)
            return False

    def _write(self, body_str: str) -> bool:
        from kubernetes import client

        api = self._client()
        try:
            cm = api.read_namespaced_config_map(self.name, self.namespace)
        except Exception as exc:
            if _api_exception_status(exc) != 404:
                raise
            cm = client.V1ConfigMap(
                metadata=client.V1ObjectMeta(name=self.name),
                data={DATA_KEY: body_str},
            )
            created = api.create_namespaced_config_map(self.namespace, cm)
            meta = getattr(created, "metadata", None)
            self._resource_version = getattr(meta, "resource_version", None)
            return True

        data = dict(getattr(cm, "data", None) or {})
        data[DATA_KEY] = body_str
        cm.data = data
        updated = api.replace_namespaced_config_map(self.name, self.namespace, cm)
        meta = getattr(updated, "metadata", None)
        self._resource_version = getattr(meta, "resource_version", None)
        return True
