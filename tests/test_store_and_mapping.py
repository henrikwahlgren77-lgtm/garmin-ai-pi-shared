"""Enhetstester för SQLite-lager och Intervals-mappning (inga nätverksanrop)."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from sync.intervals_client import _map_activity, _map_wellness, _parse_dt
from sync.store import Store
from tests.conftest import make_settings


def _nyligen(dagar: int = 2) -> str:
    """Ett passdatum som garanterat ligger innanför synkfönstret.

    Testerna nedan hade "2026-08-20T07:00:00" inskrivet för hand.
    _sync_window sträcker sig ACTIVITY_WINDOW_DAYS (14) bakåt från IDAG,
    så det datumet låg innanför fönstret när testerna skrevs — och föll ur
    det natten till 2026-09-04, utan att någon rört vare sig koden eller
    testet. Symptomet var att "hoppa över detaljhämtningen för ett
    oförändrat pass" plötsligt inte gick att verifiera: passet låg utanför
    fönstret, activity_sync_dates returnerade ingenting, och synken gick
    den fulla vägen precis som den ska göra för ett okänt pass.

    Ett relativt datum kan inte åldras ut.
    """
    return (datetime.now() - timedelta(days=dagar)).strftime("%Y-%m-%dT07:00:00")


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "test.db")
    s.init()
    return s


def test_store_uses_a_generous_sqlite_busy_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Två OLIKA processer skriver till databasen: webbservern (med sin
    schemalagda sync) och systemd-timern som kör `python -m src.main sync`.
    Ett trådlås kan inte samordna dem — det enda som hindrar den ena från
    att få "database is locked" är SQLites busy-timeout, vars standardvärde
    (5 s) är i kortaste laget för en sync som laddar ner och parsar
    FIT-filer för nya styrkepass."""
    from sync import store as store_module

    captured: dict[str, object] = {}
    real_connect = store_module.sqlite3.connect

    def _spy(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        captured.update(kwargs)
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(store_module.sqlite3, "connect", _spy)

    Store(tmp_path / "timeout.db").init()

    timeout = captured.get("timeout")
    assert isinstance(timeout, float)
    assert timeout >= 30.0, (
        "busy-timeouten ska ge en samtidig skrivning gott om tid att bli klar"
    )


def _wellness_row(day: str) -> dict:
    """Minimal wellness-rad. Bara de kolumner upsert:en kräver som namngivna
    parametrar — resten fylls med None."""
    cols = (
        "rest_day ctl atl tsb ramp_rate sleep_seconds sleep_score sleep_quality "
        "avg_sleeping_hr weight resting_hr hrv hrv_sdnn stress respiration spO2 "
        "systolic diastolic hydration soreness fatigue mood motivation injury "
        "readiness vo2max steps kcal_consumed raw_json last_synced"
    ).split()
    row: dict = {c: None for c in cols}
    row["day"] = day
    row["sleep_score"] = 80
    row["raw_json"] = "{}"
    row["last_synced"] = "2026-08-20T00:00:00"
    return row


def test_parse_dt_handles_z_suffix() -> None:
    result = _parse_dt("2024-01-15T10:30:00Z")
    assert result is not None
    assert result.endswith("+00:00")


def test_parse_dt_none() -> None:
    assert _parse_dt(None) is None
    assert _parse_dt("") is None


def test_map_activity_basic() -> None:
    raw = {
        "id": "abc123",
        "name": "Lång löprunda",
        "type": "Run",
        "sport": "Run",
        "startDate": "2024-01-15T10:30:00Z",
        "duration": 3600,
        "distance": 10000,
        "averageHeartRate": 150,
        "maxHeartRate": 175,
        "trainingStressScore": 75.5,
    }
    mapped = _map_activity(raw, detail=None)
    assert mapped["id"] == "abc123"
    assert mapped["name"] == "Lång löprunda"
    assert mapped["duration_seconds"] == 3600
    assert mapped["distance_meters"] == 10000
    assert mapped["tss"] == 75.5
    assert mapped["raw_json"] is not None


def test_map_activity_uses_correct_snake_case_duration_fields() -> None:
    """Regressionstest: Intervals officiella OpenAPI-schema använder
    snake_case (moving_time/elapsed_time) för aktivitetens längd — koden
    letade tidigare efter movingTime/elapsedTime (camelCase), som aldrig
    matchar något fält i det riktiga API-svaret. Det gjorde att "Längd"
    ofta visades tom trots att andra fält (puls, distans) fungerade,
    eftersom de redan råkade använda rätt snake_case-namn."""
    raw = {
        "id": "act1",
        "startDate": "2024-01-15T10:30:00Z",
        "moving_time": 1800,
        "elapsed_time": 2000,
        # De gamla, felaktiga camelCase-namnen ska INTE plockas upp.
        "movingTime": 9999,
        "elapsedTime": 9999,
    }
    mapped = _map_activity(raw, detail=None)
    # moving_time prioriteras (aktiv tid, inte inklusive stopp).
    assert mapped["duration_seconds"] == 1800


def test_map_activity_falls_back_to_elapsed_time_then_duration() -> None:
    raw_elapsed_only = {
        "id": "act2", "startDate": "2024-01-15T10:30:00Z", "elapsed_time": 2000,
    }
    assert _map_activity(raw_elapsed_only, detail=None)["duration_seconds"] == 2000

    raw_duration_only = {
        "id": "act3", "startDate": "2024-01-15T10:30:00Z", "duration": 1500,
    }
    assert _map_activity(raw_duration_only, detail=None)["duration_seconds"] == 1500


def test_map_wellness_basic() -> None:
    raw = {
        "day": "2024-01-15",
        "ctl": 45.2,
        "atl": 30.1,
        "tsb": 15.1,
        "sleepSecs": 28800,
        "weight": 72.5,
    }
    mapped = _map_wellness(raw)
    assert mapped["day"] == "2024-01-15"
    assert mapped["ctl"] == 45.2
    assert mapped["atl"] == 30.1
    assert mapped["tsb"] == 15.1
    assert mapped["sleep_seconds"] == 28800


def test_store_upsert_and_get_activity(store: Store) -> None:
    a = {
        "id": "act1",
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
    store.upsert_activity(a)
    fetched = store.get_activity("act1")
    assert fetched is not None
    assert fetched["name"] == "Test"
    assert fetched["tss"] == 75.0

    # Uppdatera och verifiera idempotens.
    a["name"] = "Uppdaterad"
    store.upsert_activity(a)
    fetched = store.get_activity("act1")
    assert fetched is not None
    assert fetched["name"] == "Uppdaterad"


def test_store_list_activities(store: Store) -> None:
    for i in range(3):
        store.upsert_activity(
            {
                "id": f"act{i}",
                "name": f"P{i}",
                "type": "Run",
                "sport": "Run",
                "start_time": f"2024-01-1{i}T10:00:00+00:00",
                "duration_seconds": 3600,
                "distance_meters": 10000,
                "average_heart_rate": 150,
                "max_heart_rate": 175,
                "average_watts": None,
                "normalized_watts": None,
                "average_cadence": None,
                "average_speed": None,
                "tss": 70.0,
                "intensity": None,
                "raw_json": "{}",
                "last_synced": "2024-01-15T12:00:00+00:00",
            }
        )
    activities = store.list_activities(limit=10)
    assert len(activities) == 3
    # Senaste först (sortering DESC på start_time).
    assert activities[0]["id"] == "act2"


def test_store_save_and_get_analysis(store: Store) -> None:
    aid = store.save_analysis("daily_summary", "# Sammanfattning\nText.", None, "claude-sonnet-5")
    assert aid > 0
    latest = store.latest_analysis("daily_summary")
    assert latest is not None
    assert "Sammanfattning" in latest["markdown"]
    assert latest["model"] == "claude-sonnet-5"


def test_store_activity_analysis_by_ref(store: Store) -> None:
    store.save_analysis("activity", "# Passanalys", "act1", "claude-sonnet-5")
    got = store.latest_activity_analysis("act1")
    assert got is not None
    assert got["ref_id"] == "act1"
    # Andra ref ska inte träffa.
    assert store.latest_activity_analysis("act2") is None


def test_list_activities_uses_correct_auth_and_oldest(monkeypatch) -> None:
    """Regressionstest: auth ska vara Basic med username 'API_KEY' och
    lösenord = api-nyckel, samt 'oldest'-parametern måste skickas."""
    from sync.intervals_client import IntervalsClient

    captured: dict = {}

    class FakeResp:
        status_code = 200
        content = b"[]"
        headers: dict = {}
        def raise_for_status(self) -> None:
            return None
        def json(self) -> list:
            return []

    class FakeClient:
        def __init__(self, *a, auth=None, **k):
            captured["auth"] = auth
        def request(self, method, url, params=None):
            captured["params"] = params
            return FakeResp()
        def close(self):
            pass

    settings = make_settings(intervals_api_key="my-secret-key")
    monkeypatch.setattr("sync.intervals_client.httpx.Client", FakeClient)
    client = IntervalsClient(settings)
    client.list_activities()

    # oldest-parametern måste skickas (annars 422).
    assert "oldest" in captured["params"], "oldest-parametern saknas -> skulle ge 422"
    # Auth ska vara BasicAuth med username 'API_KEY' och lösenord = api-nyckel.
    assert captured["auth"] is not None, "auth saknas"
    # httpx.BasicAuth lagrar credentials som (_username, _password).
    creds = getattr(captured["auth"], "_auth", None)
    if creds is not None:
        username, password = creds
    else:
        # Fallback för andra httpx-versioner.
        import base64
        header = captured["auth"]._auth_header  # type: ignore[attr-defined]
        decoded = base64.b64decode(header.split(" ")[1]).decode()
        username, password = decoded.split(":", 1)
    assert username == "API_KEY", f"username ska vara 'API_KEY', var {username!r}"
    assert password == "my-secret-key", f"lösenord ska vara api-nyckeln, var {password!r}"


def test_map_activity_preserves_legitimate_zero_values() -> None:
    """Regressionstest: ett fält som legitimt är 0 (t.ex. TSS eller kadens
    för ett mycket lätt/kort pass) ska inte tolkas som "saknas" och
    ersättas av nästa fallback-fält (bugg med `a or b or c`-kedjor)."""
    raw = {
        "id": "zero1",
        "startDate": "2024-01-15T10:00:00Z",
        "icu_training_load": 0,
        "trainingStressScore": 999,  # ska INTE användas, icu_training_load=0 gäller
        "average_cadence": 0,
        "averageCadence": 55,  # ska INTE användas
        "icu_intensity_factor": 0,
        "intensity": 0.8,  # ska INTE användas
    }
    mapped = _map_activity(raw, detail=None)
    assert mapped["tss"] == 0
    assert mapped["average_cadence"] == 0
    assert mapped["intensity"] == 0


def test_map_activity_handles_list_detail() -> None:
    """Detalj-endpointen kan returnera en lista; mappningen ska hantera det."""
    from sync.intervals_client import _map_activity

    raw = {"id": "abc", "name": "Pass", "startDate": "2024-01-15T10:00:00Z"}
    # detalj som lista med dict-element
    detail_as_list = [{"icu_training_load": 55.5, "icu_weighted_avg_watts": 200}]
    mapped = _map_activity(raw, detail_as_list)
    assert mapped["id"] == "abc"
    assert mapped["tss"] == 55.5
    assert mapped["normalized_watts"] == 200

    # detalj som None
    mapped_none = _map_activity(raw, None)
    assert mapped_none["id"] == "abc"

    # detalj som dict
    detail_as_dict = {"icu_training_load": 40.0}
    mapped_dict = _map_activity(raw, detail_as_dict)
    assert mapped_dict["tss"] == 40.0


def test_add_and_list_self_reports(store: Store) -> None:
    """log_self_report-verktyget (via chatten) sparar hit — append-only,
    inte upsert, eftersom samma kategori kan loggas flera gånger per dag
    (t.ex. flera drinkar under kvällen).

    Datumen räknas från idag, inte hårdkodade: list_self_reports(days=30)
    filtrerar på ett rullande fönster, och ett fast datum (tidigare
    2026-08-20) rullar förr eller senare ut ur det — testet gick från grönt
    till "assert 0 == 4" utan att koden det testar ändrats alls.
    """
    today = datetime.now().date()
    day = today.isoformat()
    yesterday = (today - timedelta(days=1)).isoformat()
    store.add_self_report(day=day, category="weight", value=91.2, note=None)
    store.add_self_report(
        day=day, category="alcohol", value=None, note="två öl till middag"
    )
    store.add_self_report(
        day=day, category="alcohol", value=None, note="ett glas vin senare"
    )
    store.add_self_report(day=yesterday, category="illness", value=None, note="lite snorig")

    today_reports = store.get_self_reports_for_date(day)
    assert len(today_reports) == 3
    categories = {r["category"] for r in today_reports}
    assert categories == {"weight", "alcohol"}

    recent = store.list_self_reports(days=30)
    assert len(recent) == 4
    # Nyast först (dag, sedan created_at).
    assert recent[0]["day"] == day


def test_list_self_reports_respects_day_cutoff(store: Store) -> None:
    from datetime import datetime, timedelta

    old_day = (datetime.now().date() - timedelta(days=60)).isoformat()
    recent_day = (datetime.now().date() - timedelta(days=2)).isoformat()
    store.add_self_report(day=old_day, category="note", note="för gammalt för att synas")
    store.add_self_report(day=recent_day, category="note", note="ska synas")

    reports = store.list_self_reports(days=30)
    notes = [r["note"] for r in reports]
    assert "ska synas" in notes
    assert "för gammalt för att synas" not in notes


# --- Styrketräning (strength_sets) -----------------------------------


def test_add_strength_sets_stores_one_row_per_set(store: Store) -> None:
    """Ett anrop med tre set ska ge tre rader, numrerade 1-3 i utförd ordning
    — inte en rad med 'sets=3'. Det är den formen som gör att set med olika
    reps (8/7/5) kan sparas och att FIT-importen kan skriva till samma tabell."""
    count = store.add_strength_sets(
        day="2026-08-20",
        exercise="bänkpress",
        sets=[
            {"reps": 8, "weight_kg": 80.0},
            {"reps": 7, "weight_kg": 80.0},
            {"reps": 5, "weight_kg": 80.0, "rpe": 9.0},
        ],
    )
    assert count == 3

    rows = store.get_strength_sets_for_date("2026-08-20")
    assert len(rows) == 3
    assert [r["set_number"] for r in rows] == [1, 2, 3]
    assert [r["reps"] for r in rows] == [8, 7, 5]
    assert rows[2]["rpe"] == 9.0
    # source defaultar till 'chat' och skiljer manuell loggning från FIT-import.
    assert {r["source"] for r in rows} == {"chat"}


def test_add_strength_sets_allows_missing_weight_for_bodyweight(store: Store) -> None:
    """Kroppsviktsövningar har reps men ingen vikt — det ska inte krascha
    eller sparas som 0 kg."""
    store.add_strength_sets(
        day="2026-08-20", exercise="chins", sets=[{"reps": 6}, {"reps": 5}]
    )
    rows = store.get_strength_sets_for_date("2026-08-20")
    assert len(rows) == 2
    assert all(r["weight_kg"] is None for r in rows)


def test_add_strength_sets_with_empty_list_is_noop(store: Store) -> None:
    assert store.add_strength_sets(day="2026-08-20", exercise="knäböj", sets=[]) == 0
    assert store.get_strength_sets_for_date("2026-08-20") == []


def test_get_strength_sets_for_activity_filters_by_activity(store: Store) -> None:
    store.add_strength_sets(
        day="2026-08-20", exercise="marklyft", sets=[{"reps": 5, "weight_kg": 120.0}],
        activity_id="act1",
    )
    store.add_strength_sets(
        day="2026-08-20", exercise="knäböj", sets=[{"reps": 5, "weight_kg": 100.0}],
        activity_id="act2",
    )
    # Loggat utan att något pass kunde matchas.
    store.add_strength_sets(
        day="2026-08-20", exercise="hantelcurl", sets=[{"reps": 10, "weight_kg": 15.0}]
    )

    assert [r["exercise"] for r in store.get_strength_sets_for_activity("act1")] == ["marklyft"]
    assert [r["exercise"] for r in store.get_strength_sets_for_activity("act2")] == ["knäböj"]
    # Alla tre finns kvar på dagen, även den utan aktivitet.
    assert len(store.get_strength_sets_for_date("2026-08-20")) == 3


def test_list_strength_sets_respects_day_cutoff(store: Store) -> None:
    from datetime import datetime, timedelta

    old_day = (datetime.now().date() - timedelta(days=60)).isoformat()
    recent_day = (datetime.now().date() - timedelta(days=2)).isoformat()
    store.add_strength_sets(day=old_day, exercise="gammal övning", sets=[{"reps": 5}])
    store.add_strength_sets(day=recent_day, exercise="ny övning", sets=[{"reps": 5}])

    exercises = [r["exercise"] for r in store.list_strength_sets(days=30)]
    assert "ny övning" in exercises
    assert "gammal övning" not in exercises


# --- Enhetligt dagsfönster i alla list-metoder -----------------------


def test_list_wellness_filters_by_date_not_row_count(store: Store) -> None:
    """list_wellness körde LIMIT N rader medan list_self_reports och
    list_strength_sets filtrerade på datum. Skillnaden märks så fort det
    finns luckor: med LIMIT gav days=7 de sju senaste raderna som fanns,
    vilket kunde spänna över betydligt fler kalenderdagar än rubriken
    lovade."""
    from datetime import date, timedelta

    today = date.today()

    def row(day: str) -> dict:
        return {
            "day": day, "rest_day": 0, "ctl": 40.0, "atl": 30.0, "tsb": 10.0,
            "ramp_rate": None, "sleep_seconds": None, "sleep_score": None,
            "sleep_quality": None, "avg_sleeping_hr": None, "weight": None,
            "resting_hr": None, "hrv": None, "hrv_sdnn": None, "stress": None,
            "respiration": None, "spO2": None, "systolic": None,
            "diastolic": None, "hydration": None, "soreness": None,
            "fatigue": None, "mood": None, "motivation": None, "injury": None,
            "readiness": None, "vo2max": None, "steps": None,
            "kcal_consumed": None, "raw_json": "{}", "last_synced": day,
        }

    # Tre dagar inom veckan och tre med en lucka långt bak i tiden.
    inside = [(today - timedelta(days=n)).isoformat() for n in (0, 3, 6)]
    outside = [(today - timedelta(days=n)).isoformat() for n in (7, 20, 60)]
    for day in inside + outside:
        store.upsert_wellness_many([row(day)])

    got = [w["day"] for w in store.list_wellness(days=7)]

    # Med LIMIT 7 hade alla sex raderna kommit med.
    assert sorted(got) == sorted(inside)
    for day in outside:
        assert day not in got


def test_day_windows_include_today_and_are_consistent(store: Store) -> None:
    """days=7 ska betyda sju kalenderdagar inklusive idag. self_reports
    och strength_sets räknade tidigare today - 7, alltså åtta dagar."""
    from datetime import date, timedelta

    from sync.store import _cutoff_day

    today = date.today()
    assert _cutoff_day(7) == (today - timedelta(days=6)).isoformat()
    assert _cutoff_day(1) == today.isoformat()

    # Dagen precis utanför fönstret ska falla bort i alla tre tabellerna.
    just_outside = (today - timedelta(days=7)).isoformat()
    edge = (today - timedelta(days=6)).isoformat()

    store.add_self_report(day=just_outside, category="note", note="för gammal")
    store.add_self_report(day=edge, category="note", note="precis inom")
    store.add_strength_sets(day=just_outside, exercise="gammal", sets=[{"reps": 5}])
    store.add_strength_sets(day=edge, exercise="ny", sets=[{"reps": 5}])

    notes = [r["note"] for r in store.list_self_reports(days=7)]
    assert notes == ["precis inom"]
    exercises = [r["exercise"] for r in store.list_strength_sets(days=7)]
    assert exercises == ["ny"]


def _wellness_with_weight(day: str, weight: float) -> dict:
    row = {
        k: None for k in (
            "rest_day", "ctl", "atl", "tsb", "ramp_rate", "sleep_seconds",
            "sleep_score", "sleep_quality", "avg_sleeping_hr", "resting_hr",
            "hrv", "hrv_sdnn", "stress", "respiration", "spO2", "systolic",
            "diastolic", "hydration", "soreness", "fatigue", "mood",
            "motivation", "injury", "readiness", "vo2max", "steps",
            "kcal_consumed", "raw_json", "last_synced",
        )
    }
    row.update(day=day, weight=weight)
    return row


def test_chat_reported_weight_wins_on_the_same_day(store: Store) -> None:
    """Regressionstest: sorteringen var bara `ORDER BY day DESC`, utan
    något som skilde två träffar från samma dag åt — wellness-raden vann
    då eftersom den kommer först i UNION:en. Rapporterade man en ny vikt i
    chatten samma dag som vågen synkat ett äldre värde gick den nyare
    siffran förlorad, och fel vikt hamnade i varje analysprompt."""
    day = "2026-08-26"
    store.upsert_wellness_many([_wellness_with_weight(day, 120.0)])
    store.add_self_report(day=day, category="weight", value=112.5)

    assert store.latest_weight() == (112.5, day)


def test_a_newer_scale_reading_still_wins_on_a_later_day(store: Store) -> None:
    """Tiebreakern får bara gälla vid LIKA datum — en färskare vägning
    dagen efter ska fortfarande slå en äldre chattrapport."""
    store.add_self_report(day="2026-08-25", category="weight", value=112.5)
    store.upsert_wellness_many([_wellness_with_weight("2026-08-26", 119.0)])

    assert store.latest_weight() == (119.0, "2026-08-26")


def test_latest_weight_is_none_without_any_data(store: Store) -> None:
    assert store.latest_weight() is None


def test_latest_wellness_day_falls_back_to_the_most_recent_row(store: Store) -> None:
    """Regressionstest: dashboarden visade '-' för sömnscore, HRV och
    vilopuls hela förmiddagen tills schemaläggarens sync-jobb (var 60:e
    minut) hunnit hämta in dagens rad. latest_wellness_day() hittar
    senaste kända dag oavsett datum, så anroparen kan falla tillbaka på
    den i stället för att visa tomma värden."""
    store.upsert_wellness_many([_wellness_with_weight("2026-08-24", 100.0)])
    store.upsert_wellness_many([_wellness_with_weight("2026-08-25", 101.0)])

    latest = store.latest_wellness_day()
    assert latest is not None
    assert latest["day"] == "2026-08-25"


def test_latest_wellness_day_is_none_when_the_database_is_empty(store: Store) -> None:
    assert store.latest_wellness_day() is None


# --- Wellness-synkens fönster och bulkskrivning (fynd 2 och 3) --------


def test_routine_wellness_sync_only_fetches_a_short_window(tmp_path: Path) -> None:
    """Synken hämtade alltid hela årshistoriken och skrev om varenda rad,
    varje timme, dygnet runt — 366 rader i journalen på Pi:n en gång i
    timmen, för att i praktiken uppdatera dagens rad. Aktivitetssynken
    hade redan ett fönster; wellness saknade det."""
    from datetime import date

    from sync.intervals_client import WELLNESS_WINDOW_DAYS, sync_wellness
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()
    # En befintlig rad => databasen är inte tom => rutinsynk, inte full.
    today = date.today()
    store.upsert_wellness_many([_wellness_row(today.isoformat())])

    asked: dict[str, object] = {}

    class _Client:
        def list_wellness(self, oldest=None, newest=None):  # noqa: ANN001, ANN202
            asked["oldest"] = oldest
            return []

    sync_wellness(_Client(), store)  # type: ignore[arg-type]

    oldest = asked["oldest"]
    span = (today - oldest.date()).days  # type: ignore[union-attr]
    assert span == WELLNESS_WINDOW_DAYS, (
        f"rutinsynken hämtade {span} dagar, väntade {WELLNESS_WINDOW_DAYS}"
    )
    assert span < 365, "rutinsynken hämtar fortfarande hela historiken"


def test_first_wellness_sync_still_fetches_the_full_history(tmp_path: Path) -> None:
    """Tom databas ska fyllas med hela historiken — fönstret gäller bara
    rutinsynken. Samma sak med full=True."""
    from datetime import date

    from sync.intervals_client import DEFAULT_HISTORY_DAYS, sync_wellness
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()

    asked: list[object] = []

    class _Client:
        def list_wellness(self, oldest=None, newest=None):  # noqa: ANN001, ANN202
            asked.append(oldest)
            return []

    sync_wellness(_Client(), store)  # type: ignore[arg-type]
    span = (date.today() - asked[0].date()).days  # type: ignore[union-attr]
    assert span == DEFAULT_HISTORY_DAYS, "tom databas ska hämta hela historiken"

    # full=True även när det finns data.
    store.upsert_wellness_many([_wellness_row(date.today().isoformat())])
    sync_wellness(_Client(), store, full=True)  # type: ignore[arg-type]
    span = (date.today() - asked[1].date()).days  # type: ignore[union-attr]
    assert span == DEFAULT_HISTORY_DAYS, "full=True ska hämta hela historiken"


def test_bulk_wellness_upsert_writes_every_row_in_one_go(tmp_path: Path) -> None:
    """upsert_wellness_many ska ge samma resultat som rad-för-rad, men i en
    transaktion i stället för en anslutning per rad."""
    from datetime import date, timedelta

    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()

    today = date.today()
    days = [(today - timedelta(days=n)).isoformat() for n in range(5)]
    rows = [_wellness_row(d) for d in days]

    assert store.upsert_wellness_many(rows) == 5
    assert len(store.list_wellness(days=10)) == 5

    # Idempotent: samma dagar igen uppdaterar i stället för att dubblera.
    rows[0]["sleep_score"] = 99
    assert store.upsert_wellness_many(rows) == 5
    assert len(store.list_wellness(days=10)) == 5
    assert store.get_wellness_day(days[0])["sleep_score"] == 99

    assert store.upsert_wellness_many([]) == 0


# --- FIT-importens "vi har tittat"-notering (fynd 4) ------------------


def test_empty_fit_file_is_not_downloaded_again_every_sync(tmp_path: Path) -> None:
    """Spärren mot omnedladdning frågade "finns det set?" i stället för
    "har vi tittat?". Ett pass vars fil saknar set-data (Strava-import,
    TCX, eller ett gympass utan loggade set) sparade ingenting, så svaret
    förblev nej och filen hämtades om varje timme i all evighet."""
    from sync.intervals_client import import_strength_sets
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()
    activity = {"id": "a1", "start_time": "2026-08-20T18:00:00"}

    downloads: list[str] = []

    class _Client:
        def download_original_file(self, activity_id):  # noqa: ANN001, ANN202
            downloads.append(activity_id)
            return b"not a fit file at all"  # parse_strength_sets ger []

    client = _Client()
    assert import_strength_sets(client, store, activity) == 0  # type: ignore[arg-type]
    assert downloads == ["a1"], "första försöket ska hämta filen"

    # Andra och tredje synken ska INTE hämta om den.
    assert import_strength_sets(client, store, activity) == 0  # type: ignore[arg-type]
    assert import_strength_sets(client, store, activity) == 0  # type: ignore[arg-type]
    assert downloads == ["a1"], (
        f"filen hämtades {len(downloads)} gånger — spärren håller inte"
    )

    assert store.has_checked_strength_import("a1") is True
    # Inga skräprader i strength_sets: noteringen bor i en egen tabell.
    assert store.get_strength_sets_for_activity("a1") == []

    # force=True ska gå förbi spärren.
    assert import_strength_sets(client, store, activity, force=True) == 0  # type: ignore[arg-type]
    assert downloads == ["a1", "a1"]


def test_previously_imported_activities_are_not_refetched_for_a_marker(
    tmp_path: Path,
) -> None:
    """Bakåtkompatibilitet: pass som importerades innan
    strength_import_attempts fanns har rader men ingen notering. De ska
    inte hämtas om en gång bara för att få en."""
    from sync.intervals_client import import_strength_sets
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()
    store.add_strength_sets(
        day="2026-08-20", exercise="marklyft",
        sets=[{"reps": 5, "weight_kg": 120.0}],
        activity_id="a1", source="fit",
    )
    assert store.has_checked_strength_import("a1") is False  # ingen notering

    class _Client:
        def download_original_file(self, activity_id):  # noqa: ANN001, ANN202
            raise AssertionError("skulle inte hämta om ett redan importerat pass")

    assert import_strength_sets(_Client(), store, {"id": "a1"}) == 0  # type: ignore[arg-type]


def test_latest_analysis_before_picks_the_nearest_day_not_the_newest_row(
    tmp_path: Path,
) -> None:
    """Sorteringen ska gå på ref_id, alltså vilken DAG analysen handlar om
    — inte på created_at. Genererar man om en gammal dag i efterhand får
    den nyast skrivna raden ett gammalt ref_id, och en sortering på
    skrivtid hade då hämtat fel dygn."""
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()
    store.save_analysis("daily_summary", "20 augusti", "2026-08-20", "m")
    store.save_analysis("daily_summary", "28 augusti", "2026-08-28", "m")
    # Skriven SIST, men handlar om den äldsta dagen.
    store.save_analysis("daily_summary", "20 augusti, omgjord", "2026-08-20", "m")

    hittad = store.latest_analysis_before("daily_summary", "2026-08-30")
    assert hittad is not None
    assert hittad["markdown"] == "28 augusti"

    # Dagen själv räknas inte som "före".
    assert store.latest_analysis_before("daily_summary", "2026-08-20") is None


# --- Synkfönster och ändringsdetektering -----------------------------------


def test_sync_window_reaches_back_a_fixed_number_of_days(tmp_path) -> None:
    """Fönstret ska ankras i IDAG, inte i senast synkade pass.

    Det ankrades tidigare i `latest_activity_start() - 1 dygn`, vilket bara
    kunde se framåt: ett pass som laddas upp i efterhand med ett äldre
    datum än det nyaste vi redan har hamnade bakom fönstrets kant och
    hämtades aldrig — inte vid nästa synk, inte någonsin."""
    from datetime import datetime, timedelta

    from sync import intervals_client
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()

    # Tom databas: hela historiken.
    tom = intervals_client._sync_window(store)
    assert (datetime.now() - tom).days >= intervals_client.DEFAULT_HISTORY_DAYS - 1

    # Ett pass från igår gör inte fönstret smalare än det fasta.
    igår = (datetime.now() - timedelta(days=1)).isoformat()
    store.upsert_activity({
        "id": "a1", "name": "Pass", "type": "Run", "sport": "Run",
        "start_time": igår, "duration_seconds": 600, "distance_meters": 1000.0,
        "average_heart_rate": None, "max_heart_rate": None, "average_watts": None,
        "normalized_watts": None, "average_cadence": None, "average_speed": None,
        "tss": None, "intensity": None, "raw_json": "{}", "last_synced": igår,
    })
    fönster = intervals_client._sync_window(store)
    dagar = (datetime.now() - fönster).days
    assert dagar >= intervals_client.ACTIVITY_WINDOW_DAYS - 1, (
        "fönstret krympte till senaste passet — retroaktiva pass blir osynliga"
    )


def test_sync_skips_detail_fetch_for_unchanged_activities(tmp_path) -> None:
    """Detaljanropet är ETT HTTP-anrop per pass och gjordes för varje pass
    i fönstret vid varje timvis synk — för att skriva tillbaka exakt samma
    rad. Intervals stämplar passen med icu_sync_date, som följer med redan
    i listsvaret, så oförändrade pass kan hoppas över helt."""
    from sync import intervals_client
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()
    hämtade = []

    class _Client:
        def list_activities(self, oldest=None, newest=None):
            return [{
                "id": "run1", "type": "Run", "sport": "Run",
                "start_date_local": _nyligen(),
                "icu_sync_date": "2026-08-20T08:00:00.000+00:00",
            }]

        def get_activity(self, activity_id):
            hämtade.append(activity_id)
            return {}

    # Första synken: passet är nytt, detaljen hämtas.
    assert intervals_client.sync_activities(_Client(), store) == 1
    assert hämtade == ["run1"]

    # Andra synken, samma icu_sync_date: inget anrop, ingen skrivning.
    assert intervals_client.sync_activities(_Client(), store) == 0
    assert hämtade == ["run1"], "detaljen hämtades igen för ett oförändrat pass"


def test_sync_refetches_when_intervals_restamps_the_activity(tmp_path) -> None:
    """Ändrar man passet i Intervals får det en ny icu_sync_date, och då
    ska det hämtas om — annars slutar ändringar nå oss."""
    from sync import intervals_client
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()
    hämtade = []

    class _Client:
        def __init__(self, stämpel, namn):
            self.stämpel, self.namn = stämpel, namn

        def list_activities(self, oldest=None, newest=None):
            return [{
                "id": "run1", "name": self.namn, "type": "Run", "sport": "Run",
                "start_date_local": _nyligen(),
                "icu_sync_date": self.stämpel,
            }]

        def get_activity(self, activity_id):
            hämtade.append(activity_id)
            return {}

    intervals_client.sync_activities(_Client("2026-08-20T08:00:00Z", "Löprunda"), store)
    assert store.get_activity("run1")["name"] == "Löprunda"

    intervals_client.sync_activities(_Client("2026-08-21T09:00:00Z", "Intervaller"), store)
    assert len(hämtade) == 2, "en ny icu_sync_date ska utlösa en ny hämtning"
    assert store.get_activity("run1")["name"] == "Intervaller"


def test_sync_still_fetches_when_the_stamp_is_missing(tmp_path) -> None:
    """Utan icu_sync_date går vi den fulla vägen — hellre ett onödigt
    anrop än ett pass som tyst slutar uppdateras."""
    from sync import intervals_client
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()
    hämtade = []

    class _Client:
        def list_activities(self, oldest=None, newest=None):
            return [{"id": "run1", "type": "Run", "sport": "Run",
                     "start_date_local": _nyligen()}]

        def get_activity(self, activity_id):
            hämtade.append(activity_id)
            return {}

    intervals_client.sync_activities(_Client(), store)
    intervals_client.sync_activities(_Client(), store)
    assert len(hämtade) == 2


class _ListClient:
    """Svarar med de pass den fått, som styrkepass på samma dag."""

    def __init__(self, ids: list[str], start: str) -> None:
        self.ids, self.start = ids, start

    def list_activities(self, oldest=None, newest=None):  # noqa: ANN001, ANN201
        return [
            {"id": i, "type": "WeightTraining", "sport": "WeightTraining",
             "start_date_local": self.start, "icu_sync_date": "2026-09-01T08:00:00Z"}
            for i in self.ids
        ]

    def get_activity(self, activity_id):  # noqa: ANN001, ANN201
        return {}


def test_sync_removes_activities_deleted_in_intervals(tmp_path) -> None:
    """Synken tog aldrig bort något. En dubblett du raderade i Intervals
    låg kvar i passlistan, i veckovolymen och i varje analys underlag.

    Klockans set för det borttagna passet försvinner med det; set du
    loggat i chatten flyttas till dagens kvarvarande styrkepass."""
    from sync import intervals_client
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()
    start = _nyligen()
    dag = start[:10]
    # Filerna räknas som lästa, så synken inte försöker ladda ner dem.
    for pass_id in ("gym1", "gym2"):
        store.record_strength_import(pass_id, dag, 1)

    intervals_client.sync_activities(_ListClient(["gym1", "gym2"], start), store)
    store.add_strength_sets(day=dag, exercise="marklyft", activity_id="gym2",
                            sets=[{"reps": 5, "weight_kg": 100.0}], source="fit")
    store.add_strength_sets(day=dag, exercise="planka", activity_id="gym2",
                            sets=[{"reps": 1}], source="chat")

    intervals_client.sync_activities(_ListClient(["gym1"], start), store)

    assert store.get_activity("gym2") is None
    assert store.get_activity("gym1") is not None
    assert [
        (r["exercise"], r["activity_id"], r["source"])
        for r in store.get_strength_sets_for_date(dag)
    ] == [("planka", "gym1", "chat")]
    assert not store.has_checked_strength_import("gym2")


def test_sync_only_removes_what_the_answer_could_have_contained(tmp_path) -> None:
    """Ett pass äldre än fönstret finns inte i svaret, och att det saknas
    där säger ingenting. Ett tomt svar går inte att skilja från ett fel
    hos Intervals, så det raderar ingenting alls."""
    from sync import intervals_client
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()
    gammal = _nyligen(dagar=intervals_client.ACTIVITY_WINDOW_DAYS + 10)
    ny = _nyligen()
    for pass_id, start in (("gammal", gammal), ("ny", ny)):
        store.upsert_activity({
            "id": pass_id, "name": "Pass", "type": "Run", "sport": "Run",
            "start_time": start, "duration_seconds": 600, "distance_meters": 1000.0,
            "average_heart_rate": None, "max_heart_rate": None, "average_watts": None,
            "normalized_watts": None, "average_cadence": None, "average_speed": None,
            "tss": None, "intensity": None, "raw_json": "{}", "last_synced": start,
        })

    class _Tom:
        def list_activities(self, oldest=None, newest=None):  # noqa: ANN001, ANN202
            return []

    intervals_client.sync_activities(_Tom(), store)
    assert store.get_activity("ny") is not None, "ett tomt svar raderade passen"

    store.record_strength_import("annat", ny[:10], 1)
    intervals_client.sync_activities(_ListClient(["annat"], ny), store)
    assert store.get_activity("ny") is None
    assert store.get_activity("gammal") is not None, (
        "ett pass utanför fönstret togs bort fast svaret inte kunde innehålla det"
    )


def test_strength_history_counts_sessions_per_exercise_not_days(tmp_path) -> None:
    """Fönstret räknas i pass per övning.

    Ett tak i kalenderdagar skulle ge tät historik för det som körs ofta och
    ingen alls för det som körs sällan — här ligger det glesa olympiska
    lyftet ett halvår bak men ska ändå med, medan knäböjen kapas vid taket.
    """
    store = Store(tmp_path / "hist.db")
    store.init()
    for n in range(1, 7):
        store.add_strength_sets(f"2026-08-{n:02d}", "knäböj", [{"reps": 5, "weight_kg": 100.0}])
    store.add_strength_sets("2026-02-01", "olympiskt lyft", [{"reps": 3, "weight_kg": 60.0}])

    rows = store.get_strength_history(
        ["knäböj", "olympiskt lyft"], before_day="2026-09-01", sessions_per_exercise=3
    )

    days = {(r["exercise"], r["day"]) for r in rows}
    assert days == {
        ("knäböj", "2026-08-06"),
        ("knäböj", "2026-08-05"),
        ("knäböj", "2026-08-04"),
        ("olympiskt lyft", "2026-02-01"),
    }


def test_strength_history_excludes_the_reference_day(tmp_path) -> None:
    store = Store(tmp_path / "hist.db")
    store.init()
    store.add_strength_sets("2026-08-20", "marklyft", [{"reps": 3, "weight_kg": 135.0}])
    store.add_strength_sets("2026-08-13", "marklyft", [{"reps": 5, "weight_kg": 130.0}])

    rows = store.get_strength_history(["marklyft"], before_day="2026-08-20")

    assert [r["day"] for r in rows] == ["2026-08-13"]


def test_strength_history_keeps_every_set_of_a_session(tmp_path) -> None:
    """fmt_strength_sessions räknar set genom att räkna rader, så historiken
    måste bära hela passet — inte en rad per dag."""
    store = Store(tmp_path / "hist.db")
    store.init()
    store.add_strength_sets(
        "2026-08-13",
        "bänkpress",
        [{"reps": 8, "weight_kg": 80.0}, {"reps": 8, "weight_kg": 80.0}],
    )

    rows = store.get_strength_history(["bänkpress"], before_day="2026-08-20")

    assert len(rows) == 2
    assert [r["set_number"] for r in rows] == [1, 2]


def test_strength_history_without_exercises_hits_no_database(tmp_path) -> None:
    store = Store(tmp_path / "hist.db")
    store.init()
    store.add_strength_sets("2026-08-13", "knäböj", [{"reps": 5, "weight_kg": 100.0}])

    assert store.get_strength_history([], before_day="2026-08-20") == []
    assert store.get_strength_history(["knäböj"], before_day="") == []


# --- Rätta och radera självrapporter ----------------------------------


def test_update_self_report_touches_only_the_given_fields(store: Store) -> None:
    """Att utelämna ett fält betyder "lämna som det är", inte "sätt till
    null" — annars hade en rättad vikt raderat anteckningen bredvid."""
    report_id = store.add_self_report(
        day="2026-09-02", category="weight", value=122.3, note="efter frukost"
    )

    assert store.update_self_report(report_id, value=121.8) is True

    rad = store.get_self_report(report_id)
    assert rad is not None
    assert rad["value"] == 121.8
    assert rad["note"] == "efter frukost"
    assert rad["day"] == "2026-09-02"
    assert rad["category"] == "weight"


def test_update_self_report_ignores_fields_outside_the_whitelist(store: Store) -> None:
    """SET-satsen byggs av nycklarna i anropet. En osållad sådan är en
    injektionsväg, och id/created_at ska inte gå att skriva om — den
    senare säger när uppgiften registrerades, inte vad den påstår."""
    report_id = store.add_self_report(day="2026-09-02", category="weight", value=122.3)
    innan = store.get_self_report(report_id)
    assert innan is not None

    assert store.update_self_report(report_id, created_at="1999-01-01") is False
    assert store.update_self_report(report_id, id=9999) is False

    efter = store.get_self_report(report_id)
    assert efter == innan


def test_update_self_report_reports_a_missing_row(store: Store) -> None:
    assert store.update_self_report(999, value=80.0) is False


def test_delete_self_report_removes_only_that_row(store: Store) -> None:
    behall = store.add_self_report(day="2026-09-02", category="weight", value=122.3)
    bort = store.add_self_report(day="2026-09-03", category="note", note="fel rad")

    assert store.delete_self_report(bort) is True
    assert store.delete_self_report(bort) is False, "redan borta"

    kvar = store.list_self_reports(days=3650)
    assert [r["id"] for r in kvar] == [behall]


def test_store_uses_normal_synchronous_in_wal_mode(tmp_path: Path) -> None:
    """SQLites standard är synchronous=FULL, vilket i WAL-läge fsync:ar
    WAL-filen vid VARJE commit. NORMAL synkar först vid checkpoint.

    Skillnaden i hållbarhet är att ett strömavbrott kan tappa de sista
    transaktionerna; en processkrasch kan det inte. Appen kör på ett
    SD-kort, backar upp varje natt, och det mesta av datan går att synka
    om från Intervals — att fsync:a varje timvis synk för den risken är
    slitage utan motsvarande nytta."""
    store = Store(tmp_path / "pragma.db")
    store.init()
    with store._conn() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1, (
            "1 = NORMAL; 2 = FULL är SQLites standard"
        )


def test_schema_has_no_foreign_keys_so_the_pragma_is_gone(tmp_path: Path) -> None:
    """PRAGMA foreign_keys=ON kördes vid varje anslutning och gjorde
    ingenting — schemat har inga främmande nycklar alls.

    Testet är vänt åt andra hållet mot vad man först tänker: det vaktar
    inte att pragman är borta, utan att FÖRUTSÄTTNINGEN för att den ska
    vara borta håller. Lägger någon till en FOREIGN KEY faller det här, och
    då måste pragman tillbaka — SQLite har kontrollen avstängd som
    standard, så en FK utan den är dekoration."""
    store = Store(tmp_path / "fk.db")
    store.init()
    with store._conn() as conn:
        tabeller = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
        for tabell in tabeller:
            fks = conn.execute(f"PRAGMA foreign_key_list({tabell})").fetchall()
            assert not fks, (
                f"{tabell} har en främmande nyckel — då måste "
                "PRAGMA foreign_keys=ON tillbaka i Store._conn"
            )


def test_add_strength_sets_normalises_the_exercise_name(store: Store) -> None:
    """Namnet är fritext från två källor: FIT-importen bygger det ur Garmins
    enum ("marklyft"), chatten får det av Claude, som ombeds skriva gemener
    men lika gärna kan skicka "Marklyft" i början av en mening."""
    store.add_strength_sets(
        day="2026-09-03", exercise="  Raka   Marklyft  ", sets=[{"reps": 5}]
    )

    (rad,) = store.get_strength_sets_for_date("2026-09-03")
    assert rad["exercise"] == "raka marklyft"


def test_init_normalises_exercise_names_written_before_the_rule(
    tmp_path: Path,
) -> None:
    """En databas skriven innan regeln fanns ska städas vid uppstart —
    annars ligger de gamla stavningarna kvar och splittrar historiken.

    SQLites lower() rör bara ASCII, så migreringen görs i Python: "KNÄBÖJ"
    hade annars blivit "knÄbÖj"."""
    import sqlite3

    db = tmp_path / "gammal.db"
    store = Store(db)
    store.init()
    with sqlite3.connect(db) as conn:
        for namn in ("KNÄBÖJ", "Knäböj", "knäböj"):
            conn.execute(
                "INSERT INTO strength_sets (day, exercise, source, created_at) "
                "VALUES ('2026-09-03', ?, 'chat', '2026-09-03T10:00:00')",
                (namn,),
            )

    Store(db).init()

    rader = store.get_strength_sets_for_date("2026-09-03")
    assert {r["exercise"] for r in rader} == {"knäböj"}


def test_latest_weight_picks_the_newest_report_of_the_day(store: Store) -> None:
    """Väger man sig två gånger samma dag är det den senare som gäller.

    Passerar även på koden före tiebreaken — där föll svaret rätt ut ändå,
    men bara därför att idx_self_reports_day råkar sortera på created_at
    DESC. Frågan sa det inte, så ett ändrat index hade tyst ändrat svaret.
    Testet finns för att fästa kontraktet, inte för att visa ett fel.
    """
    import sqlite3

    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO self_reports (day, category, value, note, created_at) "
            "VALUES ('2026-09-06', 'weight', 121.0, 'morgon', "
            "'2026-09-06T07:00:00')"
        )
        conn.execute(
            "INSERT INTO self_reports (day, category, value, note, created_at) "
            "VALUES ('2026-09-06', 'weight', 120.5, 'kväll', "
            "'2026-09-06T20:00:00')"
        )

    assert store.latest_weight() == (120.5, "2026-09-06")


def test_latest_weight_still_prefers_the_chat_over_the_scale(store: Store) -> None:
    """Vågen mäter på morgonen; säger man något i chatten är det senare på
    dagen. Tidsstämpeln som tredje nyckel får inte kasta om den ordningen —
    wellness bär last_synced, som skrivs om vid varje synk."""
    import sqlite3

    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO wellness (day, weight, last_synced) "
            "VALUES ('2026-09-06', 122.9, '2026-09-06T23:00:00')"
        )
        conn.execute(
            "INSERT INTO self_reports (day, category, value, note, created_at) "
            "VALUES ('2026-09-06', 'weight', 120.5, 'chatten', "
            "'2026-09-06T09:00:00')"
        )

    assert store.latest_weight() == (120.5, "2026-09-06")


