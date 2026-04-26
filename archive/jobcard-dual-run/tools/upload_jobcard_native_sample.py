from __future__ import annotations

import argparse
import json
import mimetypes
import subprocess
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upload a sample through the native jobcard FastAPI routes using the jobcard Python environment."
    )
    parser.add_argument("--jobcard-backend", required=True, help="Path to the jobcard backend folder")
    parser.add_argument("--jobcard-python", help="Python executable from the jobcard environment")
    parser.add_argument("--collection", choices=("documents", "mgmt_documents"), required=True)
    parser.add_argument("--file-path", required=True)
    parser.add_argument("--account", default="admin")
    parser.add_argument("--password", default="admin123")
    parser.add_argument("--title")
    parser.add_argument("--document-number")
    parser.add_argument("--revision", default="")
    parser.add_argument("--part-number")
    parser.add_argument("--ata-chapter", default="")
    parser.add_argument("--manufacturer-name", default="")
    parser.add_argument("--manufacturer", default="")
    parser.add_argument("--doc-type", default="")
    parser.add_argument("--aircraft-model", default="")
    parser.add_argument("--status", default="ACTIVE")
    parser.add_argument("--department", default="")
    parser.add_argument("--doc-category", default="")
    return parser


def resolve_jobcard_python(args: argparse.Namespace, backend_path: Path) -> Path:
    if args.jobcard_python:
        python_path = Path(args.jobcard_python).resolve()
    else:
        python_path = (backend_path.parent / ".venv" / "Scripts" / "python.exe").resolve()
    if not python_path.exists():
        raise SystemExit(f"jobcard python executable does not exist: {python_path}")
    return python_path


def resolve_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    backend_path = Path(args.jobcard_backend).resolve()
    file_path = Path(args.file_path).resolve()
    if not backend_path.exists():
        raise SystemExit(f"jobcard backend path does not exist: {backend_path}")
    if not file_path.exists() or not file_path.is_file():
        raise SystemExit(f"source file does not exist: {file_path}")
    python_path = resolve_jobcard_python(args, backend_path)
    return backend_path, python_path, file_path


def build_payload(args: argparse.Namespace, file_path: Path) -> dict[str, object]:
    mime_type, _ = mimetypes.guess_type(file_path.name)
    return {
        "collection": args.collection,
        "file_path": str(file_path),
        "file_name": file_path.name,
        "mime_type": mime_type or "application/octet-stream",
        "account": args.account,
        "password": args.password,
        "title": args.title or file_path.stem,
        "documentNumber": args.document_number or file_path.stem,
        "revision": args.revision,
        "partNumber": args.part_number or args.document_number or file_path.stem,
        "ataChapter": args.ata_chapter,
        "manufacturerName": args.manufacturer_name,
        "manufacturer": args.manufacturer,
        "docType": args.doc_type,
        "aircraftModel": args.aircraft_model,
        "status": args.status,
        "department": args.department,
        "docCategory": args.doc_category,
    }


def execute_upload(jobcard_python: Path, backend_path: Path, payload: dict[str, object]) -> dict[str, object]:
    inline_code = """
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app import app

payload = json.loads(sys.argv[1])
client = TestClient(app)

login_response = client.post(
    "/api/v1/auth/login",
    json={"account": payload["account"], "password": payload["password"]},
)
login_response.raise_for_status()
login_json = login_response.json()
token = ((login_json.get("data") or {}).get("accessToken") or (login_json.get("data") or {}).get("token"))
if not token:
    raise SystemExit(f"missing access token in login response: {login_json}")

route = "/api/v1/documents/upload" if payload["collection"] == "documents" else "/api/v1/mgmt-documents/upload"
form_data = {"title": payload["title"], "documentNumber": payload["documentNumber"], "revision": payload["revision"]}
if payload["collection"] == "documents":
    form_data.update(
        {
            "partNumber": payload["partNumber"],
            "ataChapter": payload["ataChapter"],
            "manufacturerName": payload["manufacturerName"],
            "manufacturer": payload["manufacturer"],
            "docType": payload["docType"],
            "aircraftModel": payload["aircraftModel"],
            "status": payload["status"],
        }
    )
else:
    form_data.update({"department": payload["department"], "docCategory": payload["docCategory"]})

with Path(payload["file_path"]).open("rb") as handle:
    upload_response = client.post(
        route,
        headers={"Authorization": f"Bearer {token}"},
        data=form_data,
        files={"file": (payload["file_name"], handle, payload["mime_type"])},
    )
upload_response.raise_for_status()

upload_json = upload_response.json()
document = upload_json.get("data") or upload_json
summary = {
    "collection": payload["collection"],
    "route": route,
    "document": document,
}
print(json.dumps(summary, ensure_ascii=False, indent=2))
""".strip()

    completed = subprocess.run(
        [str(jobcard_python), "-c", inline_code, json.dumps(payload, ensure_ascii=False)],
        cwd=str(backend_path),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "native jobcard upload failed"
        raise SystemExit(message)
    stdout_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not stdout_lines:
        raise SystemExit("native jobcard upload produced no output")
    return json.loads("\n".join(stdout_lines))


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    backend_path, jobcard_python, file_path = resolve_paths(args)
    payload = build_payload(args, file_path)
    result = execute_upload(jobcard_python, backend_path, payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())