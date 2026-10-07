"""P0-07: the tenant boundary.

Two properties are under test:

1. attribution — who decides which tenant a request belongs to, and what happens
   when a request parameter disagrees with the authenticated identity;
2. authorization — knowing another tenant's ``doc_id`` / ``job_id`` /
   ``export_id`` must not be enough to read or modify it.

The cross-tenant probes are deliberately generic: if a probe fails, that is a
finding, not a test bug.
"""

from __future__ import annotations

import unittest
import time

from starlette.testclient import TestClient

from parsecore.asgi import create_app
from parsecore.tenant_scope import (
    ERROR_TENANT_IDENTITY_REQUIRED,
    ERROR_TENANT_MISMATCH,
    SOURCE_DEFAULT,
    SOURCE_HOST_AUTHENTICATED,
    SOURCE_REQUEST_PARAMETER,
    TENANT_SCOPE_ENFORCED,
    TENANT_SCOPE_TRUSTED,
    TenantScopeSettings,
    resolve_tenant,
    tenant_scope_settings,
)
from tests.support import TemporaryWorkspace

DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

CONFIG = """
[project]
name = "test-tenant-boundary"
mode = "embedded-sdk"

[runtime]
execution_mode = "inline"
tenant_scope = "__TENANT_SCOPE__"
api_key_env = "PARSECORE_TENANT_TEST_KEY"

[storage]
database_url = "__DB_URL__"
object_store = "local://./var/uploads"

[index]
mode = "hybrid"

[[parsers]]
name = "docx-native"
media_types = ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
extensions = [".docx"]
""".strip()


class ResolveTenantUnitTests(unittest.TestCase):
    def test_request_parameter_chooses_the_tenant_in_trusted_mode(self) -> None:
        resolution = resolve_tenant(
            scope=TenantScopeSettings(mode=TENANT_SCOPE_TRUSTED),
            requested_tenant_id="tenant-a",
        )
        self.assertEqual(resolution.tenant_id, "tenant-a")
        self.assertEqual(resolution.source, SOURCE_REQUEST_PARAMETER)

    def test_host_identity_wins_over_the_request_parameter(self) -> None:
        resolution = resolve_tenant(
            scope=TenantScopeSettings(mode=TENANT_SCOPE_ENFORCED),
            authenticated_tenant_id="tenant-a",
            requested_tenant_id="tenant-a",
        )
        self.assertEqual(resolution.tenant_id, "tenant-a")
        self.assertEqual(resolution.source, SOURCE_HOST_AUTHENTICATED)

    def test_mismatch_is_refused_in_both_modes(self) -> None:
        for mode in (TENANT_SCOPE_TRUSTED, TENANT_SCOPE_ENFORCED):
            with self.subTest(mode=mode):
                with self.assertRaises(Exception) as captured:
                    resolve_tenant(
                        scope=TenantScopeSettings(mode=mode),
                        authenticated_tenant_id="tenant-a",
                        requested_tenant_id="tenant-b",
                    )
                self.assertEqual(captured.exception.code, ERROR_TENANT_MISMATCH)

    def test_enforced_mode_requires_an_identity(self) -> None:
        with self.assertRaises(Exception) as captured:
            resolve_tenant(
                scope=TenantScopeSettings(mode=TENANT_SCOPE_ENFORCED),
                requested_tenant_id="tenant-a",
            )
        self.assertEqual(captured.exception.code, ERROR_TENANT_IDENTITY_REQUIRED)

        with self.assertRaises(Exception) as captured:
            resolve_tenant(scope=TenantScopeSettings(mode=TENANT_SCOPE_ENFORCED))
        self.assertEqual(captured.exception.code, ERROR_TENANT_IDENTITY_REQUIRED)

    def test_trusted_mode_without_parameters_uses_the_default_tenant(self) -> None:
        resolution = resolve_tenant(scope=TenantScopeSettings(mode=TENANT_SCOPE_TRUSTED))
        self.assertEqual(resolution.tenant_id, "default")
        self.assertEqual(resolution.source, SOURCE_DEFAULT)

    def test_scope_settings_are_read_from_runtime_settings_and_normalized(self) -> None:
        class _Runtime:
            class settings:  # noqa: N801 - minimal stand-in
                class runtime:  # noqa: N801
                    tenant_scope = "ENFORCED"
                    tenant_header = "X-Custom-Tenant"

        scope = tenant_scope_settings(_Runtime())
        self.assertEqual(scope.mode, TENANT_SCOPE_ENFORCED)
        self.assertEqual(scope.header, "x-custom-tenant")

        class _BadRuntime:
            class settings:  # noqa: N801
                class runtime:  # noqa: N801
                    tenant_scope = "nonsense"
                    tenant_header = ""

        fallback = tenant_scope_settings(_BadRuntime())
        self.assertEqual(fallback.mode, TENANT_SCOPE_TRUSTED)
        self.assertEqual(fallback.header, "x-parsecore-tenant")