# --- Sportinställningar (pulszoner) --------------------------------------

_ZONNAMN = ["Recovery", "Aerobic", "Tempo", "SubThreshold"]


def _sportrader() -> list[dict[str, object]]:
    return [
        {"id": 1, "types": ["Ride", "WeightTraining"], "max_hr": 189,
         "lthr": 171, "hr_zone_names": _ZONNAMN},
        {"id": 2, "types": ["Run", "VirtualRun", "TrailRun"], "max_hr": 189,
         "lthr": 171, "hr_zone_names": ["Lugnt", "Distans", "Tröskel", "Fart"]},
        {"id": 3, "types": ["Other"], "max_hr": 189, "lthr": 171,
         "hr_zone_names": ["Z1", "Z2", "Z3", "Z4"]},
    ]


def test_zone_names_are_looked_up_per_sport(store: Store) -> None:
    """Zonerna skiljer sig mellan sporter — namnen får inte delas.

    Skarpt hos atleten slutar löpningens zon 1 vid 144 och styrkans vid
    137, och Intervals namnger dem per sportgrupp. En uppslagning som tar
    första bästa raden sätter alltså fel etikett på halva passen.
    """
    store.replace_sport_settings(_sportrader())

    assert store.hr_zone_names("WeightTraining") == _ZONNAMN
    assert store.hr_zone_names("Run")[1] == "Distans"
    assert store.hr_zone_names("TrailRun")[1] == "Distans"


