"""Region assets: keep the picture a table or figure was read from.

待办 1 asks for "先保留，后理解": a table or a figure keeps its original material
or a locator that can rebuild it.  The locator (``source_region``: page, box,
rotation, crop fraction) has been published since the catalog work; this module
writes the material itself, so a reader can look at the region a record came from
instead of trusting a transcription.

Only a ``local://`` object store is written to, the same boundary the upload and
export paths already use, and each asset records its digest and size so a
consumer can verify what it got.  Rendering needs a PDF library; when none is
available the block keeps its locator and gains nothing, which is the honest
outcome rather than an empty file.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Mapping, Sequence

#: Version of the asset descriptor that lands in block metadata.
REGION_ASSET_SCHEMA_VERSION = "2026-09-region-asset-v1"
#: Render resolution for a region asset.  Higher is sharper and slower; 144 dpi is
#: enough to read a table's cells back, which is what the asset is for.
DEFAULT_ASSET_RESOLUTION = 144
#: Region kinds worth keeping a picture of.
_ASSET_REGION_KINDS = {"pdf_table", "pdf_figure"}


@dataclass(frozen=True, slots=True)
class RegionAsset:
    """One written region picture, with what it takes to verify it."""

    block_id: str
    kind: str
    page_number: int
    bbox: tuple[float, float, float, float]
    path: str
    sha256: str
    bytes: int
    width: int
    height: int
    resolution: int

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema_version": REGION_ASSET_SCHEMA_VERSION,
            "kind": self.kind,
            "page_number": self.page_number,
            "bbox": list(self.bbox),
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "width": self.width,
            "height": self.height,
            "resolution": self.resolution,
            "verified": False,
        }


def local_object_store_root(object_store: str | None) -> Path | None:
    """The directory behind a ``local://`` object store, if that is what it is."""

    value = str(object_store or "").strip()
    if not value.startswith("local://"):
        return None
    return Path(value[len("local://"):])


def _region_of(block: Any) -> Mapping[str, Any] | None:
    metadata = getattr(block, "metadata", None) or {}
    region = metadata.get("source_region")
    if not isinstance(region, Mapping):
        return None
    if str(region.get("kind") or "") not in _ASSET_REGION_KINDS:
        return None
    return region


