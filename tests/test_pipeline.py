"""Tester för analysis/pipeline.py (ingen riktig Claude-anrop, fejkad klient)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from analysis.pipeline import AnalysisPipeline
from sync.store import Store


class FakeClaudeClient:
    """Fejkad ClaudeClient som räknar antal anrop och spelar in argumenten
    istället för att nätverksanropa."""

    def __init__(self) -> None:
        self.model = "fake-model"
        self.call_count = 0
        self.last_kwargs: dict = {}
        self.last_user_data: str = ""
        self.last_system_prompt: str = ""

    def analyze(
        self,
        system_prompt: str,
        user_data: str,
        max_tokens: int = 2048,
        max_chars: int | None = None,
    ) -> str:
        self.call_count += 1
        self.last_kwargs = {"max_tokens": max_tokens, "max_chars": max_chars}
        self.last_user_data = user_data
        self.last_system_prompt = system_prompt
        return f"# Analys #{self.call_count}"


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "test.db")
    s.init()
    return s


def _make_activity(activity_id: str) -> dict:
    return {
        "id": activity_id,
        "name": "Test",
        "type": "Run",
        "sport": "Run",
        "start_time": "2024-01-15T10:30:00+00:00",
        "duration_seconds": 3600,
        "distance_meters": 10000,
        "average_heart_rate": 150,
        "max_heart_rate": 175,
        "average_watts": None,
        "normalized_watts": None,
        "average_cadence": None,
        "average_speed": None,
        "tss": 75.0,
        "intensity": None,
        "raw_json": "{}",
        "last_synced": "2024-01-15T12:00:00+00:00",
    }


def test_analyze_activity_regenerates_every_time(store: Store) -> None:
    """Regressionstest: analyze_activity ska INTE returnera en cachad analys
    för alltid. Tidigare kortslöts anropet permanent efter första gången,
    vilket gjorde "generera om"-knappen i webb-UI:t verkningslös."""
    store.upsert_activity(_make_activity("act1"))
    claude = FakeClaudeClient()
    pipeline = AnalysisPipeline(store, claude)

    first = pipeline.analyze_activity("act1")
    second = pipeline.analyze_activity("act1")

    assert claude.call_count == 2, "Claude ska anropas på nytt varje gång, inte bara första gången"
    assert first == "# Analys #1"
    assert second == "# Analys #2"

    # Båda analyserna ska ha sparats i historiken.
    latest = store.latest_activity_analysis("act1")
    assert latest is not None
    assert latest["markdown"] == "# Analys #2"


def test_every_analysis_enforces_the_same_char_limit(store: Store) -> None:
    """Alla analyser ska skicka max_chars till Claude-klienten (hårt tak,
    se analysis/claude.py::_truncate_markdown) och inte bara lita på att
    prompten nämner en gräns som modellen kan strunta i.

    Per-passanalysen saknade tidigare max_chars helt och körde på
    standardvärdena, alltså utan backstop: när tokenbudgeten tog slut
    kapade API:et texten mitt i en mening, utan ens markeringen
    "(avkortat)" som de andra analyserna får."""
    from analysis.pipeline import MAX_ANALYSIS_CHARS

    store.upsert_activity(_make_activity("act1"))
    claude = FakeClaudeClient()
    pipeline = AnalysisPipeline(store, claude)

    for run in (
        pipeline.coaching,
        pipeline.morning_recommendation,
        pipeline.evening_summary,
        lambda: pipeline.analyze_activity("act1"),
    ):
        claude.last_kwargs = {}
        run()
        assert claude.last_kwargs["max_chars"] == MAX_ANALYSIS_CHARS
        # max_tokens måste ligga med marginal över teckengränsen, annars
        # kapar API:et rått innan den städade avkortningen hinner köra.
        assert claude.last_kwargs["max_tokens"] * 2 > MAX_ANALYSIS_CHARS


def test_length_limit_in_the_prompt_matches_the_enforced_limit() -> None:
    """Prompttexten hade siffran inskriven för hand ("under 2000 tecken")
    medan backstoppen läste en konstant — de kunde alltså glida isär och
    säga olika saker om samma gräns."""
    from analysis.prompts import LENGTH_LIMIT, MAX_ANALYSIS_CHARS

    assert str(MAX_ANALYSIS_CHARS) in LENGTH_LIMIT


def test_activity_prompt_tells_claude_to_keep_it_short() -> None:
    """ACTIVITY-prompten beställer sju sektioner men saknade helt en
    längdinstruktion, till skillnad från de tre andra analystyperna."""
    from analysis.prompts import LENGTH_LIMIT, get_prompt

    for analysis_type in ("activity", "morning_recommendation", "daily_summary", "coaching"):
        assert LENGTH_LIMIT in get_prompt(analysis_type), (
            f"{analysis_type} saknar längdinstruktion"
        )


def testswedish_date_label_formats_weekday_correctly() -> None:
    """Regressionstest för rubrikformatet ('Torsdag 20 augusti 2026' istället
    för t.ex. '🌅 Morgonrekommendation – 2026-08-20'). Verifierar mot ett
    känt datum vars veckodag är lätt att slå upp oberoende (2026-08-20 var
    en torsdag)."""
    import datetime as dt

    from analysis.pipeline import swedish_date_label

    assert swedish_date_label("2026-08-20") == "Torsdag 20 augusti 2026"
    assert swedish_date_label("2026-08-20T07:15:00+00:00") == "Torsdag 20 augusti 2026"
    assert swedish_date_label(dt.date(2026, 8, 20)) == "Torsdag 20 augusti 2026"
    # En måndag, för att verifiera att veckodagsberäkningen inte bara
    # råkar stämma för ett enda datum.
    assert swedish_date_label("2026-08-24") == "Måndag 24 augusti 2026"


def test_morning_daily_and_activity_payloads_include_date_label(store: Store) -> None:
    """Regressionstest: payloads som skickas till Claude ska innehålla ett
    färdigberäknat date_label, så prompten kan instruera Claude att
    använda det ordagrant istället för att räkna ut veckodagen själv."""
    import json

    store.upsert_activity(_make_activity("act1"))
    claude = FakeClaudeClient()
    pipeline = AnalysisPipeline(store, claude)

    pipeline.morning_recommendation()
    assert "date_label" in json.loads(claude.last_user_data)

    pipeline.evening_summary()
    assert "date_label" in json.loads(claude.last_user_data)

    pipeline.analyze_activity("act1")
    payload = json.loads(claude.last_user_data)
    assert payload["date_label"] == "Måndag 15 januari 2024"  # start_time i _make_activity


# --- Styrketräning ---------------------------------------------------


def _make_strength_activity(activity_id: str, day: str = "2026-08-20") -> dict:
    a = _make_activity(activity_id)
    a["type"] = "WeightTraining"
    a["sport"] = "WeightTraining"
    a["start_time"] = f"{day}T17:00:00+00:00"
    return a


def test_log_strength_session_writes_one_row_per_set(store: Store) -> None:
    """Verktyget ska expandera Claudes set-lista till en rad per set och
    kvittera med en sammanfattning Claude kan återberätta."""
    pipeline = AnalysisPipeline(store, FakeClaudeClient())

    result = pipeline.execute_tool(
        "log_strength_session",
        {
            "exercise": "bänkpress",
            "sets": [
                {"reps": 8, "weight_kg": 80.0},
                {"reps": 8, "weight_kg": 80.0},
                {"reps": 6, "weight_kg": 80.0},
            ],
            "day": "2026-08-20",
        },
    )

    rows = store.get_strength_sets_for_date("2026-08-20")
    assert len(rows) == 3
    assert "bänkpress" in result
    assert "3 set" in result
    assert "22 reps" in result           # 8 + 8 + 6
    assert "1760" in result              # (8+8+6) * 80 kg volym


def test_log_strength_session_defaults_to_today(store: Store) -> None:
    from datetime import datetime

    pipeline = AnalysisPipeline(store, FakeClaudeClient())
    pipeline.execute_tool(
        "log_strength_session",
        {"exercise": "knäböj", "sets": [{"reps": 5, "weight_kg": 100.0}]},
    )

    today = datetime.now().date().isoformat()
    assert len(store.get_strength_sets_for_date(today)) == 1


def test_log_strength_session_links_the_days_single_strength_activity(store: Store) -> None:
    """Finns exakt ett styrkepass den dagen ska setsen kopplas dit, så de
    kan visas på passets sida och tas med i per-passanalysen."""
    store.upsert_activity(_make_strength_activity("gym1", day="2026-08-20"))
    pipeline = AnalysisPipeline(store, FakeClaudeClient())

    pipeline.execute_tool(
        "log_strength_session",
        {
            "exercise": "marklyft",
            "sets": [{"reps": 5, "weight_kg": 120.0}],
            "day": "2026-08-20",
        },
    )

    linked = store.get_strength_sets_for_activity("gym1")
    assert len(linked) == 1
    assert linked[0]["exercise"] == "marklyft"


def test_log_strength_session_does_not_guess_between_two_strength_activities(
    store: Store,
) -> None:
    """Med två styrkepass samma dag går det inte att veta vilket setsen hörde
    till. Setsen ska sparas ändå (och synas i dagsanalysen), men utan att
    gissa fram en koppling som skulle visa fel data på fel passida."""
    store.upsert_activity(_make_strength_activity("gym1", day="2026-08-20"))
    store.upsert_activity(_make_strength_activity("gym2", day="2026-08-20"))
    pipeline = AnalysisPipeline(store, FakeClaudeClient())

    pipeline.execute_tool(
        "log_strength_session",
        {
            "exercise": "knäböj",
            "sets": [{"reps": 5, "weight_kg": 100.0}],
            "day": "2026-08-20",
        },
    )

    assert store.get_strength_sets_for_activity("gym1") == []
    assert store.get_strength_sets_for_activity("gym2") == []
    rows = store.get_strength_sets_for_date("2026-08-20")
    assert len(rows) == 1
    assert rows[0]["activity_id"] is None


def test_log_strength_session_rejects_incomplete_input(store: Store) -> None:
    """Claude kan skicka ofullständig input; det ska ge ett tydligt fel
    tillbaka som modellen kan agera på, inte en tyst tom rad i databasen."""
    pipeline = AnalysisPipeline(store, FakeClaudeClient())

    assert "Fel" in pipeline.execute_tool(
        "log_strength_session", {"exercise": "", "sets": [{"reps": 5}]}
    )
    assert "Fel" in pipeline.execute_tool(
        "log_strength_session", {"exercise": "knäböj", "sets": []}
    )
    assert store.list_strength_sets(days=30) == []


def testfmt_strength_sessions_aggregates_volume_per_exercise() -> None:
    """Volym = summan av reps x vikt. Aggregeringen håller payloaden till
    Claude kompakt (ett gympass är lätt 20+ rader råa set)."""
    from analysis.pipeline import fmt_strength_sessions

    rows = [
        {"day": "2026-08-20", "exercise": "bänkpress", "reps": 8, "weight_kg": 80.0},
        {"day": "2026-08-20", "exercise": "bänkpress", "reps": 6, "weight_kg": 85.0},
        {"day": "2026-08-20", "exercise": "chins", "reps": 6, "weight_kg": None},
    ]
    sessions = {s["exercise"]: s for s in fmt_strength_sessions(rows)}

    bench = sessions["bänkpress"]
    assert bench["sets"] == 2
    assert bench["reps_total"] == 14
    assert bench["top_weight_kg"] == 85.0
    assert bench["volume_kg"] == 8 * 80.0 + 6 * 85.0   # 1150.0

    # Kroppsviktsövning: reps räknas, men ingen volym och ingen toppvikt.
    chins = sessions["chins"]
    assert chins["sets"] == 1
    assert chins["reps_total"] == 6
    assert chins["volume_kg"] == 0.0
    assert "top_weight_kg" not in chins


def testfmt_wellness_history_includes_or_omits_steps() -> None:
    """Morgonanalysen utesluter steg (natten har bara ett par hundra från
    innan uppvaknandet, inte en dagssiffra), kvällsanalysen och chatten
    vill ha dem. include_steps styr det utan att duplicera fältlistan."""
    from analysis.pipeline import fmt_wellness_history

    rows = [{"day": "2026-08-20", "hrv": 55.0, "resting_hr": 52, "steps": 8000}]

    with_steps = fmt_wellness_history(rows)[0]
    assert with_steps["steps"] == 8000
    assert with_steps["hrv"] == 55.0

    without_steps = fmt_wellness_history(rows, include_steps=False)[0]
    assert "steps" not in without_steps
    assert without_steps["hrv"] == 55.0


def test_all_analyses_include_strength_in_payload(store: Store) -> None:
    """Regressionstest: styrkedatan är värdelös om den inte faktiskt når
    Claude. Alla fyra dagsanalyser ska skicka med 'strength'."""
    import json

    store.add_strength_sets(
        day=__import__("datetime").datetime.now().date().isoformat(),
        exercise="bänkpress",
        sets=[{"reps": 8, "weight_kg": 80.0}],
    )
    claude = FakeClaudeClient()
    pipeline = AnalysisPipeline(store, claude)

    for run in (
        pipeline.coaching,
        pipeline.morning_recommendation,
        pipeline.evening_summary,
    ):
        run()
        payload = json.loads(claude.last_user_data)
        assert "strength" in payload, f"{run.__name__} saknar strength i payloaden"
        assert payload["strength"][0]["exercise"] == "bänkpress"


def test_analyze_activity_includes_raw_sets_for_that_activity(store: Store) -> None:
    """Per-passanalysen ska få de RÅA setsen (inte den aggregerade formen),
    så den kan kommentera enskilda set — och bara passets egna."""
    import json

    store.upsert_activity(_make_strength_activity("gym1"))
    store.add_strength_sets(
        day="2026-08-20",
        exercise="marklyft",
        sets=[{"reps": 5, "weight_kg": 120.0}, {"reps": 3, "weight_kg": 130.0}],
        activity_id="gym1",
    )
    store.add_strength_sets(
        day="2026-08-20", exercise="annat pass", sets=[{"reps": 10}], activity_id="gym2"
    )

    claude = FakeClaudeClient()
    AnalysisPipeline(store, claude).analyze_activity("gym1")

    payload = json.loads(claude.last_user_data)
    assert len(payload["strength_sets"]) == 2
    assert payload["strength_sets"][0]["weight_kg"] == 120.0
    assert payload["strength_sets"][1]["set_number"] == 2
    assert all(s["exercise"] == "marklyft" for s in payload["strength_sets"])


# --- Atletprofilen: kommer från .env, vikt och ålder följer datan ---


def _profile(**kw):
    """Testprofil. Uppgifterna är konfiguration (ATHLETE_* i .env), inte
    kod — de stod tidigare hårdkodade i prompts.py, vilket la
    personuppgifter inklusive hälsodata i källkod och git-historik."""
    import datetime as dt

    from config import AthleteProfile

    # Påhittade värden — testdata ska inte innehålla någons riktiga
    # personuppgifter.
    defaults = dict(sex="man", birth_date=dt.date(1980, 5, 14), height_cm=182)
    defaults.update(kw)
    return AthleteProfile(**defaults)


def test_profile_uses_the_latest_known_weight(store: Store) -> None:
    """Vikten stod hårdkodad som 123 kg i prompten, trots att den synkas
    från Intervals och kan loggas i chatten — och trots att viktnedgång
    är atletens mål. Claude resonerade alltså alltid från en siffra som
    aldrig ändrades."""
    from analysis.prompts import athlete_profile

    profile = athlete_profile(_profile(), weight_kg=118.4, weight_day="2026-08-20")
    assert "118.4 kg" in profile
    assert "2026-08-20" in profile
    assert "123 kg" not in profile


def test_profile_says_weight_is_unknown_rather_than_guessing() -> None:
    from analysis.prompts import athlete_profile

    profile = athlete_profile(_profile(), weight_kg=None)
    assert "okänd" in profile
    # Ingen påhittad siffra.
    assert "kg" not in profile.split("- Medicinsk")[0].split("Vikt:")[1]


def test_age_is_calculated_not_hardcoded() -> None:
    """Åldern stod som '48 år' i prompten och hade tyst blivit fel på
    nästa födelsedag."""
    import datetime as dt

    from analysis.prompts import athlete_profile

    born = _profile()
    # Dagen före födelsedagen.
    assert "45 år" in athlete_profile(born, today=dt.date(2026, 5, 13))
    # På födelsedagen.
    assert "46 år" in athlete_profile(born, today=dt.date(2026, 5, 14))
    assert "46 år" in athlete_profile(born, today=dt.date(2027, 1, 1))


def test_analyses_send_the_current_weight_to_claude(store: Store) -> None:
    """End-to-end: en vikt i wellness ska dyka upp i systemprompten."""
    store.upsert_wellness_many([{
        "day": "2026-08-20", "rest_day": 0, "ctl": 40.0, "atl": 30.0, "tsb": 10.0,
        "ramp_rate": None, "sleep_seconds": 25000, "sleep_score": 80,
        "sleep_quality": 2, "avg_sleeping_hr": 50, "weight": 118.4,
        "resting_hr": 55, "hrv": 40.0, "hrv_sdnn": None, "stress": None,
        "respiration": None, "spO2": None, "systolic": None, "diastolic": None,
        "hydration": None, "soreness": None, "fatigue": None, "mood": None,
        "motivation": None, "injury": None, "readiness": None, "vo2max": None,
        "steps": 5000, "kcal_consumed": None, "raw_json": "{}",
        "last_synced": "2026-08-20T00:00:00",
    }])
    claude = FakeClaudeClient()
    pipeline = AnalysisPipeline(store, claude)

    pipeline.morning_recommendation()

    assert "118.4 kg" in claude.last_system_prompt


def test_a_weight_logged_in_chat_wins_when_it_is_newer(store: Store) -> None:
    """Vägde du dig och berättade det i chatten idag är det färskare än
    vågens senaste synk."""
    store.upsert_wellness_many([{
        "day": "2026-08-18", "rest_day": 0, "ctl": 40.0, "atl": 30.0, "tsb": 10.0,
        "ramp_rate": None, "sleep_seconds": 25000, "sleep_score": 80,
        "sleep_quality": 2, "avg_sleeping_hr": 50, "weight": 122.0,
        "resting_hr": 55, "hrv": 40.0, "hrv_sdnn": None, "stress": None,
        "respiration": None, "spO2": None, "systolic": None, "diastolic": None,
        "hydration": None, "soreness": None, "fatigue": None, "mood": None,
        "motivation": None, "injury": None, "readiness": None, "vo2max": None,
        "steps": 5000, "kcal_consumed": None, "raw_json": "{}",
        "last_synced": "2026-08-18T00:00:00",
    }])
    store.add_self_report(day="2026-08-21", category="weight", value=119.5)

    weight = store.latest_weight()
    assert weight == (119.5, "2026-08-21")


def test_token_cap_can_never_bind_before_the_char_limit() -> None:
    """Grundorsaken till att analyser slutade mitt i en mening: taket låg
    på 2000 tokens mot 3500 tecken, alltså UNDER gränsen det skulle
    skydda. Tokenbudgeten tog slut först, och eftersom ett kapat svar är
    kortare än max_chars hann den städade avkortningen aldrig köra.

    En token motsvarar aldrig färre än ett tecken, så tokentaket måste
    vara minst lika högt som teckengränsen för att max_chars garanterat
    ska binda först."""
    from analysis.pipeline import MAX_ANALYSIS_CHARS, MAX_ANALYSIS_TOKENS

    assert MAX_ANALYSIS_TOKENS >= MAX_ANALYSIS_CHARS


def test_chat_token_cap_can_never_bind_before_its_char_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Chatten hade samma fel: 2000 tokens mot 2500 tecken."""
    # web.app bygger appen vid import och kräver därför konfiguration.
    # Utan detta passerade testet bara när något annat testmodul redan
    # hunnit importera modulen med miljövariabler satta — alltså beroende
    # på körordning.
    for key, value in {
        "INTERVALS_API_KEY": "fake", "INTERVALS_ATHLETE_ID": "i1",
        "ANTHROPIC_API_KEY": "fake", "WEB_ACCESS_TOKEN": "",
        "DB_PATH": str(tmp_path / "t.db"),
    }.items():
        monkeypatch.setenv(key, value)

    import importlib
    import sys

    sys.modules.pop("web.app", None)
    webapp = importlib.import_module("web.app")

    assert webapp._MAX_CHAT_TOKENS >= webapp._MAX_CHAT_CHARS


