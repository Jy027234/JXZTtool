from __future__ import annotations

import pytest

from tools.embedding_compat_gateway_smoke import _vector, run_smoke


def test_compat_gateway_vectors_are_normalized_and_deterministic() -> None:
    first = _vector("alpha")
    second = _vector("alpha")

    assert first == second
    assert len(first) == 16
    assert sum(value * value for value in first) == pytest.approx(1.0, abs=1e-5)


def test_compat_gateway_smoke_exercises_real_http_batching() -> None:
    result = run_smoke()

    assert result["status"] == "ok"
    assert result["embedded_chunk_ratio"] == 1.0
    assert result["gateway"]["request_count"] == 2
    assert result["gateway"]["batch_sizes"] == [2, 1]
    assert result["gateway"]["auth_failures"] == 0
