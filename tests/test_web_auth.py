"""Tester för token-autentiseringen i web/app.py.

Bygger en färsk FastAPI-app per test (via omimport av web.app, som kör
create_app() vid modulimport) så att varje test kan sätta sina egna
miljövariabler utan att påverka andra tester.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _fresh_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, web_access_token: str):
    monkeypatch.setenv("INTERVALS_API_KEY", "fake-intervals-key")
    monkeypatch.setenv("INTERVALS_ATHLETE_ID", "i1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")
    monkeypatch.setenv("WEB_ACCESS_TOKEN", web_access_token)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))

    # web.app kör create_app() som modul-level-sats (`app = create_app()`),
    # så modulen måste laddas om för att plocka upp nya miljövariabler.
    sys.modules.pop("web.app", None)
    sys.modules.pop("web", None)
    module = importlib.import_module("web.app")
    return module.create_app()


def test_routes_require_token_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _fresh_app(monkeypatch, tmp_path, web_access_token="secret-token")
    with TestClient(app) as client:
        assert client.get("/").status_code == 401
        assert client.get("/", params={"token": "wrong"}).status_code == 401
        assert client.get("/", params={"token": "secret-token"}).status_code == 200


def test_token_check_is_global_not_per_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regressionstest: skyddet ska gälla ALLA routes via en delad
    dependency, inte via manuellt kopierad kod i varje route (tidigare
    risk: en ny route kunde glömma bort kontrollen)."""
    app = _fresh_app(monkeypatch, tmp_path, web_access_token="secret-token")
    with TestClient(app) as client:
        assert client.get("/api/wellness").status_code == 401
        assert (
            client.get("/api/wellness", params={"token": "secret-token"}).status_code
            == 200
        )


def test_no_token_required_when_not_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Om WEB_ACCESS_TOKEN lämnas tom (typiskt för rent hemnätverk) ska
    inget token krävas."""
    app = _fresh_app(monkeypatch, tmp_path, web_access_token="")
    with TestClient(app) as client:
        assert client.get("/").status_code == 200


def test_malformed_time_setting_names_the_variable(monkeypatch, tmp_path) -> None:
    """Regressionstest: EVENING_SUMMARY_TIME=2359 (utan kolon) kraschade
    uppstarten med "ValueError: not enough values to unpack (expected 2,
    got 1)" — ett fel som varken säger vilken inställning som är trasig
    eller hur den ska se ut."""
    import pytest as _pytest

    monkeypatch.setenv("INTERVALS_API_KEY", "x")
    monkeypatch.setenv("INTERVALS_ATHLETE_ID", "i1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("EVENING_SUMMARY_TIME", "2359")

    from config import load_settings

    with _pytest.raises(RuntimeError) as excinfo:
        load_settings()

    message = str(excinfo.value)
    assert "EVENING_SUMMARY_TIME" in message
    assert "HH:MM" in message


def test_time_outside_the_clock_is_rejected(monkeypatch, tmp_path) -> None:
    import pytest as _pytest

    monkeypatch.setenv("INTERVALS_API_KEY", "x")
    monkeypatch.setenv("INTERVALS_ATHLETE_ID", "i1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("MORNING_RECOMMENDATION_TIME", "25:00")

    from config import load_settings

    with _pytest.raises(RuntimeError, match="MORNING_RECOMMENDATION_TIME"):
        load_settings()


def test_malformed_number_setting_names_the_variable(monkeypatch, tmp_path) -> None:
    """Samma behandling som klockslagen ovan, för de numeriska.

    WEB_PORT, SYNC_INTERVAL_HOURS och BACKUP_KEEP lästes med ett naket
    int(), så en felskrivning gav "ValueError: invalid literal for int()
    with base 10: 'åtta'" ur load_settings — utan att nämna vilken
    inställning som var trasig, och med hela tjänsten nere. Klockslagen
    hade fått namngivna fel av precis det skälet; siffrorna blev kvar."""
    import pytest as _pytest

    for var in ("WEB_PORT", "SYNC_INTERVAL_HOURS", "BACKUP_KEEP"):
        monkeypatch.setenv("INTERVALS_API_KEY", "x")
        monkeypatch.setenv("INTERVALS_ATHLETE_ID", "i1")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
        monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
        monkeypatch.setenv(var, "åtta")

        from config import load_settings

        with _pytest.raises(RuntimeError) as excinfo:
            load_settings()
        assert var in str(excinfo.value), f"felet nämner inte {var}"
        monkeypatch.delenv(var)


def test_number_settings_are_clamped_not_rejected(monkeypatch, tmp_path) -> None:
    """Ett för lågt tal är entydigt och kläms uppåt i stället för att fela.

    BACKUP_KEEP=0 är en rotation som raderar backupen direkt efter att
    den skrivits, SYNC_INTERVAL_HOURS=0 en synk som aldrig vilar. Båda
    har ett rimligt närmaste värde; "åtta" har det inte."""
    monkeypatch.setenv("INTERVALS_API_KEY", "x")
    monkeypatch.setenv("INTERVALS_ATHLETE_ID", "i1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("BACKUP_KEEP", "0")
    monkeypatch.setenv("SYNC_INTERVAL_HOURS", "0")

    from config import load_settings

    settings = load_settings()
    assert settings.backup_keep == 1
    assert settings.sync_interval_hours == 1


def test_number_settings_fall_back_to_defaults_when_unset(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("INTERVALS_API_KEY", "x")
    monkeypatch.setenv("INTERVALS_ATHLETE_ID", "i1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    for var in ("WEB_PORT", "SYNC_INTERVAL_HOURS", "BACKUP_KEEP"):
        monkeypatch.delenv(var, raising=False)

    from config import load_settings

    settings = load_settings()
    assert settings.web_port == 8000
    assert settings.sync_interval_hours == 1
    assert settings.backup_keep == 14


# --- Kakan som slipper token i adressen -------------------------------


def test_valid_query_token_hands_out_a_cookie(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Utan kaka måste ?token=... finnas i varje bokmärke, på varje enhet,
    och adressen är obrukbar om man bara skriver pi5:8000 i adressfältet.
    Kakan är också det enda som bär inloggningen inne i appen: sidorna
    innehåller inget token."""
    import web.app as _webapp

    app = _fresh_app(monkeypatch, tmp_path, web_access_token="secret-token")
    with TestClient(app) as client:
        resp = client.get(
            "/", params={"token": "secret-token"}, follow_redirects=False
        )
        assert resp.status_code == 303
        kaka = resp.cookies.get(_webapp._TOKEN_COOKIE)
        assert kaka == "secret-token"

        # Andra besöket: ingen token i adressen, kakan räcker.
        assert client.get("/").status_code == 200
        assert client.get("/api/wellness").status_code == 200


