from __future__ import annotations

import argparse
import json
import mimetypes
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed or update a live jobcard store document entry backed by a real uploaded file."
    )
    parser.add_argument("--jobcard-backend", required=True, help="Path to the jobcard backend folder")
    parser.add_argument("--jobcard-python", help="Python executable from the jobcard environment")
    parser.add_argument("--upload-dir", required=True, help="Directory used by JOB_CARD_UPLOAD_DIR for uploaded files")
    parser.add_argument("--collection", choices=("documents", "mgmt_documents"), default="documents")
    parser.add_argument("--doc-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--file-path", required=True, help="Source file to register in the live store")
    parser.add_argument("--upload-name", help="Filename to expose under /uploads; defaults to the source filename")
    parser.add_argument("--document-number", help="Logical document number")
    parser.add_argument("--part-number", help="Logical part number")
    parser.add_argument("--revision", default="")
    parser.add_argument("--doc-type", default="REFERENCE")
    parser.add_argument("--ata-chapter", default="")
    parser.add_argument("--manufacturer", default="")
    parser.add_argument("--aircraft-model", default="")
    parser.add_argument("--status", default="ACTIVE")
    parser.add_argument("--related-workcard", action="append", default=[])
    parser.add_argument("--overwrite", action="store_true")
    return parser


def normalize_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    backend_path = Path(args.jobcard_backend).resolve()
    upload_dir = Path(args.upload_dir).resolve()
    source_file = Path(args.file_path).resolve()

    if not backend_path.exists():
        raise SystemExit(f"jobcard backend path does not exist: {backend_path}")
    if not source_file.exists() or not source_file.is_file():
        raise SystemExit(f"source file does not exist: {source_file}")

    upload_dir.mkdir(parents=True, exist_ok=True)
    return backend_path, upload_dir, source_file


def resolve_jobcard_python(args: argparse.Namespace, backend_path: Path) -> Path:
    if args.jobcard_python:
        python_path = Path(args.jobcard_python).resolve()
    else:
        python_path = (backend_path.parent / ".venv" / "Scripts" / "python.exe").resolve()

    if not python_path.exists():
        raise SystemExit(f"jobcard python executable does not exist: {python_path}")
    return python_path


def resolve_mime_type(source_file: Path) -> str:
    guessed, _ = mimetypes.guess_type(source_file.name)
    return guessed or "application/octet-stream"


def copy_into_upload_dir(source_file: Path, upload_dir: Path, upload_name: str) -> Path:
    destination = upload_dir / upload_name
    if source_file.resolve() != destination.resolve():
        shutil.copy2(source_file, destination)
    return destination


def build_entry(args: argparse.Namespace, stored_file: Path, upload_name: str) -> dict[str, Any]:
    file_url = f"/uploads/{upload_name}"
    document_number = args.document_number or stored_file.stem
    part_number = args.part_number or document_number
    manufacturer = args.manufacturer or ""
    mime_type = resolve_mime_type(stored_file)
    uploaded_at = now_iso()

    entry: dict[str, Any] = {
        "id": args.doc_id,
        "title": args.title,
        "documentNumber": document_number,
        "partNumber": part_number,
        "revision": args.revision,
        "docType": args.doc_type,
        "ataChapter": args.ata_chapter,
        "manufacturerName": manufacturer,
        "manufacturer": manufacturer,
        "aircraftModel": args.aircraft_model,
        "status": args.status,
        "relatedWorkCards": list(args.related_workcard),
        "fileUrl": file_url,
        "file": {
            "name": stored_file.name,
            "url": file_url,
            "size": stored_file.stat().st_size,
            "mimeType": mime_type,
            "uploadedAt": uploaded_at,
        },
        "uploadDate": uploaded_at,
    }
    return entry


def upsert_entry(jobcard_python: Path, backend_path: Path, collection: str, entry: dict[str, Any], overwrite: bool) -> dict[str, Any]:
    payload = {
        "collection": collection,
        "entry": entry,
        "overwrite": overwrite,
    }
    inline_code = """
import json
import sys
from store import store

payload = json.loads(sys.argv[1])
collection = payload[\"collection\"]
entry = payload[\"entry\"]
overwrite = payload[\"overwrite\"]

def handler(domain_payload):
    items = list(domain_payload.get(collection, []))
    existing_index = next((index for index, item in enumerate(items) if item.get(\"id\") == entry[\"id\"]), None)

    if existing_index is None:
        items.append(entry)
        action = \"created\"
    else:
        if not overwrite:
            raise SystemExit(f\"document already exists in {collection}: {entry['id']}. Use --overwrite to replace it.\")
        items[existing_index] = {**items[existing_index], **entry}
        action = \"updated\"

    domain_payload[collection] = items
    return {\"action\": action, \"collection\": collection, \"count\": len(items)}

result = store.mutate_domain(\"document\", handler)
print(json.dumps(result, ensure_ascii=False))
""".strip()
    completed = subprocess.run(
        [str(jobcard_python), "-c", inline_code, json.dumps(payload, ensure_ascii=False)],
        cwd=str(backend_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "jobcard store mutation failed"
        raise SystemExit(message)
    stdout = completed.stdout.strip().splitlines()
    if not stdout:
        raise SystemExit("jobcard store mutation produced no output")
    return json.loads(stdout[-1])


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    backend_path, upload_dir, source_file = normalize_paths(args)
    jobcard_python = resolve_jobcard_python(args, backend_path)
    upload_name = args.upload_name or source_file.name
    stored_file = copy_into_upload_dir(source_file, upload_dir, upload_name)

    entry = build_entry(args, stored_file, upload_name)
    result = upsert_entry(jobcard_python, backend_path, args.collection, entry, args.overwrite)
    summary = {
        **result,
        "doc_id": args.doc_id,
        "title": args.title,
        "file_path": str(stored_file),
        "file_url": entry["fileUrl"],
        "mime_type": entry["file"]["mimeType"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())