"""Default prompt texts. Each seeds a setting that the owner can edit in the
web admin (persona, operating rules, per-skill instructions)."""

from functools import cache
from pathlib import Path

_DIR = Path(__file__).parent


@cache
def load_prompt(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8").strip()
