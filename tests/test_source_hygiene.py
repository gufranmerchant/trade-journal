"""A regex written as "\\b" in a non-raw string silently becomes a backspace character (0x08) and then
matches nothing - twice now this has produced filters that quietly never fired. No source file may
contain a stray control character."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FILES = sorted([*(ROOT / "app").glob("*.py"), *(ROOT / "app" / "static" / "js").glob("*.js"), *(ROOT / "tests").glob("*.py")])


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_control_characters_in_source(path):
    bad = sorted({hex(ord(c)) for c in path.read_text(encoding="utf-8") if ord(c) < 32 and c not in "\n\r\t"})
    assert not bad, f"{path.name} contains control characters {bad} (a mangled \\b / \\t / \\n escape?)"
