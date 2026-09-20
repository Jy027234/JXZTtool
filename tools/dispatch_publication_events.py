"""Deliver pending publication events to a host-owned sink.

P0-03 binds the downstream-available notification to the publication event and
writes it into the outbox inside the same transaction that moves the pointer.
This module is the other half: a CLI that drains the outbox so a deployment does
not need a long-running consumer to make notifications arrive.

Delivery is at-least-once by design — the sink runs before the event is marked
dispatched, so a crash in between redelivers.  Consumers must therefore be
idempotent on ``(tenant, doc, run, event_type)``.  A failing sink never marks the
event dispatched and never re-runs the parse.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from parsecore.config import load_settings
from parsecore.run_store import publication_ready, run_store_for


DISPATCH_SCHEMA_VERSION = "2026-09-publication-event-dispatch"


def _stdout_sink(event: Mapping[str, Any]) -> bool:
    """Print one JSON object per event, which any collector can tail."""

    print(json.dumps(event, ensure_ascii=False, sort_keys=True))
    return True


def _http_sink(url: str, *, timeout_seconds: float = 10.0) -> Callable[[Mapping[str, Any]], bool]:
    """POST each event as JSON to ``url``; 2xx means delivered."""

    import urllib.error
    import urllib.request

    def deliver(event: Mapping[str, Any]) -> bool:
        body = json.dumps(dict(event), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return 200 <= int(response.status) < 300
        except urllib.error.HTTPError as exc:
            return 200 <= int(exc.code) < 300
        except Exception:
            return False

    return deliver


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deliver pending ParseCore publication events to a sink"
    )
    parser.add_argument("--config", default="parsecore.toml")
    parser.add_argument(
        "--sink",
        choices=("stdout", "http"),
        default="stdout",
        help="stdout prints one JSON event per line; http POSTs each event",
    )
    parser.add_argument("--url", default=None, help="required for --sink http")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--out", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = load_settings(args.config)
    run_store = run_store_for(settings.database_url)
    if run_store is None or not publication_ready(run_store):
        print(
            json.dumps(
                {
                    "schema_version": DISPATCH_SCHEMA_VERSION,
                    "status": "unsupported_backend",
                    "database_url": settings.database_url,
                    "message": "this backend has no publication outbox",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    if args.sink == "http":
        if not str(args.url or "").strip():
            print(
                json.dumps(
                    {
                        "schema_version": DISPATCH_SCHEMA_VERSION,
                        "status": "invalid_arguments",
                        "message": "--sink http requires --url",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        handler = _http_sink(args.url, timeout_seconds=float(args.timeout_seconds))
    else:
        handler = _stdout_sink

    report = run_store.dispatch_pending_publication_events(
        handler=handler, limit=max(1, int(args.limit))
    )
    report["sink"] = args.sink
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")
    print(rendered, file=sys.stderr)
    return 0 if report["failed_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
