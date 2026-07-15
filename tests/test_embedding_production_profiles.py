from __future__ import annotations

from pathlib import Path
import unittest

from parsecore.config import load_settings


ROOT = Path(__file__).resolve().parents[1]


class EmbeddingProductionProfileTests(unittest.TestCase):
    def test_local_pgvector_profile_uses_model_compatible_dimension(self) -> None:
        settings = load_settings(ROOT / "parsecore.pgvector.local-embedding.toml.example")

        self.assertEqual(settings.index_mode, "pgvector")
        self.assertEqual(settings.index_embedding_dimension, 384)
        self.assertTrue(settings.providers.embedding.enabled)
        self.assertEqual(settings.providers.embedding.provider, "sentence-transformers-local")
        self.assertTrue(settings.providers.embedding.options["local_files_only"])
        self.assertEqual(settings.providers.embedding.api_key_env, "")

    def test_remote_pgvector_profile_keeps_credential_out_of_config(self) -> None:
        settings = load_settings(ROOT / "parsecore.pgvector.remote-embedding.toml.example")

        self.assertEqual(settings.index_mode, "pgvector")
        self.assertEqual(settings.index_embedding_dimension, 1536)
        self.assertTrue(settings.providers.embedding.enabled)
        self.assertEqual(settings.providers.embedding.provider, "openai-compatible")
        self.assertEqual(settings.providers.embedding.api_key_env, "PARSECORE_EMBEDDING_API_KEY")
        self.assertEqual(settings.providers.embedding.options["dimensions"], 1536)