def test_an_unknown_sport_falls_back_to_other(store: Store) -> None:
    """Intervals egen reservrad heter "Other" — vi använder samma."""
    store.replace_sport_settings(_sportrader())

    assert store.hr_zone_names("Kayaking") == ["Z1", "Z2", "Z3", "Z4"]


def test_zone_names_are_none_before_the_first_sync(store: Store) -> None:
    """None, inte tom lista: "vi vet inte" och "sporten saknar zoner" är
    skilda svar för den som formaterar payloaden."""
    assert store.hr_zone_names("Run") is None
    assert store.hr_zone_names(None) is None


def test_a_removed_sport_disappears_on_the_next_sync(store: Store) -> None:
    """Hela tabellen ersätts. En upsert hade lämnat kvar en borttagen sport
    som fortsatte namnge zoner Intervals inte längre har."""
    store.replace_sport_settings(_sportrader())
    store.replace_sport_settings([_sportrader()[1]])

    assert store.hr_zone_names("WeightTraining") is None
    assert store.hr_zone_names("Run")[0] == "Lugnt"


def test_an_empty_answer_leaves_the_zone_names_alone(store: Store) -> None:
    """Ett tomt svar från Intervals får inte tömma tabellen.

    Zonnamnen ändras kanske en gång om året; ett trasigt svar kommer
    oftare än så. Att behålla gamla namn är alltid bättre än inga alls.
    """
    store.replace_sport_settings(_sportrader())

    assert store.replace_sport_settings([]) == 0
    assert store.hr_zone_names("Run")[0] == "Lugnt"


