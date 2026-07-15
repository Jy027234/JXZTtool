"""Routing tests for build_runtime backend selection."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from parsecore.bootstrap import _build_index, _build_job_store, build_runtime
from parsecore.stores import SQLiteJobStore
from parsecore.stubs import FakeRerankProvider, InMemoryJobStore, NullIndex, NullRerankProvider


ROOT = Path(__file__).resolve().parent.parent


class BootstrapRoutingTests(unittest.TestCase):
    def test_sqlite_url_returns_sqlite_store(self) -> None:
        store = _build_job_store("sqlite:///./var/test_routing.db")
        self.assertIsInstance(store, SQLiteJobStore)

    def test_sqlite_store_queries_document_records_page(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SQLiteJobStore(f"sqlite:///{(Path(tmp) / 'store.db').as_posix()}")
            store.save_document_views(
                doc_id="doc-records",
                records=[
                    {"record_id": "record-1", "page_start": 1, "fields": {"name": "alpha"}},
                    {"record_id": "record-2", "page_start": 2, "fields": {"name": "beta"}},
                    {"record_id": "record-3", "page_start": 3, "fields": {"name": "beta"}},
                ],
            )

            page = store.query_document_records(
                doc_id="doc-records",
                query="beta",
                page_start=2,
                page_end=3,
                limit=1,
                offset=1,
            )

            self.assertTrue(page["persisted"])
            self.assertEqual(page["total"], 2)
            self.assertEqual(page["items"][0]["record_id"], "record-3")

    def test_memory_url_returns_inmemory_store(self) -> None:
        self.assertIsInstance(_build_job_store("memory://"), InMemoryJobStore)
        self.assertIsInstance(_build_job_store(""), InMemoryJobStore)

    def test_inmemory_store_persists_document_views_by_tenant(self) -> None:
        store = InMemoryJobStore()

        store.save_document_views(
            doc_id="doc-views",
            tenant_id="tenant-a",
            pages=[{"page_number": 1, "text": "alpha"}],
            lines=[{"line_id": "line-1", "page_number": 1, "text": "alpha"}],
            records=[{"record_id": "record-1", "page_start": 1, "fields": {"name": "alpha"}}],
        )
        store.save_document_views(
            doc_id="doc-views",
            tenant_id="tenant-b",
            records=[{"record_id": "record-2", "page_start": 2, "fields": {"name": "beta"}}],
        )

        tenant_a = store.get_document_views(doc_id="doc-views", tenant_id="tenant-a")
        tenant_b = store.get_document_views(doc_id="doc-views", tenant_id="tenant-b")
        self.assertEqual(tenant_a["pages"][0]["text"], "alpha")
        self.assertEqual(tenant_a["records"][0]["record_id"], "record-1")
        self.assertEqual(tenant_b["records"][0]["record_id"], "record-2")
        self.assertEqual(store.get_document_records(doc_id="doc-views", tenant_id="missing"), ())

        page = store.query_document_records(
            doc_id="doc-views",
            tenant_id="tenant-a",
            query="alpha",
            field_filters={"name": "alpha"},
            limit=1,
            offset=0,
        )
        self.assertTrue(page["persisted"])
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["record_id"], "record-1")

        missing = store.query_document_records(doc_id="doc-views", tenant_id="missing")
        self.assertFalse(missing["persisted"])
        self.assertEqual(missing["total"], 0)

    def test_inmemory_store_replaces_document_views_by_part_prefix(self) -> None:
        store = InMemoryJobStore()
        prefix = "doc:merged:part-1:"
        store.save_document_views(
            doc_id="doc",
            pages=[
                {"page_number": 1, "text": "old", "page_start": 1, "page_end": 1},
                {"page_number": 2, "text": "keep", "page_start": 2, "page_end": 2},
            ],
            lines=[
                {"line_id": f"{prefix}old:line:1", "text": "old", "page_number": 1},
                {"line_id": "doc:merged:part-2:keep:line:1", "text": "keep", "page_number": 2},
            ],
            records=[
                {"record_id": "old", "block_id": f"{prefix}old", "fields": {"text": "old"}, "page_start": 1},
                {"record_id": "keep", "block_id": "doc:merged:part-2:keep", "fields": {"text": "keep"}, "page_start": 2},
            ],
        )

        store.replace_document_views_by_prefix(
            doc_id="doc",
            item_id_prefix=prefix,
            pages=[{"page_number": 1, "text": "new", "page_start": 1, "page_end": 1}],
            lines=[{"line_id": f"{prefix}new:line:1", "text": "new", "page_number": 1}],
            records=[{"record_id": "new", "block_id": f"{prefix}new", "fields": {"text": "new"}, "page_start": 1}],
        )

        views = store.get_document_views(doc_id="doc")
        self.assertEqual([page["text"] for page in views["pages"]], ["new", "keep"])
        self.assertEqual([line["text"] for line in views["lines"]], ["new", "keep"])
        self.assertEqual([record["fields"]["text"] for record in views["records"]], ["new", "keep"])

    def test_unknown_scheme_raises(self) -> None:
        with self.assertRaises(ValueError):
            _build_job_store("mysql://localhost/db")

    def test_index_default_is_null(self) -> None:
        idx = _build_index("sqlite:///./var/x.db", "hybrid")
        self.assertIsInstance(idx, NullIndex)
        idx_null = _build_index("postgresql://localhost/db", "null")
        self.assertIsInstance(idx_null, NullIndex)

    def test_postgres_index_uses_configured_embedding_dimension(self) -> None:
        with patch("parsecore.bootstrap.PgVectorIndex") as pgvector_index:
            expected = object()
            pgvector_index.return_value = expected

            actual = _build_index(
                "postgresql://localhost/parsecore",
                "pgvector",
                embedding_dimension=384,
            )

        self.assertIs(actual, expected)
        pgvector_index.assert_called_once_with(
            "postgresql://localhost/parsecore",
            dim=384,
        )

    def test_build_runtime_wires_enabled_fake_reranker(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "parsecore.toml"
            config_path.write_text(
                (ROOT / "parsecore.toml")
                .read_text(encoding="utf-8")
                .replace('database_url = "sqlite:///./var/parsecore.db"', 'database_url = "memory://"')
                .replace(
                    '[providers.rerank]\nenabled = false\nprovider = "dashscope-compatible"',
                    '[providers.rerank]\nenabled = true\nprovider = "fake"',
                ),
                encoding="utf-8",
            )

            runtime = build_runtime(config_path)

        self.assertIsInstance(runtime.rerank_provider, FakeRerankProvider)

    def test_build_runtime_falls_back_when_enabled_remote_reranker_has_no_key(self) -> None:
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "parsecore.toml"
            config_path.write_text(
                (ROOT / "parsecore.toml")
                .read_text(encoding="utf-8")
                .replace('database_url = "sqlite:///./var/parsecore.db"', 'database_url = "memory://"')
                .replace(
                    'enabled = false\nprovider = "dashscope-compatible"\nmodel = "qwen/qwen3-vl-rerank"\nbase_url = ""',
                    'enabled = true\nprovider = "dashscope-compatible"\nmodel = "qwen/qwen3-vl-rerank"\nbase_url = "https://example.invalid/v1"',
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"PARSECORE_RERANK_API_KEY": ""}, clear=False):
                runtime = build_runtime(config_path)

        self.assertIsInstance(runtime.rerank_provider, NullRerankProvider)


if __name__ == "__main__":
    unittest.main()
