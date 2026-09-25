"""Intervals.icu REST-klient.

API-dokumentation: https://intervals.icu/api
Autentisering: HTTP Basic auth med det bokstavliga användarnamnet
'API_KEY' och din api-nyckel som lösenord (se Intervals forum-guide).

Viktigt:
- /activities och /wellness kräver parametern `oldest` (ISO-datum),
  annars svarar API:et 422 Unprocessable Entity.
- Intervals ligger bakom Cloudflare, som kan utmana/blockera vissa
  klientbibliotek. Vi sätter en webbläsarlik User-Agent för att undvika det.
- Klienten återanvänder en enda httpx.Client (connection pooling) och gör
  automatiska återförsök med backoff vid nätverksfel, 429 (rate limit) och
  5xx-svar. Anropa close() (eller använd `with IntervalsClient(...) as c:`)
  när klienten inte längre behövs.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

import httpx

from config import Settings
from sync.fit_strength import is_strength_activity, parse_strength_sets
from sync.store import Store

log = logging.getLogger(__name__)

# Hindrar att två synkar kör samtidigt INOM samma process — den manuella
# synken från webbens "Synka Intervals"-knapp och det schemalagda jobbet i
# schedulern skriver båda till samma SQLite-databas.
#
# Låset låg tidigare i web/app.py och togs därför BARA av /sync-routen;
# schedulerns egna körningar (run_sync, och synken inuti
# run_morning_recommendation) gick förbi det helt. Det skyddade alltså
# inte mot den krock kommentaren påstod att det skyddade mot. Det bor här
# istället, i modulen som äger sync_activities/sync_wellness, så att alla
# anropare når samma lås.
#
# Obs: detta är ett TRÅD-lås, inte ett processlås. `python -m src.main
# sync` kör i en EGEN process och kan inte se det här låset — den
# samtidigheten hanteras av SQLites egen låsning plus busy-timeouten i
# sync/store.py.
_sync_lock = threading.Lock()


@contextmanager
def sync_lock(blocking: bool = True) -> Iterator[bool]:
    """Tar synk-låset. Yield:ar True om det gick, False om det var upptaget.

    `blocking=False` används av webbens /sync för att kunna svara 409
    direkt istället för att låta förfrågan hänga tills den schemalagda
    synken är klar. Släpper alltid låset, även om kroppen kastar.
    """
    acquired = _sync_lock.acquire(blocking)
    try:
        yield acquired
    finally:
        if acquired:
            _sync_lock.release()

BASE_URL = "https://intervals.icu/api/v1"
TIMEOUT = 30.0
# Hur långt bakåt i tiden vi hämtar vid första (full) sync.
DEFAULT_HISTORY_DAYS = 365
# Hur långt bakåt rutinsynken hämtar wellness (se sync_wellness). Måste
# rymma både att Intervals räknar om CTL/ATL/TSB bakåt när ett gammalt
# pass ändras, och att sömn- och viktdata kan komma in med fördröjning.
WELLNESS_WINDOW_DAYS = 14
# Motsvarande fönster för aktiviteter (se _sync_window). Samma längd som
# wellness: det är hur långt i efterhand ett pass rimligen kan dyka upp
# eller ändras. Kostar ett listanrop oavsett längd — detaljhämtningen,
# som är det dyra, styrs av icu_sync_date i sync_activities.
ACTIVITY_WINDOW_DAYS = 14
# Webbläsarlik User-Agent för att undgå Cloudflare-blockering av klientbibliotek.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Återförsök vid nätverksfel / 429 / 5xx.
MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SECONDS = 1.0


def _first_not_none(*values: Any) -> Any:
    """Returnerar första värdet som inte är None.

    Skiljer sig från `a or b or c`, som felaktigt hoppar vidare även när
    ett tidigare värde är ett giltigt, "falskt" tal som 0 (t.ex. TSS=0
    eller kadens=0 för ett mycket lätt/kort pass).
    """
    for v in values:
        if v is not None:
            return v
    return None


class IntervalsClient:
    """Tunn HTTP-klient mot Intervals.icu."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.athlete_id = settings.intervals_athlete_id
        # Intervals Basic auth: username är den bokstavliga strängen 'API_KEY',
        # lösenordet är din api-nyckel.
        self._auth = httpx.BasicAuth("API_KEY", settings.intervals_api_key)
        self._headers = {"User-Agent": USER_AGENT}
        # En delad, återanvänd klient (connection pooling) istället för att
        # öppna en ny TCP/TLS-anslutning per anrop.
        self._client = httpx.Client(
            timeout=TIMEOUT, headers=self._headers, auth=self._auth
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> IntervalsClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _url(self, path: str) -> str:
        return f"{BASE_URL}/athlete/{self.athlete_id}/{path.lstrip('/')}"

    def _activity_url(self, path: str) -> str:
        """URL för aktivitets-endpoints som INTE ligger under /athlete/{id}/.

        Intervals har två parallella namnrymder: /athlete/{id}/activities/...
        (som _url bygger) och /activity/{id}/... för sådant som hör till ett
        enskilt pass — streams, best-efforts, originalfilen. Den senare tar
        inget athlete-id alls, så _url kan inte återanvändas.
        """
        return f"{BASE_URL}/activity/{path.lstrip('/')}"

    def _request_with_retry(
        self, method: str, url: str, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        """Gör ett HTTP-anrop med återförsök (exponentiell backoff).

        Återförsöker vid nätverksfel (timeout, connection error), 429
        (rate limit, respekterar ev. Retry-After-header) och 5xx-svar.
        Övriga fel (t.ex. 401/404) ger upp direkt.
        """
        attempt = 0
        while True:
            try:
                response = self._client.request(method, url, params=params)
            except httpx.TransportError as exc:
                attempt += 1
                if attempt > MAX_RETRIES:
                    raise
                wait = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                log.warning(
                    "Nätverksfel mot Intervals (%s), försök %d/%d om %.1fs.",
                    exc, attempt, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                attempt += 1
                if attempt > MAX_RETRIES:
                    response.raise_for_status()
                wait = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = max(wait, float(retry_after))
                    except ValueError:
                        pass
                log.warning(
                    "Intervals svarade %d, försök %d/%d om %.1fs.",
                    response.status_code, attempt, MAX_RETRIES, wait,
                )
                time.sleep(wait)
                continue

            response.raise_for_status()
            return response

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self._request_with_retry("GET", self._url(path), params=params)
        if not response.content:
            return None
        return response.json()

    def list_activities(
        self, oldest: datetime | None = None, newest: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Hämtar aktiviteter i ett datumintervall (sorterat nyast först).

        Intervals kräver parametern `oldest`; `newest` är valfritt.
        """
        if oldest is None:
            oldest = datetime.now() - timedelta(days=DEFAULT_HISTORY_DAYS)
        params: dict[str, Any] = {"oldest": _to_date_str(oldest)}
        if newest is not None:
            params["newest"] = _to_date_str(newest)
        data = self.get("activities", params=params)
        return data or []

    def get_activity(self, activity_id: str) -> Any:
        """Hämtar detaljer för en aktivitet inkl. sektortider m.m.

        Returnerar dict eller lista beroende på endpoint-svar.
        """
        return self.get(f"activities/{activity_id}")

    def download_original_file(self, activity_id: str) -> bytes:
        """Laddar ner originalfilen för ett pass (oftast en Garmin-FIT).

        Motsvarar GET /api/v1/activity/{id}/file, som Intervals beskriver
        som "Download original activity file". Detta är den enda vägen till
        styrketräningens övnings- och viktdata: Intervals egna API-schema
        har inga sådana fält alls, men FIT-filen från klockan innehåller
        set-meddelanden med vikt och reps.

        Fungerar inte för pass som importerats från Strava — då svarar
        Intervals med ett felstatus som _request_with_retry låter bubbla upp.
        """
        response = self._request_with_retry(
            "GET", self._activity_url(f"{activity_id}/file")
        )
        return response.content

    def list_sport_settings(self) -> list[dict[str, Any]]:
        """Atletens zoninställningar per sport.

        Ger pulszonernas NAMN, som inte följer med i något passvar —
        varken listan eller detaljen. Gränserna och tiderna finns redan
        per pass (icu_hr_zones / icu_hr_zone_times); det är namnen som
        gör de två talarrayerna läsbara. Se sport_settings i store.py.

        Ett anrop returnerar alla sporter på en gång.
        """
        data = self.get("sport-settings")
        return data or []

    def list_wellness(
        self, oldest: datetime | None = None, newest: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Hämtar daglig wellness (CTL/ATL/TSB, sömn, vikt m.m.) i ett datumintervall."""
        if oldest is None:
            oldest = datetime.now() - timedelta(days=DEFAULT_HISTORY_DAYS)
        params: dict[str, Any] = {"oldest": _to_date_str(oldest)}
        if newest is not None:
            params["newest"] = _to_date_str(newest)
        data = self.get("wellness", params=params)
        return data or []


def _to_date_str(dt: datetime) -> str:
    """Intervals accepterar ISO-8601; vi skickar YYYY-MM-DD."""
    return dt.strftime("%Y-%m-%d")


def _parse_dt(value: Any) -> str | None:
    """Intervals returnerar ISO-strängar; normalisera till lokal ISO 8601.

    Om tidszon saknas i indata antas lokal tid (t.ex. start_date_local) och
    behålls som den är, så att datum-jämförelser sker i lokal tid.
    """
    if not value:
        return None
    try:
        # Hantera både '...Z' och offset-format.
        if isinstance(value, str):
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            dt = datetime.fromisoformat(value)
        else:
            dt = datetime.fromisoformat(str(value))
        return dt.isoformat()
    except Exception:
        log.warning("Kunde inte tolka datum/tid från Intervals: %r", value)
        return None


def _map_activity(raw: dict[str, Any], detail: Any = None) -> dict[str, Any]:
    """Mappar Intervals-JSON till vår tabellstruktur.

    Intervals använder `icu_*`-fältnamn för beräknade värden. Vi slår ihop
    list-data och eventuell detalj-data (som innehåller sektortider m.m.).

    Notera: detalj-endpointen kan returnera en dict, en lista eller None.
    Vi extraherar en dict ur listan om möjligt.
    """
    detail_dict: dict[str, Any] = {}
    if isinstance(detail, dict):
        detail_dict = detail
    elif isinstance(detail, list):
        # Intervals returnerar ibland en lista; ta första dict-elementet.
        for item in detail:
            if isinstance(item, dict):
                detail_dict = item
                break
    merged = {**raw, **detail_dict}
    # Intervals API använder snake_case (moving_time/elapsed_time) enligt
    # det officiella OpenAPI-schemat — tidigare letades efter movingTime/
    # elapsedTime (camelCase), som aldrig matchar något fält i svaret.
    # Det gjorde att "Längd" ofta blev tom trots att andra fält (puls,
    # distans) fungerade, eftersom de redan använde rätt snake_case-namn.
    duration = _first_not_none(
        merged.get("moving_time"), merged.get("elapsed_time"), merged.get("duration")
    )
    return {
        "id": str(merged.get("id") or merged.get("_id") or ""),
        "name": merged.get("name"),
        "type": merged.get("type"),
        "sport": merged.get("sport"),
        "start_time": _first_not_none(
            # Prioritera start_date_local (lokal tid) om det finns.
            _parse_dt(merged.get("start_date_local")),
            _parse_dt(merged.get("start_date")),
            _parse_dt(merged.get("startDate")),
            _parse_dt(merged.get("startTime")),
        ),
        "duration_seconds": int(duration) if duration is not None else None,
        "distance_meters": merged.get("distance"),
        "average_heart_rate": _first_not_none(
            merged.get("average_heartrate"),
            merged.get("averageHeartRate"),
            merged.get("avgHeartRate"),
        ),
        "max_heart_rate": _first_not_none(
            merged.get("max_heartrate"), merged.get("maxHeartRate")
        ),
        "average_watts": _first_not_none(
            merged.get("average_power"),
            merged.get("averagePower"),
            merged.get("avgWatts"),
        ),
        # Intervals använder icu_weighted_avg_watts för normalized/weighted power.
        "normalized_watts": _first_not_none(
            merged.get("icu_weighted_avg_watts"),
            merged.get("weightedAveragePower"),
            merged.get("np"),
        ),
        "average_cadence": _first_not_none(
            merged.get("average_cadence"),
            merged.get("averageCadence"),
            merged.get("avgCadence"),
        ),
        "average_speed": _first_not_none(
            merged.get("average_speed"),
            merged.get("averageSpeed"),
            merged.get("avgSpeed"),
        ),
        # Intervals använder icu_training_load för TSS-likt värde.
        "tss": _first_not_none(
            merged.get("icu_training_load"),
            merged.get("trainingStressScore"),
            merged.get("tss"),
        ),
        "intensity": _first_not_none(
            merged.get("icu_intensity_factor"), merged.get("intensity")
        ),
        "raw_json": json.dumps(merged, default=str),
        "last_synced": datetime.now().isoformat(),
    }


def _map_wellness(raw: dict[str, Any]) -> dict[str, Any]:
    day = raw.get("day") or raw.get("date") or raw.get("id")
    if day and isinstance(day, str) and len(day) >= 10:
        day = day[:10]
    return {
        "day": day,
        "rest_day": int(bool(raw.get("restDay") or raw.get("rest_day") or 0)),
        # Träningsbelastning
        "ctl": raw.get("ctl"),
        "atl": raw.get("atl"),
        # Intervals returnerar inte alltid tsb; beräkna som ctl - atl om saknas.
        "tsb": raw.get("tsb") if raw.get("tsb") is not None else (
            round(raw.get("ctl", 0) - raw.get("atl", 0), 2)
            if raw.get("ctl") is not None and raw.get("atl") is not None
            else None
        ),
        "ramp_rate": raw.get("rampRate"),
        # Sömn
        "sleep_seconds": raw.get("sleepSecs"),
        "sleep_score": raw.get("sleepScore"),
        "sleep_quality": raw.get("sleepQuality"),
        "avg_sleeping_hr": raw.get("avgSleepingHR"),
        # Kroppssammansättning
        "weight": raw.get("weight"),
        # Hjärtdata
        "resting_hr": raw.get("restingHR"),
        "hrv": raw.get("hrv"),
        "hrv_sdnn": raw.get("hrvSDNN"),
        # Stress & recovery
        "stress": raw.get("stress"),
        "respiration": raw.get("respiration"),
        "spO2": raw.get("spO2"),
        "systolic": raw.get("systolic"),
        "diastolic": raw.get("diastolic"),
        "hydration": raw.get("hydration"),
        "soreness": raw.get("soreness"),
        "fatigue": raw.get("fatigue"),
        "mood": raw.get("mood"),
        "motivation": raw.get("motivation"),
        "injury": raw.get("injury"),
        "readiness": raw.get("readiness"),
        # Prestanda
        "vo2max": raw.get("vo2max"),
        "steps": raw.get("steps"),
        "kcal_consumed": raw.get("kcalConsumed"),
        # Rådata för debugging
        "raw_json": json.dumps(raw, default=str),
        "last_synced": datetime.now().isoformat(),
    }


def _sync_window(store: Store) -> datetime:
    """Returnerar startdatum för aktivitetssynken.

    Ett fast fönster bakåt från IDAG, inte från senast synkade pass.

    Fönstret ankrades tidigare i `latest_activity_start() - 1 dygn`, vilket
    bara kunde se framåt: ett pass som laddas upp i efterhand med ett
    ÄLDRE datum än det nyaste vi redan har hamnar bakom fönstrets kant och
    hämtades aldrig — inte vid nästa synk, inte någonsin. Det händer i
    praktiken (manuellt inlagt pass man glömt, en klocka som inte synkat
    på en vecka, import från en annan enhet).

    Att bredda fönstret kostar ingenting extra i sig: listanropet är ETT
    anrop oavsett hur långt bak det sträcker sig. Det som kostade var
    detaljhämtningen per pass, och den är numera villkorad på att passet
    faktiskt ändrats — se sync_activities.

    Första körningen (tom databas) hämtar hela historiken.
    """
    if store.latest_activity_start() is None:
        return datetime.now() - timedelta(days=DEFAULT_HISTORY_DAYS)
    return datetime.now() - timedelta(days=ACTIVITY_WINDOW_DAYS)


def import_strength_sets(
    client: IntervalsClient, store: Store, activity: dict[str, Any], force: bool = False
) -> int:
    """Hämtar och sparar ett styrkepass övningar/vikter från originalfilen.

    Intervals eget API har ingen sådan data (se modulens docstring i
    sync/fit_strength.py); den finns bara i FIT-filen från klockan.
    Returnerar antal sparade set.

    Hoppar över pass som redan importerats, så den timvisa synken inte
    laddar ner samma fil om och om igen. `force=True` kringgår det och
    ersätter tidigare importerade rader — manuellt loggade set
    (source='chat') rörs aldrig.
    """
    activity_id = str(activity.get("id") or "")
    if not activity_id:
        return 0
    if not force and (
        # "Har vi tittat?" — inte "finns det set?". Den senare frågan
        # ensam lät ett pass utan set-data i filen hämtas om varje timme.
        store.has_checked_strength_import(activity_id)
        # Bakåtkompatibilitet: pass som importerades innan
        # strength_import_attempts fanns har rader men ingen notering, och
        # ska inte hämtas om en gång bara för att få en.
        or store.has_strength_sets_for_activity(activity_id, "fit")
    ):
        return 0

    start_time = activity.get("start_time") or ""
    day = start_time[:10]
    if not day:
        log.warning("Pass %s saknar starttid — kan inte datera setsen.", activity_id)
        return 0

    content = client.download_original_file(activity_id)
    exercises = parse_strength_sets(content)
    if not exercises:
        # Markera att vi tittat, annars laddas filen ner igen vid varje
        # synk så länge passet ligger kvar i fönstret. Spärren högst upp
        # frågar bara "finns det rader?", och utan set fanns det inga —
        # ett Strava-importerat pass, en TCX-fil eller ett gympass där
        # klockan inte loggade set hämtades alltså om varje timme, i all
        # evighet, trots att svaret aldrig skulle ändras.
        store.mark_strength_import_empty(activity_id, day)
        log.info("Inga set-data i originalfilen för %s.", activity_id)
        return 0

    # Ersätt en tidigare import istället för att lägga till ovanpå.
    store.delete_strength_sets_for_activity(activity_id, "fit")
    total = 0
    ersatta = 0
    for entry in exercises:
        # Klockans fil är facit för de övningar den faktiskt innehåller.
        # Samma regel gäller redan åt andra hållet: chatten vägrar logga en
        # övning som redan importerats (se _log_strength_session i
        # analysis/pipeline.py). Men den regeln gällde bara när filen kom
        # FÖRST — och den vanliga ordningen är den omvända: du loggar passet
        # på gymmet, den timvisa synken hämtar filen en timme senare.
        #
        # Utan den här rensningen blev det två exemplar av samma arbete.
        # Reproducerat: 3x5 @ 120 kg loggat i chatten och sedan importerat
        # gav 6 set och 3 600 kg volym där 1 800 lyftes — på dashboarden, på
        # passidan och i varenda payload till Claude. Dubbletterna överlevde
        # dessutom varje omsynk, eftersom raderingen ovan bara rör
        # source='fit'.
        #
        # Bara de övningar filen innehåller rensas. Loggar du något klockan
        # inte registrerade — ett set efter att du stoppat passet, en övning
        # du gjorde utan att trycka igång — ligger det kvar orört.
        #
        # Rensas FÖRE insättningen: add_strength_sets numrerar vidare från
        # övningens högsta set_number, så rader som ska bort måste vara
        # borta innan, annars börjar filens set på 4 och lämnar ett hål.
        namn = store.resolved_exercise(day, entry["exercise"])
        if namn is not None:
            ersatta += store.delete_strength_sets_for_exercise(day, namn, "chat")
        total += store.add_strength_sets(
            day=day,
            exercise=entry["exercise"],
            sets=entry["sets"],
            activity_id=activity_id,
            source="fit",
        )
    store.record_strength_import(activity_id, day, total)
    log.info(
        "Importerade %d set i %d övningar för %s%s.",
        total,
        len(exercises),
        activity_id,
        f" (ersatte {ersatta} chattloggade rader för samma övningar)"
        if ersatta
        else "",
    )
    return total


def sync_sport_settings(client: IntervalsClient, store: Store) -> int:
    """Hämtar atletens pulszonsnamn per sport. Returnerar antal sporter.

    Sväljer sina egna fel i stället för att låta dem bubbla upp som
    sync_activities och sync_wellness gör. Skälet är att zonnamn är en
    utsmyckning av analyserna, inte deras underlag: svarar den här
    endpointen 404 eller 500 ska den timvisa synken ändå spara passen och
    wellness. Utan try/except hade en trasig hjälpdata stoppat huvuddatan.

    Ett HTTP-anrop per synk. Zoner ändras kanske en gång om året, men ett
    anrop bland de tiotal synken ändå gör är inte värt en egen
    tidsstämpel och en spärr att hålla korrekt.
    """
    try:
        rows = client.list_sport_settings()
    except Exception:
        log.warning("Kunde inte hämta sportinställningar — behåller de gamla.",
                    exc_info=True)
        return 0
    if not rows:
        log.info("Inga sportinställningar att synka.")
        return 0
    count = store.replace_sport_settings(rows)
    log.info("Synkade pulszoner för %d sporter.", count)
    return count


def remove_deleted_activities(
    store: Store, remote_ids: set[str], since: str
) -> list[str]:
    """Tar bort lagrade pass i fönstret som Intervals inte längre har.

    Synken lade till och uppdaterade pass men tog aldrig bort något. Ett
    dubblettpass du raderade eller slog ihop i Intervals låg kvar här för
    alltid: i passlistan, i veckovolymen och i varje analys underlag.

    Bara inom fönstret som listanropet täckte (`since` och framåt) — ett
    pass äldre än så finns inte i svaret, och att det saknas där säger
    ingenting om huruvida det finns kvar.

    Set loggade i chatten mot ett borttaget pass flyttas till dagens enda
    kvarvarande styrkepass, om det finns exakt ett. Det är det vanliga
    fallet: dubbletten försvann och originalet står kvar. Finns noll eller
    flera lämnas de okopplade, av samma skäl som
    AnalysisPipeline._find_strength_activity_id inte gissar.

    Returnerar id:n som togs bort.
    """
    borta = sorted(store.activity_ids_since(since) - remote_ids)
    for activity_id in borta:
        activity = store.get_activity(activity_id) or {}
        day = str(activity.get("start_time") or "")[:10]
        kvar = [
            a for a in (store.get_activities_for_date(day) if day else [])
            if str(a.get("id")) not in borta and is_strength_activity(a)
        ]
        flytta_till = str(kvar[0]["id"]) if len(kvar) == 1 else None
        store.delete_activity(activity_id, move_chat_sets_to=flytta_till)
    if borta:
        log.info(
            "Tog bort %d pass som inte längre finns i Intervals: %s.",
            len(borta), ", ".join(borta),
        )
    return borta


def sync_activities(client: IntervalsClient, store: Store, fetch_detail: bool = True) -> int:
    """Synkroniserar aktiviteter till SQLite. Returnerar antal uppdaterade.

    Detaljerna för ett pass hämtas bara när passet är nytt för oss eller
    när Intervals stämplat om det (icu_sync_date, som följer med redan i
    listsvaret). Detaljanropet är ett HTTP-anrop PER PASS, och det gjordes
    tidigare för varje pass i fönstret vid varje timvis synk — för att
    skriva tillbaka exakt samma rad. Det var också det som gjorde fönstret
    dyrt att bredda, och därmed indirekt orsaken till att retroaktivt
    uppladdade pass aldrig hittades (se _sync_window).
    """
    oldest = _sync_window(store)
    log.info("Hämtar aktiviteter från %s.", _to_date_str(oldest))
    activities = client.list_activities(oldest=oldest)
    if not activities:
        log.info("Inga aktiviteter att synka.")
        return 0

    known = store.activity_sync_dates(_to_date_str(oldest))

    count = 0
    unchanged = 0
    remote_ids: set[str] = set()
    for raw in activities:
        activity_id = str(raw.get("id") or raw.get("_id") or "")
        if not activity_id:
            continue
        remote_ids.add(activity_id)

        sync_date = raw.get("icu_sync_date")
        if sync_date and known.get(activity_id) == str(sync_date):
            # Oförändrat sedan förra synken: varken detaljhämtning eller
            # skrivning behövs. Saknar passet icu_sync_date faller vi
            # igenom till den fulla vägen — hellre ett onödigt anrop än
            # ett pass som tyst slutar uppdateras.
            unchanged += 1
            mapped = store.get_activity(activity_id) or {}
        else:
            detail = None
            if fetch_detail:
                try:
                    detail = client.get_activity(activity_id)
                # nätverks-/API-fel för ett enskilt pass ska inte bryta hela syncen
                except Exception as exc:
                    log.warning("Kunde inte hämta detalj för %s: %s", activity_id, exc)
            mapped = _map_activity(raw, detail)
            if not mapped["id"]:
                continue
            store.upsert_activity(mapped)
            count += 1

        # Styrkepass: hämta övningar och vikter ur originalfilen. Körs även
        # för oförändrade pass — import_strength_sets har egna spärrar och
        # gör ingenting om filen redan lästs, men ett försök som avbröts av
        # ett nätverksfel ska kunna göras om vid nästa synk.
        #
        # Ett fel här (nätverk, Strava-importerat pass, oväntat filformat)
        # ska inte bryta hela synken — passet i sig är redan sparat.
        if mapped and is_strength_activity(mapped):
            try:
                import_strength_sets(client, store, mapped)
            except Exception as exc:
                log.warning(
                    "Kunde inte importera styrkedata för %s: %s", activity_id, exc
                )

    # Efter loopen och bara här, med ett svar som faktiskt innehöll pass:
    # ett tomt svar ovan returnerar tidigt, och ett tomt svar går inte att
    # skilja från ett tillfälligt fel hos Intervals. Att radera allt i
    # fönstret på den grunden vore värre än en dubblett som står kvar.
    remove_deleted_activities(store, remote_ids, _to_date_str(oldest))

    log.info(
        "Synkade %d aktiviteter%s.",
        count,
        f" ({unchanged} oförändrade)" if unchanged else "",
    )
    return count


def sync_wellness(client: IntervalsClient, store: Store, full: bool = False) -> int:
    """Synkroniserar daglig wellness till SQLite. Returnerar antal uppdaterade.

    Rutinsynken hämtar bara WELLNESS_WINDOW_DAYS bakåt. Den hämtade
    tidigare alltid hela årshistoriken och skrev om varenda rad, varje
    timme, dygnet runt — uppmätt i journalen på Pi:n som "Synkade 366
    wellness-dagar" en gång i timmen, för att i praktiken uppdatera dagens
    rad och möjligen gårdagens. Aktivitetssynken hade redan ett sådant
    fönster (_sync_window); wellness saknade det.

    Fönstret är ändå rejält tilltaget: Intervals räknar om CTL/ATL/TSB
    bakåt när ett gammalt pass ändras, och sömn- och viktdata kan komma in
    med några dagars fördröjning. Två veckor täcker båda med marginal.

    `full=True` hämtar hela historiken igen — för första synken (databasen
    är tom) och för `sync --full` när man vill fylla i luckor bakåt.
    """
    if full or store.latest_wellness_day() is None:
        oldest = datetime.now() - timedelta(days=DEFAULT_HISTORY_DAYS)
    else:
        oldest = datetime.now() - timedelta(days=WELLNESS_WINDOW_DAYS)
    rows = client.list_wellness(oldest=oldest)
    if not rows:
        log.info("Ingen wellness-data att synka.")
        return 0

    mapped = []
    for raw in rows:
        day_raw = raw.get("day") or raw.get("date") or raw.get("id")
        day = day_raw[:10] if isinstance(day_raw, str) and len(day_raw) >= 10 else None
        if not day:
            continue
        mapped.append(_map_wellness(raw))

    # En transaktion för alla dagar i stället för en anslutning, två
    # PRAGMA:n och en transaktion per rad (se upsert_wellness_many).
    count = store.upsert_wellness_many(mapped)
    log.info("Synkade %d wellness-dagar (från %s).", count, _to_date_str(oldest))
    return count