def test_a_broken_zone_endpoint_does_not_stop_the_sync(store: Store) -> None:
    """Zonnamn är utsmyckning; pass och wellness är underlaget.

    sync_activities och sync_wellness låter sina fel bubbla upp, och den
    timvisa synken kör alla tre i rad. Släppte sync_sport_settings igenom
    ett 404 eller 500 hade en trasig hjälpdata stoppat huvuddatan.
    """
    from sync.intervals_client import sync_sport_settings

    class _Trasig:
        def list_sport_settings(self) -> list[dict[str, object]]:
            raise RuntimeError("500 från Intervals")

    store.replace_sport_settings(
        [{"id": 1, "types": ["Run"], "hr_zone_names": ["Lugnt"]}]
    )

    assert sync_sport_settings(_Trasig(), store) == 0  # type: ignore[arg-type]
    # Och de gamla namnen ligger kvar.
    assert store.hr_zone_names("Run") == ["Lugnt"]


def test_sync_stores_the_zone_names_intervals_returns(store: Store) -> None:
    from sync.intervals_client import sync_sport_settings

    class _Client:
        def list_sport_settings(self) -> list[dict[str, object]]:
            return [{"id": 7, "types": ["Run"], "max_hr": 189, "lthr": 171,
                     "hr_zone_names": ["Lugnt", "Distans"]}]

    assert sync_sport_settings(_Client(), store) == 1  # type: ignore[arg-type]
    assert store.hr_zone_names("Run") == ["Lugnt", "Distans"]
    assert store.list_sport_settings()[0]["lthr"] == 171