def test_cookie_is_httponly_and_samesite_lax(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """httponly: sidans JavaScript behöver inte läsa kakan (fetch skickar
    den av sig själv), och det som inte går att läsa går inte att läcka via
    en XSS-lucka.

    samesite=lax: en Lax-kaka följer inte med på en POST från en annan
    sajt, så cross-site-formuläret som _reject_cross_site_writes stoppar
    skulle numera inte ens vara autentiserat.

    secure ska INTE finnas — appen kör över vanlig http på hemnätverket,
    och en secure-kaka hade aldrig skickats tillbaka."""
    app = _fresh_app(monkeypatch, tmp_path, web_access_token="secret-token")
    with TestClient(app) as client:
        resp = client.get(
            "/", params={"token": "secret-token"}, follow_redirects=False
        )

    rubrik = resp.headers["set-cookie"].lower()
    assert "httponly" in rubrik
    assert "samesite=lax" in rubrik
    assert "secure" not in rubrik
    assert "max-age=" in rubrik


def test_a_rejected_request_never_hands_out_a_cookie(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ett 401 ska inte dela ut en inloggning. Utan den här spärren hade
    fel token gett kakan ändå, och nästa förfrågan sluppit in."""
    app = _fresh_app(monkeypatch, tmp_path, web_access_token="secret-token")
    with TestClient(app) as client:
        resp = client.get("/", params={"token": "fel-token"})
        assert resp.status_code == 401
        assert "set-cookie" not in resp.headers

        # Och den avvisade förfrågan får inte ha öppnat dörren för nästa.
        assert client.get("/").status_code == 401


def test_a_stale_cookie_is_rejected_when_the_token_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Byter du WEB_ACCESS_TOKEN ska gamla kakor sluta gälla direkt.
    Jämförelsen sker mot värdet i .env, inte mot något sparat — det är
    därför det inte behövs någon utloggning."""
    import web.app as _webapp

    app = _fresh_app(monkeypatch, tmp_path, web_access_token="nya-token")
    with TestClient(app) as client:
        client.cookies.set(_webapp._TOKEN_COOKIE, "gamla-token")
        assert client.get("/").status_code == 401


def test_no_cookie_is_issued_when_no_token_is_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Med tom WEB_ACCESS_TOKEN är sidan öppen, och då ska den inte sätta
    någon kaka alls — det hade bara varit skräp i webbläsaren."""
    app = _fresh_app(monkeypatch, tmp_path, web_access_token="")
    with TestClient(app) as client:
        resp = client.get("/", params={"token": "vadsomhelst"})
        assert resp.status_code == 200
        assert "set-cookie" not in resp.headers


def test_the_cookie_is_only_set_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bär förfrågan redan rätt kaka behöver svaret inte sätta om den.
    Hemskärmsgenvägen och bokmärket har ?token=... kvar, så utan den här
    kontrollen hade varje öppning skickat en onödig Set-Cookie."""
    app = _fresh_app(monkeypatch, tmp_path, web_access_token="secret-token")
    with TestClient(app) as client:
        client.get("/", params={"token": "secret-token"})
        igen = client.get(
            "/", params={"token": "secret-token"}, follow_redirects=False
        )
        assert igen.status_code == 303
        assert "set-cookie" not in igen.headers
        api = client.get("/api/wellness", params={"token": "secret-token"})
        assert "set-cookie" not in api.headers


# --- Tokenet lämnar adressen ------------------------------------------


def test_a_page_address_is_cleaned_of_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sidan visades på adressen med ?token=..., och den adressen stod
    kvar i adressfältet, i varje skärmdump och i historiken. Nu svarar
    servern med en omdirigering till samma adress utan token, och kakan
    följer med. Övriga parametrar står kvar: ?fel= bär felbeskedet efter
    ett misslyckat "Generera"."""
    app = _fresh_app(monkeypatch, tmp_path, web_access_token="secret-token")
    with TestClient(app) as client:
        for adress, ren in (
            ("/?token=secret-token", "/"),
            ("/?token=secret-token&fel=coaching", "/?fel=coaching"),
            ("/?fel=coaching&token=secret-token", "/?fel=coaching"),
            ("/activity/i123?token=secret-token", "/activity/i123"),
        ):
            svar = client.get(adress, follow_redirects=False)
            assert svar.status_code == 303, adress
            assert svar.headers["location"] == ren, adress
            client.cookies.clear()

        # Och man landar faktiskt inloggad på den rena adressen.
        landning = client.get("/?token=secret-token")
        assert landning.status_code == 200
        assert landning.url.path == "/"
        assert "token" not in str(landning.url)


def test_the_redirect_never_leaves_the_site(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Omdirigeringen bygger adressen av förfrågans egen sökväg. En sökväg
    som börjar med två snedstreck hade blivit //exempel.se, och det läser
    webbläsaren som en adress till en annan sajt. Samma sak med ett
    omvänt snedstreck."""
    app = _fresh_app(monkeypatch, tmp_path, web_access_token="secret-token")
    with TestClient(app) as client:
        for sokvag in ("//exempel.se", "/\\exempel.se"):
            svar = client.get(
                "http://testserver" + sokvag + "?token=secret-token",
                follow_redirects=False,
            )
            assert svar.status_code == 303, sokvag
            assert svar.headers["location"] == "/exempel.se", sokvag


def test_api_calls_and_writes_keep_the_query_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """/api/* och POST syns aldrig i adressfältet. Ett skript utan
    kakburk (curl) som bär ?token= ska få sitt svar direkt, inte en
    omdirigering till en adress där det saknar inloggning."""
    app = _fresh_app(monkeypatch, tmp_path, web_access_token="secret-token")
    monkeypatch.setattr(type(app.state.pipeline), "coaching", lambda self: "# ok")
    with TestClient(app) as client:
        api = client.get(
            "/api/wellness", params={"token": "secret-token"}, follow_redirects=False
        )
        assert api.status_code == 200
        client.cookies.clear()
        post = client.post("/analyze/coaching?token=secret-token", follow_redirects=False)
        assert post.status_code == 303
        assert post.headers["location"] == "/"


# --- Startspärren utan token ------------------------------------------


@pytest.mark.parametrize(
    ("token", "host", "oppen"),
    [
        ("", "0.0.0.0", True),
        ("", "192.168.1.20", True),
        ("", "::", True),
        ("", "pi5.local", True),
        ("", "127.0.0.1", False),
        ("", "127.0.1.1", False),
        ("", "::1", False),
        ("", "localhost", False),
        ("hemligt", "0.0.0.0", False),
    ],
)
def test_the_server_knows_when_it_would_be_open_to_the_network(
    token: str, host: str, oppen: bool
) -> None:
    from types import SimpleNamespace

    from main import _open_to_the_network_without_token

    settings = SimpleNamespace(web_access_token=token, web_host=host)
    assert _open_to_the_network_without_token(settings) is oppen  # type: ignore[arg-type]


def _serve(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, token: str, host: str):
    """Kör `serve` med uvicorn utbytt, och ger (returkod, anrop till uvicorn)."""
    import uvicorn

    from main import main

    monkeypatch.setenv("INTERVALS_API_KEY", "x")
    monkeypatch.setenv("INTERVALS_ATHLETE_ID", "i1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("WEB_ACCESS_TOKEN", token)
    monkeypatch.setenv("WEB_HOST", host)
    anrop: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: anrop.append(kw))
    return main(["serve"]), anrop


def test_serve_refuses_to_open_the_network_without_a_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """WEB_HOST är 0.0.0.0 som standard och .env.example lämnar tokenet
    tomt. Servern startade då helt öppen för nätverket, också för det som
    kostar pengar, utan ett ord om det. Nu vägrar den, och säger hur man
    gör i stället."""
    kod, anrop = _serve(monkeypatch, tmp_path, token="", host="0.0.0.0")

    assert kod != 0
    assert anrop == [], "uvicorn startades ändå"
    fel = capsys.readouterr().err
    assert "WEB_ACCESS_TOKEN" in fel
    assert "WEB_HOST=127.0.0.1" in fel
    assert "token_urlsafe" in fel


def test_serve_starts_with_a_token_or_on_loopback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    kod, anrop = _serve(monkeypatch, tmp_path, token="hemligt", host="0.0.0.0")
    assert kod == 0 and len(anrop) == 1
    assert anrop[0]["host"] == "0.0.0.0"

    kod, anrop = _serve(monkeypatch, tmp_path, token="", host="127.0.0.1")
    assert kod == 0 and len(anrop) == 1


# --- Token i loggen ---------------------------------------------------


def _accessrad(path: str) -> str:
    """Kör en access-loggpost genom filtret och ger raden som den skrivs.

    Formen på posten är uvicorns egen (se _RedactQuerySecrets i main.py).
    """
    import logging

    from main import _RedactQuerySecrets

    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.5:52341", "GET", path, "1.1", 200),
        None,
    )
    _RedactQuerySecrets().filter(record)
    return record.getMessage()


def test_the_access_log_does_not_write_the_token_in_the_clear() -> None:
    """WEB_ACCESS_TOKEN följer med som ?token=... vid första klivet in (se
    _TOKEN_COOKIE i web/app.py), och uvicorn loggar hela sökvägen med
    query. Nyckeln hamnade därför i klartext i journalen, där den blev
    kvar så länge loggen roterar."""
    rad = _accessrad("/?token=hemligt-varde")

    assert "hemligt-varde" not in rad
    assert "token=***" in rad
    # Resten av raden ska vara kvar — den är hela nyttan med access-loggen.
    assert '"GET /?token=*** HTTP/1.1" 200' in rad
    assert "10.0.0.5:52341" in rad


def test_other_query_parameters_survive_the_redaction() -> None:
    """Bara token maskas. Övningsnamnet i /api/strength/progression är
    inget hemligt och behövs för att se vilken fråga som var långsam."""
    rad = _accessrad("/api/strength/progression?exercise=mark%20lyft&token=abc")

    assert "exercise=mark%20lyft" in rad
    assert "abc" not in rad


def test_a_path_without_a_query_is_left_alone() -> None:
    rad = _accessrad("/activity/i123")

    assert '"GET /activity/i123 HTTP/1.1" 200' in rad
