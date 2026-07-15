from __future__ import annotations

import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest

from parsecore.private_files import (
    ensure_private_directory,
    safe_upload_suffix,
    write_private_bytes,
)


class TestPrivateFiles(unittest.TestCase):
    def test_safe_upload_suffix_rejects_path_and_ads_characters(self) -> None:
        self.assertEqual(safe_upload_suffix("manual.PDF"), ".pdf")
        self.assertEqual(safe_upload_suffix("manual"), ".bin")
        self.assertEqual(safe_upload_suffix("manual.txt:secret"), ".bin")
        self.assertEqual(safe_upload_suffix("manual.reallylongextension"), ".bin")

    def test_private_directory_and_file_are_root_bounded_and_exclusive(self) -> None:
        with TemporaryDirectory(prefix="parsecore-private-files-") as temp_dir:
            root = Path(temp_dir) / "object-store"
            private_dir = ensure_private_directory(
                root / "_api_uploads",
                allowed_root=root,
            )
            private_file = write_private_bytes(private_dir, "doc-123.txt", b"private")

            self.assertEqual(private_file.read_bytes(), b"private")
            self.assertEqual(private_file.parent, private_dir)
            with self.assertRaises(FileExistsError):
                write_private_bytes(private_dir, "doc-123.txt", b"overwrite")

            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(private_dir.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(private_file.stat().st_mode), 0o600)

    def test_private_directory_rejects_symlink_escape(self) -> None:
        with TemporaryDirectory(prefix="parsecore-private-files-") as temp_dir:
            temp_root = Path(temp_dir)
            root = temp_root / "object-store"
            outside = temp_root / "outside"
            root.mkdir()
            outside.mkdir()
            link = root / "_api_uploads"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("directory symlinks are not available in this environment")

            with self.assertRaisesRegex(ValueError, "private_artifact_path_outside_root"):
                ensure_private_directory(link, allowed_root=root)


if __name__ == "__main__":
    unittest.main()
