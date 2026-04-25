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
        self.config_path = self.root / "parsecore.toml"
        self.config_path.write_text(
            self.config_template.replace("__DB_URL__", database_url),
            encoding="utf-8",
        )
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

    def create_text_file(self, name: str, content: str) -> Path:
        assert self.root is not None
        target = self.root / name
        target.write_text(content, encoding="utf-8")
        return target