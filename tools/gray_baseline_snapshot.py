"""Capture a ParseCore gray-release runtime baseline from HTTP endpoints."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture ParseCore gray baseline metrics")
    parser.add_argument("--base-url", default="http://127.0.0.1:8090")
    parser.add_argument("--tenant-id", default=None)
    parser.add_argument("--since-hours", type=float, default=24.0)
    parser.add_argument("--sample-size", type=int, default=200)
    parser.add_argument("--events-limit", type=int, default=100)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--out", default="var/self-check/gray-baseline.json")
    return parser


def _endpoint(base_url: str, path: str, query: dict[str, Any] | None = None) -> str:
    base = base_url.rstrip("/") + "/"
    url = urljoin(base, path.lstrip("/"))
    if not query:
        return url
    compact = {
        key: value
        for key, value in query.items()
        if value is not None and value != ""
    }
    if not compact:
        return url
    return f"{url}?{urlencode(compact, doseq=True)}"


def _get_json(url: str, *, api_key: str | None = None, timeout_seconds: float = 15.0) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def build_snapshot(
    *,
    base_url: str,
    runtime: dict[str, Any],
    metrics: dict[str, Any],
    index_metrics: dict[str, Any],
    events: dict[str, Any],
    tenant_id: str | None,
    since_hours: float | None,
    sample_size: int,
) -> dict[str, Any]:
    runtime_section = runtime.get("runtime") or {}
    durations = metrics.get("durations_s") or {}
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base_url": base_url,
        "filters": {
            "tenant_id": tenant_id,
            "since_hours": since_hours,
            "sample_size": sample_size,
        },
        "runtime": {
            "project": runtime.get("project"),
            "mode": runtime.get("mode"),
            "index_mode": runtime.get("index_mode"),
            "execution_mode": runtime_section.get("execution_mode"),
            "max_workers": runtime_section.get("max_workers"),
            "max_upload_bytes": runtime_section.get("max_upload_bytes"),
            "max_inflight_jobs": runtime_section.get("max_inflight_jobs"),
            "api_auth_enabled": runtime_section.get("api_auth_enabled"),
            "parsers": runtime.get("parsers") or [],
        },
        "metrics": {
            "total_jobs": metrics.get("total_jobs"),
            "done_jobs": metrics.get("done_jobs"),
            "failed_jobs": metrics.get("failed_jobs"),
            "active_jobs": metrics.get("active_jobs"),
            "failure_rate": metrics.get("failure_rate"),
            "durations_s": {
                "count": durations.get("count"),
                "mean": durations.get("mean"),
                "p50": durations.get("p50"),
                "p90": durations.get("p90"),
                "p99": durations.get("p99"),
                "max": durations.get("max"),
            },
        },
        "index_metrics": index_metrics,
        "observability": {
            "event_count": len(events.get("events") or []),
            "counters": events.get("counters") or {},
        },
    }


def capture_snapshot(
    *,
    base_url: str,
    tenant_id: str | None,
    since_hours: float | None,
    sample_size: int,
    events_limit: int,
    api_key: str | None,
) -> dict[str, Any]:
    metric_query = {
        "tenant_id": tenant_id,
        "since_hours": since_hours,
        "sample_size": sample_size,
    }
    event_query = {
        "tenant_id": tenant_id,
        "limit": events_limit,
    }
    runtime = _get_json(_endpoint(base_url, "/v1/runtime"), api_key=api_key)
    metrics = _get_json(_endpoint(base_url, "/v1/parse/metrics", metric_query), api_key=api_key)
    index_metrics = _get_json(
        _endpoint(base_url, "/v1/parse/indexes/metrics", {"tenant_id": tenant_id, "since_hours": since_hours}),
        api_key=api_key,
    )
    events = _get_json(_endpoint(base_url, "/v1/parse/events", event_query), api_key=api_key)
    return build_snapshot(
        base_url=base_url,
        runtime=runtime,
        metrics=metrics,
        index_metrics=index_metrics,
        events=events,
        tenant_id=tenant_id,
        since_hours=since_hours,
        sample_size=sample_size,
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    payload = capture_snapshot(
        base_url=args.base_url,
        tenant_id=args.tenant_id,
        since_hours=args.since_hours,
        sample_size=max(1, args.sample_size),
        events_limit=max(1, args.events_limit),
        api_key=args.api_key,
    )
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
