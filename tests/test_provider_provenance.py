from __future__ import annotations

import unittest

from parsecore.api_payloads import _document_projection, _document_providers_projection
from parsecore.bootstrap import build_runtime
from parsecore.models import ParseRequest
from tests.support import TemporaryWorkspace


TEXT_PROVIDER_RUNTIME_CONFIG = """
[project]
name = "test-provider-provenance"
mode = "embedded-sdk"

[storage]
database_url = "__DB_URL__"
object_store = "local://./var/uploads"

[index]
mode = "hybrid"

[translation]
enabled = true
strategy = "lazy"

[product]
adapter = "embedded"

[[providers.local_parsers]]
id = "text-native"
enabled = true
priority = 100
media_types = ["text/plain"]
extensions = [".txt", ".md"]
profiles = ["default"]
capabilities = ["native-text", "rag-baseline"]

[[parsers]]
name = "text-native"
media_types = ["text/plain"]
extensions = [".txt", ".md"]
""".strip()


class ProviderProvenanceTests(unittest.TestCase):
    def test_builtin_parser_populates_provider_and_adapter_versions(self) -> None:
        with TemporaryWorkspace(TEXT_PROVIDER_RUNTIME_CONFIG) as workspace:
            assert workspace.root is not None
            path = workspace.create_text_file("manual.txt", "Heading\n\nInspect pump.")
            runtime = build_runtime(workspace.config_path)
            runtime.submit(
                ParseRequest(
                    doc_id="doc-provider-provenance",
                    file_path=str(path),
                    media_type="text/plain",
                )
            )
            snapshot = runtime.get_document(doc_id="doc-provider-provenance")

        ir = _document_projection(snapshot, projection="ir")
        providers = _document_providers_projection(snapshot)

        self.assertEqual(ir["providers"][0]["provider_id"], "text-native")
        self.assertEqual(ir["providers"][0]["provider_version"], "parsecore-builtin")
        self.assertEqual(ir["providers"][0]["adapter_version"], "2026-06-local-provider-adapter")
        self.assertEqual(ir["blocks"][1]["provenance"]["provider_version"], "parsecore-builtin")
        self.assertEqual(ir["blocks"][1]["provenance"]["adapter_version"], "2026-06-local-provider-adapter")

        provider_entry = providers["providers"][0]
        self.assertEqual(provider_entry["provider_id"], "text-native")
        self.assertEqual(provider_entry["provider_version"], "parsecore-builtin")
        self.assertEqual(provider_entry["adapter_version"], "2026-06-local-provider-adapter")


if __name__ == "__main__":
    unittest.main()
