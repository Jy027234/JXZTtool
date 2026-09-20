"""Host identity and tenant boundary (P0-07).

The problem this module fixes: every tenant-scoped endpoint read
``tenant_id`` straight out of the request (`?tenant_id=...`, a form field, or a
JSON body), and the shared API key says nothing about *which* tenant the caller
is.  A caller who guessed another tenant's `doc_id` / `job_id` / `export_id`
could read it by passing that tenant's id.

The boundary has two documented modes:

``trusted``
    The deployment is embedded in a host or runs on a trusted network.  The
    request's ``tenant_id`` is accepted as-is — this is the historical
    behaviour, kept as the default so existing deployments do not change
    meaning.  The resolution source is recorded for audit.
``enforced``
    The host authenticates the user and injects the tenant identity.  A request
    parameter may then only *restate* it: a mismatch is ``403``, and a request
    with no authenticated identity is rejected rather than falling back to a
    shared default tenant.

Neither mode builds an account system: ParseCore records who identified the
tenant and refuses to let a request body overrule that identity.  Resource
ownership is checked against the persisted tenant of the resource itself, so
knowing another tenant's id is not enough.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from starlette.requests import Request

TENANT_SCOPE_TRUSTED = "trusted"
TENANT_SCOPE_ENFORCED = "enforced"
TENANT_SCOPE_MODES = (TENANT_SCOPE_TRUSTED, TENANT_SCOPE_ENFORCED)

DEFAULT_TENANT = "default"

#: Resolution sources, in audit order of authority.
SOURCE_HOST_AUTHENTICATED = "host_authenticated"
SOURCE_REQUEST_PARAMETER = "request_parameter"
SOURCE_DEFAULT = "default"

ERROR_TENANT_MISMATCH = "tenant_mismatch"
ERROR_TENANT_IDENTITY_REQUIRED = "tenant_identity_required"


class TenantScopeError(RuntimeError):
    """The request cannot be attributed to a tenant under the active policy."""

    def __init__(self, *, code: str, message: str, status_code: int = 403) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(slots=True, frozen=True)
class TenantScopeSettings:
    mode: str = TENANT_SCOPE_TRUSTED
    header: str = "x-parsecore-tenant"

    @property
    def enforced(self) -> bool:
        return self.mode == TENANT_SCOPE_ENFORCED

    def as_payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "identity_header": self.header,
            "supported_modes": list(TENANT_SCOPE_MODES),
            "request_parameter_may_choose_tenant": not self.enforced,
        }


def tenant_scope_settings(runtime: Any) -> TenantScopeSettings:
    """Read the tenant-scope policy from runtime settings."""

    settings = getattr(runtime, "settings", None)
    runtime_settings = getattr(settings, "runtime", None)
    mode = str(getattr(runtime_settings, "tenant_scope", TENANT_SCOPE_TRUSTED) or "").strip().lower()
    header = str(
        getattr(runtime_settings, "tenant_header", "x-parsecore-tenant") or ""
    ).strip().lower()
    if mode not in TENANT_SCOPE_MODES:
        mode = TENANT_SCOPE_TRUSTED
    if not header:
        header = "x-parsecore-tenant"
    return TenantScopeSettings(mode=mode, header=header)


@dataclass(slots=True, frozen=True)
class TenantResolution:
    """The tenant a request is attributed to, and why."""

    tenant_id: str
    source: str
    authenticated_tenant_id: str | None
    requested_tenant_id: str | None
    scope: TenantScopeSettings

    def as_payload(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "source": self.source,
            "authenticated_tenant_id": self.authenticated_tenant_id,
            "requested_tenant_id": self.requested_tenant_id,
            "scope": self.scope.as_payload(),
        }


def _normalize_tenant(value: Any) -> str:
    return str(value or "").strip()


def resolve_tenant(
    *,
    scope: TenantScopeSettings,
    authenticated_tenant_id: Any = None,
    requested_tenant_id: Any = None,
) -> TenantResolution:
    """Attribute a request to a tenant, or refuse to.

    This is the single decision point; HTTP headers, query parameters, form
    fields and JSON bodies all end up here so the two modes cannot drift.
    """

    authenticated = _normalize_tenant(authenticated_tenant_id)
    requested = _normalize_tenant(requested_tenant_id)

    if authenticated and requested and authenticated != requested:
        raise TenantScopeError(
            code=ERROR_TENANT_MISMATCH,
            message=(
                "the request asks for tenant "
                f"{requested!r} but the authenticated identity is {authenticated!r}"
            ),
            status_code=403,
        )

    if authenticated:
        return TenantResolution(
            tenant_id=authenticated,
            source=SOURCE_HOST_AUTHENTICATED,
            authenticated_tenant_id=authenticated,
            requested_tenant_id=requested or None,
            scope=scope,
        )

    if requested:
        if scope.enforced:
            raise TenantScopeError(
                code=ERROR_TENANT_IDENTITY_REQUIRED,
                message=(
                    "this deployment requires a host-authenticated tenant; a request "
                    f"parameter ({requested!r}) must not choose it"
                ),
                status_code=403,
            )
        return TenantResolution(
            tenant_id=requested,
            source=SOURCE_REQUEST_PARAMETER,
            authenticated_tenant_id=None,
            requested_tenant_id=requested,
            scope=scope,
        )

    if scope.enforced:
        raise TenantScopeError(
            code=ERROR_TENANT_IDENTITY_REQUIRED,
            message=(
                "this deployment requires a host-authenticated tenant; no identity "
                "was provided"
            ),
            status_code=403,
        )

    return TenantResolution(
        tenant_id=DEFAULT_TENANT,
        source=SOURCE_DEFAULT,
        authenticated_tenant_id=None,
        requested_tenant_id=None,
        scope=scope,
    )


def tenant_error_response(request: Request, error: TenantScopeError):
    """HTTP response for a refused tenant attribution."""

    from starlette.responses import JSONResponse

    from .api_support import _trace_id_for_request

    trace_id = _trace_id_for_request(request)
    payload = {
        "error": error.code,
        "code": error.code,
        "message": str(error),
        "trace_id": trace_id,
        "detail": {
            "tenant_scope": "enforced" if error.code != ERROR_TENANT_MISMATCH else "enforced",
            "identity_header": request.headers.get("x-parsecore-tenant"),
        },
    }
    response = JSONResponse(payload, status_code=error.status_code)
    response.headers.setdefault("x-trace-id", trace_id)
    return response


def resolve_request_tenant(
    request: Request, *, requested_tenant_id: Any = None
) -> TenantResolution:
    """Resolve the tenant for an HTTP request using the app's configured scope."""

    runtime = getattr(request.app.state, "runtime", None)
    scope = tenant_scope_settings(runtime)
    authenticated = request.headers.get(scope.header)
    return resolve_tenant(
        scope=scope,
        authenticated_tenant_id=authenticated,
        requested_tenant_id=requested_tenant_id,
    )


