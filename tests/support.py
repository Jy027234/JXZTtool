from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import zipfile


DOCX_XML = """<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>
<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">
  <w:body>
    {paragraphs}
  </w:body>
</w:document>
"""


def build_docx_paragraph(text: str) -> str:
    escaped = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return f"<w:p><w:r><w:t>{escaped}</w:t></w:r></w:p>"


def build_docx_table(rows: list[list[str]]) -> str:
    rendered_rows: list[str] = []
    for row in rows:
        rendered_cells: list[str] = []
        for cell in row:
            escaped = (
                str(cell)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            rendered_cells.append(
                "<w:tc><w:p><w:r><w:t>" + escaped + "</w:t></w:r></w:p></w:tc>"
            )
        rendered_rows.append("<w:tr>" + "".join(rendered_cells) + "</w:tr>")
    return "<w:tbl>" + "".join(rendered_rows) + "</w:tbl>"


def _escape_pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_simple_pdf_bytes(pages: list[list[str]]) -> bytes:
    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"PLACEHOLDER_PAGES")

    page_object_ids: list[int] = []
    font_object_id = 3 + len(pages) * 2

    for page_index, lines in enumerate(pages):
        page_object_id = 3 + page_index * 2
        content_object_id = page_object_id + 1
        page_object_ids.append(page_object_id)
        page_object = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font_object_id} 0 R >> >> /Contents {content_object_id} 0 R >>"
        ).encode("ascii")
        objects.append(page_object)

        stream_lines = [b"BT", b"/F1 12 Tf", b"72 720 Td"]
        for line_index, line in enumerate(lines):
            escaped = _escape_pdf_text(line).encode("latin-1", errors="replace")
            if line_index == 0:
                stream_lines.append(b"(" + escaped + b") Tj")
            else:
                stream_lines.append(b"0 -20 Td")
                stream_lines.append(b"(" + escaped + b") Tj")
        stream_lines.append(b"ET")
        stream = b"\n".join(stream_lines)
        content_object = (
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream"
        )
        objects.append(content_object)

    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    kids = b" ".join(f"{page_object_id} 0 R".encode("ascii") for page_object_id in page_object_ids)
    objects[1] = b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(len(page_object_ids)).encode("ascii") + b" >>"

    chunks = [b"%PDF-1.4\n"]
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(sum(len(chunk) for chunk in chunks))
        chunks.append(f"{index} 0 obj\n".encode("ascii"))
        chunks.append(body)
        chunks.append(b"\nendobj\n")

    xref_offset = sum(len(chunk) for chunk in chunks)
    chunks.append(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    chunks.append(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        chunks.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    chunks.append(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return b"".join(chunks)


class TemporaryWorkspace:
    def __init__(self, config_template: str) -> None:
        self.config_template = config_template
        self._tempdir: TemporaryDirectory[str] | None = None
        self.root: Path | None = None
        self.config_path: Path | None = None

    def __enter__(self) -> "TemporaryWorkspace":
        self._tempdir = TemporaryDirectory(prefix="parsecore-tests-")
        self.root = Path(self._tempdir.name)
        database_path = self.root / "parsecore.db"
        database_url = f"sqlite:///{database_path.as_posix()}"
        upload_root = self.root / "var" / "uploads"
        self.config_path = self.root / "parsecore.toml"
        config = self.config_template.replace("__DB_URL__", database_url)
        config = config.replace("local://./var/uploads", f"local://{upload_root.as_posix()}")
        if "allow_external_file_paths" not in config and "poll_interval_ms" in config:
            config = config.replace(
                "poll_interval_ms = ",
                "allow_external_file_paths = true\npoll_interval_ms = ",
                1,
            )
        self.config_path.write_text(config, encoding="utf-8")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._tempdir is not None:
            self._tempdir.cleanup()

    def create_docx(self, name: str, paragraphs: list[str]) -> Path:
        assert self.root is not None
        target = self.root / name
        document_xml = DOCX_XML.format(paragraphs="".join(build_docx_paragraph(item) for item in paragraphs))
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("word/document.xml", document_xml)
        return target

    def create_docx_with_body(self, name: str, body_xml: str) -> Path:
        assert self.root is not None
        target = self.root / name
        document_xml = DOCX_XML.format(paragraphs=body_xml)
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("word/document.xml", document_xml)
        return target

    def create_text_file(self, name: str, content: str) -> Path:
        assert self.root is not None
        target = self.root / name
        target.write_text(content, encoding="utf-8")
        return target

    def create_pdf(self, name: str, pages: list[list[str]]) -> Path:
        assert self.root is not None
        target = self.root / name
        target.write_bytes(build_simple_pdf_bytes(pages))
        return target
