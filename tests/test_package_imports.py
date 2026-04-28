from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest


class PackageImportTests(unittest.TestCase):
    def test_root_import_does_not_load_optional_api_stack(self) -> None:
        root = Path(__file__).resolve().parent.parent
        env = os.environ.copy()
        source_path = str(root / "src")
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = source_path if not existing else os.pathsep.join((source_path, existing))
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import parsecore; "
                    "print('parsecore.asgi' in sys.modules); "
                    "print('starlette' in sys.modules)"
                ),
            ],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.splitlines(), ["False", "False"])
