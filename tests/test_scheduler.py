"""Tester för scheduler.py.

Regressionstest 1: morgonrekommendationens CronTrigger (hour="7-11", minute=0)
triggar bara exakt vid hela timmar. Om tjänsten startas om strax efter en
sådan tidpunkt (vanligt under utveckling — vi har startat om tjänsten
väldigt många gånger) missas den helt tills nästa hela timme. En separat
engångs-catchup-körning vid uppstart ska täcka det fallet.

Regressionstest 2: kvällssammanfattningen (CronTrigger 23:59) uteblev en
hel natt. Loggen visade "Run time of job ... was missed by 0:03:45" —
APScheduler skippar tyst ett jobb om dess interna klocktråd blir mer
försenad än misfire_grace_time (standard: 1 sekund, mycket snålt). Fixen
har två delar: ett generöst misfire_grace_time på cron-jobbet, och samma
uppstarts-catchup-mönster som morgonrekommendationen redan hade.
"""
from __future__ import annotations

import contextlib
import importlib
import json
import sys
from datetime import datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Klockslaget alla morgontester låtsas att det är. Ligger mitt i
# standardfönstret 07:00-11:59.
MORGONTID = dt_time(9, 0)


def _frys_morgonklockan(
    monkeypatch: pytest.MonkeyPatch, scheduler_module, tid: dt_time = MORGONTID
) -> None:
    """Fryser schedulerns KLOCKSLAG men behåller dagens datum.

    Sedan run_morning_recommendation fick sin tidsspärr är den beroende
    av vad klockan är. Utan det här hade varje morgontest varit grönt kl
    09 och rött kl 23 — ett testresultat som beror på när man råkar köra
    pytest är värre än inget test alls.

    Datumet måste följa med den riktiga klockan: testerna skriver
    wellness-rader för `datetime.now().date()`, och en helt fryst
    tidsstämpel hade fått jobbet att leta efter en dag som inte finns.
    """

    class _FrystKlockslag(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206
            return datetime.combine(datetime.now().date(), tid)

    monkeypatch.setattr(scheduler_module, "datetime", _FrystKlockslag)


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


def test_morning_recommendation_runs_at_startup_not_only_on_cron(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regressionstest: tidigare kördes morgonanalysen bara av
    CronTrigger(hour='7-11', minute=0) — exakt vid hela timmar. En
    omstart strax efter en sådan tidpunkt (vanligt under utveckling)
    missade hela fönstret till nästa timme. Verifierar att
    run_morning_recommendation faktiskt anropas direkt vid uppstart,
    inte bara via cron-schemat."""
    # Sviten stänger av catchupen för alla tester (se _no_startup_catchup i
    # conftest). Det här testet handlar om den, så här sätts den på igen.
    monkeypatch.setenv("STARTUP_CATCHUP", "1")
    import scheduler as scheduler_module

    calls: list[object] = []
    monkeypatch.setattr(
        scheduler_module, "run_morning_recommendation", lambda settings: calls.append(settings)
    )
    # Catchupen kör alla fyra jobben. De andra tre stubbas ut också, annars
    # gör de riktiga anrop som bara conftests spärrar hindrar — och de
    # kastar inne i tråden, där felet sväljs.
    monkeypatch.setattr(scheduler_module, "run_coaching", lambda settings: None)
    monkeypatch.setattr(scheduler_module, "run_evening_summary", lambda settings: None)
    monkeypatch.setattr(
        scheduler_module, "run_evening_summary_recheck", lambda settings: None
    )

    app = _fresh_app(monkeypatch, tmp_path)
    with TestClient(app):
        pass

    assert len(calls) >= 1, (
        "run_morning_recommendation ska anropas minst en gång vid uppstart "
        "(catchup-jobbet), inte bara vänta på nästa hela cron-timme"
    )


def test_morning_recommendation_cron_job_is_also_registered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Det återkommande cron-jobbet (07-11, varje heltimme) ska fortfarande
    finnas kvar — catchup-jobbet ovan är ett komplement, inte en ersättning."""
    # Utan den här raden läcker maskinens riktiga .env in här (config.py
    # kör load_dotenv vid import, in i os.environ, innan testet ens
    # startar) — på pi5 står MORNING_RECOMMENDATION_TIME till 08:00, vilket
    # fick asserten nedan att slå fel trots att koden var korrekt. Sätts
    # explicit precis som test_morning_window_follows_the_configured_time
    # redan gör för sina egna tider.
    monkeypatch.setenv("MORNING_RECOMMENDATION_TIME", "07:00")
    app = _fresh_app(monkeypatch, tmp_path)

    with TestClient(app):
        scheduler = app.state.scheduler
        job = scheduler.get_job("morning_recommendation")
        assert job is not None
        assert job.trigger.fields[5].expressions[0].first == 7  # hour-fältet: 7-11


def test_morning_recommendation_catchup_respects_no_new_data_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catchup-jobbet är samma funktion (run_morning_recommendation) som
    cron-jobbet — det ska alltså fortfarande hoppa över generering om det
    inte finns någon wellness-data för idag, inte blint generera vid varje
    omstart oavsett tid på dygnet.

    Synken stubbas ut: den ligger numera före spärren (se
    run_morning_recommendation), så den körs alltid — men här hämtar den
    ingenting, och databasen är fortfarande tom när beslutet fattas."""
    from scheduler import run_morning_recommendation

    calls: list[str] = []

    class _FakeStore:
        def init(self) -> None:
            pass

        def get_wellness_day(self, day: str):
            calls.append(f"get_wellness_day({day})")
            return None  # Ingen data för idag.

    import scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): _FakeStore())
    monkeypatch.setattr(
        scheduler_module,
        "IntervalsClient",
        lambda settings: contextlib.nullcontext(_NoopIntervalsClient()),
    )
    monkeypatch.setattr(scheduler_module, "sync_activities", lambda client, store: 0)
    monkeypatch.setattr(
        scheduler_module, "sync_wellness", lambda client, store, full=False: 0
    )

    _frys_morgonklockan(monkeypatch, scheduler_module)

    class _FakeSettings:

        strength_corrections = ()
        db_path = "unused.db"
        morning_recommendation_time = "07:00"

    run_morning_recommendation(_FakeSettings())  # type: ignore[arg-type]

    assert any("get_wellness_day" in c for c in calls)
    # Inget AnalysisPipeline-anrop ska ha skett eftersom vi returnerade
    # tidigt — om koden kraschat på grund av det hade testet fallerat med
    # ett traceback istället för att komma hit.


def test_run_morning_recommendation_does_not_swallow_empty_claude_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regressionstest för det rapporterade felet: Claude gav ett tomt svar
    (stop_reason='max_tokens', 0 tecken text) kl 09:00. run_morning_
    recommendation loggade det ändå som 'genererad baserat på ny
    sömndata' och sparade en tom analys, vilket blockerade 'redan
    genererat idag'-spärren från att låta 10:00- eller 11:00-försöket
    (eller en omstart) faktiskt försöka igen.

    run_morning_recommendation() fångar (avsiktligt) alla Exceptions och
    bara loggar dem — den kraschar aldrig hela schemaläggaren. Det vi
    verifierar här är att ClaudeClient.analyze() nu raise:ar på ett tomt
    svar (se test_claude_client.py), vilket gör att
    store.save_analysis() aldrig hinner köras — dvs INGEN analys-rad
    sparas, så nästa schemalagda försök inte blockeras av en fantom-
    'lyckad' tom analys."""
    import scheduler as scheduler_module
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    today = datetime.now().date().isoformat()
    store.upsert_wellness_many([{
        "day": today, "rest_day": 0, "ctl": 40.0, "atl": 30.0, "tsb": 5.0,
        "ramp_rate": None, "sleep_seconds": 25000, "sleep_score": 80,
        "sleep_quality": 2, "avg_sleeping_hr": 50, "weight": 90.0,
        "resting_hr": 55, "hrv": 60.0, "hrv_sdnn": None, "stress": None,
        "respiration": None, "spO2": None, "systolic": None, "diastolic": None,
        "hydration": None, "soreness": None, "fatigue": None, "mood": None,
        "motivation": None, "injury": None, "readiness": None, "vo2max": None,
        "steps": 5000, "kcal_consumed": None, "raw_json": "{}",
        "last_synced": datetime.now().isoformat(),
    }])

    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)
    monkeypatch.setattr(
        scheduler_module,
        "IntervalsClient",
        lambda settings: contextlib.nullcontext(_NoopIntervalsClient()),
    )
    monkeypatch.setattr(scheduler_module, "sync_activities", lambda client, store: None)
    monkeypatch.setattr(scheduler_module, "sync_wellness", lambda client, store: None)

    anrop: list[str] = []

    class _EmptyClaudeClient:
        model = "fake-model"

        def analyze(self, *a, **kw):
            anrop.append("analyze")
            raise RuntimeError("Claude gav ett tomt svar (stop_reason=max_tokens, ...).")

    monkeypatch.setattr(scheduler_module, "ClaudeClient", lambda settings: _EmptyClaudeClient())
    _frys_morgonklockan(monkeypatch, scheduler_module)

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        morning_recommendation_time = "07:00"
        athlete = None

    # Ska INTE kasta (run_morning_recommendation fångar internt), men ska
    # heller INTE ha sparat någon analys-rad.
    scheduler_module.run_morning_recommendation(_FakeSettings())  # type: ignore[arg-type]

    # Utan den här raden var testet grönt av fel skäl: inställningarna
    # saknade `athlete`, jobbet föll på AttributeError innan Claude ens
    # anropades, och "ingen rad sparad" stämde ändå.
    assert anrop == ["analyze"], "Claude anropades aldrig — testet mäter fel fel"
    saved = store.latest_analysis("morning_recommendation")
    assert saved is None, (
        "Ingen analys-rad ska sparas när Claude gav ett tomt svar — annars "
        "blockeras nästa schemalagda försök av en fantom-'lyckad' tom analys"
    )


def test_a_failed_sync_does_not_cancel_the_morning_analysis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Synkfelet fångades av funktionens yttre except, och var Intervals
    nere hela morgonfönstret blev det ingen morgonanalys alls — trots att
    den timvisa synken redan hämtat nattens data."""
    import scheduler as scheduler_module
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    today = datetime.now().date().isoformat()
    store.upsert_wellness_many([{
        "day": today, "rest_day": 0, "ctl": 40.0, "atl": 30.0, "tsb": 5.0,
        "ramp_rate": None, "sleep_seconds": 25000, "sleep_score": 80,
        "sleep_quality": 2, "avg_sleeping_hr": 50, "weight": 90.0,
        "resting_hr": 55, "hrv": 60.0, "hrv_sdnn": None, "stress": None,
        "respiration": None, "spO2": None, "systolic": None, "diastolic": None,
        "hydration": None, "soreness": None, "fatigue": None, "mood": None,
        "motivation": None, "injury": None, "readiness": None, "vo2max": None,
        "steps": 5000, "kcal_consumed": None, "raw_json": "{}",
        "last_synced": datetime.now().isoformat(),
    }])
    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)

    def _intervals_nere(settings):  # noqa: ANN001, ANN202
        raise ConnectionError("intervals.icu svarar inte")

    monkeypatch.setattr(scheduler_module, "IntervalsClient", _intervals_nere)

    class _Claude:
        model = "fake-model"

        def analyze(self, *a, **kw):  # noqa: ANN002, ANN003, ANN202
            return "# Morgon\nKör lugnt."

    monkeypatch.setattr(scheduler_module, "ClaudeClient", lambda settings: _Claude())
    _frys_morgonklockan(monkeypatch, scheduler_module)

    class _FakeSettings:
        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        morning_recommendation_time = "07:00"
        athlete = None

    scheduler_module.run_morning_recommendation(_FakeSettings())  # type: ignore[arg-type]

    saved = store.latest_analysis("morning_recommendation")
    assert saved is not None and saved["ref_id"] == today, (
        "morgonanalysen uteblev för att synken före den misslyckades"
    )


class _NoopIntervalsClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# Ett komplett, tomt wellness-underlag att bygga vidare på med {**_TOM_WELLNESS,
# "day": ..., "steps": ...} — SQL:en binder varje kolumn vid namn, så
# raden måste ha dem alla.
_TOM_WELLNESS = {
    "day": "2000-01-01", "rest_day": 0, "ctl": 40.0, "atl": 30.0, "tsb": 5.0,
    "ramp_rate": None, "sleep_seconds": 25000, "sleep_score": 80,
    "sleep_quality": 2, "avg_sleeping_hr": 50, "weight": 90.0,
    "resting_hr": 55, "hrv": 60.0, "hrv_sdnn": None, "stress": None,
    "respiration": None, "spO2": None, "systolic": None, "diastolic": None,
    "hydration": None, "soreness": None, "fatigue": None, "mood": None,
    "motivation": None, "injury": None, "readiness": None, "vo2max": None,
    "steps": 5000, "kcal_consumed": None, "raw_json": "{}",
    "last_synced": "2000-01-01T00:00:00",
}


# Kvällskontrollens markörer ligger i job_state under EN nyckel var, med
# dygnet i värdet (se _EVENING_UNDERLAG_KEY i scheduler.py). Hjälparna
# nedan finns för att inget test ska stava nyckelnamnet fel och tyst
# skriva en markör ingen läser.
def _notera_underlag(store, day: str, **falt) -> None:
    """Skriver markören som run_evening_summary hade skrivit för `day`.

    Bara de fält som anges hamnar i markören — precis som en markör
    skriven av en äldre version, som bara kände till "steps".
    """
    store.set_job_state(
        "evening_summary_underlag", json.dumps({"day": day, **falt})
    )


def _noterat_underlag(store) -> dict | None:
    raw = store.get_job_state("evening_summary_underlag")
    return json.loads(raw) if raw else None


def _markorens_dag(store, key: str) -> str | None:
    raw = store.get_job_state(key)
    return json.loads(raw)["day"] if raw else None


# --- Delat synk-lås ---------------------------------------------------


def test_run_sync_holds_the_shared_sync_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regressionstest: låset som ska hindra två samtidiga synkar låg i
    web/app.py och togs därför BARA av /sync-routen. Schemaläggarens egen
    run_sync gick rakt förbi det, så den krock kommentaren beskrev (manuell
    synk mot schemalagd, båda skrivande mot samma SQLite-databas) kunde
    inträffa ändå. Nu bor låset i sync/intervals_client.py och ska tas av
    alla som synkar."""
    import scheduler as scheduler_module
    from sync import intervals_client

    seen: dict[str, bool] = {}

    def _spy_sync_activities(client, store):  # noqa: ANN001, ANN202
        seen["locked_during_sync"] = intervals_client._sync_lock.locked()
        return 0

    monkeypatch.setattr(scheduler_module, "sync_activities", _spy_sync_activities)
    monkeypatch.setattr(
        scheduler_module, "sync_wellness", lambda client, store, full=False: 0
    )
    monkeypatch.setattr(
        scheduler_module,
        "IntervalsClient",
        lambda settings: contextlib.nullcontext(_NoopIntervalsClient()),
    )

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")

    assert scheduler_module.run_sync(_FakeSettings()) is True  # type: ignore[arg-type]

    assert seen.get("locked_during_sync") is True, (
        "run_sync ska hålla det delade synk-låset medan den skriver — annars "
        "skyddar /sync-routens lås ingenting"
    )
    assert not intervals_client._sync_lock.locked(), "låset ska släppas efteråt"


def test_morning_recommendation_sync_also_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """run_morning_recommendation gör en egen Intervals-synk innan den
    analyserar. Även den ska ta det delade låset."""
    import scheduler as scheduler_module
    from sync import intervals_client
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    today = datetime.now().date().isoformat()
    store.upsert_wellness_many([{
        "day": today, "rest_day": 0, "ctl": 40.0, "atl": 30.0, "tsb": 5.0,
        "ramp_rate": None, "sleep_seconds": 25000, "sleep_score": 80,
        "sleep_quality": 2, "avg_sleeping_hr": 50, "weight": 90.0,
        "resting_hr": 55, "hrv": 60.0, "hrv_sdnn": None, "stress": None,
        "respiration": None, "spO2": None, "systolic": None, "diastolic": None,
        "hydration": None, "soreness": None, "fatigue": None, "mood": None,
        "motivation": None, "injury": None, "readiness": None, "vo2max": None,
        "steps": 5000, "kcal_consumed": None, "raw_json": "{}",
        "last_synced": datetime.now().isoformat(),
    }])

    seen: dict[str, bool] = {}

    def _spy_sync_activities(client, store):  # noqa: ANN001, ANN202
        seen["locked_during_sync"] = intervals_client._sync_lock.locked()
        return 0

    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)
    monkeypatch.setattr(scheduler_module, "sync_activities", _spy_sync_activities)
    monkeypatch.setattr(
        scheduler_module, "sync_wellness", lambda client, store, full=False: 0
    )
    monkeypatch.setattr(
        scheduler_module,
        "IntervalsClient",
        lambda settings: contextlib.nullcontext(_NoopIntervalsClient()),
    )

    class _StopAfterSync:
        model = "fake-model"

        def analyze(self, *a, **kw):  # noqa: ANN002, ANN003, ANN202
            raise RuntimeError("stoppar här — synken är redan gjord")

    monkeypatch.setattr(scheduler_module, "ClaudeClient", lambda settings: _StopAfterSync())
    _frys_morgonklockan(monkeypatch, scheduler_module)

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        athlete = None
        morning_recommendation_time = "07:00"

    scheduler_module.run_morning_recommendation(_FakeSettings())  # type: ignore[arg-type]

    assert seen.get("locked_during_sync") is True
    assert not intervals_client._sync_lock.locked(), (
        "låset ska släppas även när analysen därefter misslyckas"
    )


