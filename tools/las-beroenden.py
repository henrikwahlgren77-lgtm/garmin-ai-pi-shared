"""Skriver requirements.lock: exakta versioner för hela beroendeträdet.

    .venv/Scripts/python tools/las-beroenden.py      (Windows)
    .venv/bin/python tools/las-beroenden.py          (Pi:n)

Läser pyproject.toml:s beroenden och följer dem rekursivt genom det som är
installerat i venv:et skriptet körs med. Låsfilen blir alltså precis den
uppsättning som finns här. Kör testsviten efteråt: det är den kombinationen
som ska till Pi:n.

Varför en låsfil: pyproject.toml anger spann, och pip tar det senaste inom
spannet vid varje installation. Datorn och Pi:n hade på det sättet glidit
isär över tre majorgränser — pydantic 2 mot 1, anthropic 1.0 mot 0.122,
starlette 1.6 mot 0.50. Testerna kördes mot en uppsättning, tjänsten mot en
annan.

Låsfilen gäller två miljöer: Pi:n (Linux) och en Windows-dator. Ett paket
som bara behövs i den ena får en plattformsmarkör. Finns det inte
installerat här (uvloop på Windows, colorama på Pi:n) går versionen inte
att läsa av, och den gamla låsfilens version står kvar. Den uppdateras för
hand.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

import tomllib
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROT = Path(__file__).resolve().parents[1]
LASFIL = ROT / "requirements.lock"

# Markörerna utvärderas mot båda miljöerna. Ger de samma svar avgörs
# kravet här; ger de olika svar skrivs plattformen ut i låsfilen.
MILJOER: dict[str, dict[str, str]] = {
    "linux": {
        "sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
        "platform_machine": "aarch64", "python_version": "3.13",
        "python_full_version": "3.13.5", "platform_python_implementation": "CPython",
        "implementation_name": "cpython",
    },
    "windows": {
        "sys_platform": "win32", "platform_system": "Windows", "os_name": "nt",
        "platform_machine": "AMD64", "python_version": "3.14",
        "python_full_version": "3.14.7", "platform_python_implementation": "CPython",
        "implementation_name": "cpython",
    },
}
MARKOR = {
    frozenset({"linux"}): 'sys_platform != "win32"',
    frozenset({"windows"}): 'sys_platform == "win32"',
}
HAR = "windows" if sys.platform == "win32" else "linux"

HUVUD = """\
# Exakta versioner för hela beroendeträdet. Genererad av
# tools/las-beroenden.py — redigera inte för hand, utom de paket som bara
# finns på den andra plattformen (se skriptet).
#
# Installera med låsfilen som tak och golv:
#     pip install -e . -c requirements.lock          (Pi:n)
#     pip install -e ".[dev]" -c requirements.lock   (utveckling)
"""


@dataclass
class Las:
    miljoer: set[str] = field(default_factory=set)
    extras: set[str] = field(default_factory=set)


def _galler(req: Requirement, foralder_extras: set[str], miljo: str) -> bool:
    if req.marker is None:
        return True
    return any(
        req.marker.evaluate({**MILJOER[miljo], "extra": extra})
        for extra in (foralder_extras or {""})
    )


def _gamla_versioner() -> dict[str, str]:
    if not LASFIL.exists():
        return {}
    versioner = {}
    for rad in LASFIL.read_text(encoding="utf-8").splitlines():
        rad = rad.split("#", 1)[0].split(";", 1)[0].strip()
        if "==" in rad:
            namn, version = rad.split("==", 1)
            versioner[canonicalize_name(namn)] = version.strip()
    return versioner


def main() -> int:
    projekt = tomllib.loads((ROT / "pyproject.toml").read_text(encoding="utf-8"))
    ko: list[tuple[Requirement, set[str], set[str]]] = [
        (Requirement(krav), set(), set(MILJOER))
        for krav in projekt["project"]["dependencies"]
    ]
    las: dict[str, Las] = {}
    while ko:
        req, foralder_extras, arvda = ko.pop()
        miljoer = {m for m in arvda if _galler(req, foralder_extras, m)}
        if not miljoer:
            continue
        namn = canonicalize_name(req.name)
        post = las.setdefault(namn, Las())
        if miljoer <= post.miljoer and req.extras <= post.extras:
            continue
        post.miljoer |= miljoer
        post.extras |= req.extras
        try:
            barn = metadata.distribution(namn).requires or []
        except metadata.PackageNotFoundError:
            barn = []
        ko.extend((Requirement(b), set(post.extras), set(post.miljoer)) for b in barn)

    gamla = _gamla_versioner()
    rader, saknas = [], []
    for namn in sorted(las):
        post = las[namn]
        try:
            version = metadata.version(namn)
        except metadata.PackageNotFoundError:
            if HAR in post.miljoer or namn not in gamla:
                saknas.append(namn)
                continue
            version = gamla[namn]
        markor = MARKOR.get(frozenset(post.miljoer))
        rader.append(f"{namn}=={version}" + (f" ; {markor}" if markor else ""))

    if saknas:
        print("Inte installerade här, och utan version i den gamla låsfilen:",
              ", ".join(saknas), file=sys.stderr)
        return 1
    LASFIL.write_text(HUVUD + "\n" + "\n".join(rader) + "\n", encoding="utf-8")
    print(f"Skrev {len(rader)} paket till {LASFIL.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
