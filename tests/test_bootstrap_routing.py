"""Routing tests for build_runtime backend selection."""

from __future__ import annotations

import unittest

from parsecore.bootstrap import _build_index, _build_job_store
from parsecore.stores import SQLiteJobStore
from parsecore.stubs import InMemoryJobStore, NullIndex


class BootstrapRoutingTests(unittest.TestCase):
    def test_sqlite_url_returns_sqlite_store(self) -> None:
        store = _build_job_store("sqlite:///./var/test_routing.db")
        self.assertIsInstance(store, SQLiteJobStore)

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

    def test_unknown_scheme_raises(self) -> None:
        with self.assertRaises(ValueError):
            _build_job_store("mysql://localhost/db")

    def test_index_default_is_null(self) -> None:
        idx = _build_index("sqlite:///./var/x.db", "hybrid")
        self.assertIsInstance(idx, NullIndex)
        idx_null = _build_index("postgresql://localhost/db", "null")
        self.assertIsInstance(idx_null, NullIndex)


if __name__ == "__main__":
    unittest.main()
