from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .config import ParserSettings
from .contracts import ChunkBuilder, ParserAdapter
from .models import Block, Chunk, ParseRequest


@dataclass(slots=True, frozen=True)
class PipelineStageSpec:
    name: str
    execution: str = "runtime"
    enabled_by_default: bool = True
    option_keys: tuple[str, ...] = ()
    requires_remote_services: bool = False
    enable_option_path: str | None = None
    allow_failure: bool = True


@dataclass(slots=True, frozen=True)
class PipelineCapabilities:
    pipeline_name: str
    format_name: str
    backend_name: str
    parser_name: str
    supports_parse: bool = True
    supports_rechunk: bool = True
    supports_reembed: bool = True
    stage_names: tuple[str, ...] = ()
    parser_backed_stage_names: tuple[str, ...] = ()
    chunker_name: str = "artifact-chunker"
    option_keys: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class DocumentArtifactItem:
    item_id: str
    block_ids: tuple[str, ...]
    kind: str
    text: str
    page_number: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ParsedDocumentArtifact:
    doc_id: str
    pipeline_name: str
    parser_name: str
    backend_name: str
    format_name: str
    source_path: str
    options_hash: str
    blocks: tuple[Block, ...]
    items: tuple[DocumentArtifactItem, ...]
    metadata: dict[str, Any] = field(default_factory=dict)


class EnrichmentStage(Protocol):
    spec: PipelineStageSpec

    def run(
        self,
        *,
        request: ParseRequest,
        document: ParsedDocumentArtifact,
    ) -> ParsedDocumentArtifact: ...


class ArtifactChunker(Protocol):
    name: str

    def build(self, *, document: ParsedDocumentArtifact) -> Sequence[Chunk]: ...


@dataclass(slots=True, frozen=True)
class PipelineRegistration:
    pipeline_name: str
    format_name: str
    backend_name: str
    parser_name: str
    media_types: tuple[str, ...]
    extensions: tuple[str, ...]
    options: Mapping[str, Any]
    parser: ParserAdapter
    stage_specs: tuple[PipelineStageSpec, ...]
    capabilities: PipelineCapabilities


@dataclass(slots=True)
class PipelineCache:
    _pipelines: dict[str, "DocumentPipeline"] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def get_or_create(self, *, key: str, factory: callable) -> "DocumentPipeline":
        pipeline = self._pipelines.get(key)
        if pipeline is not None:
            self.hits += 1
            return pipeline
        self.misses += 1
        pipeline = factory()
        self._pipelines[key] = pipeline
        return pipeline

    def describe(self) -> dict[str, int]:
        return {
            "size": len(self._pipelines),
            "hits": self.hits,
            "misses": self.misses,
        }


class PipelineProvenanceStage:
    spec = PipelineStageSpec(
        name="pipeline-provenance",
        execution="runtime",
        enabled_by_default=True,
    )

    def __init__(self, registration: PipelineRegistration) -> None:
        self._registration = registration

    def run(
        self,
        *,
        request: ParseRequest,
        document: ParsedDocumentArtifact,
    ) -> ParsedDocumentArtifact:
        metadata = dict(document.metadata)
        metadata.update(
            {
                "pipeline_name": self._registration.pipeline_name,
                "parser_name": self._registration.parser_name,
                "backend_name": self._registration.backend_name,
                "format_name": self._registration.format_name,
                "request_options": _json_safe_mapping(request.options),
                "registered_options": _json_safe_mapping(self._registration.options),
                "active_stages": [spec.name for spec in self._registration.stage_specs],
                "parser_backed_stages": [
                    spec.name for spec in self._registration.stage_specs if spec.execution == "parser-backed"
                ],
            }
        )
        return replace(document, metadata=metadata)


class DocumentSummaryStage:
    spec = PipelineStageSpec(
        name="document-summary",
        execution="runtime",
        enabled_by_default=True,
    )

    def run(
        self,
        *,
        request: ParseRequest,
        document: ParsedDocumentArtifact,
    ) -> ParsedDocumentArtifact:
        metadata = dict(document.metadata)
        role_counts: dict[str, int] = {}
        page_numbers: set[int] = set()
        for item in document.items:
            role_counts[item.kind] = role_counts.get(item.kind, 0) + 1
            if item.page_number is not None:
                page_numbers.add(item.page_number)
        metadata["summary"] = {
            "block_count": len(document.blocks),
            "item_count": len(document.items),
            "page_count": len(page_numbers),
            "item_kinds": role_counts,
        }
        return replace(document, metadata=metadata)