# --- Nedstängning ------------------------------------------------------


def test_no_self_removing_one_shot_jobs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regressionstest för en kapplöpning vid nedstängning.

    Uppstarts-catchuparna registrerades tidigare som DateTrigger-jobb. Ett
    sådant jobb har ingen nästa körtid och raderar därför sig självt när
    det körts — vilket kunde krocka med shutdown(): APScheduler tömmer
    jobstore:n medan dess klocktråd står i _process_jobs och är på väg att
    ta bort samma jobb, och tråden dog med ett ohanterat JobLookupError
    (syntes som PytestUnhandledThreadExceptionWarning i testkörningen).

    Cron- och intervall-triggers har alltid en nästa körtid och når aldrig
    den kodvägen, så inga engångsjobb får finnas kvar i schemaläggaren."""
    from apscheduler.triggers.date import DateTrigger

    app = _fresh_app(monkeypatch, tmp_path)

    with TestClient(app):
        jobs = app.state.scheduler.get_jobs()
        assert jobs, "schemaläggaren ska ha registrerat jobb"
        offenders = [j.id for j in jobs if isinstance(j.trigger, DateTrigger)]
        assert not offenders, (
            f"engångsjobb som raderar sig själva kan krocka med nedstängningen: {offenders}"
        )


def test_no_job_is_registered_in_paused_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regressionstest: sync-jobbet registrerades med next_run_time=None.

    Det ser ut som "ingen särskild starttid, använd triggern", men är
    precis hur APScheduler PAUSAR ett jobb — pause_job() är implementerat
    som modify_job(next_run_time=None), och _real_add_job beräknar bara en
    körtid när attributet saknas helt. Jobbet lades alltså till pausat och
    kördes aldrig: SYNC_INTERVAL_HOURS var död konfiguration och all
    synkning kom i själva verket från systemd-timern i en annan process.

    Ett pausat jobb är tyst — inget fel loggas — så bara ett test fångar
    det om det smyger tillbaka."""
    app = _fresh_app(monkeypatch, tmp_path)

    with TestClient(app):
        paused = [
            job.id for job in app.state.scheduler.get_jobs() if job.next_run_time is None
        ]

    assert not paused, (
        f"följande jobb är pausade och kommer aldrig att köras: {paused}"
    )


def test_sync_job_honours_the_configured_interval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SYNC_INTERVAL_HOURS ska faktiskt styra sync-jobbets intervall."""
    monkeypatch.setenv("SYNC_INTERVAL_HOURS", "3")
    app = _fresh_app(monkeypatch, tmp_path)

    with TestClient(app):
        job = app.state.scheduler.get_job("sync")
        assert job is not None
        assert job.next_run_time is not None
        assert job.trigger.interval.total_seconds() == 3 * 3600


def test_intervals_client_is_closed_on_shutdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """IntervalsClient skapas en gång i create_app och har en egen
    httpx-anslutningspool. Den stängdes tidigare aldrig — lifespan stängde
    bara schemaläggaren, så poolen låg kvar öppen tills processen dog."""
    app = _fresh_app(monkeypatch, tmp_path)

    assert not app.state.intervals._client.is_closed
    with TestClient(app):
        pass

    assert app.state.intervals._client.is_closed, (
        "httpx-klienten ska stängas när appen stängs ner"
    )


# --- Kvällssammanfattningen: misfire-marginal + startup-catchup -------


def test_cron_jobs_have_a_generous_misfire_grace_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regressionstest: kvällssammanfattningen missades en hel natt trots
    att tjänsten kördes helt normalt. Loggen visade "was missed by
    0:03:45" — APScheduler skippar tyst ett jobb om dess interna
    klocktråd blir mer försenad än misfire_grace_time, vars standardvärde
    bara är 1 sekund. Båda de dagliga analys-jobben ska ha en marginal
    som gott och väl täcker en vanlig, kortvarig försening."""
    app = _fresh_app(monkeypatch, tmp_path)

    with TestClient(app):
        scheduler = app.state.scheduler
        for job_id in ("morning_recommendation", "evening_summary"):
            job = scheduler.get_job(job_id)
            assert job is not None
            assert job.misfire_grace_time == 3600, (
                f"{job_id} har för snäv misfire-marginal — ett par minuters "
                "försening (fullt normalt) hade tyst hoppat över jobbet"
            )


def test_evening_summary_cron_job_is_still_registered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Det återkommande cron-jobbet (23:59) ska finnas kvar oförändrat —
    catchup-jobbet nedan är ett komplement, inte en ersättning."""
    app = _fresh_app(monkeypatch, tmp_path)

    with TestClient(app):
        scheduler = app.state.scheduler
        job = scheduler.get_job("evening_summary")
        assert job is not None
        assert job.trigger.fields[5].expressions[0].first == 23  # hour
        assert job.trigger.fields[6].expressions[0].first == 59  # minute


def test_the_startup_catchup_can_be_switched_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """STARTUP_CATCHUP=0 ska ge en uppstart utan catchup-tråd alls.

    Sviten sätter den för varje test (se _no_startup_catchup i conftest).
    Tråden gjorde annars riktiga anrop mot Intervals och Anthropic vid var
    och en av de 114 TestClient-uppstarterna, och hölls ofarlig bara av
    spärrar vars fel sväljs av except Exception inne i tråden — en trasig
    spärr hade synts som en räkning, inte som ett rött test.
    """
    import scheduler as scheduler_module

    calls: list[object] = []
    monkeypatch.setattr(
        scheduler_module,
        "run_morning_recommendation",
        lambda settings: calls.append(settings),
    )

    app = _fresh_app(monkeypatch, tmp_path)
    with TestClient(app):
        assert app.state.startup_catchup is None, "ingen tråd ska ha startats"

    assert calls == [], "catchupen ska inte ha kört någon analys"


def test_the_scheduler_can_be_switched_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """SCHEDULER_ENABLED=0 ska ge en webbserver utan jobb och utan catchup.

    En andra instans som delar databas med en som redan kör jobben hade
    annars kört varje synk, analys och backup två gånger.
    """
    import scheduler as scheduler_module

    calls: list[object] = []
    monkeypatch.setattr(
        scheduler_module,
        "run_morning_recommendation",
        lambda settings: calls.append(settings),
    )
    monkeypatch.setenv("SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("STARTUP_CATCHUP", "1")

    app = _fresh_app(monkeypatch, tmp_path)
    with TestClient(app) as client:
        assert app.state.scheduler is None, "ingen scheduler ska ha startats"
        assert app.state.startup_catchup is None, "ingen catchup-tråd heller"
        assert client.get("/static/icon.svg").status_code == 200

    assert calls == []


def test_an_unreadable_catchup_setting_keeps_the_catchup_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """En bortslarvad eller felskriven rad i .env får inte tyst stänga av
    dagens analyser. Bara de uttryckliga av-orden räknas."""
    from config import _bool_setting

    for av in ("0", "false", "FALSE", "no", "off"):
        monkeypatch.setenv("X_CATCHUP", av)
        assert _bool_setting("X_CATCHUP", True) is False, av
    for på in ("1", "true", "YES", "on"):
        monkeypatch.setenv("X_CATCHUP", på)
        assert _bool_setting("X_CATCHUP", False) is True, på
    for strunt in ("", "  ", "kanske", "ja"):
        monkeypatch.setenv("X_CATCHUP", strunt)
        assert _bool_setting("X_CATCHUP", True) is True, repr(strunt)
    monkeypatch.delenv("X_CATCHUP")
    assert _bool_setting("X_CATCHUP", True) is True


def test_evening_summary_runs_at_startup_not_only_on_cron(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regressionstest: en omstart precis runt 23:59 missade tidigare
    cron-triggern helt till nästa dygn — det fanns ingen catchup, till
    skillnad från morgonrekommendationen. EVENING_SUMMARY_TIME sätts till
    '00:00' här: (now.hour, now.minute) är aldrig < (0, 0), så
    tidsspärren i run_evening_summary passerar oavsett när testet råkar
    köras, precis som att kvällen redan passerat."""
    monkeypatch.setenv("EVENING_SUMMARY_TIME", "00:00")
    # Se kommentaren i morgonvarianten ovan.
    monkeypatch.setenv("STARTUP_CATCHUP", "1")
    import scheduler as scheduler_module

    calls: list[object] = []
    monkeypatch.setattr(
        scheduler_module, "run_evening_summary", lambda settings: calls.append(settings)
    )
    # Se kommentaren i morgonvarianten: de andra tre stubbas ut.
    monkeypatch.setattr(
        scheduler_module, "run_morning_recommendation", lambda settings: None
    )
    monkeypatch.setattr(scheduler_module, "run_coaching", lambda settings: None)
    monkeypatch.setattr(
        scheduler_module, "run_evening_summary_recheck", lambda settings: None
    )

    app = _fresh_app(monkeypatch, tmp_path)
    with TestClient(app):
        pass

    assert len(calls) >= 1, (
        "run_evening_summary ska anropas minst en gång vid uppstart "
        "(catchup-jobbet), inte bara vänta på nästa 23:59"
    )