def request_tenant_id(request: Request, requested_tenant_id: Any = None) -> str:
    """Tenant id for a handler.

    ``requested_tenant_id`` is whatever the handler read from the query string,
    form or body; it is a *request* for a tenant, not an identity.  When the
    middleware already attributed the request, an explicit request value is
    re-checked against it instead of silently overriding it — otherwise a handler
    could re-open the hole the middleware just closed.
    """

    cached = getattr(request.state, "tenant_resolution", None)
    if isinstance(cached, TenantResolution) and requested_tenant_id is None:
        return cached.tenant_id
    resolution = resolve_request_tenant(request, requested_tenant_id=requested_tenant_id)
    if isinstance(cached, TenantResolution) and cached.tenant_id != resolution.tenant_id:
        raise TenantScopeError(
            code=ERROR_TENANT_MISMATCH,
            message=(
                "the request asks for tenant "
                f"{resolution.tenant_id!r} but this request is attributed to "
                f"{cached.tenant_id!r}"
            ),
            status_code=403,
        )
    return resolution.tenant_id


def resource_tenant_id(resource: Any) -> str:
    """Persisted tenant of a resource, normalized for comparison."""

    return _normalize_tenant(getattr(resource, "tenant_id", None)) or DEFAULT_TENANT


def tenant_matches(resource: Any, tenant_id: str) -> bool:
    """Whether a loaded resource belongs to the resolved tenant.

    Resources without a tenant attribute are treated as belonging to the
    default tenant, which is how the stores normalize a missing tenant.
    """

    return resource_tenant_id(resource) == (_normalize_tenant(tenant_id) or DEFAULT_TENANT)


def explicit_request_tenant_id(request: Request, requested_tenant_id: Any = None) -> str | None:
    """Tenant filter for observability queries, or ``None`` for "do not filter".

    A bare `GET /v1/parse/events` (no identity header, no parameter) carries no
    tenant claim, and the historical behaviour is an unfiltered view for the
    operator.  Once a request *does* claim a tenant — through the host identity
    header or a parameter — the filter applies, so an enforced deployment cannot
    read another tenant's events.
    """

    if requested_tenant_id in (None, ""):
        cached = getattr(request.state, "tenant_resolution", None)
        if isinstance(cached, TenantResolution):
            return None if cached.source == SOURCE_DEFAULT else cached.tenant_id
    return request_tenant_id(request, requested_tenant_id)


TENANT_BOUNDARY_POLICY: Mapping[str, str] = {
    "trusted": (
        "The calling host owns authentication and tenant injection. ParseCore "
        "accepts the request tenant_id as the tenant and records the resolution "
        "source for audit. Do not expose this mode to untrusted callers."
    ),
    "enforced": (
        "The host authenticates the user and injects the tenant identity through "
        "the configured header. A request parameter may only restate it; a "
        "mismatch is 403 tenant_mismatch and a missing identity is 403 "
        "tenant_identity_required. Resource ownership is checked against the "
        "persisted tenant of the resource."
    ),
    "resource_authorization": (
        "A resource is only served when its persisted tenant equals the resolved "
        "tenant. A mismatch returns the same not-found response as a missing "
        "resource so the boundary does not leak existence."
    ),
}


__all__ = [
    "DEFAULT_TENANT",
    "ERROR_TENANT_IDENTITY_REQUIRED",
    "ERROR_TENANT_MISMATCH",
    "SOURCE_DEFAULT",
    "SOURCE_HOST_AUTHENTICATED",
    "SOURCE_REQUEST_PARAMETER",
    "TENANT_BOUNDARY_POLICY",
    "TENANT_SCOPE_ENFORCED",
    "TENANT_SCOPE_MODES",
    "TENANT_SCOPE_TRUSTED",
    "TenantResolution",
    "TenantScopeError",
    "TenantScopeSettings",
    "explicit_request_tenant_id",
    "request_tenant_id",
    "resolve_request_tenant",
    "resolve_tenant",
    "resource_tenant_id",
    "tenant_error_response",
    "tenant_matches",
    "tenant_scope_settings",
]