class TableStructureStage:
    spec = PipelineStageSpec(
        name="table-structure",
        execution="runtime",
        enabled_by_default=False,
        option_keys=(
            "enrichment.table_structure.enabled",
            "enrichment.table_structure.header_rows",
            "enrichment.table_structure.output_format",
        ),
        enable_option_path="enrichment.table_structure.enabled",
        allow_failure=True,
    )

    def __init__(self, registration: PipelineRegistration) -> None:
        self._registration = registration

    def run(
        self,
        *,
        request: ParseRequest,
        document: ParsedDocumentArtifact,
    ) -> ParsedDocumentArtifact:
        effective_options = _effective_options(self._registration.options, request.options)
        header_rows = max(1, _as_int(_lookup_option_path(effective_options, "enrichment.table_structure.header_rows"), default=1))
        output_format = _as_str(
            _lookup_option_path(effective_options, "enrichment.table_structure.output_format"),
            default="markdown",
        ).lower()

        updated_items: list[DocumentArtifactItem] = []
        enriched_items = 0
        skipped_items = 0
        for item in document.items:
            if str(item.kind).strip().lower() != "table":
                updated_items.append(item)
                continue

            cells = _normalize_table_cells(item.metadata.get("cells"))
            if not cells:
                skipped_items += 1
                updated_items.append(item)
                continue

            rendered_text = _render_table_text(
                cells,
                header_rows=header_rows,
                output_format=output_format,
            )
            metadata = dict(item.metadata)
            enrichment = dict(metadata.get("enrichment") or {})
            enrichment["table_structure"] = {
                "header_rows": header_rows,
                "output_format": output_format,
                "row_count": len(cells),
                "col_count": max((len(row) for row in cells), default=0),
            }
            metadata["enrichment"] = enrichment
            metadata["rendered_text"] = rendered_text
            if output_format == "markdown":
                metadata["table_markdown"] = rendered_text
            updated_items.append(
                replace(
                    item,
                    text=rendered_text,
                    metadata=metadata,
                )
            )
            enriched_items += 1

        metadata = dict(document.metadata)
        metadata["table_structure"] = {
            "enabled": True,
            "enriched_items": enriched_items,
            "skipped_items": skipped_items,
            "output_format": output_format,
            "header_rows": header_rows,
        }
        return replace(document, items=tuple(updated_items), metadata=metadata)


class ArtifactBackedChunker:
    name = "artifact-chunker"

    def __init__(self, *, base_builder: ChunkBuilder) -> None:
        self._base_builder = base_builder

    def build(self, *, document: ParsedDocumentArtifact) -> Sequence[Chunk]:
        base_chunks = tuple(
            self._base_builder.build(doc_id=document.doc_id, blocks=document.blocks)
        )
        if not base_chunks:
            return ()
        item_by_block_id = {
            item.block_ids[0]: item
            for item in document.items
            if len(item.block_ids) == 1
        }
        enriched: list[Chunk] = []
        for chunk in base_chunks:
            semantic_role = chunk.semantic_role
            text = chunk.text
            if len(chunk.block_ids) == 1:
                item = item_by_block_id.get(chunk.block_ids[0])
                if item is not None:
                    semantic_role = str(item.metadata.get("semantic_role") or semantic_role)
                    rendered_text = item.metadata.get("rendered_text")
                    text = str(rendered_text or item.text or text)
            enriched.append(replace(chunk, semantic_role=semantic_role, text=text))
        return tuple(enriched)