def test_evening_summary_catchup_skips_before_scheduled_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """En omstart mitt på dagen (t.ex. efter en vanlig uppdatering) ska
    INTE generera en kvällssammanfattning av en dag som knappt börjat.
    Klockan mockas till 14:00, schemalagd tid är 23:59 — alltså långt
    kvar."""
    import scheduler as scheduler_module
    from sync.store import Store

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return cls(2026, 8, 24, 14, 0)

    monkeypatch.setattr(scheduler_module, "datetime", _FixedDatetime)

    store = Store(tmp_path / "test.db")
    store.init()
    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)

    def _boom(*a, **kw):
        raise AssertionError("ClaudeClient ska inte anropas — det är för tidigt på dagen")

    monkeypatch.setattr(scheduler_module, "ClaudeClient", _boom)

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        evening_summary_time = "23:59"
        athlete = None

    scheduler_module.run_evening_summary(_FakeSettings())  # type: ignore[arg-type]

    assert store.latest_analysis("daily_summary") is None


def test_evening_summary_catchup_skips_if_already_generated_today(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """En omstart efter kl 23:59 (t.ex. flera uppdateringar samma kväll)
    ska inte generera om en sammanfattning som redan finns för idag."""
    import scheduler as scheduler_module
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    today = datetime.now().date().isoformat()
    store.save_analysis("daily_summary", "# Redan klar", today, "fake-model")
    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)

    def _boom(*a, **kw):
        raise AssertionError(
            "ClaudeClient ska inte anropas — dagens sammanfattning finns redan"
        )

    monkeypatch.setattr(scheduler_module, "ClaudeClient", _boom)

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        evening_summary_time = "00:00"  # garanterat redan passerat
        athlete = None

    scheduler_module.run_evening_summary(_FakeSettings())  # type: ignore[arg-type]

    # Oförändrad — inte skriven över med en ny (identisk) rad.
    assert store.latest_analysis("daily_summary")["markdown"] == "# Redan klar"


def test_evening_summary_regenerates_if_only_a_premature_early_run_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """En manuell "Generera om" strax efter midnatt (t.ex. under testning)
    sparar en rad med dagens ref_id, timmar innan dagens träning och sömn
    ens finns. Den ska INTE räknas som "redan gjord" och blockera den
    riktiga 23:59-körningen — annars uteblir kvällssammanfattningen helt
    den dagen, trots att allt underlag finns när klockan väl slår 23:59."""
    import sqlite3

    import scheduler as scheduler_module
    import sync.store as store_module
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    today = datetime.now().date().isoformat()
    # Sparad 00:05 — långt före den schemalagda tiden 23:59. Byggd ur
    # `today` och inte ur ett eget datetime.now(): två avläsningar av
    # klockan kan hamna på var sin sida om midnatt.
    premature = f"{today}T00:05:00"
    with sqlite3.connect(tmp_path / "test.db") as conn:
        conn.execute(
            "INSERT INTO analyses (analysis_type, ref_id, created_at, model, markdown) "
            "VALUES (?, ?, ?, ?, ?)",
            ("daily_summary", today, premature, "fake-model", "# Tom, för tidig"),
        )
    # Store stämplar created_at med den VERKLIGA klockan, och
    # latest_analysis sorterar på den. Utan den här raden föll testet
    # varje natt mellan 00:00 och 00:05: den nygenererade raden fick då
    # en tidsstämpel FÖRE den "för tidiga" 00:05-raden ovan, och sorterades
    # därför under den — testet såg den gamla texten och trodde att
    # spärren blockerat, fast analysen hade genererats. Loggen sa
    # "Kvällssammanfattning genererad" i samma körning.
    #
    # Fönstret är smalt men inte teoretiskt: det träffade en fullständig
    # körning av sviten som råkade passera midnatt.
    monkeypatch.setattr(store_module, "_now_iso", lambda: f"{today}T23:59:30")
    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)

    class _FakeClaudeClient:
        model = "fake-model"

        def analyze(self, system_prompt, user_data, max_tokens=2048, max_chars=None):
            return "# Kväll\n\nBra dag, med riktigt underlag."

    monkeypatch.setattr(scheduler_module, "ClaudeClient", lambda settings: _FakeClaudeClient())

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        evening_summary_time = "23:59"
        athlete = None

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return cls(*datetime.now().timetuple()[:3], 23, 59)

    monkeypatch.setattr(scheduler_module, "datetime", _FixedDatetime)

    scheduler_module.run_evening_summary(_FakeSettings())  # type: ignore[arg-type]

    saved = store.latest_analysis("daily_summary")
    assert saved is not None
    assert saved["markdown"] == "# Kväll\n\nBra dag, med riktigt underlag."


def test_evening_summary_catchup_generates_when_guards_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Positiva fallet: klockan har passerat schemalagd tid och ingen
    sammanfattning finns än — då ska den faktiskt genereras och sparas."""
    import scheduler as scheduler_module
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)

    class _FakeClaudeClient:
        model = "fake-model"

        def analyze(self, system_prompt, user_data, max_tokens=2048, max_chars=None):
            return "# Kväll\n\nBra dag."

    monkeypatch.setattr(scheduler_module, "ClaudeClient", lambda settings: _FakeClaudeClient())

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        evening_summary_time = "00:00"  # garanterat redan passerat
        athlete = None

    scheduler_module.run_evening_summary(_FakeSettings())  # type: ignore[arg-type]

    saved = store.latest_analysis("daily_summary")
    assert saved is not None
    assert saved["ref_id"] == datetime.now().date().isoformat()


# --- Stegkontroll: gör om kvällssammanfattningen om steg efterjusteras -


def test_steps_have_drifted_ignores_small_relative_changes() -> None:
    """En vanlig, liten efterjustering (uppmätt skarpt: 12 540 -> 12 670,
    +1%) ska inte trigga en ny (betald) analys."""
    from scheduler import _steps_have_drifted

    assert _steps_have_drifted(12_540, 12_670) is False


def test_steps_have_drifted_catches_a_real_case() -> None:
    """Fallet som upptäckte problemet: 19 674 -> 22 125, +12%, ska trigga."""
    from scheduler import _steps_have_drifted

    assert _steps_have_drifted(19_674, 22_125) is True


def test_steps_have_drifted_ignores_a_small_absolute_change_on_a_low_count() -> None:
    """Ren procentsats hade gjort om sammanfattningen för en dag på 200
    steg som blev 210 (+5%) — betydelselöst i absoluta tal. Absoluttröskeln
    ska hindra det."""
    from scheduler import _steps_have_drifted

    assert _steps_have_drifted(200, 210) is False


def test_a_summary_written_without_step_data_counts_as_drift() -> None:
    """Skrevs sammanfattningen innan wellness-raden synkat sa den "ingen
    stegdata tillgänglig". Kommer siffran in under natten är det det
    starkaste skälet att göra om den, inte det svagaste — men den ska ändå
    vara värd en betald analys."""
    from scheduler import _steps_have_drifted

    assert _steps_have_drifted(None, 22_125) is True
    assert _steps_have_drifted(None, 40) is False, "40 steg är ingen ny rapport"


def test_steps_that_vanish_are_not_drift() -> None:
    """En sammanfattning som säger ett tal är bättre än en som säger
    ingenting."""
    from scheduler import _steps_have_drifted

    assert _steps_have_drifted(19_674, None) is False
    assert _steps_have_drifted(None, None) is False


def test_the_recheck_says_in_the_log_that_it_ran(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Grenen "ingen sammanfattning att kontrollera" var tyst.

    Uppstarts-catchupen 2026-09-07 18:23 körde kontrollen skarpt, skrev
    sin markör och lämnade inte ett spår i journalen. Ett jobb som inte
    syns när det kör går inte att felsöka när det inte gör det den ska.
    """
    import logging

    import scheduler as scheduler_module
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)

    def _boom(*a, **kw):
        raise AssertionError("ingen markör finns — inget ska synkas eller analyseras")

    monkeypatch.setattr(scheduler_module, "IntervalsClient", _boom)
    monkeypatch.setattr(scheduler_module, "ClaudeClient", _boom)

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        evening_summary_recheck_time = "00:00"
        athlete = None

    with caplog.at_level(logging.INFO, logger="scheduler"):
        scheduler_module.run_evening_summary_recheck(_FakeSettings())  # type: ignore[arg-type]

    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    assert any(yesterday in r.getMessage() for r in caplog.records), (
        "körningen ska säga i loggen vilket dygn den tittade på"
    )
    assert _markorens_dag(store, "evening_summary_recheck_done") == yesterday


def test_evening_summary_records_the_steps_it_was_written_with(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """run_evening_summary ska minnas stegtalet den byggde på, så
    run_evening_summary_recheck har något att jämföra mot i morgon."""
    import scheduler as scheduler_module
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    today = datetime.now().date().isoformat()
    store.upsert_wellness_many([{**_TOM_WELLNESS, "day": today, "steps": 19_674}])
    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)

    class _FakeClaudeClient:
        model = "fake-model"

        def analyze(self, system_prompt, user_data, max_tokens=2048, max_chars=None):
            return "# Kväll"

    monkeypatch.setattr(scheduler_module, "ClaudeClient", lambda settings: _FakeClaudeClient())

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        evening_summary_time = "00:00"  # garanterat redan passerat
        athlete = None

    scheduler_module.run_evening_summary(_FakeSettings())  # type: ignore[arg-type]

    noterat = _noterat_underlag(store)
    assert noterat is not None
    assert noterat["day"] == today
    assert noterat["steps"] == 19_674