def test_profile_comes_from_config_not_from_the_source(store: Store) -> None:
    """Regressionsskydd: atletprofilen låg hårdkodad i prompts.py, alltså
    personuppgifter — inklusive hälsodata — i källkoden och i hela
    git-historiken. Den som kör appen ska beskriva sig själv i .env, inte
    redigera Python."""
    from pathlib import Path as _Path

    source = (_Path(__file__).resolve().parents[1] / "src" / "analysis" / "prompts.py")
    text = source.read_text(encoding="utf-8")

    # Inga konkreta personuppgifter kvar i prompttexten.
    for leaked in ("1977", "197 cm", "förmaksflimmer", "123 kg"):
        assert leaked not in text, f"{leaked!r} är hårdkodat i prompts.py"


def test_empty_profile_tells_claude_not_to_guess() -> None:
    """Utan ifylld profil ska prompten säga åt Claude att avstå, inte låta
    den anta kön, ålder eller hälsa."""
    from analysis.prompts import athlete_profile

    profile = athlete_profile()

    assert "ingen profil ifylld" in profile
    assert "undvik antaganden" in profile


def test_profile_omits_fields_that_are_not_filled_in() -> None:
    """Halvifylld profil ska ge halv profil, inte platshållare."""
    from analysis.prompts import athlete_profile
    from config import AthleteProfile

    profile = athlete_profile(AthleteProfile(sex="kvinna", goal="springa ett maraton"))

    assert "- Kön: kvinna" in profile
    assert "springa ett maraton" in profile
    for absent in ("Ålder", "Längd", "Medicinskt", "Träningsrutin"):
        assert absent not in profile


def test_analyses_use_the_configured_profile(store: Store) -> None:
    """End-to-end: profilen från konfigurationen ska nå systemprompten."""
    claude = FakeClaudeClient()
    pipeline = AnalysisPipeline(
        store, claude, _profile(medical="astma", goal="må bra")
    )

    pipeline.morning_recommendation()

    assert "- Kön: man" in claude.last_system_prompt
    assert "182 cm" in claude.last_system_prompt
    assert "astma" in claude.last_system_prompt
    assert "må bra" in claude.last_system_prompt


# --- Tilltalsnamn i prompten ------------------------------------------