class DocumentPipeline:
    def __init__(
        self,
        *,
        registration: PipelineRegistration,
        chunker: ArtifactChunker,
        runtime_stages: Sequence[EnrichmentStage],
    ) -> None:
        self.registration = registration
        self.parser = registration.parser
        self.chunker = chunker
        self.runtime_stages = tuple(runtime_stages)

    @property
    def name(self) -> str:
        return self.registration.pipeline_name

    @property
    def capabilities(self) -> PipelineCapabilities:
        return self.registration.capabilities

    def validate(self, *, request: ParseRequest, purpose: str = "parse") -> None:
        normalized_purpose = str(purpose or "parse").strip().lower()
        if normalized_purpose == "parse" and not self.capabilities.supports_parse:
            raise RuntimeError(f"pipeline {self.name!r} does not support parse")
        if normalized_purpose == "rechunk" and not self.capabilities.supports_rechunk:
            raise RuntimeError(f"pipeline {self.name!r} does not support rechunk")
        if normalized_purpose == "reembed" and not self.capabilities.supports_reembed:
            raise RuntimeError(f"pipeline {self.name!r} does not support re-embed")

    def parse_blocks(self, *, request: ParseRequest) -> tuple[Block, ...]:
        self.validate(request=request, purpose="parse")
        return tuple(self.parser.parse(request))

    def build_document(
        self,
        *,
        request: ParseRequest,
        blocks: Sequence[Block],
    ) -> ParsedDocumentArtifact:
        items = tuple(_build_document_items(blocks))
        document = ParsedDocumentArtifact(
            doc_id=request.doc_id,
            pipeline_name=self.registration.pipeline_name,
            parser_name=self.registration.parser_name,
            backend_name=self.registration.backend_name,
            format_name=self.registration.format_name,
            source_path=request.file_path,
            options_hash=_options_hash(_effective_options(self.registration.options, request.options)),
            blocks=tuple(blocks),
            items=items,
            metadata={
                "normalized_items": len(items),
            },
        )
        active_runtime_stages: list[str] = []
        skipped_runtime_stages: list[str] = []
        failed_runtime_stages: list[dict[str, str]] = []
        for stage in self.runtime_stages:
            if not self._is_runtime_stage_enabled(stage=stage, request=request):
                skipped_runtime_stages.append(stage.spec.name)
                continue
            try:
                document = stage.run(request=request, document=document)
                active_runtime_stages.append(stage.spec.name)
            except Exception as exc:
                if not stage.spec.allow_failure:
                    raise
                failed_runtime_stages.append({"stage": stage.spec.name, "error": str(exc)})
        metadata = dict(document.metadata)
        metadata["active_runtime_stages"] = active_runtime_stages
        metadata["skipped_runtime_stages"] = skipped_runtime_stages
        metadata["failed_runtime_stages"] = failed_runtime_stages
        document = replace(document, metadata=metadata)
        return document

    def build_chunks(
        self,
        *,
        request: ParseRequest,
        blocks: Sequence[Block],
    ) -> tuple[Chunk, ...]:
        self.validate(
            request=request,
            purpose="rechunk" if str(request.options.get("mode", "")).strip().lower() == "rerun_chunks_only" else "parse",
        )
        document = self.build_document(request=request, blocks=blocks)
        return tuple(self.chunker.build(document=document))

    def _is_runtime_stage_enabled(self, *, stage: EnrichmentStage, request: ParseRequest) -> bool:
        spec = stage.spec
        if spec.execution != "runtime":
            return False
        effective_options = _effective_options(self.registration.options, request.options)
        if spec.enable_option_path:
            resolved = _lookup_option_path(effective_options, spec.enable_option_path)
            if resolved is not None:
                return _as_bool(resolved)
        return bool(spec.enabled_by_default)


class PipelineRegistry:
    def __init__(
        self,
        *,
        registrations: Sequence[PipelineRegistration],
        chunk_builder: ChunkBuilder,
        cache: PipelineCache | None = None,
    ) -> None:
        self.registrations = tuple(registrations)
        self._chunk_builder = chunk_builder
        self.cache = cache or PipelineCache()

    def warmup(self) -> None:
        for registration in self.registrations:
            self._get_or_create_pipeline(registration=registration, request_options={})

    def resolve(self, request: ParseRequest, *, purpose: str = "parse") -> DocumentPipeline:
        suffix = Path(request.file_path).suffix.lower()
        for registration in self.registrations:
            if registration.parser.supports(media_type=request.media_type, suffix=suffix):
                pipeline = self._get_or_create_pipeline(
                    registration=registration,
                    request_options=request.options,
                )
                pipeline.validate(request=request, purpose=purpose)
                return pipeline
        raise LookupError(
            f"No pipeline registered for media_type={request.media_type!r}, suffix={suffix!r}"
        )

    def describe(self) -> dict[str, Any]:
        return {
            "pipelines": [
                {
                    "name": registration.pipeline_name,
                    "format": registration.format_name,
                    "backend": registration.backend_name,
                    "parser": registration.parser_name,
                    "media_types": list(registration.media_types),
                    "extensions": list(registration.extensions),
                    "stages": [spec.name for spec in registration.stage_specs],
                    "runtime_stages": [spec.name for spec in registration.stage_specs if spec.execution == "runtime"],
                    "parser_backed_stages": [
                        spec.name for spec in registration.stage_specs if spec.execution == "parser-backed"
                    ],
                    "chunker": registration.capabilities.chunker_name,
                    "option_keys": list(registration.capabilities.option_keys),
                    "options": _json_safe_mapping(registration.options),
                }
                for registration in self.registrations
            ],
            "cache": self.cache.describe(),
        }

    def _get_or_create_pipeline(
        self,
        *,
        registration: PipelineRegistration,
        request_options: Mapping[str, Any],
    ) -> DocumentPipeline:
        effective_options = _effective_options(registration.options, request_options)
        cache_key = f"{registration.pipeline_name}:{_options_hash(effective_options)}"
        return self.cache.get_or_create(
            key=cache_key,
            factory=lambda: DocumentPipeline(
                registration=registration,
                chunker=ArtifactBackedChunker(base_builder=self._chunk_builder),
                runtime_stages=(
                    PipelineProvenanceStage(registration),
                    TableStructureStage(registration),
                    DocumentSummaryStage(),
                ),
            ),
        )


