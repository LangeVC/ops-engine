"""Rate-limit tracker — decorator for GitHub API calls.

Logs X-RateLimit-Remaining on every GitHub API response.
Warns at <500 remaining. Writes Prometheus textfile gauge.

Usage:
    from ops_engine.utils.rate_limit_tracker import track_rate_limit

    @track_rate_limit(namespace="capacium-ops")
    def my_api_call(url):
        return urllib.request.urlopen(url)
"""
from __future__ import annotations

import functools
import logging
import os
from datetime import datetime, timezone
from http.client import HTTPResponse
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

PROMETHEUS_FILE = os.environ.get(
    "OPS_ENGINE_RATE_LIMIT_METRICS",
    "/var/lib/prometheus/node-exporter/ops-engine-rate-limit.prom",
)
WARN_THRESHOLD = int(os.environ.get("OPS_ENGINE_RATE_LIMIT_WARN", "500"))


def _extract_rate_limit(response: HTTPResponse) -> tuple[int, int]:
    remaining = int(response.getheader("X-RateLimit-Remaining", -1))
    limit = int(response.getheader("X-RateLimit-Limit", -1))
    return remaining, limit


def _write_prometheus_metric(namespace: str, remaining: int) -> None:
    try:
        path = Path(PROMETHEUS_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        with open(str(path) + ".tmp", "a") as f:
            f.write(
                f"ops_engine_rate_limit_remaining{{namespace=\"{namespace}\"}} "
                f"{remaining} {ts}\n"
            )
        path.with_suffix(".prom").write_text(
            path.with_suffix(".prom.tmp").read_text()
            if path.with_suffix(".prom.tmp").exists()
            else ""
        )
    except OSError:
        pass


def track_rate_limit(namespace: str):
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = func(*args, **kwargs)
            if isinstance(result, HTTPResponse):
                remaining, limit = _extract_rate_limit(result)
                if remaining >= 0:
                    level = logging.WARNING if remaining < WARN_THRESHOLD else logging.DEBUG
                    logger.log(
                        level,
                        "rate-limit namespace=%s remaining=%s/%s",
                        namespace,
                        remaining,
                        limit,
                    )
                    _write_prometheus_metric(namespace, remaining)
            return result
        return wrapper
    return decorator
