from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from parsecore.bootstrap import build_runtime  # noqa: E402


if __name__ == "__main__":
    runtime = build_runtime(ROOT / "parsecore.toml")
    print(json.dumps(runtime.describe(), ensure_ascii=False, indent=2))