def build_pipeline_registry(
    *,
    parser_settings: Sequence[ParserSettings],
    parsers: Sequence[ParserAdapter],
    chunk_builder: ChunkBuilder,
) -> PipelineRegistry:
    registrations: list[PipelineRegistration] = []
    for settings, parser in zip(parser_settings, parsers, strict=True):
        format_name = _infer_format_name(settings)
        backend_name = _infer_backend_name(settings)
        stage_specs = _stage_specs_for_parser(settings)
        capabilities = PipelineCapabilities(
            pipeline_name=f"{settings.name}/default",
            format_name=format_name,
            backend_name=backend_name,
            parser_name=settings.name,
            stage_names=tuple(spec.name for spec in stage_specs),
            parser_backed_stage_names=tuple(
                spec.name for spec in stage_specs if spec.execution == "parser-backed"
            ),
            chunker_name=ArtifactBackedChunker.name,
            option_keys=_flatten_option_keys(settings.options),
        )
        registrations.append(
            PipelineRegistration(
                pipeline_name=capabilities.pipeline_name,
                format_name=format_name,
                backend_name=backend_name,
                parser_name=settings.name,
                media_types=settings.media_types,
                extensions=settings.extensions,
                options=settings.options,
                parser=parser,
                stage_specs=stage_specs,
                capabilities=capabilities,
            )
        )
    return PipelineRegistry(registrations=registrations, chunk_builder=chunk_builder)


def _build_document_items(blocks: Sequence[Block]) -> list[DocumentArtifactItem]:
    items: list[DocumentArtifactItem] = []
    for position, block in enumerate(blocks, start=1):
        metadata = dict(block.metadata)
        semantic_role = str(metadata.get("semantic_role") or block.type.value)
        items.append(
            DocumentArtifactItem(
                item_id=f"itm-{position}",
                block_ids=(block.block_id,),
                kind=semantic_role,
                text=block.content,
                page_number=_safe_int(metadata.get("page")),
                metadata=metadata,
                provenance={
                    "block_type": block.type.value,
                    "parser": str(metadata.get("parser") or ""),
                    "position": position,
                },
            )
        )
    return items


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _infer_format_name(settings: ParserSettings) -> str:
    normalized_name = settings.name.strip().lower()
    if normalized_name == "pdf-text":
        return "pdf"
    if normalized_name == "docx-native":
        return "docx"
    if normalized_name == "image-ocr":
        return "image"
    if normalized_name == "text-native":
        return "text"
    if settings.extensions:
        return settings.extensions[0].lstrip(".").lower() or normalized_name
    return normalized_name


def _infer_backend_name(settings: ParserSettings) -> str:
    normalized_name = settings.name.strip().lower()
    if normalized_name == "pdf-text":
        return "native-text"
    if normalized_name == "docx-native":
        return "native-xml"
    if normalized_name == "image-ocr":
        return "ocr"
    if normalized_name == "text-native":
        return "native-text"
    return normalized_name