def test_profile_includes_the_name_and_asks_claude_to_use_it() -> None:
    """Analyserna ska tilltala atleten vid namn. Bara namnet i profilen
    räcker inte — utan en instruktion vet Claude att namnet finns men
    använder det inte nödvändigtvis."""
    from analysis.prompts import athlete_profile
    from config import AthleteProfile

    profile = athlete_profile(AthleteProfile(name="Anna", sex="man"))

    assert "- Namn: Anna" in profile
    assert "Tilltala atleten vid namn" in profile
    # Ska hållas sparsamt, annars blir varje stycke "Anna, ...".
    assert "Sparsamt" in profile
    # H1:an är reserverad för datumet (se TITLE_INSTRUCTION).
    assert "Aldrig i rubriker" in profile


def test_profile_without_a_name_says_nothing_about_names() -> None:
    """ATHLETE_NAME är frivilligt — utan namn ska prompten varken innehålla
    en tom namnrad eller en instruktion om att tilltala någon."""
    from analysis.prompts import athlete_profile
    from config import AthleteProfile

    profile = athlete_profile(AthleteProfile(sex="man"))

    assert "Namn" not in profile
    assert "Tilltala" not in profile


def test_a_name_alone_does_not_count_as_a_filled_in_profile() -> None:
    """Regressionstest: spärren för tom profil räknade rader
    (`len(lines) == 2`) i stället för faktiskt ifyllda fält. Ett namn hade
    då tystat varningen om att inte gissa ålder, kön och hälsa — trots att
    ett namn inte säger något alls om kroppen."""
    from analysis.prompts import athlete_profile
    from config import AthleteProfile

    profile = athlete_profile(AthleteProfile(name="Anna"))

    assert "- Namn: Anna" in profile
    assert "ingen profil ifylld" in profile
    assert "undvik antaganden" in profile


def test_name_reaches_the_system_prompt_of_every_analysis(store: Store) -> None:
    """End-to-end: namnet ska nå prompten i alla analystyper, inte bara
    dyka upp i profilfunktionen."""
    claude = FakeClaudeClient()
    pipeline = AnalysisPipeline(store, claude, _profile(name="Anna"))

    for run in (
        pipeline.coaching,
        pipeline.morning_recommendation,
        pipeline.evening_summary,
    ):
        run()
        assert "- Namn: Anna" in claude.last_system_prompt, (
            f"{run.__name__} skickade en prompt utan namnet"
        )
        assert "Tilltala atleten vid namn" in claude.last_system_prompt

    assert claude.call_count == 3


def test_swedish_weekday_genitive() -> None:
    """Alla svenska veckodagar slutar på -dag, så bestämd genitiv är
    alltid ändelsen -ens (fredag -> fredagen -> fredagens)."""
    import datetime as dt

    from analysis.pipeline import swedish_weekday_genitive

    # 2026-08-24 är en måndag.
    expected = [
        "Måndagens", "Tisdagens", "Onsdagens", "Torsdagens",
        "Fredagens", "Lördagens", "Söndagens",
    ]
    for offset, word in enumerate(expected):
        assert swedish_weekday_genitive(dt.date(2026, 8, 24) + dt.timedelta(days=offset)) == word


def test_swedish_weekday_genitive_accepts_the_same_types_as_date_label() -> None:
    """Tar date, datetime och ISO-sträng, precis som swedish_date_label."""
    import datetime as dt

    from analysis.pipeline import swedish_weekday_genitive

    assert swedish_weekday_genitive(dt.date(2026, 8, 28)) == "Fredagens"
    assert swedish_weekday_genitive(dt.datetime(2026, 8, 28, 23, 30)) == "Fredagens"
    assert swedish_weekday_genitive("2026-08-28T07:00:00") == "Fredagens"


# --- Tokentak kontra tänkande -----------------------------------------

# Uppmätt mot claude-sonnet-5 på den skarpa morgonprompten: svensk
# markdown ger ~2,0 tecken/token. Avrundat nedåt till 1,6 här så testet
# inte blir falskt grönt om tokeniseringen råkar bli sämre än uppmätt.
_CHARS_PER_TOKEN_SV = 1.6

# Andelen av svaret som gick till thinking i samma mätning var 54 %. Ett
# tak som bara rymmer texten räcker alltså inte — vi kräver att det finns
# minst lika mycket utrymme kvar till tänkandet som texten behöver.
_MIN_THINKING_HEADROOM_RATIO = 1.0


def test_analysis_token_ceiling_leaves_room_for_thinking() -> None:
    """Regressionstest: MAX_ANALYSIS_TOKENS var satt till MAX_ANALYSIS_CHARS
    med motiveringen att en token aldrig är färre än ett tecken. Det
    stämmer, men Sonnet 5 returnerar thinking-block utan att man ber om
    det, och de dras från samma max_tokens som svaret. Morgonanalysen
    kapades därför mitt i — före själva rekommendationen — fyra gånger på
    en vecka i drift.

    Invarianten är att max_chars ska binda FÖRE max_tokens, så att ett
    långt svar klipps städat av _truncate_markdown i stället för rått av
    API:et."""
    from analysis.pipeline import MAX_ANALYSIS_CHARS, MAX_ANALYSIS_TOKENS

    tokens_for_text = MAX_ANALYSIS_CHARS / _CHARS_PER_TOKEN_SV
    headroom = MAX_ANALYSIS_TOKENS - tokens_for_text

    assert headroom >= tokens_for_text * _MIN_THINKING_HEADROOM_RATIO, (
        f"MAX_ANALYSIS_TOKENS={MAX_ANALYSIS_TOKENS} lämnar bara {headroom:.0f} "
        f"tokens till tänkande efter {tokens_for_text:.0f} för texten — "
        "för snålt, svaret kapas innan max_chars hinner binda"
    )


def test_every_analysis_sends_the_raised_token_ceiling(store: Store) -> None:
    """Taket ska nå ända fram till API-anropet för alla analystyper, inte
    bara stå som en konstant."""
    from analysis.pipeline import MAX_ANALYSIS_TOKENS

    store.upsert_activity(_make_activity("act1"))
    claude = FakeClaudeClient()
    pipeline = AnalysisPipeline(store, claude)

    for run in (
        pipeline.coaching,
        pipeline.morning_recommendation,
        pipeline.evening_summary,
        lambda: pipeline.analyze_activity("act1"),
    ):
        claude.last_kwargs = {}
        run()
        assert claude.last_kwargs["max_tokens"] == MAX_ANALYSIS_TOKENS


def test_coaching_payload_and_prompt_carry_the_date_label(store: Store) -> None:
    """Regressionstest: coaching var den enda analysen utan date_label i
    payloaden OCH utan TITLE_INSTRUCTION i prompten. Claude skrev därför
    datumet på egen hand utifrån generated_at och landade i "26 augusti
    2026" — utan veckodag, och i ett annat format än övriga analyser."""
    import json

    from analysis.prompts import TITLE_INSTRUCTION, get_prompt

    claude = FakeClaudeClient()
    AnalysisPipeline(store, claude).coaching()

    payload = json.loads(claude.last_user_data)
    assert "date_label" in payload, "coaching-payloaden saknar date_label"
    # Samma format som de andra analyserna: veckodag, dag, månad, år.
    assert payload["date_label"].split()[0] in (
        "Måndag", "Tisdag", "Onsdag", "Torsdag", "Fredag", "Lördag", "Söndag"
    ), f"date_label saknar veckodag: {payload['date_label']!r}"

    assert TITLE_INSTRUCTION in get_prompt("coaching")


def test_every_analysis_prompt_carries_the_title_instruction() -> None:
    """Alla fyra analystyper ska instrueras att använda date_label
    ordagrant — annars skriver modellen sitt eget datumformat."""
    from analysis.prompts import TITLE_INSTRUCTION, get_prompt

    for analysis_type in ("morning_recommendation", "daily_summary", "coaching", "activity"):
        assert TITLE_INSTRUCTION in get_prompt(analysis_type), (
            f"{analysis_type} saknar rubrikinstruktionen"
        )


def test_morning_payload_leaves_out_step_counts(store: Store) -> None:
    """Morgonen läser dygnet innan det hänt.

    Dagens wellness-rad innehåller då ett par hundra steg från nattens
    rörelse, och modellen läste dem som en dagssiffra: "Igår (29/8) syns
    ingen aktivitet i loggen – total vilodag, endast 180 steg registrerat".
    Yesterday hade i själva verket 13 043 steg; de 180 var nattens.

    Steg säger ingenting om hur natten gick, vilket är det morgonanalysen
    handlar om, så de utelämnas helt i stället för att prompten ska behöva
    förklara bort dem. Kvällssammanfattningen behåller dem — där är dygnet
    slut och siffran betyder något."""
    import json
    from datetime import date

    today = date.today().isoformat()
    row = dict.fromkeys(
        "day rest_day ctl atl tsb ramp_rate sleep_seconds sleep_score "
        "sleep_quality avg_sleeping_hr weight resting_hr hrv hrv_sdnn stress "
        "respiration spO2 systolic diastolic hydration soreness fatigue mood "
        "motivation injury readiness vo2max steps kcal_consumed raw_json "
        "last_synced".split()
    )
    row.update({
        "day": today, "sleep_seconds": 25000, "sleep_score": 80, "hrv": 60.0,
        "resting_hr": 55, "steps": 180, "raw_json": "{}",
        "last_synced": f"{today}T06:00:00",
    })
    store.upsert_wellness_many([row])

    claude = FakeClaudeClient()
    pipeline = AnalysisPipeline(store, claude)

    pipeline.morning_recommendation()
    morgon = json.loads(claude.last_user_data)
    assert "steps" not in morgon["last_night_sleep"]
    assert all("steps" not in dag for dag in morgon["wellness_history"])
    # Stegsiffran ska inte heller läcka in via något annat fält (den låg
    # tidigare kvar i wellness-radens raw_json). generated_at utelämnas ur
    # sökningen: det är en tidsstämpel med mikrosekunder, och den råkar
    # innehålla "180" i ungefär 0,4 % av körningarna — testet föll alltså
    # slumpmässigt ungefär var 250:e gång utan att något var fel.
    stabilt = {k: v for k, v in morgon.items() if k != "generated_at"}
    assert "180" not in json.dumps(stabilt, ensure_ascii=False)

    pipeline.evening_summary()
    kväll = json.loads(claude.last_user_data)
    assert kväll["last_night_sleep"]["steps"] == 180
    assert any("steps" in dag for dag in kväll["wellness_history"])