class TenantBoundaryTestCase(unittest.TestCase):
    """Shared fixture: two tenants with one document each."""

    tenant_scope = TENANT_SCOPE_ENFORCED
    api_key = "tenant-test-key"

    def setUp(self) -> None:
        import os
        from unittest.mock import patch

        self._env = patch.dict(os.environ, {"PARSECORE_TENANT_TEST_KEY": self.api_key})
        self._env.start()
        self.addCleanup(self._env.stop)
        self._workspaces: list[TemporaryWorkspace] = []

    def _app(self):
        workspace = TemporaryWorkspace(
            CONFIG.replace("__TENANT_SCOPE__", self.tenant_scope)
        )
        workspace.__enter__()
        self.addCleanup(workspace.__exit__, None, None, None)
        self._workspaces.append(workspace)
        return workspace, create_app(workspace.config_path)

    def _headers(self, tenant: str | None) -> dict[str, str]:
        headers = {"x-api-key": self.api_key}
        if tenant is not None:
            headers["x-parsecore-tenant"] = tenant
        return headers

    def _seed(self, workspace, client) -> dict[str, str]:
        """Create one parsed document for tenant-a and one for tenant-b."""

        ids: dict[str, str] = {}
        for tenant in ("tenant-a", "tenant-b"):
            document = workspace.create_docx(f"{tenant}.docx", [f"{tenant} manual"])
            response = client.post(
                "/v1/parse/jobs",
                json={
                    "doc_id": f"doc-{tenant}",
                    "file_path": str(document),
                    "media_type": DOCX_MEDIA_TYPE,
                    "tenant_id": tenant,
                },
                headers=self._headers(tenant),
            )
            self.assertEqual(response.status_code, 202, response.text)
            ids[f"job-{tenant}"] = response.json()["job_id"]
        return ids