# --- Gallring av ersatta analysversioner ---------------------------------


def _skriv_analys(store: Store, typ: str, ref: str | None, dagar_sedan: int,
                  text: str) -> None:
    """Lägger in en analys med en bestämd ålder.

    save_analysis stämplar med datetime.now(), och gallringen frågar just
    om åldern — så raderna måste skrivas direkt.
    """
    import sqlite3
    from datetime import datetime, timedelta

    nar = (datetime.now() - timedelta(days=dagar_sedan)).isoformat()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO analyses (analysis_type, ref_id, created_at, model, "
            "markdown) VALUES (?, ?, ?, 'fake', ?)",
            (typ, ref, nar, text),
        )


def _analyser(store: Store) -> list[tuple]:
    import sqlite3

    with sqlite3.connect(store.db_path) as conn:
        return conn.execute(
            "SELECT analysis_type, ref_id, markdown FROM analyses "
            "ORDER BY created_at"
        ).fetchall()


def test_pruning_keeps_the_newest_version_of_every_day(store: Store) -> None:
    """Det som gallras är omskrivningar, inte dygn.

    Skarpt låg 75 av 133 rader på en (typ, ref_id) som redan hade en nyare
    version — som mest åtta versioner av samma morgonrekommendation. Den
    nyaste måste överleva oavsett ålder: den går inte att återskapa utan
    att betala för analysen igen, och det är den dashboarden visar.
    """
    for i, dagar in enumerate((100, 99, 98)):
        _skriv_analys(store, "morning_recommendation", "2026-01-01", dagar, f"v{i}")

    assert store.prune_analyses(keep_days=30) == 2
    assert _analyser(store) == [("morning_recommendation", "2026-01-01", "v2")]