def test_no_analysis_payload_carries_raw_json(store: Store) -> None:
    """raw_json ska aldrig följa med till Claude.

    Kolumnen håller hela Intervals-svaret för dygnet — exakt samma
    mätvärden som kolumnerna bredvid, bara under Intervals egna
    camelCase-namn. coaching() och daily_summary() la wellness-raderna
    rakt i payloaden (list_wellness är SELECT *), så dubbletten följde
    med varje gång.

    Uppmätt på skarp data innan fixen: 28 582 av coachinganalysens
    49 507 tecken wellness var raw_json — 58 %, varje dag kl 12. Den
    äldre daily_summary (90 dagar) skickade 148 506 tecken av samma skäl.
    Morgon- och kvällsanalysen använde redan den kompakta
    fmt_wellness_history för sin dagserie, men skickade raw_json i
    last_night_sleep.

    last_synced åker med av samma skäl: en synkstämpel säger ingenting om
    kroppen.
    """
    from datetime import date

    today = date.today().isoformat()
    row = dict.fromkeys(
        "day rest_day ctl atl tsb ramp_rate sleep_seconds sleep_score "
        "sleep_quality avg_sleeping_hr weight resting_hr hrv hrv_sdnn stress "
        "respiration spO2 systolic diastolic hydration soreness fatigue mood "
        "motivation injury readiness vo2max steps kcal_consumed raw_json "
        "last_synced".split()
    )
    row.update({
        "day": today, "sleep_seconds": 25000, "sleep_score": 80, "hrv": 60.0,
        "resting_hr": 55, "ctl": 42.0, "atl": 38.0, "tsb": 4.0,
        # Ett värde som bara finns i raw_json, så testet inte kan passera
        # på att rådatan råkade vara tom.
        "raw_json": '{"sleepSecs": 25000, "SPARARE_UNIKT": "får inte synas"}',
        "last_synced": f"{today}T06:00:00",
    })
    store.upsert_wellness_many([row])

    claude = FakeClaudeClient()
    pipeline = AnalysisPipeline(store, claude)

    for namn, kör in (
        ("morning_recommendation", pipeline.morning_recommendation),
        ("evening_summary", pipeline.evening_summary),
        ("coaching", pipeline.coaching),
    ):
        kör()
        assert "SPARARE_UNIKT" not in claude.last_user_data, (
            f"{namn} skickar raw_json till Claude"
        )
        assert "last_synced" not in claude.last_user_data, (
            f"{namn} skickar synkstämpeln till Claude"
        )
        # De riktiga mätvärdena ska finnas kvar — fixen tar bort
        # dubbletten, inte information.
        assert "25000" in claude.last_user_data, (
            f"{namn} tappade sömndatan när raw_json rensades"
        )


def test_wellness_payload_keeps_every_real_measurement(store: Store) -> None:
    """Bara raw_json och last_synced faller bort — inget mätvärde.

    Alternativet var att köra allt genom fmt_wellness_history, som bara
    behåller nio fält. Det hade tagit bort stress, readiness, vikt och
    resten ur coachinganalysens underlag, vilket är en helt annan sorts
    ändring än att sluta skicka en dubblett."""
    from analysis.pipeline import wellness_for_payload

    row = {
        "day": "2026-09-01", "hrv": 60.0, "stress": 22, "readiness": 78,
        "weight": 91.2, "vo2max": 48.0, "steps": 9000,
        "raw_json": "{}", "last_synced": "2026-09-01T06:00:00",
    }
    ut = wellness_for_payload(row)
    assert ut is not None
    assert set(ut) == {
        "day", "hrv", "stress", "readiness", "weight", "vo2max", "steps",
    }

    utan_steg = wellness_for_payload(row, include_steps=False)
    assert utan_steg is not None
    assert "steps" not in utan_steg
    assert utan_steg["readiness"] == 78

    assert wellness_for_payload(None) is None


def test_evening_summary_marks_whether_the_day_had_anything_to_summarise(
    store: Store,
) -> None:
    """Dashboarden faller tillbaka på gårdagens sammanfattning när dagens
    skrevs om ett tomt dygn (se api_evening_analysis). Markören säger om
    dygnet hade något NÄR texten skrevs, och den ska vända när en natt
    registrerats — annars döljer en gammal tom markör en riktig
    sammanfattning."""
    from datetime import date

    from analysis.pipeline import EVENING_CONTENT_KEY

    today = date.today().isoformat()
    claude = FakeClaudeClient()
    pipeline = AnalysisPipeline(store, claude)

    # Tomt dygn: varken pass eller registrerad natt.
    pipeline.evening_summary()
    assert store.get_job_state(EVENING_CONTENT_KEY) == f"{today}:0"

    # Med en registrerad natt ska markören vända.
    row = dict.fromkeys(
        "day rest_day ctl atl tsb ramp_rate sleep_seconds sleep_score "
        "sleep_quality avg_sleeping_hr weight resting_hr hrv hrv_sdnn stress "
        "respiration spO2 systolic diastolic hydration soreness fatigue mood "
        "motivation injury readiness vo2max steps kcal_consumed raw_json "
        "last_synced".split()
    )
    row.update({"day": today, "sleep_seconds": 25000, "raw_json": "{}"})
    store.upsert_wellness_many([row])

    pipeline.evening_summary()
    assert store.get_job_state(EVENING_CONTENT_KEY) == f"{today}:1"


# --- Progressionshistorik i per-passanalysen -------------------------


def _payload(claude: FakeClaudeClient) -> dict:
    import json

    return json.loads(claude.last_user_data)


def test_activity_analysis_includes_history_for_the_same_exercises(
    store: Store,
) -> None:
    """Analysen ska kunna bedöma progression.

    Prompten (BASE + ACTIVITY) ber uttryckligen om "hur det står sig mot
    tidigare pass med samma övningar", men payloaden innehöll länge bara
    passets egna set — så svaret blev "utan historik för dessa övningar kan
    jag inte bedöma progression över tid" fast databasen hade underlaget.
    """
    store.upsert_activity(_make_strength_activity("gym-idag", day="2026-08-20"))
    for day, weight in (("2026-07-30", 50.0), ("2026-08-06", 52.5)):
        store.add_strength_sets(day, "axelpress", [{"reps": 5, "weight_kg": weight}])
    store.add_strength_sets(
        "2026-08-20", "axelpress", [{"reps": 5, "weight_kg": 55.0}], activity_id="gym-idag"
    )

    claude = FakeClaudeClient()
    AnalysisPipeline(store, claude).analyze_activity("gym-idag")

    history = _payload(claude)["strength_history"]
    assert [h["day"] for h in history] == ["2026-08-06", "2026-07-30"]
    assert [h["top_weight_kg"] for h in history] == [52.5, 50.0]


def test_activity_history_leaves_out_the_analysed_session_itself(store: Store) -> None:
    """Passets egna set ligger redan i strength_sets. Kom de även tillbaka i
    historiken skulle Claude läsa dagens pass som två pass."""
    store.upsert_activity(_make_strength_activity("gym-idag", day="2026-08-20"))
    store.add_strength_sets("2026-08-13", "knäböj", [{"reps": 5, "weight_kg": 100.0}])
    store.add_strength_sets(
        "2026-08-20", "knäböj", [{"reps": 5, "weight_kg": 110.0}], activity_id="gym-idag"
    )

    claude = FakeClaudeClient()
    AnalysisPipeline(store, claude).analyze_activity("gym-idag")

    payload = _payload(claude)
    assert [h["day"] for h in payload["strength_history"]] == ["2026-08-13"]
    assert payload["strength_sets"][0]["weight_kg"] == 110.0


def test_activity_history_only_covers_the_exercises_that_were_done(store: Store) -> None:
    """Historik för övningar passet inte innehöll är brus, inte underlag."""
    store.upsert_activity(_make_strength_activity("gym-idag", day="2026-08-20"))
    store.add_strength_sets("2026-08-13", "bänkpress", [{"reps": 5, "weight_kg": 80.0}])
    store.add_strength_sets("2026-08-13", "marklyft", [{"reps": 5, "weight_kg": 130.0}])
    store.add_strength_sets(
        "2026-08-20", "marklyft", [{"reps": 3, "weight_kg": 135.0}], activity_id="gym-idag"
    )

    claude = FakeClaudeClient()
    AnalysisPipeline(store, claude).analyze_activity("gym-idag")

    assert {h["exercise"] for h in _payload(claude)["strength_history"]} == {"marklyft"}


def test_activity_without_earlier_sessions_gets_no_history_field(store: Store) -> None:
    """Första passet med en övning har inget att jämföra mot. Då ska fältet
    utebli helt, inte skickas tomt — prompten säger åt Claude att använda
    det som finns, och en tom lista inbjuder till att kommentera tomheten."""
    store.upsert_activity(_make_strength_activity("gym-idag", day="2026-08-20"))
    store.add_strength_sets(
        "2026-08-20", "släde", [{"reps": 5, "weight_kg": 60.0}], activity_id="gym-idag"
    )

    claude = FakeClaudeClient()
    AnalysisPipeline(store, claude).analyze_activity("gym-idag")

    assert "strength_history" not in _payload(claude)


def test_run_analysis_carries_no_strength_history(store: Store) -> None:
    """Ett löppass har inga set, och ska inte dra med sig gymhistorik."""
    store.upsert_activity(_make_activity("lopp"))
    store.add_strength_sets("2024-01-10", "knäböj", [{"reps": 5, "weight_kg": 100.0}])

    claude = FakeClaudeClient()
    AnalysisPipeline(store, claude).analyze_activity("lopp")

    payload = _payload(claude)
    assert "strength_history" not in payload
    assert "strength_sets" not in payload


def test_uniform_sets_report_reps_per_set_not_just_the_sum(store: Store) -> None:
    """reps_total är en summa, men "3 set, 15 reps" läses lätt som 3x15 —
    och gjorde det: en skarp analys beskrev 3 set x 5 reps som "3x15 @ 130
    kg", ett pass som aldrig kördes."""
    from analysis.pipeline import fmt_strength_sessions

    rows = [
        {"day": "2026-08-27", "exercise": "marklyft", "reps": 5, "weight_kg": 130.0}
        for _ in range(3)
    ]

    (session,) = fmt_strength_sessions(rows)

    assert session["sets"] == 3
    assert session["reps_total"] == 15
    assert session["reps_per_set"] == 5


def test_varying_sets_report_no_reps_per_set(store: Store) -> None:
    """Droppset (8/7/5) har inget reps-per-set. Då ska fältet utebli hellre
    än att ljuga med ett snitt."""
    from analysis.pipeline import fmt_strength_sessions

    rows = [
        {"day": "2026-08-27", "exercise": "bänkpress", "reps": r, "weight_kg": 80.0}
        for r in (8, 7, 5)
    ]

    (session,) = fmt_strength_sessions(rows)

    assert session["reps_total"] == 20
    assert "reps_per_set" not in session


def test_aggregation_leaks_no_internal_bookkeeping(store: Store) -> None:
    """Mellanlagringen för reps får inte följa med ut i payloaden till Claude."""
    from analysis.pipeline import fmt_strength_sessions

    (session,) = fmt_strength_sessions(
        [{"day": "2026-08-27", "exercise": "knäböj", "reps": 5, "weight_kg": 100.0}]
    )

    assert not [k for k in session if k.startswith("_")]


# --- Rätta och radera självrapporter ----------------------------------


def test_correct_self_report_changes_only_the_fields_given(store: Store) -> None:
    """self_reports var append-only. Loggade Claude fel vikt följde raden
    med i morgon- och kvällsanalysen i fjorton dagar och i
    träningsanalysen i trettio, och enda boteboten var sqlite3 för hand.

    Att utelämna ett fält betyder "lämna som det är" — annars hade en
    rättad vikt raderat anteckningen bredvid."""
    pipeline = AnalysisPipeline(store, FakeClaudeClient())
    report_id = store.add_self_report(
        day="2026-09-02", category="weight", value=122.3, note="efter frukost"
    )

    svar = pipeline.execute_tool(
        "correct_self_report", {"report_id": report_id, "value": 121.8}
    )

    rad = store.get_self_report(report_id)
    assert rad is not None
    assert rad["value"] == 121.8
    assert rad["note"] == "efter frukost", "anteckningen skulle lämnas orörd"
    assert rad["day"] == "2026-09-02"
    assert "121.8" in svar and "122.3" in svar, (
        "kvittensen ska visa både före och efter, så det syns i chatten"
    )