def _stage_specs_for_parser(settings: ParserSettings) -> tuple[PipelineStageSpec, ...]:
    normalized_name = settings.name.strip().lower()
    specs: list[PipelineStageSpec] = [
        PipelineStageSpec(name="normalized-items", execution="runtime", enabled_by_default=True),
        PipelineProvenanceStage.spec,
        TableStructureStage.spec,
        DocumentSummaryStage.spec,
    ]
    if normalized_name == "pdf-text":
        specs.extend(
            [
                PipelineStageSpec(
                    name="layout-reading-order",
                    execution="parser-backed",
                    enabled_by_default=False,
                    option_keys=(
                        "post_process.layout_reading_order",
                        "enrichment.layout_reading_order.enabled",
                    ),
                ),
                PipelineStageSpec(
                    name="table-detection",
                    execution="parser-backed",
                    enabled_by_default=True,
                    option_keys=(
                        "post_process.dual_channel",
                        "post_process.merge_table_continuations",
                    ),
                ),
                PipelineStageSpec(
                    name="ocr-fallback",
                    execution="parser-backed",
                    enabled_by_default=False,
                    option_keys=(
                        "post_process.ocr_bad_pages",
                        "enable_ocr",
                    ),
                ),
                PipelineStageSpec(
                    name="boundary-refinement",
                    execution="parser-backed",
                    enabled_by_default=False,
                    option_keys=(
                        "post_process.llm_refine_min_length",
                        "post_process.llm_refine_min_markers",
                    ),
                    requires_remote_services=True,
                ),
            ]
        )
    if normalized_name == "image-ocr":
        specs.append(
            PipelineStageSpec(
                name="ocr-extract",
                execution="parser-backed",
                enabled_by_default=True,
            )
        )
    return tuple(specs)


def _flatten_option_keys(options: Mapping[str, Any], *, prefix: str = "") -> tuple[str, ...]:
    collected: list[str] = []
    for key, value in options.items():
        key_text = str(key)
        path = f"{prefix}.{key_text}" if prefix else key_text
        collected.append(path)
        if isinstance(value, Mapping):
            collected.extend(_flatten_option_keys(value, prefix=path))
    return tuple(sorted(dict.fromkeys(collected)))


def _effective_options(
    registered_options: Mapping[str, Any],
    request_options: Mapping[str, Any],
) -> dict[str, Any]:
    merged = _json_safe_mapping(registered_options)
    for key, value in _json_safe_mapping(request_options).items():
        if key == "mode":
            continue
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge_mappings(merged[key], value)
            continue
        merged[key] = value
    return merged


def _merge_mappings(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge_mappings(merged[key], value)
            continue
        merged[key] = value
    return merged


def _options_hash(options: Mapping[str, Any]) -> str:
    payload = json.dumps(_json_safe_mapping(options), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _json_safe_mapping(mapping: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _json_safe_value(value) for key, value in mapping.items()}


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _json_safe_mapping(value)
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    return value


def _lookup_option_path(options: Mapping[str, Any], path: str) -> Any:
    current: Any = options
    for segment in str(path).split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return None
        current = current[segment]
    return current


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return bool(value)


def _as_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, *, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _normalize_table_cells(raw_cells: Any) -> list[list[str]]:
    if not isinstance(raw_cells, Sequence) or isinstance(raw_cells, (str, bytes, bytearray)):
        return []
    rows: list[list[str]] = []
    for row in raw_cells:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes, bytearray)):
            continue
        normalized_row = ["" if cell is None else str(cell).strip() for cell in row]
        if any(cell for cell in normalized_row):
            rows.append(normalized_row)
    return rows


def _render_table_text(
    cells: Sequence[Sequence[str]],
    *,
    header_rows: int,
    output_format: str,
) -> str:
    normalized_rows = _pad_rows(cells)
    if not normalized_rows:
        return ""
    normalized_format = output_format.lower()
    if normalized_format == "tsv":
        return "\n".join("\t".join(row) for row in normalized_rows)

    header_count = min(max(1, header_rows), len(normalized_rows))
    header = normalized_rows[0]
    body = normalized_rows[header_count:]
    separator = ["---"] * len(header)
    lines = [
        f"| {' | '.join(header)} |",
        f"| {' | '.join(separator)} |",
    ]
    if not body and header_count > 1:
        body = normalized_rows[1:]
    for row in body:
        lines.append(f"| {' | '.join(row)} |")
    return "\n".join(lines)


def _pad_rows(rows: Sequence[Sequence[str]]) -> list[list[str]]:
    width = max((len(row) for row in rows), default=0)
    if width <= 0:
        return []
    padded: list[list[str]] = []
    for row in rows:
        normalized_row = [str(cell).replace("\n", " ").strip() for cell in row]
        padded.append(normalized_row + [""] * (width - len(normalized_row)))
    return padded