def test_the_steps_marker_records_what_the_summary_actually_saw(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Markören ska minnas stegtalet TEXTEN byggde på, inte det som råkar
    stå i databasen när Claude svarat.

    Claude-anropet tar en halv minut och den timvisa synken tar inte
    analyslåset. Lästes stegen efter anropet skrev en synk som landade i
    det fönstret in ett tal sammanfattningen aldrig sett — och
    morgonkontrollen jämförde då mot fel baslinje, såg noll drift, och
    missade tyst precis den efterjustering den finns för att fånga.
    """
    import scheduler as scheduler_module
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    today = datetime.now().date().isoformat()
    store.upsert_wellness_many([{**_TOM_WELLNESS, "day": today, "steps": 19_674}])
    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)

    class _SynkandeClaudeClient:
        """Låtsas vara den synk som landar mitt under Claude-anropet."""

        model = "fake-model"

        def analyze(self, system_prompt, user_data, max_tokens=2048, max_chars=None):
            store.upsert_wellness_many([{**_TOM_WELLNESS, "day": today, "steps": 22_125}])
            return "# Kväll"

    monkeypatch.setattr(
        scheduler_module, "ClaudeClient", lambda settings: _SynkandeClaudeClient()
    )

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        evening_summary_time = "00:00"  # garanterat redan passerat
        athlete = None

    scheduler_module.run_evening_summary(_FakeSettings())  # type: ignore[arg-type]

    noterat = _noterat_underlag(store)
    assert noterat is not None
    assert noterat["steps"] == 19_674, (
        "markören ska hålla talet sammanfattningen skrevs med, inte det "
        "synken hann skriva medan Claude svarade"
    )


def test_evening_summary_recheck_regenerates_when_steps_drifted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Huvudfallet: gårdagens sammanfattning byggde på 19 674 steg, en
    eftersynk höjer det till 22 125 (+12%) — sammanfattningen ska göras om."""
    import scheduler as scheduler_module
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    store.upsert_wellness_many([{**_TOM_WELLNESS, "day": yesterday, "steps": 22_125}])
    store.save_analysis("daily_summary", "# Gammal, 19 674 steg", yesterday, "fake-model")
    _notera_underlag(store, yesterday, steps=19_674)
    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)
    monkeypatch.setattr(
        scheduler_module, "sync_wellness", lambda client, store, full=False: 0
    )
    monkeypatch.setattr(
        scheduler_module, "sync_activities", lambda client, store, **kw: 0
    )
    monkeypatch.setattr(
        scheduler_module,
        "IntervalsClient",
        lambda settings: contextlib.nullcontext(_NoopIntervalsClient()),
    )

    class _FakeClaudeClient:
        model = "fake-model"

        def analyze(self, system_prompt, user_data, max_tokens=2048, max_chars=None):
            return "# Ny, 22 125 steg"

    monkeypatch.setattr(scheduler_module, "ClaudeClient", lambda settings: _FakeClaudeClient())

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        evening_summary_recheck_time = "00:00"  # garanterat redan passerat
        athlete = None

    scheduler_module.run_evening_summary_recheck(_FakeSettings())  # type: ignore[arg-type]

    saved = store.latest_analysis("daily_summary", yesterday)
    assert saved is not None
    assert saved["markdown"] == "# Ny, 22 125 steg"


def test_evening_summary_recheck_regenerates_a_summary_written_without_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Wellness-raden hade inte synkat kl 23:59, så sammanfattningen skrevs
    utan stegdata alls. Kommer siffran in under natten ska den skrivas om."""
    import scheduler as scheduler_module
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    store.upsert_wellness_many([{**_TOM_WELLNESS, "day": yesterday, "steps": 22_125}])
    store.save_analysis(
        "daily_summary", "# Ingen stegdata tillgänglig", yesterday, "fake-model"
    )
    _notera_underlag(store, yesterday, steps=None)
    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)
    monkeypatch.setattr(
        scheduler_module, "sync_wellness", lambda client, store, full=False: 0
    )
    monkeypatch.setattr(
        scheduler_module, "sync_activities", lambda client, store, **kw: 0
    )
    monkeypatch.setattr(
        scheduler_module,
        "IntervalsClient",
        lambda settings: contextlib.nullcontext(_NoopIntervalsClient()),
    )

    class _FakeClaudeClient:
        model = "fake-model"

        def analyze(self, system_prompt, user_data, max_tokens=2048, max_chars=None):
            return "# Ny, 22 125 steg"

    monkeypatch.setattr(scheduler_module, "ClaudeClient", lambda settings: _FakeClaudeClient())

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        evening_summary_recheck_time = "00:00"
        athlete = None

    scheduler_module.run_evening_summary_recheck(_FakeSettings())  # type: ignore[arg-type]

    saved = store.latest_analysis("daily_summary", yesterday)
    assert saved is not None
    assert saved["markdown"] == "# Ny, 22 125 steg"


def test_evening_summary_recheck_skips_when_steps_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ingen drift, ingen anledning att betala för en ny analys."""
    import scheduler as scheduler_module
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    store.upsert_wellness_many([{**_TOM_WELLNESS, "day": yesterday, "steps": 12_670}])
    store.save_analysis("daily_summary", "# Oförändrad", yesterday, "fake-model")
    _notera_underlag(store, yesterday, steps=12_540)
    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)
    monkeypatch.setattr(
        scheduler_module, "sync_wellness", lambda client, store, full=False: 0
    )
    monkeypatch.setattr(
        scheduler_module, "sync_activities", lambda client, store, **kw: 0
    )
    monkeypatch.setattr(
        scheduler_module,
        "IntervalsClient",
        lambda settings: contextlib.nullcontext(_NoopIntervalsClient()),
    )

    def _boom(*a, **kw):
        raise AssertionError("ClaudeClient ska inte anropas — steget har inte ändrats nog")

    monkeypatch.setattr(scheduler_module, "ClaudeClient", _boom)

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        evening_summary_recheck_time = "00:00"
        athlete = None

    scheduler_module.run_evening_summary_recheck(_FakeSettings())  # type: ignore[arg-type]

    saved = store.latest_analysis("daily_summary", yesterday)
    assert saved is not None
    assert saved["markdown"] == "# Oförändrad"