def test_correct_self_report_can_move_a_report_to_another_day(store: Store) -> None:
    """'Det där var igår, inte idag' — en av de vanligaste rättningarna."""
    pipeline = AnalysisPipeline(store, FakeClaudeClient())
    report_id = store.add_self_report(day="2026-09-03", category="alcohol", note="två öl")

    pipeline.execute_tool(
        "correct_self_report", {"report_id": report_id, "day": "2026-09-02"}
    )

    rad = store.get_self_report(report_id)
    assert rad is not None
    assert rad["day"] == "2026-09-02"


def test_correct_self_report_refuses_an_unknown_id(store: Store) -> None:
    """Ett id Claude hittat på ska ge ett begripligt svar tillbaka i
    chatten, inte tyst skapa eller ändra fel rad."""
    pipeline = AnalysisPipeline(store, FakeClaudeClient())

    svar = pipeline.execute_tool("correct_self_report", {"report_id": 999, "value": 80})

    assert "Fel" in svar and "999" in svar


def test_correct_self_report_needs_something_to_change(store: Store) -> None:
    pipeline = AnalysisPipeline(store, FakeClaudeClient())
    report_id = store.add_self_report(day="2026-09-02", category="weight", value=122.3)

    svar = pipeline.execute_tool("correct_self_report", {"report_id": report_id})

    assert "Fel" in svar
    assert store.get_self_report(report_id)["value"] == 122.3


def test_correct_self_report_survives_a_non_numeric_id(store: Store) -> None:
    """Claude skickar ibland heltal som strängar, och ibland skräp. Ett id
    som inte går att tolka ska bli ett svar, inte ett ValueError som
    bubblar upp som 'Fel vid körning av verktyget'."""
    pipeline = AnalysisPipeline(store, FakeClaudeClient())

    assert "Fel" in pipeline.execute_tool(
        "correct_self_report", {"report_id": "inte ett tal", "value": 80}
    )
    assert "Fel" in pipeline.execute_tool("correct_self_report", {"value": 80})


def test_delete_self_report_says_what_disappeared(store: Store) -> None:
    """Kvittensen läses av atleten via Claudes svar. 'Raderat id 12' säger
    ingenting — raden måste beskrivas innan den försvinner, för efteråt
    finns den inte att beskriva."""
    pipeline = AnalysisPipeline(store, FakeClaudeClient())
    report_id = store.add_self_report(
        day="2026-09-03", category="note", note="Född 1977-10-25"
    )

    svar = pipeline.execute_tool("delete_self_report", {"report_id": report_id})

    assert store.get_self_report(report_id) is None
    assert "1977-10-25" in svar and "2026-09-03" in svar


def test_delete_self_report_refuses_an_unknown_id(store: Store) -> None:
    pipeline = AnalysisPipeline(store, FakeClaudeClient())
    store.add_self_report(day="2026-09-03", category="weight", value=122.3)

    svar = pipeline.execute_tool("delete_self_report", {"report_id": 999})

    assert "Fel" in svar
    assert len(store.list_self_reports(days=30)) == 1, "ingen annan rad fick röras"


def test_self_report_ids_reach_the_chat_but_not_the_analyses(store: Store) -> None:
    """Verktygen adresserar en rad med dess id, så chatten måste se det.
    Analyserna har inga verktyg att använda id:t med — där är det brus."""
    from analysis.pipeline import fmt_self_reports

    store.add_self_report(day="2026-09-02", category="weight", value=122.3)
    rows = store.list_self_reports(days=30)

    assert "id" not in fmt_self_reports(rows)[0]
    assert "id" in fmt_self_reports(rows, include_ids=True)[0]


# --- Wellness i coaching- och dagsanalysens payload --------------------


def _tom_wellness_rad(day: str) -> dict:
    """En wellness-rad som liknar den skarpa: en handfull mätvärden, resten
    null. Av 29 kolumner levererar Garmin/Intervals i den här synkkedjan
    fjorton — stress, spO2, blodtryck, readiness, soreness och ett dussin
    till är alltid tomma."""
    row = dict.fromkeys(
        "day rest_day ctl atl tsb ramp_rate sleep_seconds sleep_score "
        "sleep_quality avg_sleeping_hr weight resting_hr hrv hrv_sdnn stress "
        "respiration spO2 systolic diastolic hydration soreness fatigue mood "
        "motivation injury readiness vo2max steps kcal_consumed raw_json "
        "last_synced".split()
    )
    row.update({
        "day": day, "ctl": 8.7, "atl": 13.4, "tsb": -4.7, "ramp_rate": -0.4,
        "sleep_seconds": 25000, "sleep_score": 74, "sleep_quality": 3,
        "resting_hr": 60, "hrv": 40.0, "steps": 8000, "rest_day": 0,
        # Rådatan är den stora posten: hela Intervals-svaret för dygnet,
        # samma mätvärden en gång till under deras egna namn.
        "raw_json": '{"ctl": 8.7, "atl": 13.4, "sleepScore": 74}' * 20,
        "last_synced": f"{day}T06:00:00",
    })
    return row


def test_coaching_sends_a_slim_wellness_series_not_thirty_full_rows(
    store: Store,
) -> None:
    """Coachingen skickade 30 KOMPLETTA wellness-rader till Claude.

    Uppmätt på den skarpa databasen var 499 av 870 fält (57 %) null, och
    serien tog 15 917 tecken — för det som morgon- och kvällsanalysen
    klarar på 4 856 med samma 30 dagar. Fältet hette dessutom "wellness"
    medan prompterna konsekvent ber modellen jämföra mot
    "wellness_history", så det fanns under ett annat namn än det den
    ombads leta efter.
    """
    import json
    from datetime import date as _date
    from datetime import timedelta

    idag = _date.today()
    for i in range(30):
        dag = (idag - timedelta(days=i)).isoformat()
        store.upsert_wellness_many([_tom_wellness_rad(dag)])

    claude = FakeClaudeClient()
    AnalysisPipeline(store, claude).coaching()
    payload = json.loads(claude.last_user_data)

    assert "wellness" not in payload, "det gamla fältnamnet ska vara borta"
    assert "wellness_history" in payload, "prompterna talar om wellness_history"
    assert len(payload["wellness_history"]) == 30

    # Serien bär bara de fält som faktiskt betyder något för en trend.
    rad = payload["wellness_history"][0]
    for tomt in ("stress", "spO2", "systolic", "readiness", "soreness", "raw_json"):
        assert tomt not in rad, f"{tomt} har aldrig ett värde och ska inte skickas"

    # Dagens kompletta rad följer med separat, precis som morgon- och
    # kvällsanalysen redan gjorde — där finns ramptakt och sömnkvalitet.
    assert payload["wellness_today"]["ramp_rate"] == -0.4
    assert payload["wellness_today"]["sleep_quality"] == 3
    assert "raw_json" not in payload["wellness_today"]


def test_coaching_does_not_send_yesterday_as_today(store: Store) -> None:
    """wellness_today var seriens första rad, alltså den SENASTE. Hade
    dagens synk inte kommit in var det gårdagens rad, och Claude fick
    gårdagens sömn och HRV under rubriken "idag"."""
    import json
    from datetime import date as _date
    from datetime import timedelta

    igar = (_date.today() - timedelta(days=1)).isoformat()
    store.upsert_wellness_many([_tom_wellness_rad(igar)])

    claude = FakeClaudeClient()
    AnalysisPipeline(store, claude).coaching()
    payload = json.loads(claude.last_user_data)

    assert payload["wellness_today"] is None, "gårdagen skickades som idag"
    # Gårdagen finns kvar i serien, med sitt riktiga datum.
    assert payload["wellness_history"][0]["day"] == igar


def test_chat_logging_refuses_an_exercise_already_imported_from_the_watch(
    store: Store,
) -> None:
    """Reproducerat före fixen: ett FIT-importerat marklyftspass plus samma
    pass loggat i chatten gav 2 sessions och 1200 kg där 600 lyftes.
    Staplarna på dashboarden blev dubbelt så höga, "du har lyft N veckor"
    räknade två pass där det fanns ett, och analyserna fick siffran två
    gånger.

    Skyddet var tidigare bara en varning i verktygsbeskrivningen — alltså
    ingen spärr alls, bara en förhoppning."""
    pipeline = AnalysisPipeline(store, FakeClaudeClient())
    store.add_strength_sets(
        day="2026-09-03",
        exercise="marklyft",
        sets=[{"reps": 5, "weight_kg": 120.0}] * 5,
        activity_id="i1",
        source="fit",
    )

    svar = pipeline.execute_tool(
        "log_strength_session",
        {
            "exercise": "marklyft",
            "day": "2026-09-03",
            "sets": [{"reps": 5, "weight_kg": 120.0}] * 5,
        },
    )

    assert "Redan importerat" in svar
    assert "120 kg" in svar, "kvittensen ska visa vad som redan finns"
    rader = store.get_strength_sets_for_date("2026-09-03")
    assert len(rader) == 5, "inga nya rader ska ha lagts till"
    assert {r["source"] for r in rader} == {"fit"}


def test_chat_logging_still_allows_adding_to_an_earlier_chat_log(store: Store) -> None:
    """Spärren gäller BARA source='fit'. Att fylla på i efterhand ("jag
    glömde, det var fem set inte tre") är ett riktigt användningsfall, och
    den som loggar för hand är den som vet."""
    pipeline = AnalysisPipeline(store, FakeClaudeClient())
    store.add_strength_sets(
        day="2026-09-03",
        exercise="bänkpress",
        sets=[{"reps": 8, "weight_kg": 80.0}] * 3,
        source="chat",
    )

    svar = pipeline.execute_tool(
        "log_strength_session",
        {
            "exercise": "bänkpress",
            "day": "2026-09-03",
            "sets": [{"reps": 8, "weight_kg": 80.0}] * 2,
        },
    )

    assert "Sparat" in svar
    assert len(store.get_strength_sets_for_date("2026-09-03")) == 5


def test_chat_logging_of_a_different_exercise_is_unaffected(store: Store) -> None:
    """Spärren är per övning och dag, inte per pass. Klockan loggade
    marklyft; att du sedan berättar om bicepscurlen den missade ska gå."""
    pipeline = AnalysisPipeline(store, FakeClaudeClient())
    store.add_strength_sets(
        day="2026-09-03", exercise="marklyft",
        sets=[{"reps": 5, "weight_kg": 120.0}], activity_id="i1", source="fit",
    )

    svar = pipeline.execute_tool(
        "log_strength_session",
        {"exercise": "bicepscurl", "day": "2026-09-03",
         "sets": [{"reps": 10, "weight_kg": 20.0}]},
    )

    assert "Sparat" in svar


# --- Övningsnamn är ett namn, inte flera stavningar -------------------


def test_chat_logging_catches_a_capitalised_duplicate(store: Store) -> None:
    """Spärren jämförde rå sträng mot rå sträng. "Marklyft" gick därför rakt
    förbi den och blev ett andra exemplar av samma pass — 3 600 kg volym där
    1 800 lyftes, och två övningar i analysen där det fanns en.

    Blanksteg fångades redan (.strip() fanns); versaler gjorde det inte."""
    pipeline = AnalysisPipeline(store, FakeClaudeClient())
    store.add_strength_sets(
        day="2026-09-03",
        exercise="marklyft",
        sets=[{"reps": 5, "weight_kg": 120.0}] * 3,
        activity_id="i1",
        source="fit",
    )

    for stavning in ("Marklyft", "MARKLYFT", "  marklyft  "):
        svar = pipeline.execute_tool(
            "log_strength_session",
            {
                "exercise": stavning,
                "day": "2026-09-03",
                "sets": [{"reps": 5, "weight_kg": 120.0}] * 3,
            },
        )
        assert "Redan importerat" in svar, f"{stavning!r} smet förbi spärren"

    rader = store.get_strength_sets_for_date("2026-09-03")
    assert len(rader) == 3, "inga nya rader ska ha lagts till"


