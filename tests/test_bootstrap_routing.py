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
