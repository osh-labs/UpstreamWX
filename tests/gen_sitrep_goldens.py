"""Regenerate the committed SITREP golden files from the test scenarios.

Run after an intentional renderer format change::

    .venv/bin/python tests/gen_sitrep_goldens.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_sitrep_render import CASES, GOLDEN_DIR  # noqa: E402
from upstreamwx.sitrep import render_md  # noqa: E402


def main() -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for case in CASES:
        kwargs, filename = case()
        (GOLDEN_DIR / filename).write_text(render_md(**kwargs))
        print(f"wrote {GOLDEN_DIR / filename}")


if __name__ == "__main__":
    main()