def test_pruning_leaves_a_day_that_was_only_written_once(store: Store) -> None:
    """En gammal analys utan efterföljare är inte överflödig — den är den
    enda som finns."""
    _skriv_analys(store, "daily_summary", "2025-01-01", 500, "ensam")

    assert store.prune_analyses(keep_days=30) == 0
    assert len(_analyser(store)) == 1


def test_a_recent_rewrite_is_kept_for_comparison(store: Store) -> None:
    """Fönstret finns för att en analys man just genererat om ska gå att
    jämföra med den den ersatte. Först när den åldrats bort gallras den."""
    _skriv_analys(store, "coaching", "2026-06-01", 3, "gammal")
    _skriv_analys(store, "coaching", "2026-06-01", 2, "ny")

    assert store.prune_analyses(keep_days=30) == 0
    assert store.prune_analyses(keep_days=1) == 1
    assert _analyser(store) == [("coaching", "2026-06-01", "ny")]


def test_pruning_can_be_switched_off(store: Store) -> None:
    """ANALYSIS_KEEP_DAYS=0 ska inte betyda "gallra allt" — det ska betyda
    "gallra inget". Noll dagars fönster hade annars raderat varenda ersatt
    version direkt, vilket är motsatsen till vad inställningen läses som."""
    _skriv_analys(store, "coaching", "2026-06-01", 400, "gammal")
    _skriv_analys(store, "coaching", "2026-06-01", 399, "ny")

    assert store.prune_analyses(keep_days=0) == 0
    assert store.prune_analyses(keep_days=-1) == 0
    assert len(_analyser(store)) == 2