def test_logged_exercise_is_stored_under_one_name(store: Store) -> None:
    """Två stavningar av samma övning blir två rader i payloaden till
    Claude, och get_strength_history (som matchar exercise IN (...)) hittar
    aldrig historiken till den ena."""
    from analysis.pipeline import fmt_strength_sessions

    pipeline = AnalysisPipeline(store, FakeClaudeClient())
    for stavning in ("Bänkpress", "bänkpress"):
        pipeline.execute_tool(
            "log_strength_session",
            {
                "exercise": stavning,
                "day": "2026-09-03",
                "sets": [{"reps": 8, "weight_kg": 80.0}],
            },
        )

    sessioner = fmt_strength_sessions(store.list_strength_sets(days=400))
    assert [s["exercise"] for s in sessioner] == ["bänkpress"]
    assert sessioner[0]["sets"] == 2


def test_partially_measured_sets_report_no_reps_per_set(store: Store) -> None:
    """Tre set där ett saknar reps gav sets=3 och reps_per_set=8 — som läses
    som 3x8 = 24 — medan reps_total sa 16. Samma tvetydighet som fältet
    infördes för att ta bort, fast från andra hållet."""
    from analysis.pipeline import fmt_strength_sessions

    rows = [
        {"day": "2026-09-03", "exercise": "chins", "reps": reps, "weight_kg": None}
        for reps in (8, 8, None)
    ]

    (session,) = fmt_strength_sessions(rows)

    assert session["sets"] == 3
    assert session["reps_total"] == 16
    assert "reps_per_set" not in session


# --- Payloaden och kortet ska räkna likadant -----------------------------
#
# Skarpt fall: däckvältning 2026-05-05, två set på 130 kg som klockan
# loggade utan att repsräkna. Payloaden till Claude sa "tyngsta vikt 130
# kg", progressionskortet sa "ingen mätbar vikt" — två svar ur samma två
# rader, eftersom aritmetiken fanns i två kopior som blivit oense om vad
# som räknas som ett mätbart set. Databasen har 26 sådana rader.


def _oraknade_set(dag: str = "2026-05-05", ovning: str = "däckvältning"):
    return [
        {"day": dag, "exercise": ovning, "reps": 0, "weight_kg": 130.0},
        {"day": dag, "exercise": ovning, "reps": 0, "weight_kg": 130.0},
    ]


def test_the_payload_and_the_card_agree_on_the_heaviest_weight() -> None:
    from analysis.pipeline import fmt_strength_sessions
    from analysis.strength import session_progression

    rader = _oraknade_set()
    (payload,) = fmt_strength_sessions(rader)
    (kort,) = session_progression(rader)

    assert payload.get("top_weight_kg") == kort["top_weight_kg"], (
        "payloaden till Claude och progressionskortet ska ge samma svar på "
        "vad som lyftes — annars är ett av talen en osanning"
    )
    assert "top_weight_kg" not in payload, (
        "ett set klockan inte repsräknat är ingen vikt att rapportera"
    )


def test_a_set_the_watch_never_counted_reports_no_reps_per_set() -> None:
    """'reps_per_set: 0' läses som '2 set x 0 reps på 130 kg' — ett
    påstående om hur passet gick till som inte står i filen."""
    from analysis.pipeline import fmt_strength_sessions

    (session,) = fmt_strength_sessions(_oraknade_set())

    assert session["reps_total"] == 0
    assert "reps_per_set" not in session


def test_the_payload_says_how_many_sets_were_measurable() -> None:
    """Utan measured_sets ser ett pass med 'sets 3, volym 1200' ut som om
    alla tre räknats in i volymen."""
    from analysis.pipeline import fmt_strength_sessions

    (session,) = fmt_strength_sessions([
        {"day": "2026-08-25", "exercise": "raka marklyft", "reps": 10, "weight_kg": 60.0},
        {"day": "2026-08-25", "exercise": "raka marklyft", "reps": 10, "weight_kg": 60.0},
        {"day": "2026-08-25", "exercise": "raka marklyft", "reps": 0, "weight_kg": 60.0},
    ])

    assert session["sets"] == 3
    assert session["measured_sets"] == 2
    assert session["volume_kg"] == 1200.0


def test_a_fully_measured_pass_carries_no_measured_sets_field() -> None:
    """Fältet ska bara dyka upp när det säger något — annars är det brus i
    varje pass för de få dagar det gäller."""
    from analysis.pipeline import fmt_strength_sessions

    (session,) = fmt_strength_sessions([
        {"day": "2026-08-27", "exercise": "marklyft", "reps": 5, "weight_kg": 130.0},
    ])

    assert "measured_sets" not in session


def test_correct_self_report_can_move_a_report_to_another_category(
    store: Store,
) -> None:
    """Loggade Claude "alkohol" där atleten menade sömn gick raden bara att
    radera och skriva om — verktyget kunde ändra värde, anteckning och dag,
    men inte vilken sorts uppgift det var."""
    pipeline = AnalysisPipeline(store, FakeClaudeClient())
    report_id = store.add_self_report(
        day="2026-09-03", category="alcohol", note="sov dåligt"
    )

    svar = pipeline.execute_tool(
        "correct_self_report", {"report_id": report_id, "category": "mood"}
    )

    assert "Rättat" in svar
    assert store.get_self_report(report_id)["category"] == "mood"


def test_correct_self_report_refuses_an_unknown_category(store: Store) -> None:
    """Enumen i schemat är en instruktion till modellen, inte en spärr."""
    pipeline = AnalysisPipeline(store, FakeClaudeClient())
    report_id = store.add_self_report(day="2026-09-03", category="note", note="x")

    svar = pipeline.execute_tool(
        "correct_self_report", {"report_id": report_id, "category": "sömn"}
    )

    assert "ingen giltig kategori" in svar
    assert store.get_self_report(report_id)["category"] == "note"


def test_log_self_report_refuses_an_unknown_category(store: Store) -> None:
    """Kategorin kontrollerades bara vid rättning. log_self_report skrev
    in vad som helst, och en vikt under "Weight" eller "vikt" hittas
    aldrig av latest_weight, som letar efter just "weight"."""
    pipeline = AnalysisPipeline(store, FakeClaudeClient())

    for fel_kategori in ("vikt", "Weight", ["weight"]):
        svar = pipeline.execute_tool(
            "log_self_report", {"category": fel_kategori, "value": 82}
        )
        assert "ingen giltig kategori" in svar, fel_kategori
        assert "Inget sparades" in svar
    assert store.list_self_reports(days=30) == []

    # En utelämnad kategori är fortfarande en anteckning.
    assert "Sparat: note" in pipeline.execute_tool("log_self_report", {"note": "x"})


def test_a_note_that_is_not_text_is_refused(store: Store) -> None:
    """En lista eller ett objekt i note gick inte att skriva alls (sqlite3
    vägrar binda det), och felet blev "Fel vid körning av verktyget" utan
    ledtråd till vad som var fel."""
    pipeline = AnalysisPipeline(store, FakeClaudeClient())
    report_id = store.add_self_report(day="2026-09-03", category="note", note="x")

    for verktyg, indata in (
        ("log_self_report", {"category": "note", "note": ["halsont"]}),
        ("correct_self_report", {"report_id": report_id, "note": {"text": "y"}}),
        ("log_strength_session", {"exercise": "bänkpress",
                                  "sets": [{"reps": 8, "weight_kg": 80}],
                                  "note": 5}),
    ):
        svar = pipeline.execute_tool(verktyg, indata)
        assert "ingen text" in svar, verktyg
        assert "Inget sparades" in svar, verktyg

    assert len(store.list_self_reports(days=30)) == 1
    assert store.get_self_report(report_id)["note"] == "x"
    assert store.list_strength_sets(days=30) == []


def test_a_day_in_the_future_is_refused(store: Store) -> None:
    """Ett korrekt skrivet datum kan ändå vara fel år — "2026-12-31" när
    atleten sa "i nyårsafton" i januari. Fönsterfrågorna räknar bakåt från
    idag utan tak framåt, så raden följde med i varje analys tills
    datumet hunnit ikapp."""
    from datetime import date, timedelta

    idag = date.today()
    imorgon = (idag + timedelta(days=1)).isoformat()
    pipeline = AnalysisPipeline(store, FakeClaudeClient())
    report_id = store.add_self_report(day=idag.isoformat(), category="illness", note="snuva")

    for verktyg, indata in (
        ("log_self_report", {"category": "illness", "note": "snuva", "day": imorgon}),
        ("correct_self_report", {"report_id": report_id, "day": imorgon}),
        ("log_strength_session", {"exercise": "knäböj",
                                  "sets": [{"reps": 5, "weight_kg": 100}],
                                  "day": imorgon}),
    ):
        svar = pipeline.execute_tool(verktyg, indata)
        assert "ligger i framtiden" in svar, verktyg
        # Felsvaret räknar ut gårdagen åt modellen, så den kan göra om
        # anropet rätt i stället för att gissa igen.
        assert (idag - timedelta(days=1)).isoformat() in svar, verktyg

    assert len(store.list_self_reports(days=30)) == 1
    assert store.get_self_report(report_id)["day"] == idag.isoformat()
    assert store.list_strength_sets(days=30) == []

    # Idag går fortfarande bra.
    assert "Sparat" in pipeline.execute_tool(
        "log_self_report", {"category": "note", "note": "ok", "day": idag.isoformat()}
    )


def test_both_self_report_tools_share_one_category_list() -> None:
    """Två kopior av samma enum kan glida isär, och då blir en kategori
    möjlig att skriva men omöjlig att rätta till."""
    from analysis.pipeline import (
        CORRECT_SELF_REPORT_TOOL,
        LOG_SELF_REPORT_TOOL,
        SELF_REPORT_CATEGORIES,
    )

    skapa = LOG_SELF_REPORT_TOOL["input_schema"]["properties"]["category"]["enum"]
    ratta = CORRECT_SELF_REPORT_TOOL["input_schema"]["properties"]["category"]["enum"]
    assert skapa == ratta == SELF_REPORT_CATEGORIES


def test_evening_summary_can_target_an_earlier_day(store: Store) -> None:
    """Uppsamlingen efter midnatt ber om gårdagen. Då ska payloaden handla
    om gårdagen — dess pass, dess sömn, dess rubrik — och raden sparas med
    gårdagens ref_id."""
    import json

    claude = FakeClaudeClient()
    pipeline = AnalysisPipeline(store, claude)
    activity = _make_activity("i-igar")
    activity["start_time"] = "2026-09-05T18:00:00"
    store.upsert_activity(activity)

    pipeline.evening_summary("2026-09-05")

    payload = json.loads(claude.last_user_data)
    assert payload["today_date"] == "2026-09-05"
    assert "5 september 2026" in payload["date_label"]
    assert [a["start_time"] for a in payload["today_activities"]] == [
        "2026-09-05T18:00:00"
    ]
    saved = store.latest_analysis("daily_summary")
    assert saved["ref_id"] == "2026-09-05"