def test_evening_summary_recheck_only_runs_once_per_day(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ett andra anrop samma morgon (t.ex. uppstarts-catchupen efter en
    omstart minuter efter cron-jobbet) ska inte synka och jämföra igen."""
    import scheduler as scheduler_module
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    store.upsert_wellness_many([{**_TOM_WELLNESS, "day": yesterday, "steps": 22_125}])
    store.save_analysis("daily_summary", "# Gammal", yesterday, "fake-model")
    _notera_underlag(store, yesterday, steps=19_674)
    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)

    calls: list[str] = []
    monkeypatch.setattr(
        scheduler_module,
        "sync_wellness",
        lambda client, store, full=False: calls.append("synced"),
    )
    monkeypatch.setattr(
        scheduler_module, "sync_activities", lambda client, store, **kw: 0
    )
    monkeypatch.setattr(
        scheduler_module,
        "IntervalsClient",
        lambda settings: contextlib.nullcontext(_NoopIntervalsClient()),
    )

    class _FakeClaudeClient:
        model = "fake-model"

        def analyze(self, system_prompt, user_data, max_tokens=2048, max_chars=None):
            return "# Ny"

    monkeypatch.setattr(scheduler_module, "ClaudeClient", lambda settings: _FakeClaudeClient())

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        evening_summary_recheck_time = "00:00"
        athlete = None

    scheduler_module.run_evening_summary_recheck(_FakeSettings())  # type: ignore[arg-type]
    scheduler_module.run_evening_summary_recheck(_FakeSettings())  # type: ignore[arg-type]

    assert calls == ["synced"], "andra körningen ska ha hoppat över synken helt"


def test_evening_summary_recheck_skips_before_scheduled_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Uppstarts-catchupen ropar på funktionen vid varje omstart oavsett
    klockslag — en omstart klockan 03 ska inte trigga stegkontrollen innan
    Intervals rimligen hunnit synka klart gårdagen."""
    import scheduler as scheduler_module

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return cls(2026, 8, 24, 3, 0)

    monkeypatch.setattr(scheduler_module, "datetime", _FixedDatetime)

    def _boom(*a, **kw):
        raise AssertionError("Store ska inte ens öppnas — det är för tidigt på dygnet")

    monkeypatch.setattr(scheduler_module, "Store", _boom)

    class _FakeSettings:

        strength_corrections = ()
        db_path = "unused.db"
        evening_summary_recheck_time = "09:00"
        athlete = None

    scheduler_module.run_evening_summary_recheck(_FakeSettings())  # type: ignore[arg-type]


def test_evening_summary_recheck_cron_job_is_registered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = _fresh_app(monkeypatch, tmp_path)

    with TestClient(app):
        scheduler = app.state.scheduler
        job = scheduler.get_job("evening_summary_recheck")
        assert job is not None
        assert job.trigger.fields[5].expressions[0].first == 9  # timme
        assert job.trigger.fields[6].expressions[0].first == 0  # minut
        assert job.misfire_grace_time == 3600


# --- Konfigurerbara tider ---------------------------------------------


def test_morning_window_follows_the_configured_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regressionstest: triggern hårdkodade hour="7-11", minute=0 och var
    alltså helt frikopplad från MORNING_RECOMMENDATION_TIME, som lästes in
    i config.py och dokumenterades i .env.example men aldrig användes till
    något — att sätta den gjorde ingenting alls."""
    monkeypatch.setenv("MORNING_RECOMMENDATION_TIME", "05:30")
    app = _fresh_app(monkeypatch, tmp_path)

    with TestClient(app):
        job = app.state.scheduler.get_job("morning_recommendation")
        assert job is not None
        assert job.trigger.fields[5].expressions[0].first == 5   # timme
        assert job.trigger.fields[6].expressions[0].first == 30  # minut


def test_morning_window_does_not_wrap_past_midnight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """En sen starttid ska klippas vid midnatt, inte svepa runt till nästa
    dygn — en "morgonanalys" kl 02 hade sammanfattat fel natt."""
    monkeypatch.setenv("MORNING_RECOMMENDATION_TIME", "22:00")
    app = _fresh_app(monkeypatch, tmp_path)

    with TestClient(app):
        job = app.state.scheduler.get_job("morning_recommendation")
        assert job is not None
        assert job.trigger.fields[5].expressions[0].last <= 23


def test_evening_time_is_configurable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EVENING_SUMMARY_TIME", "21:15")
    app = _fresh_app(monkeypatch, tmp_path)

    with TestClient(app):
        job = app.state.scheduler.get_job("evening_summary")
        assert job is not None
        assert job.trigger.fields[5].expressions[0].first == 21
        assert job.trigger.fields[6].expressions[0].first == 15


# --- Schemalagd träningsanalys ----------------------------------------


def test_coaching_job_is_registered_at_the_configured_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Träningsanalysen genererades tidigare bara när man tryckte på
    knappen i webben. Den ska nu köras automatiskt (COACHING_TIME,
    default 12:00)."""
    app = _fresh_app(monkeypatch, tmp_path)

    with TestClient(app):
        job = app.state.scheduler.get_job("coaching")
        assert job is not None
        assert job.trigger.fields[5].expressions[0].first == 12  # timme
        assert job.trigger.fields[6].expressions[0].first == 0   # minut
        assert job.next_run_time is not None, "jobbet får inte registreras pausat"


def test_coaching_time_is_configurable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COACHING_TIME", "14:30")
    app = _fresh_app(monkeypatch, tmp_path)

    with TestClient(app):
        job = app.state.scheduler.get_job("coaching")
        assert job is not None
        assert job.trigger.fields[5].expressions[0].first == 14
        assert job.trigger.fields[6].expressions[0].first == 30


def test_coaching_skips_when_already_generated_today(tmp_path: Path) -> None:
    """Spärren kräver att coaching sparas med dagens datum som ref_id —
    pipelinen sparade tidigare None, vilket gjort den omöjlig att avgöra."""
    import scheduler as scheduler_module
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()
    today = datetime.now().date().isoformat()
    store.save_analysis("coaching", "# Redan gjord", today, "fake-model")

    calls: list[str] = []

    class _Boom:
        def __init__(self, settings):  # noqa: ANN001
            calls.append("claude skapades")

    scheduler_module.ClaudeClient = _Boom  # type: ignore[misc]
    try:
        class _S:
            strength_corrections = ()
            db_path = str(tmp_path / "t.db")
            coaching_time = "00:00"
        scheduler_module.run_coaching(_S())  # type: ignore[arg-type]
    finally:
        importlib.reload(scheduler_module)

    assert calls == [], "ingen ny analys ska genereras när dagens redan finns"


def test_coaching_skips_before_the_scheduled_time(tmp_path: Path) -> None:
    import scheduler as scheduler_module
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()

    class _S:

        strength_corrections = ()
        db_path = str(tmp_path / "t.db")
        coaching_time = "23:59"

    # Ingen analys sparas, och inget Claude-anrop görs — hade det gjorts
    # hade nätverksspärren i conftest.py fällt testet.
    scheduler_module.run_coaching(_S())  # type: ignore[arg-type]
    assert store.latest_analysis("coaching") is None


# --- Morgonanalysens spärr --------------------------------------------


def _wellness_for(day: str, **overrides: object) -> dict[str, object]:
    """En komplett wellness-rad med sömndata, för spärrtesterna nedan."""
    row: dict[str, object] = {
        "day": day, "rest_day": 0, "ctl": 40.0, "atl": 30.0, "tsb": 10.0,
        "ramp_rate": None, "sleep_seconds": 25000, "sleep_score": 80,
        "sleep_quality": 2, "avg_sleeping_hr": 50, "weight": 90.0,
        "resting_hr": 55, "hrv": 60.0, "hrv_sdnn": None, "stress": None,
        "respiration": None, "spO2": None, "systolic": None, "diastolic": None,
        "hydration": None, "soreness": None, "fatigue": None, "mood": None,
        "motivation": None, "injury": None, "readiness": None, "vo2max": None,
        "steps": 5000, "kcal_consumed": None, "raw_json": "{}",
        "last_synced": datetime.now().isoformat(),
    }
    row.update(overrides)
    return row


def _morning_harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Kopplar bort nät och Claude, räknar hur många analyser som genereras."""
    import scheduler as scheduler_module
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    calls: list[str] = []

    class _CountingClaude:
        model = "fake-model"

        def analyze(self, *a, **kw):  # noqa: ANN002, ANN003, ANN202
            calls.append("analyze")
            return "# Morgon\n\nSov gott."

    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)
    monkeypatch.setattr(
        scheduler_module,
        "IntervalsClient",
        lambda settings: contextlib.nullcontext(_NoopIntervalsClient()),
    )
    monkeypatch.setattr(scheduler_module, "sync_activities", lambda client, store: 0)
    monkeypatch.setattr(
        scheduler_module, "sync_wellness", lambda client, store, full=False: 0
    )
    monkeypatch.setattr(
        scheduler_module, "ClaudeClient", lambda settings: _CountingClaude()
    )

    _frys_morgonklockan(monkeypatch, scheduler_module)

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        athlete = None
        # Alla tiderna, så att testerna kan gå via _run_startup_catchups —
        # den riktiga vägen in vid en omstart. Fattades en slukade
        # catchupens except-sats felet, och jobbet föll tyst bort ur
        # testet i stället för att köras.
        morning_recommendation_time = "07:00"
        coaching_time = "12:00"
        evening_summary_time = "23:59"
        evening_summary_recheck_time = "09:00"
        backup_time = "03:30"
        backup_dir = tmp_path / "backups"
        backup_keep = 14
        analysis_keep_days = 30

    return scheduler_module, store, calls, _FakeSettings()


def test_morning_recommendation_ignores_a_pure_resync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regressionstest för buggen som gav 5-8 morgonanalyser per dygn.

    Spärren jämförde wellness.last_synced med analysens created_at.
    last_synced sätts till datetime.now() vid varje upsert oavsett om
    något värde ändrats, så med SYNC_INTERVAL_HOURS=1 såg varje timme ut
    som ny sömndata. Uppmätt på Pi5:an innan fixen: 5 analyser 29 aug,
    7 den 28:e, 8 den 27:e — alla utom den första onödiga, och den
    rekommendation man läst 07:15 ersattes tyst av en annan 08:00.

    Här synkas samma data om, precis som den timvisa synken gör: bara
    last_synced ändras. Ingen ny analys ska genereras.
    """
    scheduler_module, store, calls, settings = _morning_harness(monkeypatch, tmp_path)
    today = datetime.now().date().isoformat()
    store.upsert_wellness_many([_wellness_for(today)])

    scheduler_module.run_morning_recommendation(settings)  # type: ignore[arg-type]
    assert calls == ["analyze"], "första körningen ska generera"

    # Timvis synk: identisk data, ny synktidsstämpel.
    store.upsert_wellness_many([_wellness_for(today, last_synced=datetime.now().isoformat())])
    scheduler_module.run_morning_recommendation(settings)  # type: ignore[arg-type]
    scheduler_module.run_morning_recommendation(settings)  # type: ignore[arg-type]

    assert calls == ["analyze"], (
        "en omsynk av oförändrad data är inte ny sömndata och ska inte "
        "kosta ett nytt Claude-anrop"
    )


def test_morning_recommendation_regenerates_when_sleep_data_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spärren får inte bli så hård att den missar riktigt ny sömndata —
    klockan synkar inte alltid färdigt innan första försöket."""
    scheduler_module, store, calls, settings = _morning_harness(monkeypatch, tmp_path)
    today = datetime.now().date().isoformat()
    store.upsert_wellness_many([_wellness_for(today, sleep_score=None, hrv=None)])

    scheduler_module.run_morning_recommendation(settings)  # type: ignore[arg-type]
    assert calls == ["analyze"]

    # Sömnpoängen och HRV kom in en timme senare.
    store.upsert_wellness_many([_wellness_for(today, sleep_score=91, hrv=64.0)])
    scheduler_module.run_morning_recommendation(settings)  # type: ignore[arg-type]

    assert len(calls) == 2, "ny sömndata ska ge en ny rekommendation"


def test_morning_snapshot_ignores_values_that_drift_during_the_day(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """steps och stress växer hela dagen. Räknades de som nytt underlag
    hade spärren börjat släppa igenom igen, av en anledning som inte har
    med nattens återhämtning att göra."""
    import scheduler as scheduler_module

    today = datetime.now().date().isoformat()
    morning = scheduler_module._morning_data_snapshot(
        today, _wellness_for(today, steps=800, stress=20, kcal_consumed=300.0)
    )
    later = scheduler_module._morning_data_snapshot(
        today, _wellness_for(today, steps=9400, stress=41, kcal_consumed=2100.0)
    )
    assert morning == later

    # ...men ett nytt dygn är alltid nytt underlag, även med identiska värden.
    tomorrow = (datetime.now() + timedelta(days=1)).date().isoformat()
    assert scheduler_module._morning_data_snapshot(
        tomorrow, _wellness_for(today)
    ) != morning


def test_morning_recommendation_ignores_the_watch_refining_its_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regressionstest för andra försöket med spärren.

    Version ett jämförde VÄRDENA och gav två analyser den 30 augusti,
    07:00 och 08:00, båda loggade som "ny sömndata" trots att natten redan
    var registrerad klockan sju. Klockan fortsätter skicka justeringar
    efter första synken — hrv 45 -> 46, vilopuls 61 -> 62, sömnscore som
    räknas om — och varje sådan kostade en hel ny analys.

    Fönstret finns för att vänta in att datan DYKER UPP, inte för att följa
    den medan den finslipas."""
    scheduler_module, store, calls, settings = _morning_harness(monkeypatch, tmp_path)
    today = datetime.now().date().isoformat()
    store.upsert_wellness_many([_wellness_for(today, hrv=45.0, resting_hr=61, sleep_score=80)])

    scheduler_module.run_morning_recommendation(settings)  # type: ignore[arg-type]
    assert calls == ["analyze"]

    # Klockan justerar samma mätvärden under förmiddagen.
    for hrv, puls, score in ((46.0, 62, 81), (46.5, 62, 82)):
        store.upsert_wellness_many(
            [_wellness_for(today, hrv=hrv, resting_hr=puls, sleep_score=score)]
        )
        scheduler_module.run_morning_recommendation(settings)  # type: ignore[arg-type]

    assert calls == ["analyze"], "finjusterade värden är inte nytt underlag"


def test_morning_recommendation_reacts_to_a_measurement_that_was_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Men ett fält som saknades helt och sedan dyker upp — sömnen synkade
    sent, HRV kom först vid nio — är riktigt nytt underlag."""
    scheduler_module, store, calls, settings = _morning_harness(monkeypatch, tmp_path)
    today = datetime.now().date().isoformat()
    store.upsert_wellness_many([_wellness_for(today, hrv=None)])

    scheduler_module.run_morning_recommendation(settings)  # type: ignore[arg-type]
    assert calls == ["analyze"]

    store.upsert_wellness_many([_wellness_for(today, hrv=44.0)])
    scheduler_module.run_morning_recommendation(settings)  # type: ignore[arg-type]

    assert len(calls) == 2


def test_morning_recommendation_retries_after_a_failed_claude_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Markören ska sättas efter analysen, inte före: ett misslyckat
    Claude-anrop får inte se ut som ett gjort jobb för nästa heltimme."""
    scheduler_module, store, calls, settings = _morning_harness(monkeypatch, tmp_path)
    today = datetime.now().date().isoformat()
    store.upsert_wellness_many([_wellness_for(today)])

    class _FailingClaude:
        model = "fake-model"

        def analyze(self, *a, **kw):  # noqa: ANN002, ANN003, ANN202
            calls.append("failed")
            raise RuntimeError("Claude gav ett tomt svar")

    monkeypatch.setattr(
        scheduler_module, "ClaudeClient", lambda settings: _FailingClaude()
    )
    scheduler_module.run_morning_recommendation(settings)  # type: ignore[arg-type]

    assert store.get_job_state(scheduler_module._MORNING_STATE_KEY) is None
    assert store.latest_analysis("morning_recommendation") is None


def test_morning_recommendation_syncs_before_it_decides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Synken låg tidigare efter spärren, så den var onåbar i precis det
    läge där den behövdes: fanns ingen wellness-rad för idag returnerade
    funktionen utan att någonsin hämta en."""
    scheduler_module, store, calls, settings = _morning_harness(monkeypatch, tmp_path)
    today = datetime.now().date().isoformat()

    def _sync_brings_todays_sleep(client, store_arg, full=False):  # noqa: ANN001, ANN202
        store.upsert_wellness_many([_wellness_for(today)])
        return 1

    monkeypatch.setattr(scheduler_module, "sync_wellness", _sync_brings_todays_sleep)

    # Databasen är tom när jobbet startar; bara synken kan fylla den.
    scheduler_module.run_morning_recommendation(settings)  # type: ignore[arg-type]

    assert calls == ["analyze"]


# --- Morgonanalysens tidsspärr ----------------------------------------


def test_morning_recommendation_skips_a_restart_just_after_midnight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Det som faktiskt hände på Pi:n 2026-09-03.

    _run_startup_catchups anropar jobbet vid VARJE omstart, och jobbet
    hade — till skillnad från kvälls- och coachingjobbet — ingen
    tidsspärr alls. En omstart kvart över midnatt skrev därför en
    "morgonrekommendation" för ett dygn som var femton minuter gammalt.
    Ur journalen:

        00:15:01  Started garmin-ai-pi.service
        00:15:28  Morgonrekommendation genererad baserat på ny sömndata

    Går via _run_startup_catchups och inte direkt på funktionen, för det
    är den vägen felet kom in.
    """
    scheduler_module, store, calls, settings = _morning_harness(monkeypatch, tmp_path)
    _frys_morgonklockan(monkeypatch, scheduler_module, dt_time(0, 15))
    today = datetime.now().date().isoformat()
    store.upsert_wellness_many([_wellness_for(today)])

    scheduler_module._run_startup_catchups(settings)

    assert store.latest_analysis("morning_recommendation") is None, (
        "en omstart efter midnatt ska inte ge en morgonanalys"
    )
    # Kvällsjobbet DÄREMOT ska köra här: 00:15 ligger inom
    # uppsamlingsfönstret efter gårdagens 23:59, och det är precis den
    # rapporten som annars uteblir (se _evening_target_day). Testet mäter
    # därför analystyp i databasen och inte antalet Claude-anrop — samma
    # omstart har numera legitim anledning att göra ett.
    igar = (datetime.now().date() - timedelta(days=1)).isoformat()
    assert store.latest_analysis("daily_summary")["ref_id"] == igar
    assert calls == ["analyze"], "exakt ett anrop: gårdagens kvällsrapport"


def test_morning_recommendation_skips_a_restart_in_the_evening(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Samma spärr åt andra hållet, och det är HÄR den behövs mest.

    Datasspärren räcker inte: dyker ett wellness-fält som saknades i
    morse upp under dagen — vikten från en kvällsvägning, till exempel —
    ser underlaget nytt ut, och en omstart klockan åtta på kvällen hade
    skrivit en färsk "morgonrekommendation" som ersatte dagens plan på
    dashboarden.

    Testet sätter upp precis det läget: en riktig körning 09:00, sedan
    ett nytt fält, sedan omstarten. Utan tidsspärren blir det två
    analyser.
    """
    scheduler_module, store, calls, settings = _morning_harness(monkeypatch, tmp_path)
    today = datetime.now().date().isoformat()

    # 09:00 — den riktiga morgonkörningen, utan vikt i wellness.
    store.upsert_wellness_many([_wellness_for(today, weight=None)])
    scheduler_module.run_morning_recommendation(settings)
    assert calls == ["analyze"]

    # Under dagen dyker vikten upp: nytt underlag enligt datasspärren.
    store.upsert_wellness_many([_wellness_for(today, weight=91.2)])
    _frys_morgonklockan(monkeypatch, scheduler_module, dt_time(20, 0))
    scheduler_module.run_morning_recommendation(settings)

    assert calls == ["analyze"], (
        "ett nytt mätvärde på kvällen är inte skäl att skriva om morgonens plan"
    )


def test_morning_window_edges_follow_the_configured_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spärren läser MORNING_RECOMMENDATION_TIME, den har ingen egen
    uppfattning om vad "morgon" är. Med starttid 05:00 sträcker sig
    fönstret till och med 09:59 (fyra timmars omförsök), så 09:30 ska
    släppas igenom och 10:30 inte."""
    scheduler_module, store, calls, settings = _morning_harness(monkeypatch, tmp_path)
    settings.morning_recommendation_time = "05:00"
    today = datetime.now().date().isoformat()
    store.upsert_wellness_many([_wellness_for(today)])

    _frys_morgonklockan(monkeypatch, scheduler_module, dt_time(4, 59))
    scheduler_module.run_morning_recommendation(settings)
    assert calls == [], "en minut före fönstret"

    _frys_morgonklockan(monkeypatch, scheduler_module, dt_time(9, 30))
    scheduler_module.run_morning_recommendation(settings)
    assert calls == ["analyze"], "sista timmen räknas hel"

    store.upsert_wellness_many([_wellness_for(today, weight=91.2)])
    _frys_morgonklockan(monkeypatch, scheduler_module, dt_time(10, 30))
    scheduler_module.run_morning_recommendation(settings)
    assert calls == ["analyze"], "en timme efter fönstret"


def test_morning_guard_and_cron_trigger_share_one_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fönstret räknas på ETT ställe (_morning_window).

    Räknade triggern och spärren var för sig kunde de glida isär, och då
    hade cron brunnit på en timme som spärren avvisar — morgonanalysen
    slutar köras, utan att något felar eller loggar ett fel. Det här
    testet kopplar ihop dem: varje timme triggern faktiskt brinner på ska
    spärren släppa igenom, och timmarna precis utanför ska den avvisa.
    """
    import scheduler as scheduler_module

    monkeypatch.setenv("MORNING_RECOMMENDATION_TIME", "05:30")
    app = _fresh_app(monkeypatch, tmp_path)

    with TestClient(app):
        job = app.state.scheduler.get_job("morning_recommendation")
        assert job is not None
        timmar = job.trigger.fields[5].expressions[0]
        minut = job.trigger.fields[6].expressions[0].first
        settings = app.state.settings

    forsta = timmar.first
    sista = timmar.last if timmar.last is not None else timmar.first

    for hour in range(forsta, sista + 1):
        assert scheduler_module._within_morning_window(
            datetime(2026, 9, 3, hour, minut), settings
        ), f"triggern brinner {hour:02d}:{minut:02d} men spärren avvisar det"

    assert not scheduler_module._within_morning_window(
        datetime(2026, 9, 3, forsta, minut - 1), settings
    )
    assert not scheduler_module._within_morning_window(
        datetime(2026, 9, 3, sista + 1, 0), settings
    )


# --- Nattlig backup ---------------------------------------------------


def test_backup_job_is_registered_at_the_configured_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BACKUP_TIME", "04:15")
    app = _fresh_app(monkeypatch, tmp_path)

    with TestClient(app):
        job = app.state.scheduler.get_job("backup")
        assert job is not None
        assert job.trigger.fields[5].expressions[0].first == 4
        assert job.trigger.fields[6].expressions[0].first == 15


def test_backup_writes_a_dated_copy_and_rotates(tmp_path: Path) -> None:
    import scheduler as scheduler_module
    from sync.store import Store

    db_path = tmp_path / "training.db"
    store = Store(db_path)
    store.init()
    today = datetime.now().date().isoformat()
    store.upsert_wellness_many([_wellness_for(today)])

    backup_dir = tmp_path / "backups"

    class _S:

        strength_corrections = ()
        db_path = str(tmp_path / "training.db")
        backup_dir = tmp_path / "backups"
        backup_keep = 3
        analysis_keep_days = 30

    # Fyra äldre kopior som rotationen ska beskära ner till tre totalt.
    backup_dir.mkdir()
    for day in ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"):
        (backup_dir / f"training-{day}.db").write_bytes(b"gammal")

    dest = scheduler_module.run_backup(_S())  # type: ignore[arg-type]

    assert dest is not None and dest.exists()
    assert dest.name == f"training-{today}.db"
    kept = sorted(p.name for p in backup_dir.glob("training-*.db"))
    assert len(kept) == 3, kept
    assert dest.name in kept
    assert "training-2026-01-01.db" not in kept, "äldst ska rensas först"

    # Kopian ska gå att öppna och innehålla datan, inte bara existera.
    restored = Store(dest)
    assert restored.get_wellness_day(today) is not None


def _backup_settings(tmp_path: Path):
    from sync.store import Store

    Store(tmp_path / "training.db").init()

    class _S:
        strength_corrections = ()
        db_path = str(tmp_path / "training.db")
        backup_dir = tmp_path / "backups"
        backup_time = "03:30"
        backup_keep = 14
        analysis_keep_days = 30

    return _S()


def test_a_missed_night_backup_is_taken_at_startup(tmp_path: Path) -> None:
    """Backupjobbet brinner vid BACKUP_TIME och inte annars. Var Pi:n
    avstängd eller startade om just då blev det ingen kopia det dygnet,
    och inget tog igen den."""
    import scheduler as scheduler_module

    settings = _backup_settings(tmp_path)
    tio = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)

    dest = scheduler_module.run_backup_catchup(settings, now=tio)  # type: ignore[arg-type]

    assert dest is not None and dest.exists(), "den missade backupen togs inte"


def test_the_backup_catchup_leaves_an_existing_copy_alone(tmp_path: Path) -> None:
    import scheduler as scheduler_module

    settings = _backup_settings(tmp_path)
    idag = datetime.now().date()
    settings.backup_dir.mkdir()
    (settings.backup_dir / f"training-{idag.isoformat()}.db").write_bytes(b"nattens")
    tio = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)

    assert scheduler_module.run_backup_catchup(settings, now=tio) is None  # type: ignore[arg-type]
    assert (settings.backup_dir / f"training-{idag.isoformat()}.db").read_bytes() == b"nattens"


def test_before_the_backup_time_last_nights_copy_is_what_counts(tmp_path: Path) -> None:
    """Klockan två har dagens backup inte hunnit bli aktuell än. Det som
    ska finnas är gårdagens — saknas den tas en kopia nu, finns den görs
    ingenting."""
    import scheduler as scheduler_module

    settings = _backup_settings(tmp_path)
    idag = datetime.now().date()
    tva = datetime.now().replace(hour=2, minute=0, second=0, microsecond=0)
    settings.backup_dir.mkdir()

    forrgar = (idag - timedelta(days=2)).isoformat()
    (settings.backup_dir / f"training-{forrgar}.db").write_bytes(b"gammal")
    assert scheduler_module.run_backup_catchup(settings, now=tva) is not None  # type: ignore[arg-type]

    for p in settings.backup_dir.iterdir():
        p.unlink()
    igar = (idag - timedelta(days=1)).isoformat()
    (settings.backup_dir / f"training-{igar}.db").write_bytes(b"natten")
    assert scheduler_module.run_backup_catchup(settings, now=tva) is None  # type: ignore[arg-type]


def test_startup_catchups_include_the_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    import scheduler as scheduler_module

    anrop: list[str] = []
    for jobb in ("run_morning_recommendation", "run_coaching", "run_evening_summary",
                 "run_evening_summary_recheck", "run_backup_catchup"):
        monkeypatch.setattr(scheduler_module, jobb, lambda s, _j=jobb: anrop.append(_j))

    scheduler_module._run_startup_catchups(object())  # type: ignore[arg-type]

    assert anrop[-1] == "run_backup_catchup"


def test_prune_backups_cannot_touch_the_live_database(tmp_path: Path) -> None:
    """BACKUP_DIR kan pekas på samma katalog som databasen. Bindestrecket i
    prefixet är det som gör att rotationen inte kan radera training.db."""
    import scheduler as scheduler_module

    live = tmp_path / "training.db"
    live.write_bytes(b"levande databas")
    for day in ("2026-01-01", "2026-01-02"):
        (tmp_path / f"training-{day}.db").write_bytes(b"kopia")

    removed = scheduler_module._prune_backups(tmp_path, keep=1)

    assert live.exists(), "den levande databasen får aldrig träffas av rotationen"
    assert [p.name for p in removed] == ["training-2026-01-01.db"]


def test_backup_copy_does_not_spawn_wal_files_when_read(tmp_path: Path) -> None:
    """Kopian ska inte ligga i WAL-läge.

    Backup-API:et tar med källans filhuvud, och en kopia i WAL-läge får en
    -wal och en -shm bredvid sig så fort någon öppnar den för att titta.
    Upptäckt genom att köra kontrollkommandona mot Pi:n: en `sqlite3
    -readonly`-fråga mot backupen lämnade två sidofiler efter sig.
    """
    import sqlite3

    from sync.store import Store

    db_path = tmp_path / "training.db"
    store = Store(db_path)
    store.init()
    dest = tmp_path / "backups" / "training-2026-08-30.db"
    store.backup(dest)

    # Läs kopian precis som en kontroll skulle göra.
    conn = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    conn.execute("SELECT count(*) FROM wellness").fetchall()
    conn.close()

    strays = sorted(p.name for p in dest.parent.iterdir() if p.name != dest.name)
    assert strays == [], f"backupen lämnade sidofiler efter sig: {strays}"


def test_prune_removes_sidecar_files_with_their_backup(tmp_path: Path) -> None:
    """Rotationen letade bara efter *.db, så -wal och -shm blev kvar för
    evigt när sin huvudfil rensades bort."""
    import scheduler as scheduler_module

    for day in ("2026-01-01", "2026-01-02"):
        (tmp_path / f"training-{day}.db").write_bytes(b"kopia")
        (tmp_path / f"training-{day}.db-wal").write_bytes(b"wal")
        (tmp_path / f"training-{day}.db-shm").write_bytes(b"shm")

    scheduler_module._prune_backups(tmp_path, keep=1)

    kvar = sorted(p.name for p in tmp_path.iterdir())
    assert kvar == [
        "training-2026-01-02.db",
        "training-2026-01-02.db-shm",
        "training-2026-01-02.db-wal",
    ], kvar


def test_backup_failure_is_logged_not_raised(tmp_path: Path) -> None:
    """En trasig backup ska inte ta ner schemaläggaren — nästa natt får
    försöka igen."""
    import scheduler as scheduler_module

    class _S:

        strength_corrections = ()
        db_path = str(tmp_path / "finns-inte" / "training.db")
        backup_dir = tmp_path / "backups"
        backup_keep = 3

    assert scheduler_module.run_backup(_S()) is None  # type: ignore[arg-type]


# --- Kvällssammanfattningen över midnatt ------------------------------


def test_evening_target_day_covers_a_delay_past_midnight() -> None:
    """misfire_grace_time är en timme, men jobbet avvisade sig självt på
    (timme, minut) mot 23:59 — och varje försening från 23:59 passerar
    midnatt. Uppmätt före rättelsen: i tid och 30 s sent genererades, 4 min
    och 46 min sent hoppades över. Nåden var 60 sekunder i praktiken."""
    from scheduler import _evening_target_day

    scheduled = (23, 59)
    assert _evening_target_day(datetime(2026, 9, 5, 23, 59), scheduled) == "2026-09-05"
    assert _evening_target_day(datetime(2026, 9, 6, 0, 3), scheduled) == "2026-09-05"
    assert _evening_target_day(datetime(2026, 9, 6, 0, 45), scheduled) == "2026-09-05"
    # Utanför nåden: gårdagen är förlorad, och dagens dygn har knappt börjat.
    assert _evening_target_day(datetime(2026, 9, 6, 1, 30), scheduled) is None
    assert _evening_target_day(datetime(2026, 9, 6, 14, 0), scheduled) is None


def test_evening_target_day_is_today_once_the_time_has_passed() -> None:
    """Ett tidigare klockslag ska inte dra in gårdagen i onödan."""
    from scheduler import _evening_target_day

    assert _evening_target_day(datetime(2026, 9, 6, 22, 0), (21, 0)) == "2026-09-06"
    assert _evening_target_day(datetime(2026, 9, 6, 20, 0), (21, 0)) is None


def test_evening_summary_after_midnight_summarises_yesterday(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ett 23:59-jobb som blir fyra minuter försenat kör efter midnatt. Det
    ska skriva GÅRDAGENS rapport — inte en tom rapport om ett fyra minuter
    gammalt dygn, och inte ingenting alls."""
    import scheduler as scheduler_module
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return cls(2026, 9, 6, 0, 3)

    monkeypatch.setattr(scheduler_module, "datetime", _FixedDatetime)

    sedda: dict = {}

    class _FakePipeline:
        def __init__(self, *a, **kw) -> None:
            pass

        def evening_summary(self, day=None):  # noqa: ANN001
            sedda["day"] = day
            store.save_analysis("daily_summary", "# Gårdagen", day, "fake-model")
            return "# Gårdagen"

    import analysis.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "AnalysisPipeline", _FakePipeline)
    monkeypatch.setattr(scheduler_module, "ClaudeClient", lambda settings: None)

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        evening_summary_time = "23:59"
        athlete = None

    scheduler_module.run_evening_summary(_FakeSettings())  # type: ignore[arg-type]

    assert sedda["day"] == "2026-09-05"
    assert store.latest_analysis("daily_summary")["ref_id"] == "2026-09-05"


def test_evening_catchup_does_not_regenerate_what_it_just_wrote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Gårdagens rapport skriven 00:05 idag ligger EFTER gårdagens 23:59 och
    räknas som gjord. En jämförelse på bara (timme, minut) hade läst 00:05
    som "före 23:59" och genererat om den vid varje omstart."""
    import scheduler as scheduler_module
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)
    store.save_analysis("daily_summary", "# Gårdagen", "2026-09-05", "fake-model")
    import sqlite3

    with sqlite3.connect(tmp_path / "test.db") as conn:
        conn.execute(
            "UPDATE analyses SET created_at='2026-09-06T00:05:00' WHERE ref_id=?",
            ("2026-09-05",),
        )

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return cls(2026, 9, 6, 0, 20)

    monkeypatch.setattr(scheduler_module, "datetime", _FixedDatetime)

    def _boom(*a, **kw):
        raise AssertionError("gårdagens rapport finns redan — ska inte göras om")

    monkeypatch.setattr(scheduler_module, "ClaudeClient", _boom)

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        evening_summary_time = "23:59"
        athlete = None

    scheduler_module.run_evening_summary(_FakeSettings())  # type: ignore[arg-type]

    assert store.latest_analysis("daily_summary")["markdown"] == "# Gårdagen"


# --- Kapplöpning mellan cron-jobbet och uppstarts-catchupen -------------


def _wellness_rad(day: str) -> dict:
    """En komplett wellness-rad. Kolumnerna måste finnas allihop."""
    kolumner = (
        "rest_day ctl atl tsb ramp_rate sleep_seconds sleep_score sleep_quality "
        "avg_sleeping_hr weight resting_hr hrv hrv_sdnn stress respiration spO2 "
        "systolic diastolic hydration soreness fatigue mood motivation injury "
        "readiness vo2max steps kcal_consumed raw_json"
    ).split()
    rad: dict = dict.fromkeys(kolumner)
    rad.update({
        "day": day, "rest_day": 0, "ctl": 40.0, "atl": 30.0, "tsb": 5.0,
        "sleep_seconds": 25000, "sleep_score": 80, "resting_hr": 55,
        "hrv": 60.0, "steps": 5000, "raw_json": "{}",
        "last_synced": datetime.now().isoformat(),
    })
    return rad


def _morgonuppsattning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Riggar run_morning_recommendation utan nät: egen store, stubbad synk.

    Returnerar (scheduler_module, store, settings).
    """
    import scheduler as scheduler_module
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    today = datetime.now().date().isoformat()
    store.upsert_wellness_many([_wellness_rad(today)])

    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)
    monkeypatch.setattr(scheduler_module, "sync_activities", lambda client, store: 0)
    monkeypatch.setattr(
        scheduler_module, "sync_wellness", lambda client, store, full=False: 0
    )
    monkeypatch.setattr(
        scheduler_module, "sync_sport_settings", lambda client, store: 0
    )
    monkeypatch.setattr(
        scheduler_module,
        "IntervalsClient",
        lambda settings: contextlib.nullcontext(_NoopIntervalsClient()),
    )
    monkeypatch.setattr(
        scheduler_module, "ClaudeClient", lambda settings: _NoopIntervalsClient()
    )
    _frys_morgonklockan(monkeypatch, scheduler_module)

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        athlete = None
        morning_recommendation_time = "07:00"

    return scheduler_module, store, _FakeSettings()