def test_analyses_without_a_ref_id_form_their_own_group(store: Store) -> None:
    """Coaching sparade tidigare ref_id=NULL, och skarpt finns nio sådana
    rader. De hör ihop som en grupp — nyaste sparas, resten gallras — och
    får inte blandas ihop med de daterade."""
    for i, dagar in enumerate((100, 99, 98)):
        _skriv_analys(store, "coaching", None, dagar, f"utan-ref-{i}")
    _skriv_analys(store, "coaching", "2026-06-01", 97, "med-ref")

    assert store.prune_analyses(keep_days=30) == 2
    kvar = sorted(rad[2] for rad in _analyser(store))
    assert kvar == ["med-ref", "utan-ref-2"]


def test_different_types_of_the_same_day_do_not_prune_each_other(
    store: Store,
) -> None:
    """Morgon-, kvälls- och coachinganalys för samma dygn är olika saker,
    inte tre versioner av en."""
    for typ in ("morning_recommendation", "daily_summary", "coaching"):
        _skriv_analys(store, typ, "2026-01-01", 100, typ)

    assert store.prune_analyses(keep_days=30) == 0
    assert len(_analyser(store)) == 3


# --- Setnumrering över flera anrop -----------------------------------


def _set_nummer(store: Store, day: str, exercise: str) -> list[int]:
    import sqlite3

    with sqlite3.connect(store.db_path) as conn:
        return [
            r[0] for r in conn.execute(
                "SELECT set_number FROM strength_sets WHERE day=? AND exercise=? "
                "ORDER BY created_at, id",
                (day, exercise),
            ).fetchall()
        ]


def test_the_same_exercise_written_twice_keeps_counting_up(tmp_path) -> None:
    """Skarpt fall 2026-04-21: aktivitetssidan visade set 1, 2, 1, 2 för
    marklyft.

    Klockan hade loggat två poster som en rättelse i .env döpte om till
    samma övning, FIT-importen anropade add_strength_sets en gång per
    post, och båda numrerade från 1. Numret ska svara på "vilket set i
    ordningen" — det svaret finns inte i ett enskilt anrop.
    """
    store = Store(tmp_path / "nummer.db")
    store.init()
    store.add_strength_sets(
        "2026-04-21", "marklyft",
        [{"reps": 5, "weight_kg": 100.0}, {"reps": 5, "weight_kg": 100.0}],
        activity_id="a1",
    )
    store.add_strength_sets(
        "2026-04-21", "marklyft",
        [{"reps": 5, "weight_kg": 110.0}, {"reps": 3, "weight_kg": 120.0}],
        activity_id="a1",
    )

    assert _set_nummer(store, "2026-04-21", "marklyft") == [1, 2, 3, 4]


def test_reimporting_a_pass_lands_on_the_same_numbers(tmp_path) -> None:
    """Numren räknas per PASS, inte per dag, så en omsynk inte flyttar dem.

    FIT-importen raderar bara sina egna rader för passet och skriver nya
    (se delete_strength_sets_for_activity). Räknades numren per dag hade
    ett annat pass samma dag puttat dem uppåt vid varje timvis synk.
    """
    store = Store(tmp_path / "omimport.db")
    store.init()
    store.add_strength_sets(
        "2026-04-21", "knäböj", [{"reps": 5, "weight_kg": 100.0}],
        activity_id="fm", source="fit",
    )
    store.add_strength_sets(
        "2026-04-21", "knäböj", [{"reps": 5, "weight_kg": 105.0}],
        activity_id="em", source="fit",
    )

    for _ in range(3):
        store.delete_strength_sets_for_activity("fm", "fit")
        store.add_strength_sets(
            "2026-04-21", "knäböj", [{"reps": 5, "weight_kg": 100.0}],
            activity_id="fm", source="fit",
        )

    rader = store.get_strength_sets_for_activity("fm")
    assert [r["set_number"] for r in rader] == [1]
    assert [r["set_number"] for r in store.get_strength_sets_for_activity("em")] == [1]


def test_sets_logged_in_chat_without_a_pass_still_count_up(tmp_path) -> None:
    """activity_id är NULL för set loggade utan pass att koppla till, och
    NULL = NULL är aldrig sant i SQL — uppslaget måste använda IS."""
    store = Store(tmp_path / "utan-pass.db")
    store.init()
    store.add_strength_sets("2026-04-21", "chins", [{"reps": 8}])
    store.add_strength_sets("2026-04-21", "chins", [{"reps": 6}])

    assert _set_nummer(store, "2026-04-21", "chins") == [1, 2]


def test_the_migration_renumbers_sets_that_share_a_number(tmp_path) -> None:
    """De felaktiga raderna ligger redan i databasen. Att vänta på att
    passen synkas om är inget svar — ett pass från april hämtas aldrig mer."""
    import sqlite3

    store = Store(tmp_path / "migrering.db")
    store.init()
    with sqlite3.connect(store.db_path) as conn:
        conn.executemany(
            "INSERT INTO strength_sets (day, activity_id, exercise, set_number, "
            "reps, weight_kg, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("2026-04-21", "a1", "marklyft", 1, 5, 100.0, "fit", "2026-04-21T10:00:00"),
                ("2026-04-21", "a1", "marklyft", 2, 5, 100.0, "fit", "2026-04-21T10:00:00"),
                ("2026-04-21", "a1", "marklyft", 1, 5, 110.0, "fit", "2026-04-21T10:00:01"),
                ("2026-04-21", "a1", "marklyft", 2, 3, 120.0, "fit", "2026-04-21T10:00:01"),
            ],
        )

    store.init()

    assert _set_nummer(store, "2026-04-21", "marklyft") == [1, 2, 3, 4]
    assert [r["weight_kg"] for r in store.get_strength_sets_for_activity("a1")] == [
        100.0, 100.0, 110.0, 120.0
    ], "ordningen ska bevaras — numret byts, inte raderna"


