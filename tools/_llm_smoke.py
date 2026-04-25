"""One-shot smoke test for the DashScope LLM provider.

Run:
    set PARSECORE_LLM_API_KEY=...; python tools/_llm_smoke.py
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from parsecore.config import load_settings  # noqa: E402
from parsecore.llm import build_llm_client  # noqa: E402


def main() -> int:
    settings = load_settings(ROOT / "parsecore.toml")
    overridden = dataclasses.replace(settings.providers.llm, enabled=True)
    client = build_llm_client(overridden)
    if client is None:
        print("client not built; provider disabled", file=sys.stderr)
        return 1
    prompt = (
        "Split this paragraph into structurally coherent sub-paragraphs and "
        "respond ONLY with a JSON array of strings.\n\n"
        "PARAGRAPH:\nNOTE: Do A. (1) Step one. (2) Step two."
    )
    response = client.complete(
        prompt,
        system="You output strict JSON arrays of strings.",
    )
    print("response:")
    print(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
