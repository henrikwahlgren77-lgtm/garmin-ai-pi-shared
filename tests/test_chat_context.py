"""Regressionstest: POST /chat använde tidigare gårdagens wellness-data
(datetime.now() - 1 dag) istället för dagens, trots att Intervals lägger
sömnen på uppvaknandedagen (samma konvention som morning_recommendation
m.fl. redan följer korrekt)."""
from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeToolUseBlock:
    def __init__(self, name: str, tool_input: dict, block_id: str = "tool_1") -> None:
        self.type = "tool_use"
        self.name = name
        self.input = tool_input
        self.id = block_id


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text)]
        self.stop_reason = "end_turn"


class _FakeToolUseResponse:
    def __init__(self, blocks: list) -> None:
        self.content = blocks
        self.stop_reason = "tool_use"


def _fresh_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("INTERVALS_API_KEY", "fake-intervals-key")
    monkeypatch.setenv("INTERVALS_ATHLETE_ID", "i1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")
    monkeypatch.setenv("WEB_ACCESS_TOKEN", "")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))

    sys.modules.pop("web.app", None)
    sys.modules.pop("web", None)
    module = importlib.import_module("web.app")
    return module.create_app()


def test_chat_uses_todays_wellness_not_yesterdays(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _fresh_app(monkeypatch, tmp_path)
    store = app.state.store

    today = datetime.now().date().isoformat()
    yesterday = (datetime.now() - timedelta(days=1)).date().isoformat()

    def _wellness_row(day: str, sleep_score: int, hrv: float) -> dict:
        return {
            "day": day, "rest_day": 0, "ctl": 40.0, "atl": 30.0, "tsb": 5.0,
            "ramp_rate": None, "sleep_seconds": 25000, "sleep_score": sleep_score,
            "sleep_quality": 4, "avg_sleeping_hr": 50, "weight": 90.0,
            "resting_hr": 55, "hrv": hrv, "hrv_sdnn": None, "stress": None,
            "respiration": None, "spO2": None, "systolic": None, "diastolic": None,
            "hydration": None, "soreness": None, "fatigue": None, "mood": None,
            "motivation": None, "injury": None, "readiness": None, "vo2max": None,
            "steps": 5000, "kcal_consumed": None, "raw_json": "{}",
            "last_synced": datetime.now().isoformat(),
        }

    # Distinkta, lätt igenkännbara värden så vi kan avgöra vilket datum
    # som faktiskt skickades med till Claude.
    store.upsert_wellness_many([_wellness_row(yesterday, sleep_score=11, hrv=11.0)])
    store.upsert_wellness_many([_wellness_row(today, sleep_score=99, hrv=99.0)])

    captured: dict = {}

    def fake_create(**kwargs):
        captured["system"] = kwargs.get("system", "")
        return _FakeResponse("Testsvar")

    monkeypatch.setattr(
        app.state.pipeline.claude._client.messages, "create", fake_create
    )

    with TestClient(app) as client:
        resp = client.post("/chat", json={"message": "Hur mår jag?", "history": []})
        assert resp.status_code == 200

    system_prompt = captured["system"]
    assert today in system_prompt, "Dagens datum ska nämnas i kontexten"
    assert "99" in system_prompt, "Dagens (99) wellness-värden ska skickas med"

    # "Senaste wellness" är dagens ENDA snapshot (hrv/sömn/vilopuls just nu)
    # och ska inte blanda in gårdagen. Den 30-dagars historik som skickas
    # separat (för trendfrågor, se fmt_wellness_history) FÅR och SKA
    # innehålla gårdagen — därför kollas bara snapshot-raden, inte hela
    # system-prompten.
    snapshot_line = next(
        line for line in system_prompt.splitlines() if line.startswith("Senaste wellness")
    )
    assert '"sleep_score": 11' not in snapshot_line, (
        "Gårdagens wellness-data (11) ska INTE finnas i dagens snapshot"
    )
    assert '"sleep_score": 99' in snapshot_line


def test_chat_falls_back_to_the_latest_wellness_day(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Har synken inte kört sedan midnatt finns ingen rad för idag.

    Chatten slog upp just dagens datum och inget annat, så den svarade
    "jag har ingen data om din sömn" hela förmiddagen — medan sidan bakom
    den visade gårdagens siffror med samma fallback som /api/wellness
    använder. Två svar på samma fråga, i samma app.
    """
    app = _fresh_app(monkeypatch, tmp_path)
    store = app.state.store

    today = datetime.now().date().isoformat()
    yesterday = (datetime.now() - timedelta(days=1)).date().isoformat()
    store.upsert_wellness_many([{
        "day": yesterday, "rest_day": 0, "ctl": 40.0, "atl": 30.0, "tsb": 5.0,
        "ramp_rate": None, "sleep_seconds": 25000, "sleep_score": 77,
        "sleep_quality": 4, "avg_sleeping_hr": 50, "weight": 90.0,
        "resting_hr": 55, "hrv": 66.0, "hrv_sdnn": None, "stress": None,
        "respiration": None, "spO2": None, "systolic": None, "diastolic": None,
        "hydration": None, "soreness": None, "fatigue": None, "mood": None,
        "motivation": None, "injury": None, "readiness": None, "vo2max": None,
        "steps": 5000, "kcal_consumed": None, "raw_json": "{}",
        "last_synced": datetime.now().isoformat(),
    }])

    captured: dict = {}

    def fake_create(**kwargs):
        captured["system"] = kwargs.get("system", "")
        return _FakeResponse("Testsvar")

    monkeypatch.setattr(
        app.state.pipeline.claude._client.messages, "create", fake_create
    )

    with TestClient(app) as client:
        resp = client.post("/chat", json={"message": "Hur sov jag?", "history": []})
        assert resp.status_code == 200

    snapshot_line = next(
        line for line in captured["system"].splitlines()
        if line.startswith("Senaste wellness")
    )
    assert '"sleep_score": 77' in snapshot_line, (
        "utan rad för idag ska den senaste kända dagen skickas med"
    )
    assert yesterday in snapshot_line, (
        "och raden måste säga vilken dag talen gäller, annars kallar "
        "Claude förrgårnatten för 'i natt'"
    )
    assert today in captured["system"], "dagens datum ska ändå finnas i kontexten"


def test_chat_context_includes_wellness_history_for_trend_questions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fråga i chatten om t.ex. hur HRV sett ut "över tid" kan bara
    besvaras med grund i faktiska siffror om en dagserie skickas med, inte
    bara dagens enda värde. Se fmt_wellness_history i analysis/pipeline.py."""
    app = _fresh_app(monkeypatch, tmp_path)
    store = app.state.store

    from datetime import date, timedelta

    def _wellness_row(day: str, hrv: float) -> dict:
        return {
            "day": day, "rest_day": 0, "ctl": 40.0, "atl": 30.0, "tsb": 5.0,
            "ramp_rate": None, "sleep_seconds": 25000, "sleep_score": 80,
            "sleep_quality": 4, "avg_sleeping_hr": 50, "weight": 90.0,
            "resting_hr": 55, "hrv": hrv, "hrv_sdnn": None, "stress": None,
            "respiration": None, "spO2": None, "systolic": None, "diastolic": None,
            "hydration": None, "soreness": None, "fatigue": None, "mood": None,
            "motivation": None, "injury": None, "readiness": None, "vo2max": None,
            "steps": 5000, "kcal_consumed": None, "raw_json": "{}",
            "last_synced": datetime.now().isoformat(),
        }

    today = date.today()
    for offset, hrv in [(0, 60.0), (3, 55.0), (7, 50.0)]:
        day = (today - timedelta(days=offset)).isoformat()
        store.upsert_wellness_many([_wellness_row(day, hrv)])

    captured: dict = {}

    def fake_create(**kwargs):
        captured["system"] = kwargs.get("system", "")
        return _FakeResponse("Testsvar")

    monkeypatch.setattr(
        app.state.pipeline.claude._client.messages, "create", fake_create
    )

    with TestClient(app) as client:
        resp = client.post(
            "/chat", json={"message": "Hur har min HRV sett ut senaste veckan?", "history": []}
        )
        assert resp.status_code == 200

    system_prompt = captured["system"]
    assert "Wellness senaste 30 dagarna" in system_prompt
    assert '"hrv": 60.0' in system_prompt
    assert '"hrv": 55.0' in system_prompt
    assert '"hrv": 50.0' in system_prompt


def test_chat_endpoint_truncates_overlong_reply_instead_of_raw_cutoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regressionstest: ett rapporterat problem var att chattsvar klipptes
    av mitt i en mening. /chat hade tidigare ingen max_chars-gräns alls
    (bara ett max_tokens-tak, vilket API:et själv klipper rått vid) —
    nu ska svaret klippas städat vid max_chars=2500 med en tydlig
    markering istället."""
    app = _fresh_app(monkeypatch, tmp_path)

    long_reply = "\n".join(f"Rad {i} med lite text om träning." for i in range(300))
    assert len(long_reply) > 2500

    monkeypatch.setattr(
        app.state.pipeline.claude._client.messages,
        "create",
        lambda **kwargs: _FakeResponse(long_reply),
    )

    with TestClient(app) as client:
        resp = client.post("/chat", json={"message": "Ge mig alla detaljer", "history": []})
        assert resp.status_code == 200
        reply = resp.json()["reply"]
        assert len(reply) <= 2500
        assert "avkortat" in reply


def test_chat_persists_self_report_via_tool_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: att skriva 'jag vägde mig, 91.2' i chatten ska faktiskt
    spara en rad i self_reports (via log_self_report-verktyget), och den
    ska finnas kvar och kunna plockas upp av framtida analyser/chattar —
    inte bara nämnas i det ena svaret."""
    app = _fresh_app(monkeypatch, tmp_path)

    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _FakeToolUseResponse(
                [_FakeToolUseBlock(
                    "log_self_report",
                    {"category": "weight", "value": 91.2},
                )]
            )
        return _FakeResponse("Noterat, 91.2 kg sparat! Bra jobbat.")

    monkeypatch.setattr(app.state.pipeline.claude._client.messages, "create", fake_create)

    with TestClient(app) as client:
        resp = client.post(
            "/chat", json={"message": "Vägde mig i morse, 91.2", "history": []}
        )
        assert resp.status_code == 200
        assert resp.json()["reply"] == "Noterat, 91.2 kg sparat! Bra jobbat."

    # Verifiera att det faktiskt ligger kvar i databasen, oberoende av
    # den här konversationen.
    saved = app.state.store.list_self_reports(days=1)
    assert len(saved) == 1
    assert saved[0]["category"] == "weight"
    assert saved[0]["value"] == 91.2


def test_chat_context_includes_recent_self_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regressionstest: Claude ska se tidigare loggad self_reports-historik
    i kontexten (inte bara kunna SKRIVA nya via verktyget) — annars minns
    den ingenting mellan chattsessioner."""
    app = _fresh_app(monkeypatch, tmp_path)
    app.state.store.add_self_report(
        day=datetime.now().date().isoformat(),
        category="illness",
        note="halsont och lite feber",
    )

    captured = {}

    def fake_create(**kwargs):
        captured["system"] = kwargs.get("system", "")
        return _FakeResponse("Vila idag med tanke på förkylningen.")

    monkeypatch.setattr(app.state.pipeline.claude._client.messages, "create", fake_create)

    with TestClient(app) as client:
        resp = client.post(
            "/chat", json={"message": "Ska jag träna idag?", "history": []}
        )
        assert resp.status_code == 200

    assert "halsont och lite feber" in captured["system"]


def test_chat_persists_strength_session_via_tool_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: att skriva 'bänkpress 3x8 på 80' i chatten ska faktiskt
    spara set-rader. Intervals.icu skickar ingen övnings- eller viktdata alls
    för styrkepass, så detta är enda vägen in för den informationen."""
    app = _fresh_app(monkeypatch, tmp_path)

    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _FakeToolUseResponse(
                [_FakeToolUseBlock(
                    "log_strength_session",
                    {
                        "exercise": "bänkpress",
                        "sets": [
                            {"reps": 8, "weight_kg": 80.0},
                            {"reps": 8, "weight_kg": 80.0},
                            {"reps": 8, "weight_kg": 80.0},
                        ],
                    },
                )]
            )
        return _FakeResponse("Noterat! Stark bänk idag.")

    monkeypatch.setattr(app.state.pipeline.claude._client.messages, "create", fake_create)

    with TestClient(app) as client:
        resp = client.post(
            "/chat", json={"message": "Körde bänkpress 3x8 på 80 kg", "history": []}
        )
        assert resp.status_code == 200

    saved = app.state.store.list_strength_sets(days=1)
    assert len(saved) == 3
    assert {r["exercise"] for r in saved} == {"bänkpress"}
    assert sorted(r["set_number"] for r in saved) == [1, 2, 3]


def test_chat_context_includes_recent_strength_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regressionstest: utan tidigare styrkepass i kontexten kan Claude inte
    svara på 'vad lyfte jag förra gången?' eller föreslå en rimlig ökning."""
    app = _fresh_app(monkeypatch, tmp_path)
    app.state.store.add_strength_sets(
        day=datetime.now().date().isoformat(),
        exercise="marklyft",
        sets=[{"reps": 5, "weight_kg": 120.0}],
    )

    captured = {}

    def fake_create(**kwargs):
        captured["system"] = kwargs.get("system", "")
        return _FakeResponse("Förra gången tog du 120 kg.")

    monkeypatch.setattr(app.state.pipeline.claude._client.messages, "create", fake_create)

    with TestClient(app) as client:
        resp = client.post(
            "/chat", json={"message": "Vad lyfte jag i marklyft sist?", "history": []}
        )
        assert resp.status_code == 200

    assert "marklyft" in captured["system"]
    assert "120" in captured["system"]


def test_chat_offers_every_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Alla verktyg ska skickas med. Registrerades tidigare bara
    log_self_report, och då kunde Claude inte spara styrkepass alls.

    Rättnings- och raderingsverktygen tillkom av samma skäl åt andra
    hållet: utan dem gick en felloggad uppgift inte att ta tillbaka från
    chatten, och den följde med i analyserna i upp till trettio dagar."""
    app = _fresh_app(monkeypatch, tmp_path)

    captured = {}

    def fake_create(**kwargs):
        captured["tools"] = kwargs.get("tools", [])
        return _FakeResponse("Hej!")

    monkeypatch.setattr(app.state.pipeline.claude._client.messages, "create", fake_create)

    with TestClient(app) as client:
        client.post("/chat", json={"message": "Tja", "history": []})

    names = {t["name"] for t in captured["tools"]}
    assert names == {
        "log_self_report",
        "log_strength_session",
        "correct_self_report",
        "delete_self_report",
    }


def test_chat_context_carries_self_report_ids_so_they_can_be_corrected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """correct_self_report och delete_self_report adresserar en rad med
    dess id. Utan id i kontexten kan Claude inte veta vilket det är — den
    hade fått gissa, och ett gissat id pekar på någon annans uppgift.

    Analyserna får dem inte: se test_pipeline.py, där id:t är brus som
    inte går att tolka och det inte finns några verktyg att använda det
    med."""
    app = _fresh_app(monkeypatch, tmp_path)
    report_id = app.state.store.add_self_report(
        day=datetime.now().date().isoformat(), category="weight", value=122.3
    )

    captured = {}

    def fake_create(**kwargs):
        captured["system"] = kwargs.get("system", "")
        return _FakeResponse("Noterat.")

    monkeypatch.setattr(app.state.pipeline.claude._client.messages, "create", fake_create)

    with TestClient(app) as client:
        client.post("/chat", json={"message": "Vad vägde jag?", "history": []})

    assert f'"id": {report_id}' in captured["system"]


def test_activity_page_renders_logged_strength_sets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Loggade set ska synas på passets sida, med volym per set uträknad."""
    app = _fresh_app(monkeypatch, tmp_path)
    store = app.state.store
    store.upsert_activity({
        "id": "gym1", "name": "Styrka", "type": "WeightTraining",
        "sport": "WeightTraining", "start_time": "2026-08-20T17:00:00+00:00",
        "duration_seconds": 3600, "distance_meters": None,
        "average_heart_rate": 110, "max_heart_rate": 140, "average_watts": None,
        "normalized_watts": None, "average_cadence": None, "average_speed": None,
        "tss": 30.0, "intensity": None, "raw_json": "{}",
        "last_synced": datetime.now().isoformat(),
    })
    store.add_strength_sets(
        day="2026-08-20", exercise="bänkpress",
        sets=[{"reps": 8, "weight_kg": 80.0}, {"reps": 6, "weight_kg": 82.5}],
        activity_id="gym1",
    )

    with TestClient(app) as client:
        resp = client.get("/activity/gym1")
        assert resp.status_code == 200
        body = resp.text

    assert "Styrketräning" in body
    assert "bänkpress" in body
    assert "640 kg" in body   # 8 reps x 80 kg

    # Vikterna lagras som REAL. Ett jämnt set visades som "80.0 kg";
    # decimalen ska falla bort när den är noll men stå kvar när den
    # betyder något.
    assert "80 kg" in body and "80.0 kg" not in body
    # Decimalkomma, som dashboardens vikter.
    assert "82,5 kg" in body


def test_activity_page_hides_strength_card_when_nothing_logged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ett löppass ska inte få ett tomt styrkekort."""
    app = _fresh_app(monkeypatch, tmp_path)
    app.state.store.upsert_activity({
        "id": "run1", "name": "Löprunda", "type": "Run", "sport": "Run",
        "start_time": "2026-08-20T07:00:00+00:00", "duration_seconds": 1800,
        "distance_meters": 5000, "average_heart_rate": 150, "max_heart_rate": 170,
        "average_watts": None, "normalized_watts": None, "average_cadence": None,
        "average_speed": None, "tss": 40.0, "intensity": None, "raw_json": "{}",
        "last_synced": datetime.now().isoformat(),
    })

    with TestClient(app) as client:
        body = client.get("/activity/run1").text

    assert "Loggat via chatten" not in body


def test_activity_page_follows_the_dashboard_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Aktivitetssidan låg kvar i den gamla formen medan dashboarden bytt
    riktning: smalare spalt, .topbar med avatar i stället för hero, svart
    gradientblock kring passnamnet och en fast menyrad i botten. Två sidor
    i samma app såg ut som två appar.

    Den ska nu använda dashboardens skal — och menyraden är ersatt av en
    tillbakalänk uppe till vänster."""
    app = _fresh_app(monkeypatch, tmp_path)
    app.state.store.upsert_activity({
        "id": "run2", "name": "Löprunda", "type": "Run", "sport": "Run",
        "start_time": "2026-08-20T07:00:00+00:00", "duration_seconds": 1800,
        "distance_meters": 5000, "average_heart_rate": 150, "max_heart_rate": 170,
        "average_watts": None, "normalized_watts": None, "average_cadence": None,
        "average_speed": None, "tss": 40.0, "intensity": None, "raw_json": "{}",
        "last_synced": datetime.now().isoformat(),
    })
    # Med analys, så att prosa-vägen faktiskt renderas och inte bara
    # tomtillståndet.
    app.state.store.save_analysis("activity", "# Passet\n\nBra tempo.", "run2", "m")

    with TestClient(app) as client:
        body = client.get("/activity/run2").text
        dashboard = client.get("/").text

    # Dashboardens skal, inte den gamla .app-layouten med topbar.
    assert 'class="dash-hero"' in body
    assert 'class="dash"' in body
    assert 'class="topbar"' not in body
    assert 'class="avatar"' not in body

    # Menyraden borta, tillbakalänken på plats. Dashboarden har aldrig
    # haft menyraden, så jämförelsen visar att sidorna nu är eniga.
    assert 'class="bottom-nav"' not in body
    assert 'class="bottom-nav"' not in dashboard
    assert 'class="backlink"' in body

    # Samma byggstenar som dashboarden, inga egna för den här sidan.
    for klass in ('class="section-title"', 'class="gauge"', 'class="prose"',
                  'class="linkbtn"'):
        assert klass in body, klass
    for gammal in ('class="run-header"', 'class="metric-card"',
                   'class="metrics-grid"', 'class="btn"'):
        assert gammal not in body, gammal


def test_activity_analysis_headings_sit_below_the_page_heading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Analyserna inleds med "# Onsdag 26 augusti 2026", vilket gav sidan
    två h1 — passets namn och analysens datumrad — och lade analysens
    egna h2:or på samma nivå som sidans avsnittsrubriker. Analysens titel
    är ett avsnitt PÅ sidan, inte sidans titel.

    Nedtrappningen gjordes tidigare server-side här (md-filtrets
    toc-baselevel) och i JavaScript på dashboarden — två implementationer
    av samma regel. Serverfiltret var dessutom en XSS-lucka och är borta;
    båda sidorna använder nu demoteHeadings i markdown.js. Testet
    kontrollerar därför att sidan har en enda h1 och att den delade
    renderaren är inkopplad, inte att servern skickar färdiga h2:or."""
    app = _fresh_app(monkeypatch, tmp_path)
    app.state.store.upsert_activity({
        "id": "run3", "name": "Löprunda", "type": "Run", "sport": "Run",
        "start_time": "2026-08-20T07:00:00+00:00", "duration_seconds": 1800,
        "distance_meters": 5000, "average_heart_rate": 150, "max_heart_rate": 170,
        "average_watts": None, "normalized_watts": None, "average_cadence": None,
        "average_speed": None, "tss": 40.0, "intensity": None, "raw_json": "{}",
        "last_synced": datetime.now().isoformat(),
    })
    app.state.store.save_analysis(
        "activity", "# Onsdag 26 augusti\n\n## Bedömning\n\n### Detalj\n\nText.",
        "run3", "m",
    )

    with TestClient(app) as client:
        body = client.get("/activity/run3").text

    assert body.count("<h1") == 1, "sidan ska ha exakt en h1: passets namn"
    # Analysens rubriker kommer som markdown-text, inte som färdiga
    # rubriktaggar — annars har serverrenderingen smugit sig tillbaka.
    assert "# Onsdag 26 augusti" in body
    assert "Onsdag 26 augusti</h2>" not in body

    root = Path(__file__).resolve().parents[1]
    activity_html = (root / "src/web/templates/activity.html").read_text(encoding="utf-8")
    markdown_js = (root / "src/web/static/markdown.js").read_text(encoding="utf-8")
    assert "renderAnalysisElement(analysisEl)" in activity_html
    assert "function demoteHeadings(" in markdown_js
    assert "demoteHeadings(el)" in markdown_js, (
        "renderAnalysisElement trappar inte ned rubrikerna"
    )


def test_removed_activity_styles_are_gone_from_the_stylesheet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Stilmallen ska inte växa med regler ingen längre kan koppla till
    något på skärmen.

    Två omgångar har fallit bort. Aktivitetssidan var enda användaren av
    .run-header, .metrics-grid, .metric-card, .analysis, .meta och .empty
    tills den fick dashboardens form. Chattsidan var sedan enda användaren
    av .topbar, .avatar, .title-block, .bottom-nav, .nav-icon och
    main.app — och den sidan är borttagen."""
    css = (
        Path(__file__).resolve().parents[1] / "src/web/static/style.css"
    ).read_text(encoding="utf-8")

    for regel in (
        ".run-header", ".metrics-grid", ".metric-card", ".metric-dot",
        ".topbar", ".avatar", ".title-block", ".bottom-nav", ".nav-icon",
        "main.app",
        # Dashboardens synkknapp i full bredd längst ner var den sista
        # användaren av .btn. Synken är nu en .textbtn överst.
        ".btn",
    ):
        assert regel not in css, f"{regel} används inte längre"

    # ...men det som fortfarande används ska stå kvar. .card och
    # .table-scroll sitter på aktivitetssidan.
    for behålls in (".card", ".table-scroll", ".analysis-block", ".prose"):
        assert behålls in css, behålls


def test_chat_rejects_a_body_that_is_not_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """En trasig kropp är klientens fel, inte serverns.

    request.json() kastade rakt igenom, så en icke-JSON-kropp gav en
    ohanterad 500 i stället för ett begripligt 400."""
    app = _fresh_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        svar = client.post(
            "/chat", content=b"inte json alls",
            headers={"Content-Type": "application/json"},
        )
    assert svar.status_code == 400

    with TestClient(app) as client:
        # Giltig JSON, men inte ett objekt.
        assert client.post("/chat", json=["en", "lista"]).status_code == 400


def test_chat_rejects_a_message_that_is_not_a_string(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Kontrollen ovan täcker en kropp som inte är ett objekt, men täckte
    inte fältets typ.

    `(body.get("message") or "").strip()` anropar .strip() på allt som är
    sant, så {"message": {"a": 1}} gav en ohanterad AttributeError och en
    500 — samma trasiga indata som testet ovan, ett annat sätt att vara
    trasig, motsatt svar."""
    app = _fresh_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        for felaktig in ({"a": 1}, [1, 2], 42, True):
            svar = client.post("/chat", json={"message": felaktig, "history": []})
            assert svar.status_code == 400, f"{felaktig!r} gav {svar.status_code}"


def test_chat_rejects_an_overlong_message_before_paying_for_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Kroppen hade ingen storleksgräns alls: antalet historikmeddelanden
    var begränsat men inte deras längd, så taket var 20 × oändligt. Ett
    meddelande på en megabyte gick rakt vidare till Anthropic och blev en
    megabyte betald prompt."""
    from web.app import _MAX_CHAT_INPUT_CHARS, _trim_chat_history

    app = _fresh_app(monkeypatch, tmp_path)

    def _skulle_ha_kostat(*a, **kw):
        raise AssertionError("för långt meddelande nådde ända fram till Claude")

    monkeypatch.setattr(app.state.pipeline.claude, "chat", _skulle_ha_kostat)

    with TestClient(app) as client:
        svar = client.post(
            "/chat",
            json={"message": "a" * (_MAX_CHAT_INPUT_CHARS + 1), "history": []},
        )
    assert svar.status_code == 413

    # Samma gräns på historiken, som kommer från samma klient. En välartad
    # klient kan aldrig komma över den: våra egna svar kapas vid
    # _MAX_CHAT_CHARS och användarens meddelanden avvisas ovan.
    behållen = _trim_chat_history([
        {"role": "user", "content": "a" * (_MAX_CHAT_INPUT_CHARS + 1)},
        {"role": "user", "content": "kort och rimligt"},
    ])
    assert behållen == [{"role": "user", "content": "kort och rimligt"}]


def test_chat_logs_and_reports_when_claude_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Chatten var enda routen utan felhantering kring Claude-anropet: ett
    API-fel, ett tomt svar eller ett trasigt verktygsanrop blev en 500 utan
    en enda rad i loggen. Alla /analyze/*-routes loggade redan."""
    import logging

    app = _fresh_app(monkeypatch, tmp_path)

    def _boom(*a, **kw):
        raise RuntimeError("överbelastad, försök igen")

    monkeypatch.setattr(app.state.pipeline.claude, "chat", _boom)

    with caplog.at_level(logging.ERROR), TestClient(app) as client:
        svar = client.post("/chat", json={"message": "hej"})

    assert svar.status_code == 500
    assert "Chattsvar misslyckades" in caplog.text, "felet loggades inte"
    # Undantagets text stannar i journalen. Chattens gränssnitt läser ändå
    # aldrig detail — det visar bara statuskoden — så ingenting går
    # förlorat för användaren, och ett fel från Anthropic-klienten bär
    # request-id och delar av anropet.
    assert "överbelastad" in caplog.text
    assert svar.json()["detail"] == (
        "Chattsvaret misslyckades. Felet finns i tjänstens logg."
    )


def test_chat_endpoint_answers_429_when_the_limit_is_hit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spärren ska ligga efter valideringen men FÖRE Claude-anropet — ett
    avvisat meddelande får inte kosta något."""
    app = _fresh_app(monkeypatch, tmp_path)
    # EFTER _fresh_app, som poppar web.app ur sys.modules och importerar om
    # den. En referens hämtad före pekar på den gamla modulinstansen, och
    # då patchar man ett tak som routen inte läser.
    webapp = sys.modules["web.app"]
    anrop: list[int] = []

    def fake_create(**kwargs):
        anrop.append(1)
        return _FakeResponse("Hej!")

    monkeypatch.setattr(app.state.pipeline.claude._client.messages, "create", fake_create)
    monkeypatch.setattr(webapp, "_CHAT_RATE_LIMIT", 3)
    webapp._chat_calls.clear()

    with TestClient(app) as client:
        for _ in range(3):
            assert client.post("/chat", json={"message": "hej"}).status_code == 200
        svar = client.post("/chat", json={"message": "hej"})

    assert svar.status_code == 429
    assert "för många" in svar.json()["detail"].lower()
    assert len(anrop) == 3, "det avvisade meddelandet ska inte ha nått Anthropic"
    webapp._chat_calls.clear()