def _fejka_pipeline(monkeypatch: pytest.MonkeyPatch, generera) -> None:
    """Byter ut AnalysisPipeline mot en som kör `generera` för morgonanalysen.

    Klassen slås ut i analysis.pipeline och inte i scheduler, eftersom
    run_morning_recommendation importerar den inne i funktionskroppen.
    """
    import analysis.pipeline as pipeline_module

    class _FakePipeline:
        def __init__(self, store, claude, athlete=None) -> None:  # noqa: ANN001
            self.store = store

        def morning_recommendation(self) -> str:
            return generera(self.store)

    monkeypatch.setattr(pipeline_module, "AnalysisPipeline", _FakePipeline)


def test_the_morning_analysis_holds_the_analysis_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Låset ska hållas MEDAN analysen genereras, inte bara runt spärren.

    Samma sorts kontroll som test_run_sync_holds_the_shared_sync_lock: en
    spion inne i det skyddade avsnittet frågar om låset är taget.
    """
    scheduler_module, store, settings = _morgonuppsattning(monkeypatch, tmp_path)
    sett: dict[str, bool] = {}

    def _generera(s) -> str:  # noqa: ANN001
        sett["last"] = scheduler_module._analysis_lock.locked()
        s.save_analysis(
            "morning_recommendation", "# Analys",
            datetime.now().date().isoformat(), "fake",
        )
        return "# Analys"

    _fejka_pipeline(monkeypatch, _generera)
    scheduler_module.run_morning_recommendation(settings)  # type: ignore[arg-type]

    assert sett.get("last") is True, (
        "analysen ska genereras med låset taget — annars hinner cron-jobbet "
        "och uppstarts-catchupen läsa samma spärr och båda generera"
    )
    assert not scheduler_module._analysis_lock.locked(), "låset ska släppas efteråt"


def test_two_morning_runs_at_the_same_time_generate_one_analysis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Kapplöpningen mellan cron-jobbet och uppstarts-catchupen.

    Startas tjänsten om strax före en schemalagd tid hinner catchupen in i
    sitt Claude-anrop — en halv minut lång — innan cron brinner. Cron läste
    då en spärr som ännu inte skrivits, och båda genererade: två betalda
    analyser och två rapporter för samma dygn.

    Trådarna överlappar med hjälp av en kort paus inne i den första
    genereringen. Pausen är en fördröjning, inte en synkronisering: med
    låset på plats blir svaret 1 oavsett hur trådarna schemaläggs, för den
    andra tråden hinner aldrig förbi spärren. Utan låset blir det 2 så
    snart tråd två får köra alls under pausen.
    """
    import threading

    scheduler_module, store, settings = _morgonuppsattning(monkeypatch, tmp_path)
    anrop: list[str] = []
    lås = threading.Lock()

    def _generera(s) -> str:  # noqa: ANN001
        with lås:
            anrop.append("generera")
        threading.Event().wait(0.3)
        s.save_analysis(
            "morning_recommendation", "# Analys",
            datetime.now().date().isoformat(), "fake",
        )
        return "# Analys"

    _fejka_pipeline(monkeypatch, _generera)

    trådar = [
        threading.Thread(
            target=scheduler_module.run_morning_recommendation, args=(settings,)
        )
        for _ in range(2)
    ]
    for t in trådar:
        t.start()
    for t in trådar:
        t.join(timeout=10)

    assert anrop == ["generera"], (
        f"två samtidiga körningar genererade {len(anrop)} analyser — "
        "spärren måste läsas och skrivas under samma lås"
    )