class EnforcedBoundaryTests(TenantBoundaryTestCase):
    tenant_scope = TENANT_SCOPE_ENFORCED

    def test_request_without_host_identity_is_refused(self) -> None:
        _workspace, app = self._app()
        with TestClient(app) as client:
            response = client.get("/v1/parse/jobs", headers={"x-api-key": self.api_key})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], ERROR_TENANT_IDENTITY_REQUIRED)

    def test_request_parameter_cannot_impersonate_another_tenant(self) -> None:
        _workspace, app = self._app()
        with TestClient(app) as client:
            response = client.get(
                "/v1/parse/jobs?tenant_id=tenant-b",
                headers=self._headers("tenant-a"),
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], ERROR_TENANT_MISMATCH)

    def test_response_records_how_the_tenant_was_decided(self) -> None:
        _workspace, app = self._app()
        with TestClient(app) as client:
            response = client.get("/v1/parse/jobs", headers=self._headers("tenant-a"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-parsecore-tenant"], "tenant-a")
        self.assertEqual(response.headers["x-parsecore-tenant-source"], SOURCE_HOST_AUTHENTICATED)

    def test_job_list_is_scoped_to_the_authenticated_tenant(self) -> None:
        workspace, app = self._app()
        with TestClient(app) as client:
            self._seed(workspace, client)
            as_a = client.get("/v1/parse/jobs", headers=self._headers("tenant-a")).json()
            as_b = client.get("/v1/parse/jobs", headers=self._headers("tenant-b")).json()

        self.assertEqual({job["doc_id"] for job in as_a["items"]}, {"doc-tenant-a"})
        self.assertEqual({job["doc_id"] for job in as_b["items"]}, {"doc-tenant-b"})

    def test_cross_tenant_job_read_is_not_found(self) -> None:
        workspace, app = self._app()
        with TestClient(app) as client:
            ids = self._seed(workspace, client)
            own = client.get(
                f"/v1/parse/jobs/{ids['job-tenant-a']}", headers=self._headers("tenant-a")
            )
            foreign = client.get(
                f"/v1/parse/jobs/{ids['job-tenant-a']}", headers=self._headers("tenant-b")
            )

        self.assertEqual(own.status_code, 200)
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.json()["code"], "job_not_found")

    def test_cross_tenant_job_cancel_is_not_found(self) -> None:
        workspace, app = self._app()
        with TestClient(app) as client:
            ids = self._seed(workspace, client)
            foreign = client.post(
                f"/v1/parse/jobs/{ids['job-tenant-a']}/cancel",
                headers=self._headers("tenant-b"),
            )
            still_there = client.get(
                f"/v1/parse/jobs/{ids['job-tenant-a']}", headers=self._headers("tenant-a")
            )

        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(still_there.status_code, 200)
        self.assertNotEqual(still_there.json()["state"], "failed")

    def test_cross_tenant_document_read_is_not_found(self) -> None:
        workspace, app = self._app()
        with TestClient(app) as client:
            self._seed(workspace, client)
            own = client.get("/v1/parse/documents/doc-tenant-a", headers=self._headers("tenant-a"))
            foreign = client.get(
                "/v1/parse/documents/doc-tenant-a", headers=self._headers("tenant-b")
            )

        self.assertEqual(own.status_code, 200)
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.json()["code"], "document_not_found")

    def test_cross_tenant_document_views_are_not_found(self) -> None:
        workspace, app = self._app()
        with TestClient(app) as client:
            self._seed(workspace, client)
            paths = (
                "/v1/parse/documents/doc-tenant-a/reader",
                "/v1/parse/documents/doc-tenant-a/coverage",
                "/v1/parse/documents/doc-tenant-a/quality",
                "/v1/parse/documents/doc-tenant-a/publication",
                "/v1/parse/documents/doc-tenant-a/records",
                "/v1/parse/documents/doc-tenant-a/providers",
                "/v1/parse/documents/doc-tenant-a/parts",
                "/v1/parse/documents/doc-tenant-a/search?q=manual",
                "/v1/parse/documents/doc-tenant-a/structure-search?q=manual",
                "/v1/parse/documents/doc-tenant-a/tasks/search?q=manual",
                "/v1/parse/documents/doc-tenant-a/exports",
            )
            for path in paths:
                with self.subTest(path=path):
                    response = client.get(path, headers=self._headers("tenant-b"))
                    # Publication state is reported as not_ready rather than
                    # not_found: it is a pointer query, not a document read.
                    if path.endswith("/publication"):
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json()["consumer_signal"], "not_ready")
                        continue
                    self.assertEqual(response.status_code, 404, response.text)

    def test_cross_tenant_rerun_is_not_found(self) -> None:
        workspace, app = self._app()
        with TestClient(app) as client:
            self._seed(workspace, client)
            for path, body in (
                ("/v1/parse/documents/doc-tenant-a/reparse", {}),
                ("/v1/parse/documents/doc-tenant-a/rechunk", {}),
                ("/v1/parse/documents/doc-tenant-a/re-embed", {}),
                ("/v1/parse/documents/doc-tenant-a/parts/plan", {}),
            ):
                with self.subTest(path=path):
                    response = client.post(
                        path, json=body, headers=self._headers("tenant-b")
                    )
                    self.assertEqual(response.status_code, 404, response.text)

    def test_cross_tenant_export_is_not_found(self) -> None:
        workspace, app = self._app()
        with TestClient(app) as client:
            self._seed(workspace, client)
            created = client.post(
                "/v1/parse/documents/doc-tenant-a/export-jobs",
                json={"dataset": "reader", "format": "json"},
                headers=self._headers("tenant-a"),
            )
            self.assertIn(created.status_code, (200, 201, 202), created.text)
            export_id = created.json().get("export_id") or created.json().get("id")
            self.assertTrue(export_id)

            foreign = client.get(
                f"/v1/parse/export-jobs/{export_id}", headers=self._headers("tenant-b")
            )
            own = client.get(
                f"/v1/parse/export-jobs/{export_id}", headers=self._headers("tenant-a")
            )

        self.assertEqual(own.status_code, 200)
        self.assertEqual(foreign.status_code, 404, foreign.text)

    def test_metrics_are_scoped_to_the_resolved_tenant(self) -> None:
        workspace, app = self._app()
        with TestClient(app) as client:
            self._seed(workspace, client)
            response = client.get(
                "/v1/parse/metrics", headers=self._headers("tenant-a")
            )
            dashboard = client.get(
                "/v1/parse/dashboard", headers=self._headers("tenant-a")
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(dashboard.json()["tenant_id"], "tenant-a")

    def test_publication_pointer_is_per_tenant(self) -> None:
        workspace, app = self._app()
        with TestClient(app) as client:
            self._seed(workspace, client)
            for tenant in ("tenant-a", "tenant-b"):
                path = f"/v1/parse/documents/doc-{tenant}/publication"
                deadline = time.monotonic() + 5
                while True:
                    response = client.get(path, headers=self._headers(tenant))
                    self.assertEqual(response.status_code, 200, response.text)
                    body = response.json()
                    if body["consumer_signal"] == "published" or time.monotonic() >= deadline:
                        break
                    time.sleep(0.01)
                self.assertEqual(body["tenant_id"], tenant)
                self.assertEqual(body["consumer_signal"], "published", body)
                self.assertIsNotNone(body["active_run_id"])

                other_tenant = "tenant-b" if tenant == "tenant-a" else "tenant-a"
                foreign = client.get(path, headers=self._headers(other_tenant)).json()
                self.assertEqual(foreign["tenant_id"], other_tenant)
                self.assertEqual(foreign["consumer_signal"], "not_ready")
                self.assertIsNone(foreign["active_run_id"])


class TrustedBoundaryTests(TenantBoundaryTestCase):
    """The default mode keeps working: the host owns authentication."""

    tenant_scope = TENANT_SCOPE_TRUSTED

    def test_request_tenant_parameter_still_selects_the_tenant(self) -> None:
        workspace, app = self._app()
        with TestClient(app) as client:
            self._seed(workspace, client)
            response = client.get(
                "/v1/parse/jobs",
                params={"tenant_id": "tenant-a"},
                headers={"x-api-key": self.api_key},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual({job["doc_id"] for job in response.json()["items"]}, {"doc-tenant-a"})
        self.assertEqual(response.headers["x-parsecore-tenant-source"], SOURCE_REQUEST_PARAMETER)

    def test_mismatch_is_still_refused_when_the_host_does_authenticate(self) -> None:
        _workspace, app = self._app()
        with TestClient(app) as client:
            response = client.get(
                "/v1/parse/jobs",
                params={"tenant_id": "tenant-b"},
                headers=self._headers("tenant-a"),
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], ERROR_TENANT_MISMATCH)

    def test_unclaimed_request_keeps_the_historical_document_scoped_view(self) -> None:
        # The embedded host may forward an id it already learned without
        # restating the tenant; that must keep working, because the claim — not
        # an implicit default tenant — is what the boundary enforces.
        workspace, app = self._app()
        with TestClient(app) as client:
            ids = self._seed(workspace, client)
            listed = client.get(
                "/v1/parse/jobs",
                params={"doc_id": "doc-tenant-a"},
                headers={"x-api-key": self.api_key},
            )
            job = client.get(
                f"/v1/parse/jobs/{ids['job-tenant-a']}", headers={"x-api-key": self.api_key}
            )
            usage = client.get(
                "/v1/parse/quotas/usage", headers={"x-api-key": self.api_key}
            )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual({item["doc_id"] for item in listed.json()["items"]}, {"doc-tenant-a"})
        self.assertEqual(job.status_code, 200)
        self.assertEqual(job.json()["tenant_id"], "tenant-a")
        self.assertEqual(usage.status_code, 200)
        self.assertGreaterEqual(usage.json()["total_jobs"], 1)

    def test_claimed_request_cannot_read_another_tenants_job(self) -> None:
        # The compatibility path above must not become a hole: as soon as the
        # request claims a tenant, ownership is enforced.
        workspace, app = self._app()
        with TestClient(app) as client:
            ids = self._seed(workspace, client)
            foreign = client.get(
                f"/v1/parse/jobs/{ids['job-tenant-a']}", headers=self._headers("tenant-b")
            )

        self.assertEqual(foreign.status_code, 404)

    def test_cross_tenant_events_are_filtered_once_a_tenant_is_claimed(self) -> None:
        workspace, app = self._app()
        with TestClient(app) as client:
            self._seed(workspace, client)
            as_a = client.get("/v1/parse/events", headers=self._headers("tenant-a")).json()
            as_b = client.get("/v1/parse/events", headers=self._headers("tenant-b")).json()

        # Each tenant sees only its own events; neither list contains the other's
        # document events.
        self.assertTrue(all("tenant-b" not in str(event) for event in as_a["events"]))
        self.assertTrue(all("tenant-a" not in str(event) for event in as_b["events"]))


if __name__ == "__main__":
    unittest.main()
