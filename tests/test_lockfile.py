"""requirements.lock mot pyproject.toml.

Låsfilen är det som installeras; pyproject.toml säger vad som är tillåtet.
Ett nytt beroende som bara läggs till i pyproject.toml installeras olåst,
och en låsning utanför spannet går inte att installera alls — pip vägrar
när konstrainten och kravet inte går ihop, och det märks först på Pi:n.
"""
from __future__ import annotations

from pathlib import Path

import pytest

tomllib = pytest.importorskip("tomllib")

from packaging.requirements import Requirement  # noqa: E402
from packaging.utils import canonicalize_name  # noqa: E402
from packaging.version import Version  # noqa: E402

_ROT = Path(__file__).resolve().parents[1]


def _last() -> dict[str, Version]:
    last: dict[str, Version] = {}
    for rad in (_ROT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        rad = rad.split("#", 1)[0].split(";", 1)[0].strip()
        if not rad:
            continue
        namn, _, version = rad.partition("==")
        assert version, f"{rad!r} är inte låst till en exakt version"
        namn = canonicalize_name(namn)
        assert namn not in last, f"{namn} står två gånger i låsfilen"
        last[namn] = Version(version)
    return last


def test_every_dependency_is_locked_inside_its_range() -> None:
    projekt = tomllib.loads((_ROT / "pyproject.toml").read_text(encoding="utf-8"))
    last = _last()

    for krav in map(Requirement, projekt["project"]["dependencies"]):
        namn = canonicalize_name(krav.name)
        assert namn in last, (
            f"{namn} står i pyproject.toml men inte i requirements.lock — "
            "kör tools/las-beroenden.py"
        )
        assert krav.specifier.contains(last[namn], prereleases=True), (
            f"{namn}=={last[namn]} ligger utanför {krav.specifier} i pyproject.toml"
        )
