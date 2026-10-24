"""Pytest-konfiguration: sys.path, en Settings-fabrik och ett spärrat nätverk."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def make_settings(**overrides: Any) -> Any:
    """Bygger en komplett Settings för tester som behöver ett riktigt objekt.

    Fanns tidigare som samma tolvradiga literal upprepad på fem ställen i
    tre testfiler. Varje ny inställning i Settings — som är en frozen
    dataclass utan defaults — sänkte då sjutton tester som inte hade
    någonting med den inställningen att göra. Testet ska bara behöva
    nämna de fält det faktiskt bryr sig om.
    """
    from config import AthleteProfile, Settings

    defaults: dict[str, Any] = {
        "intervals_api_key": "k",
        "intervals_athlete_id": "i1",
        "anthropic_api_key": "a",
        "anthropic_model": "claude-sonnet-5",
        "web_access_token": "",
        "web_host": "127.0.0.1",
        "web_port": 8000,
        "db_path": Path("data/x.db"),
        "sync_interval_hours": 1,
        "morning_recommendation_time": "07:00",
        "evening_summary_time": "23:59",
        "evening_summary_recheck_time": "09:00",
        "coaching_time": "12:00",
        "backup_dir": Path("data/backups"),
        "backup_time": "03:30",
        "backup_keep": 14,
        "analysis_keep_days": 30,
        "hidden_exercises": (),
        "strength_corrections": (),
        "startup_catchup": False,
        "athlete": AthleteProfile(),
    }
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture(autouse=True)
def _no_startup_catchup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Låt inte varje TestClient starta uppstarts-catchupen.

    Lifespan startade förr catchup-tråden villkorslöst, och sviten öppnar
    114 TestClient. Var och en försökte alltså synka mot Intervals och
    anropa Claude på riktigt. Att det inte kostade något berodde helt på
    _no_live_intervals_calls och _no_live_anthropic_calls nedan — och de
    kastar inne i en TRÅD, vars fel sväljs av except Exception i
    _run_startup_catchups. En spärr som slutade fungera hade alltså inte
    synts som ett rött test, utan som en oväntad räkning från Anthropic.

    Tråden gjorde också testerna icke-deterministiska: den tar
    synk-låset, så ett test som postade mot /sync kunde få 409 i stället
    för 200 — men bara om klockan råkade ligga i morgonfönstret
    07:00-11:59, eftersom morgonkörningen annars returnerar direkt.

    Testerna som handlar om catchupen själv sätter STARTUP_CATCHUP=1
    tillbaka.
    """
    monkeypatch.setenv("STARTUP_CATCHUP", "0")


@pytest.fixture(autouse=True)
def _no_live_intervals_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Låt inget test nå intervals.icu på riktigt.

    Samma sorts spärr som _no_live_anthropic_calls nedan, och den behövdes
    av samma anledning: när run_morning_recommendation började synka innan
    den fattar sitt beslut, gjorde varje TestClient-uppstart plötsligt
    riktiga HTTP-anrop mot intervals.icu. De besvarades med 401 eftersom
    testnyckeln är påhittad, men gjorde testkörningen beroende av att det
    finns internet — och långsammare.

    Spärren sitter på httpx.Client.request och tittar på värden, så
    TestClient (som också är en httpx.Client, mot http://testserver) och
    tester som fejkar sin egen klient påverkas inte.
    """
    import httpx

    real_request = httpx.Client.request

    def _guarded(self: httpx.Client, method: str, url: object, *args: object, **kwargs: object):
        if "intervals.icu" in str(url):
            raise AssertionError(
                f"Testet försökte anropa intervals.icu på riktigt ({method} {url}). "
                "Fejka klienten, eller hindra koden från att nå hit. Se "
                "_no_live_intervals_calls i tests/conftest.py."
            )
        return real_request(self, method, url, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.Client, "request", _guarded)


@pytest.fixture(autouse=True)
def _no_live_anthropic_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Låt inget test nå Anthropics API på riktigt.

    Upptäcktes när ett schemalagt coaching-jobb lades till i uppstarts-
    catchupen: varje TestClient-uppstart anropade då Claude, och en enda
    testfil gjorde 125 riktiga HTTP-anrop mot api.anthropic.com. De
    besvarades med 401 eftersom testnyckeln är påhittad — men med en
    riktig nyckel i miljön hade `pytest` bränt tokens, skrivit riktiga
    analyser i databasen och tagit minuter i stället för sekunder.

    Spärren sitter på Messages.create, alltså på det lager som faktiskt
    når nätet, i stället för på ClaudeClient. Tester som vill fejka ett
    svar monkeypatchar sin egen klientinstans och skriver över den här —
    monkeypatch på instansen är mer specifik än den här på klassen, så de
    fortsätter fungera oförändrat.

    Slår spärren till betyder det att kod under test försökte anropa
    Claude utan att testet bett om det. Fejka anropet, eller lägg till en
    spärr i koden som gör att det inte sker.
    """
    from anthropic.resources.messages import Messages

    def _blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "Testet försökte anropa Anthropics API på riktigt. Fejka "
            "messages.create på klientinstansen, eller hindra koden från "
            "att nå hit. Se _no_live_anthropic_calls i tests/conftest.py."
        )

    monkeypatch.setattr(Messages, "create", _blocked)