def test_evening_summary_history_stops_at_the_day_it_summarises(
    store: Store,
) -> None:
    """Fönstren räknas från idag. Vid en uppsamling efter midnatt hade det
    nya dygnets halvtomma rader annars legat överst i trendserien som
    analysen ska läsa bakåt ur."""
    import json
    from datetime import datetime as dt
    from datetime import timedelta

    def _rad(day: str, hrv: float) -> dict:
        row = dict.fromkeys(
            "day rest_day ctl atl tsb ramp_rate sleep_seconds sleep_score "
            "sleep_quality avg_sleeping_hr weight resting_hr hrv hrv_sdnn stress "
            "respiration spO2 systolic diastolic hydration soreness fatigue mood "
            "motivation injury readiness vo2max steps kcal_consumed raw_json "
            "last_synced".split()
        )
        row.update({"day": day, "hrv": hrv, "raw_json": "{}",
                    "last_synced": f"{day}T06:00:00"})
        return row

    idag = dt.now().date()
    igar = (idag - timedelta(days=1)).isoformat()
    store.upsert_wellness_many([_rad(idag.isoformat(), 99)])
    store.upsert_wellness_many([_rad(igar, 55)])
    store.add_self_report(day=idag.isoformat(), category="note", note="idag")
    store.add_self_report(day=igar, category="note", note="igår")

    claude = FakeClaudeClient()
    AnalysisPipeline(store, claude).evening_summary(igar)

    payload = json.loads(claude.last_user_data)
    assert [w["day"] for w in payload["wellness_history"]] == [igar]
    assert [r["note"] for r in payload["self_reports"]] == ["igår"]


# --- Pulszoner och temperatur --------------------------------------------

_ZONNAMN = ["Recovery", "Aerobic", "Tempo", "SubThreshold",
            "SuperThreshold", "Aerobic Capacity", "Anaerobic"]
# Skarpa värden från ett löppass respektive ett gympass på Pi:n.
_LOPZONER = [144, 152, 161, 170, 175, 180, 189]
_LOPTIDER = [305, 955, 774, 80, 0, 0, 0]
_STYRKEZONER = [137, 152, 159, 170, 175, 180, 189]
_STYRKETIDER = [3256, 0, 0, 0, 0, 0, 0]


def test_each_zone_carries_its_name_and_its_limits() -> None:
    """Payloaden bar tidigare två parallella talarrayer och inga namn.

    Att [144, 152, ...] och [305, 955, ...] hörde ihop parvis stod
    ingenstans, och prompten ber ändå analysen redovisa "tid i varje
    pulszon". Modellen fick para ihop dem själv och hitta på etiketterna.
    """
    from analysis.pipeline import fmt_hr_zones

    zoner = fmt_hr_zones(_LOPZONER, _LOPTIDER, _ZONNAMN)

    assert zoner is not None
    assert zoner[0] == {"zone": 1, "name": "Recovery", "min_bpm": 0,
                        "max_bpm": 144, "seconds": 305}
    # Zon 2 börjar ett slag ovanför zon 1:s tak, inte på det.
    assert zoner[1] == {"zone": 2, "name": "Aerobic", "min_bpm": 145,
                        "max_bpm": 152, "seconds": 955}


def test_zones_without_time_are_left_out() -> None:
    """Ett styrkepass ligger helt i zon 1. Sex rader nollor per pass är
    brus i en payload som redan är stor — att en zon saknas betyder att
    den inte berördes."""
    from analysis.pipeline import fmt_hr_zones

    zoner = fmt_hr_zones(_STYRKEZONER, _STYRKETIDER, _ZONNAMN)

    assert zoner is not None
    assert [z["zone"] for z in zoner] == [1]
    assert zoner[0]["seconds"] == 3256


def test_zones_still_work_before_the_names_have_been_synced() -> None:
    """Namnen kommer från en endpoint som kan svara fel (se
    sync_sport_settings). Gränser och tider finns i passet självt och ska
    fortsätta gå fram utan dem."""
    from analysis.pipeline import fmt_hr_zones

    zoner = fmt_hr_zones(_LOPZONER, _LOPTIDER, None)

    assert zoner is not None
    assert "name" not in zoner[0]
    assert zoner[0]["max_bpm"] == 144
    assert fmt_hr_zones(_LOPZONER, None, _ZONNAMN) is None


def test_the_activity_payload_names_its_zones_after_the_sport(store: Store) -> None:
    """Uppslagningen sker på passets typ. Löpningens zon 1 slutar vid 144
    och styrkans vid 137, så en analys som tar första bästa sportraden
    etiketterar halva passen fel."""
    import json

    store.replace_sport_settings([
        {"id": 1, "types": ["WeightTraining"], "hr_zone_names": _ZONNAMN},
        {"id": 2, "types": ["Run"], "hr_zone_names": ["Lugnt"] + _ZONNAMN[1:]},
    ])
    aktivitet = _make_activity("styrka")
    aktivitet["type"] = "WeightTraining"
    aktivitet["raw_json"] = json.dumps({
        "icu_hr_zones": _STYRKEZONER, "icu_hr_zone_times": _STYRKETIDER,
    })
    store.upsert_activity(aktivitet)
    claude = FakeClaudeClient()

    AnalysisPipeline(store, claude).analyze_activity("styrka")

    payload = json.loads(claude.last_user_data)
    assert payload["hr_zones"] == [
        {"zone": 1, "name": "Recovery", "min_bpm": 0, "max_bpm": 137,
         "seconds": 3256}
    ]
    # De råa arrayerna ska vara borta, inte ligga kvar bredvid.
    assert "hr_zone_times" not in payload


def test_the_activity_payload_carries_the_temperature(store: Store) -> None:
    """average_temp fanns på 156 av 160 pass och lästes aldrig. Värme höjer
    pulsen vid samma arbete, så utan den läses ett varmt gym som sämre form."""
    import json

    aktivitet = _make_activity("varmt")
    aktivitet["raw_json"] = json.dumps(
        {"average_temp": 28.201132, "min_temp": 27, "max_temp": 29}
    )
    store.upsert_activity(aktivitet)
    claude = FakeClaudeClient()

    AnalysisPipeline(store, claude).analyze_activity("varmt")

    payload = json.loads(claude.last_user_data)
    # Avrundad: klockan rapporterar sex decimaler den inte har täckning för.
    assert payload["average_temp_c"] == 28.2
    assert payload["min_temp_c"] == 27
    assert payload["max_temp_c"] == 29


# --- Skattat maxlyft i analyserna ----------------------------------------


def test_every_analysis_gets_the_estimated_max_lift() -> None:
    """fmt_strength_sessions matar alla fyra analyserna. Utan ett skattat
    maxlyft kan de inte jämföra pass med olika upplägg: 5x120 och 1x130
    står som "120" och "130" och läses som en försämring, fast det första
    är det tyngre lyftet."""
    from analysis.pipeline import fmt_strength_sessions

    rader = fmt_strength_sessions([
        {"day": "2026-09-03", "exercise": "marklyft", "reps": 5, "weight_kg": 120},
        {"day": "2026-09-03", "exercise": "marklyft", "reps": 1, "weight_kg": 130},
    ])

    assert rader[0]["top_weight_kg"] == 130
    assert rader[0]["estimated_1rm"] == 140.0, "bästa setet, inte tyngsta stången"


def test_an_exercise_without_weight_gets_no_estimate_at_all() -> None:
    """Fältet ska utebli, inte stå som null. Armhävningar har ingen vikt
    loggad, och ett tomt fält i payloaden är en inbjudan att gissa."""
    from analysis.pipeline import fmt_strength_sessions

    rader = fmt_strength_sessions([
        {"day": "2026-08-27", "exercise": "armhävning", "reps": 12, "weight_kg": None},
    ])

    assert "estimated_1rm" not in rader[0]


def test_the_analysis_is_only_told_about_records_that_really_happened(
    store: Store,
) -> None:
    """Ett skattat maxlyft är ingen notering atleten satt.

    Kortet på dashboarden kallade en Epley-skattning för "Personligt
    rekord": bänkpressens 89,4 kg markerade en dag då tyngsta stången vägde
    72,5. Samma osanning fick inte gå vidare till analysen — den skriver
    text som läses som fakta.
    """
    import json

    aktivitet = _make_activity("gympass")
    aktivitet["type"] = "WeightTraining"
    aktivitet["start_time"] = "2026-09-03T17:00:00"
    store.upsert_activity(aktivitet)
    store.add_strength_sets("2026-06-02", "marklyft", [{"reps": 5, "weight_kg": 130}])
    store.add_strength_sets(
        "2026-09-03", "marklyft", [{"reps": 3, "weight_kg": 135}],
        activity_id="gympass",
    )
    claude = FakeClaudeClient()

    AnalysisPipeline(store, claude).analyze_activity("gympass")

    rekord = json.loads(claude.last_user_data)["strength_records"]["marklyft"]
    assert "estimated_1rm" not in rekord, "en skattning är inget rekord"
    assert "volume_kg" not in rekord
    # Tyngsta vikten är ett riktigt lyft, och repsen hör till beskedet.
    assert rekord["top_weight_kg"] == {
        "value": 135.0, "day": "2026-09-03", "reps": 3,
    }
    assert rekord["reps_total"]["day"] == "2026-06-02"


def test_the_record_is_the_whole_history_not_the_window(store: Store) -> None:
    """get_strength_history skickar bara de senaste passen. Räknades
    rekordet ur dem fick bänkpressen 89,4 kg medan det riktiga — 120 kg
    från 5 maj — låg utanför och aldrig syntes."""
    import json

    from analysis.pipeline import STRENGTH_HISTORY_SESSIONS

    aktivitet = _make_activity("gympass")
    aktivitet["type"] = "WeightTraining"
    aktivitet["start_time"] = "2026-09-03T17:00:00"
    store.upsert_activity(aktivitet)
    # Det tyngsta lyftet ligger långt bortom historikfönstret.
    store.add_strength_sets("2026-01-05", "marklyft", [{"reps": 1, "weight_kg": 150}])
    for i in range(STRENGTH_HISTORY_SESSIONS + 3):
        store.add_strength_sets(
            f"2026-06-{i + 1:02d}", "marklyft", [{"reps": 5, "weight_kg": 100}]
        )
    store.add_strength_sets(
        "2026-09-03", "marklyft", [{"reps": 5, "weight_kg": 110}],
        activity_id="gympass",
    )
    claude = FakeClaudeClient()

    AnalysisPipeline(store, claude).analyze_activity("gympass")

    rekord = json.loads(claude.last_user_data)["strength_records"]["marklyft"]
    assert rekord["top_weight_kg"] == {
        "value": 150.0, "day": "2026-01-05", "reps": 1,
    }


def test_a_struck_exercise_is_not_reported_as_saved(tmp_path: Path) -> None:
    """Rättelselistan kan stryka en övning en bestämd dag (se
    parse_strength_corrections i sync/store.py). Kvittensen byggdes då av
    det Claude skickade IN och inte av det som skrevs, så den sa
    "Sparat: bänkpress, 0 set, 24 reps, volym 1920 kg" om noll rader.

    Claude läser kvittensen och berättar för atleten vad som hänt — den
    fick alltså veta att ett pass sparats som inte finns någonstans.
    """
    store = Store(tmp_path / "t.db", ["2026-05-05:bänkpress"])
    store.init()
    pipeline = AnalysisPipeline(store, FakeClaudeClient())

    result = pipeline.execute_tool(
        "log_strength_session",
        {
            "exercise": "bänkpress",
            "sets": [{"reps": 8, "weight_kg": 80.0}] * 3,
            "day": "2026-05-05",
        },
    )

    assert store.get_strength_sets_for_date("2026-05-05") == []
    assert "Sparat:" not in result
    assert "struken" in result
    # Volymen som aldrig lyftes ska inte stå i kvittensen alls.
    assert "1920" not in result