def test_two_coaching_runs_at_the_same_time_generate_one_analysis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Samma kapplöpning för träningsanalysen (12:00).

    Uppstarts-catchupen kör de tre analyserna i följd, så en omstart
    30 sekunder före 12:00 kan lika gärna sätta coaching-körningen mitt i
    sitt Claude-anrop när cron brinner.
    """
    import threading

    import analysis.pipeline as pipeline_module
    import scheduler as scheduler_module
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    today = datetime.now().date().isoformat()
    store.upsert_wellness_many([_wellness_rad(today)])

    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)
    monkeypatch.setattr(
        scheduler_module, "ClaudeClient", lambda settings: _NoopIntervalsClient()
    )

    anrop: list[str] = []
    lås = threading.Lock()

    class _FakePipeline:
        def __init__(self, s, claude, athlete=None) -> None:  # noqa: ANN001
            self.store = s

        def coaching(self) -> str:
            with lås:
                anrop.append("generera")
            threading.Event().wait(0.3)
            self.store.save_analysis("coaching", "# Coaching", today, "fake")
            return "# Coaching"

    monkeypatch.setattr(pipeline_module, "AnalysisPipeline", _FakePipeline)

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        athlete = None
        coaching_time = "00:00"

    trådar = [
        threading.Thread(target=scheduler_module.run_coaching, args=(_FakeSettings(),))
        for _ in range(2)
    ]
    for t in trådar:
        t.start()
    for t in trådar:
        t.join(timeout=10)

    assert anrop == ["generera"], (
        f"två samtidiga körningar genererade {len(anrop)} träningsanalyser"
    )


def test_the_backup_is_written_before_the_analyses_are_pruned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Gallringen är appens enda rutin som raderar något.

    Kopian ska tas FÖRE den, så att natten backup innehåller raderna som
    just togs bort. Görs det tvärtom är den kvällens skyddsnät redan
    beskuret när man behöver det.
    """
    import scheduler as scheduler_module
    from sync.store import Store

    db_path = tmp_path / "training.db"
    store = Store(db_path)
    store.init()
    today = datetime.now().date().isoformat()
    store.upsert_wellness_many([_wellness_for(today)])

    ordning: list[str] = []
    riktig_backup = Store.backup
    riktig_prune = Store.prune_analyses

    def _spionerad_backup(self, dest):  # noqa: ANN001, ANN202
        ordning.append("backup")
        return riktig_backup(self, dest)

    def _spionerad_prune(self, keep_days):  # noqa: ANN001, ANN202
        ordning.append("prune")
        return riktig_prune(self, keep_days)

    monkeypatch.setattr(Store, "backup", _spionerad_backup)
    monkeypatch.setattr(Store, "prune_analyses", _spionerad_prune)

    class _S:

        strength_corrections = ()
        db_path = str(tmp_path / "training.db")
        backup_dir = tmp_path / "backups"
        backup_keep = 3
        analysis_keep_days = 30

    assert scheduler_module.run_backup(_S()) is not None  # type: ignore[arg-type]
    assert ordning == ["backup", "prune"]