def _bbox_of(region: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    bbox = region.get("bbox")
    if not isinstance(bbox, Sequence) or isinstance(bbox, (str, bytes, bytearray)):
        return None
    if len(bbox) != 4:
        return None
    try:
        x0, top, x1, bottom = (float(value) for value in bbox)
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or bottom <= top:
        return None
    return (x0, top, x1, bottom)


def render_region_png(
    *, source_path: str, page_number: int, bbox: tuple[float, float, float, float], resolution: int
) -> tuple[bytes, int, int] | None:
    """Render one page region to PNG bytes, or ``None`` when it cannot be drawn."""

    document_path = Path(str(source_path or ""))
    if not document_path.exists():
        return None
    try:
        import fitz  # type: ignore
    except ImportError:
        return _render_region_png_with_pdfplumber(
            document_path=document_path,
            page_number=page_number,
            bbox=bbox,
            resolution=resolution,
        )
    try:
        with fitz.open(str(document_path)) as document:
            if page_number < 1 or page_number > document.page_count:
                return None
            page = document.load_page(page_number - 1)
            zoom = max(0.1, float(resolution) / 72.0)
            clip = fitz.Rect(bbox[0], bbox[1], bbox[2], bbox[3])
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
            return pixmap.tobytes("png"), int(pixmap.width), int(pixmap.height)
    except Exception:
        return None


def _render_region_png_with_pdfplumber(
    *,
    document_path: Path,
    page_number: int,
    bbox: tuple[float, float, float, float],
    resolution: int,
) -> tuple[bytes, int, int] | None:
    try:
        import io

        import pdfplumber  # type: ignore
    except ImportError:
        return None
    try:
        with pdfplumber.open(str(document_path)) as document:
            if page_number < 1 or page_number > len(document.pages):
                return None
            page = document.pages[page_number - 1]
            image = page.crop(bbox).to_image(resolution=resolution).original.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue(), int(image.width), int(image.height)
    except Exception:
        return None


def _clamp_to_page(
    bbox: tuple[float, float, float, float], region: Mapping[str, Any]
) -> tuple[float, float, float, float]:
    """Keep the box on the page: a region never extends past its own page.

    A box that reaches past the page edge is clamped rather than padded, so the
    asset is the part of the page that exists and the descriptor says so.
    """

    try:
        width = float(region.get("page_width") or 0.0)
        height = float(region.get("page_height") or 0.0)
    except (TypeError, ValueError):
        return bbox
    if width <= 0.0 or height <= 0.0:
        return bbox
    x0 = min(max(bbox[0], 0.0), width)
    top = min(max(bbox[1], 0.0), height)
    x1 = min(max(bbox[2], 0.0), width)
    bottom = min(max(bbox[3], 0.0), height)
    if x1 <= x0 or bottom <= top:
        return bbox
    return (x0, top, x1, bottom)


def write_region_assets(
    *,
    source_path: str,
    blocks: Sequence[Any],
    root: Path,
    doc_id: str,
    resolution: int = DEFAULT_ASSET_RESOLUTION,
) -> tuple[tuple[Any, ...], tuple[RegionAsset, ...], str | None]:
    """Write one picture per table/figure region and reference it from the block.

    Returns the blocks (with an ``asset`` descriptor added where one was written),
    the assets, and a reason when nothing could be written.
    """

    if not str(source_path or "").strip():
        return tuple(blocks), (), "asset_source_unavailable"
    directory = Path(root) / "regions" / _safe_segment(doc_id)
    written: list[RegionAsset] = []
    updated: list[Any] = []
    failure: str | None = None
    for block in blocks:
        region = _region_of(block)
        if region is None:
            updated.append(block)
            continue
        bbox = _bbox_of(region)
        if bbox is None:
            updated.append(block)
            continue
        bbox = _clamp_to_page(bbox, region)
        rendered = render_region_png(
            source_path=source_path,
            page_number=int(region.get("page_number") or 1),
            bbox=bbox,
            resolution=resolution,
        )
        if rendered is None:
            failure = failure or "asset_render_failed"
            updated.append(block)
            continue
        content, width, height = rendered
        digest = sha256(content).hexdigest()
        temporary: Path | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            # Block ids are reused on reruns.  The bytes, not that mutable id,
            # identify evidence retained by an older published version.
            target = directory / f"{_safe_segment(str(getattr(block, 'block_id', 'region')))}-{digest}.png"
            with NamedTemporaryFile(dir=directory, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
            os.replace(temporary, target)
        except OSError:
            failure = failure or "asset_write_failed"
            updated.append(block)
            continue
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        asset = RegionAsset(
            block_id=str(getattr(block, "block_id", "")),
            kind=str(region.get("kind") or ""),
            page_number=int(region.get("page_number") or 1),
            bbox=bbox,
            path=str(target),
            sha256=digest,
            bytes=len(content),
            width=width,
            height=height,
            resolution=int(resolution),
        )
        written.append(asset)
        metadata = dict(getattr(block, "metadata", None) or {})
        metadata["asset"] = asset.as_payload()
        metadata["asset"]["page_width"] = region.get("page_width")
        metadata["asset"]["page_height"] = region.get("page_height")
        metadata["asset"]["rotation_degrees"] = region.get("rotation_degrees")
        metadata["asset"]["crop_fraction"] = region.get("crop_fraction")
        updated.append(replace(block, metadata=metadata))
    return tuple(updated), tuple(written), failure


def region_assets_manifest(assets: Sequence[RegionAsset]) -> dict[str, Any]:
    """A digest manifest for the written assets, for a run or a report."""

    return {
        "schema_version": REGION_ASSET_SCHEMA_VERSION,
        "count": len(assets),
        "total_bytes": sum(asset.bytes for asset in assets),
        "assets": [
            {
                "block_id": asset.block_id,
                "kind": asset.kind,
                "page_number": asset.page_number,
                "path": asset.path,
                "sha256": asset.sha256,
                "bytes": asset.bytes,
            }
            for asset in assets
        ],
    }


def _safe_segment(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "_"
        for character in str(value or "")
    ).strip("._")
    return cleaned[:120] or "region"


def assets_manifest_json(assets: Sequence[RegionAsset]) -> str:
    return json.dumps(region_assets_manifest(assets), ensure_ascii=False, sort_keys=True)