def test_a_renamed_exercise_is_reported_under_the_name_it_got(
    tmp_path: Path,
) -> None:
    """Rättelselistan kan också DÖPA OM en övning för en dag. Kvittensen
    sa namnet Claude skickade in, medan raderna hamnade under det rättade
    — och nästa fråga om det inskickade namnet hittar då ingenting."""
    store = Store(tmp_path / "t.db", ["2026-04-21:raka marklyft=marklyft"])
    store.init()
    pipeline = AnalysisPipeline(store, FakeClaudeClient())

    result = pipeline.execute_tool(
        "log_strength_session",
        {
            "exercise": "raka marklyft",
            "sets": [{"reps": 5, "weight_kg": 120.0}] * 3,
            "day": "2026-04-21",
        },
    )

    assert {r["exercise"] for r in store.get_strength_sets_for_date("2026-04-21")} == {
        "marklyft"
    }
    assert "Sparat: marklyft" in result
    assert "3 set" in result


def test_the_receipt_names_the_exercise_as_it_is_stored(store: Store) -> None:
    """Utan rättelse ska namnet ändå vara det normaliserade — tabellen
    innehåller bara gemener (se normalize_exercise)."""
    pipeline = AnalysisPipeline(store, FakeClaudeClient())

    result = pipeline.execute_tool(
        "log_strength_session",
        {"exercise": "  Knäböj ", "sets": [{"reps": 5, "weight_kg": 100.0}],
         "day": "2026-08-20"},
    )

    assert [r["exercise"] for r in store.get_strength_sets_for_date("2026-08-20")] == [
        "knäböj"
    ]
    assert "Sparat: knäböj" in result
    assert "Obs:" not in result, "inget att påpeka när namnet inte bytts"


# --- Otypad indata från verktygen -------------------------------------
#
# Verktygens JSON-schema validerar inte tool_input — det är en instruktion
# till modellen, ingen spärr. Se _tool_number/_tool_day i pipeline.py.


def test_string_reps_do_not_save_rows_behind_a_failed_receipt(
    store: Store,
) -> None:
    """Regressionstest: reps som strängen "8" skrev raderna och kraschade
    sedan summeringen (sum() på str).

    claude.py fångar undantaget och säger åt Claude att INTE påstå att
    något sparades — men raderna låg redan i tabellen. Atleten fick höra
    att det misslyckades, loggade om passet, och chattspärren släppte
    igenom det (den spärrar bara source='fit'). Volymen dubbelräknades.
    """
    pipeline = AnalysisPipeline(store, FakeClaudeClient())

    result = pipeline.execute_tool(
        "log_strength_session",
        {"exercise": "marklyft", "sets": [{"reps": "8", "weight_kg": 60.0}],
         "day": "2026-08-20"},
    )

    # Talet ska tolkas, inte avvisas — "8" är entydigt.
    assert "Sparat: marklyft" in result
    rader = store.get_strength_sets_for_date("2026-08-20")
    assert [(r["reps"], r["weight_kg"]) for r in rader] == [(8, 60.0)]


def test_a_weight_with_a_unit_is_refused_before_anything_is_written(
    store: Store,
) -> None:
    """"60 kg" går inte att tolka som ett tal. Då ska INGENTING skrivas
    och kvittensen säga varför — inte lämna halva passet i tabellen."""
    pipeline = AnalysisPipeline(store, FakeClaudeClient())

    result = pipeline.execute_tool(
        "log_strength_session",
        {
            "exercise": "marklyft",
            "sets": [{"reps": 8, "weight_kg": 60.0}, {"reps": 8, "weight_kg": "60 kg"}],
            "day": "2026-08-20",
        },
    )

    assert "Fel:" in result
    assert "Sparat" not in result
    assert store.get_strength_sets_for_date("2026-08-20") == [], (
        "det första setet får inte ligga kvar när det andra avvisades"
    )


def test_a_non_numeric_weight_report_never_reaches_the_table(
    store: Store,
) -> None:
    """Regressionstest: value="82 kg" lagrades som TEXT i en REAL-kolumn.

    store.latest_weight() gör float() på den, och den anropas från
    system_prompt() — alltså av varje analys OCH varje chattmeddelande.
    En enda rad gjorde hela appen obrukbar, och eftersom chatten låg nere
    gick raden inte att rätta därifrån heller.
    """
    pipeline = AnalysisPipeline(store, FakeClaudeClient())

    result = pipeline.execute_tool(
        "log_self_report",
        {"category": "weight", "value": "82 kg", "day": "2026-08-20"},
    )

    assert "Fel:" in result
    assert store.get_self_reports_for_date("2026-08-20") == []
    assert store.latest_weight() is None


def test_latest_weight_survives_a_row_that_is_already_broken(
    store: Store,
) -> None:
    """Spärren ovan stoppar nya rader. En som redan ligger där får inte
    ta ner varenda analys — då finns ingen väg tillbaka utom sqlite3."""
    store.add_self_report(day="2026-08-19", category="weight", value=81.0)
    store.add_self_report(day="2026-08-20", category="weight", value="82 kg")

    assert store.latest_weight() == (81.0, "2026-08-19")


def test_an_unparsable_day_is_refused_instead_of_silently_stored(
    store: Store,
) -> None:
    """Regressionstest: 'day' gick orört in i kolumnen som alla
    fönsterfrågor jämför som STRÄNG.

    "igår" sorterar över varje verkligt datum och faller därför aldrig ur
    ett rullande fönster — den följde med i varje betald analyspayload i
    all framtid. "20 september 2026" sorterar under och försvann tyst i
    stället, så uppgiften var bara borta.
    """
    pipeline = AnalysisPipeline(store, FakeClaudeClient())

    for dag in ("igår", "20 september 2026", "2026-13-45", "2026-9-20"):
        result = pipeline.execute_tool(
            "log_self_report", {"category": "weight", "value": 82.0, "day": dag}
        )
        assert "Fel:" in result, f"{dag!r} togs emot"
        assert "YYYY-MM-DD" in result, "svaret ska säga hur datumet ska se ut"

    assert store.list_self_reports(days=3650) == []


def test_an_omitted_day_still_means_today(store: Store) -> None:
    """Skärpningen ovan får inte träffa det vanliga fallet: båda verktygen
    dokumenterar att ett utelämnat 'day' betyder idag."""
    pipeline = AnalysisPipeline(store, FakeClaudeClient())
    idag = datetime.now().date().isoformat()

    pipeline.execute_tool("log_self_report", {"category": "weight", "value": 82.0})
    pipeline.execute_tool(
        "log_strength_session",
        {"exercise": "knäböj", "sets": [{"reps": 5, "weight_kg": 100.0}]},
    )

    assert [r["day"] for r in store.get_self_reports_for_date(idag)] == [idag]
    assert [r["day"] for r in store.get_strength_sets_for_date(idag)] == [idag]


def test_a_correction_cannot_reintroduce_a_broken_value(store: Store) -> None:
    """Rättelseverktyget skriver till samma kolumner. Utan samma kontroll
    där hade en vikt med enhet kommit in bakvägen och lämnat raden i
    precis det skick verktyget finns för att laga."""
    pipeline = AnalysisPipeline(store, FakeClaudeClient())
    report_id = store.add_self_report(day="2026-08-20", category="weight", value=82.0)

    for falt, trasigt in (("value", "83 kg"), ("day", "igår")):
        result = pipeline.execute_tool(
            "correct_self_report", {"report_id": report_id, falt: trasigt}
        )
        assert "Fel:" in result, f"{falt}={trasigt!r} togs emot"

    kvar = store.get_self_report(report_id)
    assert (kvar["value"], kvar["day"]) == (82.0, "2026-08-20")


def test_a_set_list_that_is_a_string_writes_nothing(store: Store) -> None:
    """Regressionstest: "3x8" itererades tecken för tecken.

    Varje tecken blev ett tomt set, så tre rader utan reps och utan vikt
    skrevs — och kvittensen sa "Sparat: bänkpress, 3 set". Raderna räknas
    som pass i veckovolymen och gör övningen valbar i progressions-
    väljaren; bara volymen låter bli dem, eftersom den kräver en vikt.

    Schemat säger "array of objects", men det är en instruktion till
    modellen och ingen spärr.
    """
    pipeline = AnalysisPipeline(store, FakeClaudeClient())

    result = pipeline.execute_tool(
        "log_strength_session",
        {"exercise": "bänkpress", "sets": "3x8", "day": "2026-08-20"},
    )

    assert "Fel:" in result
    assert store.get_strength_sets_for_date("2026-08-20") == []


def test_a_set_list_that_is_an_object_writes_nothing(store: Store) -> None:
    """Regressionstest: en dict itererades nyckel för nyckel.

    {"reps": 5} blev alltså EN rad utan reps — innehållet försvann tyst
    och kvittensen påstod att setet sparats.
    """
    pipeline = AnalysisPipeline(store, FakeClaudeClient())

    result = pipeline.execute_tool(
        "log_strength_session",
        {"exercise": "marklyft", "sets": {"reps": 5}, "day": "2026-08-20"},
    )

    assert "Fel:" in result
    assert store.get_strength_sets_for_date("2026-08-20") == []


def test_a_set_that_is_not_an_object_stops_the_whole_call(store: Store) -> None:
    """Ett enskilt set av fel typ blev tidigare ett tomt set, tyst.

    Det andra setet nedan är bara ett tal. Det ska avvisas — och eftersom
    kontrollen sker före första skrivningen ska INGET av setsen sparas,
    inte ens det första som är helt i sin ordning.
    """
    pipeline = AnalysisPipeline(store, FakeClaudeClient())

    result = pipeline.execute_tool(
        "log_strength_session",
        {"exercise": "knäböj", "sets": [{"reps": 5, "weight_kg": 100.0}, 80],
         "day": "2026-08-20"},
    )

    assert "set nummer 2" in result
    assert store.get_strength_sets_for_date("2026-08-20") == []


def test_a_fractional_rep_count_is_refused_instead_of_rounded(store: Store) -> None:
    """Regressionstest: int(2.7) gav 2, och kvittensen sa "2 reps".

    Ett halvt rep finns inte, så talet är antingen en felskrivning eller
    en missförstådd instruktion — båda är sådant atleten ska få veta om,
    inte något som tyst ska avrundas åt hen.
    """
    pipeline = AnalysisPipeline(store, FakeClaudeClient())

    result = pipeline.execute_tool(
        "log_strength_session",
        {"exercise": "chins", "sets": [{"reps": 2.7}], "day": "2026-08-20"},
    )

    assert "Fel:" in result
    assert store.get_strength_sets_for_date("2026-08-20") == []


def test_a_whole_number_written_as_a_decimal_still_saves(store: Store) -> None:
    """5.0 reps ÄR fem reps. Spärren ovan ska inte träffa den."""
    pipeline = AnalysisPipeline(store, FakeClaudeClient())

    result = pipeline.execute_tool(
        "log_strength_session",
        {"exercise": "chins", "sets": [{"reps": 5.0}], "day": "2026-08-20"},
    )

    assert "Sparat: chins" in result
    assert [r["reps"] for r in store.get_strength_sets_for_date("2026-08-20")] == [5]