def test_the_migration_leaves_correct_numbering_alone(tmp_path) -> None:
    """Idempotent: en tabell utan dubbletter ska inte röras."""
    store = Store(tmp_path / "oror.db")
    store.init()
    store.add_strength_sets(
        "2026-04-21", "bänkpress",
        [{"reps": 8, "weight_kg": 80.0}, {"reps": 8, "weight_kg": 80.0}],
        activity_id="a1",
    )
    store.init()

    assert _set_nummer(store, "2026-04-21", "bänkpress") == [1, 2]


def test_the_migration_moves_the_old_per_day_job_markers(tmp_path) -> None:
    """Kvällskontrollen lade in två rader per dygn som ingen läste igen
    efter morgonen därpå — 730 rader om året i en tabell som ska rymma en
    handfull. Se REGELN FÖR NYCKLAR i SCHEMA.

    Den nyaste av varje sort ska FLYTTAS, inte slängas: kontrollen kör
    09:00 och läser gårdagens markör, så en uppgradering klockan åtta
    hade annars raderat underlaget den skulle jämföra mot.
    """
    import json
    import sqlite3

    store = Store(tmp_path / "markorer.db")
    store.init()
    store.set_job_state("evening_summary_steps:2026-09-06", '{"steps": 12000}')
    store.set_job_state("evening_summary_steps:2026-09-07", '{"steps": 19674}')
    store.set_job_state("evening_summary_recheck_done:2026-09-06", "2026-09-07T09:00:12")
    store.set_job_state("morning_recommendation_data", "{}")

    store.init()

    with sqlite3.connect(store.db_path) as conn:
        kvar = {r[0] for r in conn.execute("SELECT key FROM job_state").fetchall()}
    assert kvar == {
        "morning_recommendation_data",
        "evening_summary_underlag",
        "evening_summary_recheck_done",
    }
    assert json.loads(store.get_job_state("evening_summary_underlag")) == {
        "day": "2026-09-07", "steps": 19674,
    }
    # Done-markören höll en naken tidsstämpel, inte JSON — allt som läses
    # ur den är vilket dygn den gäller.
    assert json.loads(store.get_job_state("evening_summary_recheck_done")) == {
        "day": "2026-09-06",
    }


def test_the_migration_does_not_overwrite_a_marker_already_moved(tmp_path) -> None:
    """Körs vid varje start. En markör som hunnit skrivas av den nya koden
    får inte ersättas av en gammal rad som blivit kvar."""
    import json
    import sqlite3

    store = Store(tmp_path / "flyttad.db")
    store.init()
    store.set_job_state(
        "evening_summary_underlag", '{"day": "2026-09-08", "steps": 22125}'
    )
    store.set_job_state("evening_summary_steps:2026-09-07", '{"steps": 19674}')

    store.init()

    assert json.loads(store.get_job_state("evening_summary_underlag")) == {
        "day": "2026-09-08", "steps": 22125,
    }
    with sqlite3.connect(store.db_path) as conn:
        kvar = {r[0] for r in conn.execute("SELECT key FROM job_state").fetchall()}
    assert kvar == {"evening_summary_underlag"}, "den gamla raden ska ändå bort"


def test_the_write_lock_is_taken_before_the_block_reads_anything(tmp_path) -> None:
    """immediate=True finns för läs-modifiera-skriv, och då måste
    transaktionen vara igång redan vid första SELECT.

    sqlite3 kör i sitt äldre läge (isolation_level="") där en transaktion
    börjar först vid INSERT/UPDATE/DELETE — en ensam SELECT går i
    autocommit. Utan BEGIN IMMEDIATE är alltså uppslaget oskyddat.
    """
    store = Store(tmp_path / "lås.db")
    store.init()

    with store._conn() as conn:
        conn.execute("SELECT COUNT(*) FROM strength_sets").fetchone()
        assert conn.in_transaction is False, "utgångsläget som motiverar flaggan"

    with store._conn(immediate=True) as conn:
        assert conn.in_transaction is True, (
            "skrivlåset ska vara taget innan blocket läser något"
        )
        conn.execute("SELECT COUNT(*) FROM strength_sets").fetchone()
        assert conn.in_transaction is True


def test_two_threads_logging_the_same_exercise_do_not_share_a_number(tmp_path) -> None:
    """Appen har två skrivare som kan hamna här samtidigt: chattens
    _log_strength_session (webbtråd) och FIT-importen i sync_activities
    (schemaläggartråd). Läser båda samma maxvärde skriver båda från N+1 —
    exakt de dubbletter _migrate finns för att städa."""
    import threading

    store = Store(tmp_path / "samtidigt.db")
    store.init()

    start = threading.Barrier(8)
    fel: list[BaseException] = []

    def logga() -> None:
        try:
            start.wait(timeout=10)
            store.add_strength_sets(
                "2026-04-21", "marklyft", [{"reps": 5, "weight_kg": 100.0}],
                activity_id="a1",
            )
        except BaseException as exc:  # noqa: BLE001 — tråden får inte tyst dö
            fel.append(exc)

    tradar = [threading.Thread(target=logga) for _ in range(8)]
    for t in tradar:
        t.start()
    for t in tradar:
        t.join(timeout=30)

    assert not fel, f"trådar föll: {fel}"
    assert sorted(_set_nummer(store, "2026-04-21", "marklyft")) == [1, 2, 3, 4, 5, 6, 7, 8]


def test_a_crashed_backup_leaves_no_empty_file_under_the_final_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regressionstest: backupen skrev rakt på slutnamnet.

    Backup-API:et rullar visserligen tillbaka en BEFINTLIG målfil om
    kopieringen avbryts. Men namnet bär dygnets datum, så varje natt
    skriver till ett namn som inte fanns förut — och då finns ingen
    tidigare version att rulla tillbaka till. Kvar blev en tom fil under
    slutnamnet, som rotationen räknade som en giltig backup och som
    därmed knuffade ut en kopia som faktiskt gick att läsa.
    """
    store = Store(tmp_path / "training.db")
    store.init()
    store.add_self_report(day="2026-09-19", category="weight", value=82.0)
    backup_dir = tmp_path / "backups"
    dest = backup_dir / "training-2026-09-20.db"

    class _AvbrytMitt(sqlite3.Connection):
        """Avbryter kopieringen efter första sidan, som ett fullt SD-kort."""

        def backup(self, target: object, **k: object) -> None:  # type: ignore[override]
            def _stopp(status: int, kvar: int, total: int) -> None:
                raise sqlite3.OperationalError("disk I/O error")

            super().backup(target, pages=1, progress=_stopp)  # type: ignore[arg-type]

    @contextmanager
    def _avbruten(self: Store, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, factory=_AvbrytMitt)
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(Store, "_conn", _avbruten)
    with pytest.raises(sqlite3.OperationalError):
        store.backup(dest)
    monkeypatch.undo()

    assert not dest.exists(), (
        "en avbruten kopiering får inte lämna något under slutnamnet — "
        "rotationen räknar training-*.db som giltiga backuper"
    )
    assert list(backup_dir.iterdir()) == [], "inte heller en kvarglömd .part"


def test_a_second_backup_the_same_day_replaces_the_first(tmp_path: Path) -> None:
    """Två körningar samma dygn ska ge EN fil — inbytet får inte lämna
    både den gamla och en temporärfil kvar."""
    store = Store(tmp_path / "training.db")
    store.init()
    store.add_self_report(day="2026-09-20", category="weight", value=82.0)
    dest = tmp_path / "backups" / "training-2026-09-20.db"

    store.backup(dest)
    store.add_self_report(day="2026-09-20", category="weight", value=83.0)
    store.backup(dest)

    assert [p.name for p in sorted(dest.parent.iterdir())] == [dest.name]
    # Den andra kopian ska bära den nyare raden, alltså faktiskt ha bytts in.
    assert len(Store(dest).get_self_reports_for_date("2026-09-20")) == 2


def test_old_hack_squat_rows_are_renamed_to_the_current_name(tmp_path: Path) -> None:
    """Rader som skrevs innan översättningen ändrades ska följa med.

    Klockan döper atletens knäböj till barbell_hack_squat, och det blev
    länge "hack squat" i tabellen. Utan migreringen ligger de raderna kvar
    under det namnet medan nya skrivs som "knäböj", och progressionen delas
    i två serier som var för sig ser ut att stanna av. Passen går inte att
    hämta igen — ett pass från maj finns inte kvar hos Intervals.
    """
    store = Store(tmp_path / "t.db")
    store.init()
    store.add_strength_sets("2026-05-05", "knäböj", [{"reps": 5, "weight_kg": 100.0}])
    # Förbi add_strength_sets: det gamla namnet går inte att skriva längre.
    with sqlite3.connect(tmp_path / "t.db") as conn:
        conn.execute("UPDATE strength_sets SET exercise='hack squat'")

    Store(tmp_path / "t.db").init()

    assert [r["exercise"] for r in store.get_strength_sets_for_date("2026-05-05")] == [
        "knäböj"
    ]