def test_vacuum_only_runs_when_something_was_actually_pruned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """VACUUM skriver om hela databasen. Att göra det varje natt för
    sakens skull är ren förslitning på ett SD-kort — bara när en gallring
    faktiskt frigjort sidor finns det något att hämta tillbaka."""
    import scheduler as scheduler_module
    from sync.store import Store

    db_path = tmp_path / "training.db"
    store = Store(db_path)
    store.init()
    today = datetime.now().date().isoformat()
    store.upsert_wellness_many([_wellness_for(today)])

    vacuums: list[int] = []
    monkeypatch.setattr(Store, "vacuum", lambda self: vacuums.append(1))

    class _S:

        strength_corrections = ()
        db_path = str(tmp_path / "training.db")
        backup_dir = tmp_path / "backups"
        backup_keep = 3
        analysis_keep_days = 30

    monkeypatch.setattr(Store, "prune_analyses", lambda self, keep_days: 0)
    scheduler_module.run_backup(_S())  # type: ignore[arg-type]
    assert vacuums == [], "inget raderat, ingenting att ge tillbaka"

    monkeypatch.setattr(Store, "prune_analyses", lambda self, keep_days: 7)
    scheduler_module.run_backup(_S())  # type: ignore[arg-type]
    assert vacuums == [1]


# --- Kvällskontrollen ser hela underlaget, inte bara stegen -----------


def _pass_for(day: str, activity_id: str) -> dict:
    return {
        "id": activity_id, "name": "Kvällspass", "type": "Run", "sport": "Run",
        "start_time": f"{day}T18:00:00+00:00", "duration_seconds": 3600,
        "distance_meters": 10000, "average_heart_rate": 150,
        "max_heart_rate": 175, "average_watts": None, "normalized_watts": None,
        "average_cadence": None, "average_speed": None, "tss": 75.0,
        "intensity": None, "raw_json": "{}", "last_synced": f"{day}T20:00:00+00:00",
    }


def _kor_kontrollen(monkeypatch, tmp_path, store, claude_svar):
    """Kör run_evening_summary_recheck med synk och Claude utbytta.

    `claude_svar=None` betyder att ingen analys FÅR göras — anropas
    ClaudeClient ändå faller testet. Returnerar listan över synkade
    källor, så tester kan kontrollera att passen hämtades och inte bara
    wellness.
    """
    import scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)
    synkat: list[str] = []
    monkeypatch.setattr(
        scheduler_module,
        "sync_wellness",
        lambda client, store, full=False: synkat.append("wellness"),
    )
    monkeypatch.setattr(
        scheduler_module,
        "sync_activities",
        lambda client, store, **kw: synkat.append("activities"),
    )
    monkeypatch.setattr(
        scheduler_module,
        "IntervalsClient",
        lambda settings: contextlib.nullcontext(_NoopIntervalsClient()),
    )

    if claude_svar is None:
        def _boom(*a, **kw):
            raise AssertionError("ClaudeClient ska inte anropas — inget har ändrats")

        monkeypatch.setattr(scheduler_module, "ClaudeClient", _boom)
    else:
        class _FakeClaudeClient:
            model = "fake-model"

            def analyze(self, system_prompt, user_data, max_tokens=2048, max_chars=None):
                return claude_svar

        monkeypatch.setattr(
            scheduler_module, "ClaudeClient", lambda settings: _FakeClaudeClient()
        )

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        evening_summary_recheck_time = "00:00"
        athlete = None

    scheduler_module.run_evening_summary_recheck(_FakeSettings())  # type: ignore[arg-type]
    return synkat


def test_the_recheck_redoes_a_summary_that_missed_a_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ett pass som ligger kvar i klockan över natten laddas upp först på
    morgonen och saknas alltså HELT i en sammanfattning skriven 23:59.

    Kontrollen fanns först bara för stegtalet, men det var att laga ett
    exempel snarare än felet: en sammanfattning som missar ett helt pass
    är fel på ett sätt inget stegtal räddar.
    """
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    store.upsert_wellness_many([{**_TOM_WELLNESS, "day": yesterday, "steps": 12_000}])
    store.save_analysis("daily_summary", "# Ingen träning igår", yesterday, "fake-model")
    _notera_underlag(store, yesterday, steps=12_000, sleep_seconds=25000, activities=[])
    store.upsert_activity(_pass_for(yesterday, "sent-uppladdat"))

    _kor_kontrollen(monkeypatch, tmp_path, store, "# Du sprang faktiskt")

    saved = store.latest_analysis("daily_summary", yesterday)
    assert saved is not None
    assert saved["markdown"] == "# Du sprang faktiskt"


def test_the_recheck_syncs_the_passes_before_it_compares(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Wellness-synken hämtar inga pass. Utan aktivitetssynken hade
    kontrollen jämfört mot en databas där morgonens uppladdning ännu inte
    fanns — och sett noll ändring."""
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    store.upsert_wellness_many([{**_TOM_WELLNESS, "day": yesterday, "steps": 12_000}])
    store.save_analysis("daily_summary", "# Gammal", yesterday, "fake-model")
    _notera_underlag(store, yesterday, steps=12_000, sleep_seconds=25000, activities=[])

    synkat = _kor_kontrollen(monkeypatch, tmp_path, store, None)

    assert "activities" in synkat
    assert "wellness" in synkat


def test_the_recheck_redoes_a_summary_when_the_night_was_recalculated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Kvällssammanfattningen är "dygnets träning + nattens sömn". Räknar
    Garmin om natten när klockan synkat klart slår det lika hårt mot
    texten som stegtalet gör."""
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    store.upsert_wellness_many([
        {**_TOM_WELLNESS, "day": yesterday, "steps": 12_000, "sleep_seconds": 28_800}
    ])
    store.save_analysis("daily_summary", "# Du sov 5h55m", yesterday, "fake-model")
    _notera_underlag(
        store, yesterday, steps=12_000, sleep_seconds=21_300, activities=[]
    )

    _kor_kontrollen(monkeypatch, tmp_path, store, "# Du sov 8h")

    saved = store.latest_analysis("daily_summary", yesterday)
    assert saved is not None
    assert saved["markdown"] == "# Du sov 8h"


def test_a_few_minutes_of_sleep_is_not_worth_a_new_analysis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Skillnaden mellan 6h55m och 7h00m är inget att skriva om texten för."""
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    store.upsert_wellness_many([
        {**_TOM_WELLNESS, "day": yesterday, "steps": 12_000, "sleep_seconds": 25_200}
    ])
    store.save_analysis("daily_summary", "# Oförändrad", yesterday, "fake-model")
    _notera_underlag(
        store, yesterday, steps=12_000, sleep_seconds=24_900, activities=[]
    )

    _kor_kontrollen(monkeypatch, tmp_path, store, None)

    saved = store.latest_analysis("daily_summary", yesterday)
    assert saved is not None
    assert saved["markdown"] == "# Oförändrad"


def test_an_old_marker_is_not_read_as_a_change_in_fields_it_never_saw(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Markörerna som redan ligger på Pi:n känner bara till "steps".

    Ett fält en äldre version aldrig noterade kan inte ha ändrats sedan
    dess. Behandlades det som en ändring hade varenda sammanfattning
    gjorts om en gång vid uppgraderingen — betalda analyser för att koden
    bytt format.
    """
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    store.upsert_wellness_many([{**_TOM_WELLNESS, "day": yesterday, "steps": 12_000}])
    store.save_analysis("daily_summary", "# Oförändrad", yesterday, "fake-model")
    store.upsert_activity(_pass_for(yesterday, "fanns-hela-tiden"))
    _notera_underlag(store, yesterday, steps=12_000)

    _kor_kontrollen(monkeypatch, tmp_path, store, None)

    saved = store.latest_analysis("daily_summary", yesterday)
    assert saved is not None
    assert saved["markdown"] == "# Oförändrad"


def test_a_marker_for_another_day_is_not_something_to_compare_against(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Markören är EN rad som skrivs över, så den kan hålla ett annat dygn
    än det kontrollen gäller — t.ex. efter ett dygn då tjänsten låg nere
    och ingen kvällssammanfattning skrevs. Då finns inget att jämföra
    mot, och kontrollen ska säga det i loggen i stället för att gissa."""
    import logging

    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    store.upsert_wellness_many([{**_TOM_WELLNESS, "day": yesterday, "steps": 22_125}])
    store.save_analysis("daily_summary", "# Oförändrad", yesterday, "fake-model")
    _notera_underlag(store, "2020-01-01", steps=1)

    with caplog.at_level(logging.INFO, logger="scheduler"):
        _kor_kontrollen(monkeypatch, tmp_path, store, None)

    assert any(yesterday in r.getMessage() for r in caplog.records)
    saved = store.latest_analysis("daily_summary", yesterday)
    assert saved is not None
    assert saved["markdown"] == "# Oförändrad"
    assert _markorens_dag(store, "evening_summary_recheck_done") == yesterday


def test_the_evening_markers_do_not_grow_a_row_per_day(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """job_state ska rymma en handfull rader, inte 730 om året.

    Markörerna frågas bara om GÅRDAGEN, en enda gång, så det finns inget
    att spara. Vilket dygn de gäller ligger i värdet.
    """
    import sqlite3

    import scheduler as scheduler_module
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    today = datetime.now().date().isoformat()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    store.upsert_wellness_many([{**_TOM_WELLNESS, "day": today, "steps": 19_674}])
    store.upsert_wellness_many([{**_TOM_WELLNESS, "day": yesterday, "steps": 12_000}])
    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)

    class _FakeClaudeClient:
        model = "fake-model"

        def analyze(self, system_prompt, user_data, max_tokens=2048, max_chars=None):
            return "# Kväll"

    monkeypatch.setattr(
        scheduler_module, "ClaudeClient", lambda settings: _FakeClaudeClient()
    )

    class _KvallsSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        evening_summary_time = "00:00"
        athlete = None

    scheduler_module.run_evening_summary(_KvallsSettings())  # type: ignore[arg-type]
    _kor_kontrollen(monkeypatch, tmp_path, store, "# Ny")

    with sqlite3.connect(store.db_path) as conn:
        nycklar = {r[0] for r in conn.execute("SELECT key FROM job_state").fetchall()}
    for nyckel in nycklar:
        assert today not in nyckel and yesterday not in nyckel, (
            f"{nyckel} bär ett datum — då blir det en ny rad varje dygn"
        )
    assert len(nycklar) <= 4, f"job_state växer: {nycklar}"


def test_the_recheck_does_not_hold_the_analysis_lock_while_it_syncs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Synken är ett nätverksanrop på en halv minut, plus nedladdning av
    FIT-filer för nya styrkepass.

    Låg den innanför _analysis_lock blockerade kvällskontrollen de tre
    andra dagliga analyserna hela den tiden. Morgonrekommendationens
    omförsök kör 09:00, samma minut som den här, och hade fått vänta in
    något den inte har med att göra. run_morning_recommendation synkar
    utanför låset av precis det skälet.
    """
    import scheduler as scheduler_module
    from sync.store import Store

    store = Store(tmp_path / "test.db")
    store.init()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    store.upsert_wellness_many([{**_TOM_WELLNESS, "day": yesterday, "steps": 12_000}])
    store.save_analysis("daily_summary", "# Oförändrad", yesterday, "fake-model")
    _notera_underlag(store, yesterday, steps=12_000, sleep_seconds=25000, activities=[])

    monkeypatch.setattr(scheduler_module, "Store", lambda db_path, corrections=(): store)

    laset_var_ledigt: list[bool] = []

    def _kolla_laset(client, store, **kw):
        # En annan analystråd skulle just nu kunna ta låset — kan vi?
        ledigt = scheduler_module._analysis_lock.acquire(blocking=False)
        laset_var_ledigt.append(ledigt)
        if ledigt:
            scheduler_module._analysis_lock.release()
        return 0

    monkeypatch.setattr(scheduler_module, "sync_activities", _kolla_laset)
    monkeypatch.setattr(
        scheduler_module,
        "sync_wellness",
        lambda client, store, full=False: _kolla_laset(client, store),
    )
    monkeypatch.setattr(
        scheduler_module,
        "IntervalsClient",
        lambda settings: contextlib.nullcontext(_NoopIntervalsClient()),
    )

    def _boom(*a, **kw):
        raise AssertionError("ClaudeClient ska inte anropas — inget har ändrats")

    monkeypatch.setattr(scheduler_module, "ClaudeClient", _boom)

    class _FakeSettings:

        strength_corrections = ()
        db_path = str(tmp_path / "test.db")
        evening_summary_recheck_time = "00:00"
        athlete = None

    scheduler_module.run_evening_summary_recheck(_FakeSettings())  # type: ignore[arg-type]

    assert laset_var_ledigt == [True, True], (
        "analyslåset ska vara ledigt under hela synken"
    )
    assert _markorens_dag(store, "evening_summary_recheck_done") == yesterday, (
        "och kontrollen ska ändå ha gjorts klart"
    )
