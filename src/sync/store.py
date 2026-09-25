"""SQLite-lager för aktiviteter, wellness och genererade analyser."""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    id TEXT PRIMARY KEY,
    name TEXT,
    type TEXT,
    sport TEXT,
    start_time TEXT,            -- ISO 8601 UTC
    duration_seconds INTEGER,
    distance_meters REAL,
    average_heart_rate INTEGER,
    max_heart_rate INTEGER,
    average_watts REAL,
    normalized_watts REAL,
    average_cadence REAL,
    average_speed REAL,
    tss REAL,
    intensity REAL,
    raw_json TEXT,              -- oförändrad Intervals-JSON (detalj)
    last_synced TEXT
);

CREATE TABLE IF NOT EXISTS wellness (
    day TEXT PRIMARY KEY,       -- YYYY-MM-DD
    rest_day INTEGER,
    ctl REAL,
    atl REAL,
    tsb REAL,
    ramp_rate REAL,
    sleep_seconds INTEGER,
    sleep_score INTEGER,
    sleep_quality INTEGER,
    avg_sleeping_hr INTEGER,
    weight REAL,
    resting_hr INTEGER,
    hrv REAL,
    hrv_sdnn REAL,
    stress INTEGER,
    respiration REAL,
    spO2 INTEGER,
    systolic INTEGER,
    diastolic INTEGER,
    hydration REAL,
    soreness INTEGER,
    fatigue REAL,
    mood INTEGER,
    motivation INTEGER,
    injury INTEGER,
    readiness INTEGER,
    vo2max REAL,
    steps INTEGER,
    kcal_consumed REAL,
    raw_json TEXT,
    last_synced TEXT
);

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_type TEXT NOT NULL,   -- 'daily_summary' | 'activity' | 'coaching'
    ref_id TEXT,                  -- activity id eller datum YYYY-MM-DD (eller NULL)
    created_at TEXT NOT NULL,
    model TEXT,
    markdown TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_analyses_type_ref
    ON analyses(analysis_type, ref_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activities_start ON activities(start_time DESC);

-- Självrapporterad data från chatten (vikt, sjukdom, alkohol, skada,
-- fri anteckning) som INTE kommer från Intervals.icu-synken. Egen tabell
-- (inte wellness) eftersom wellness skrivs över helt vid varje synk —
-- att lägga det där hade riskerat att synken raderar det du just
-- rapporterat. Append-only logg (inte upsert per dag) eftersom samma
-- kategori kan rapporteras flera gånger samma dag (t.ex. flera drinkar
-- under kvällen).
CREATE TABLE IF NOT EXISTS self_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,          -- YYYY-MM-DD, dagen rapporten gäller
    category TEXT NOT NULL,     -- 'weight' | 'illness' | 'alcohol' | 'injury' | 'mood' | 'note'
    value REAL,                 -- numeriskt värde om relevant (t.ex. vikt i kg)
    note TEXT,                  -- fritext, t.ex. "två öl till middag"
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_self_reports_day ON self_reports(day DESC, created_at DESC);

-- Styrketräning: övning, vikt och reps per set. Intervals.icu har inga
-- styrkefält alls i sitt API-schema (verifierat mot deras OpenAPI-spec:
-- inga properties heter reps/weight/exercise/sets), så den här datan kan
-- omöjligt komma från den vanliga aktivitetssynken — den loggas manuellt
-- via chatten (source='chat') eller parsas ur originalfilens FIT-poster
-- (source='fit').
--
-- En rad per SET, inte per övning: då fungerar både jämna set ("3x8 @ 80")
-- och set som droppar (8/7/5 reps), och formen matchar FIT-filens
-- set-meddelanden så båda källorna kan skriva till samma tabell.
CREATE TABLE IF NOT EXISTS strength_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,          -- YYYY-MM-DD, dagen passet utfördes
    activity_id TEXT,           -- kopplat Intervals-pass om vi kunde matcha, annars NULL
    exercise TEXT NOT NULL,     -- övningens namn, t.ex. "bänkpress"
    set_number INTEGER,         -- 1..n inom övningen
    reps INTEGER,
    weight_kg REAL,
    rpe REAL,                   -- upplevd ansträngning 1-10, valfritt
    note TEXT,
    source TEXT NOT NULL DEFAULT 'chat',   -- 'chat' | 'fit'
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_strength_sets_day ON strength_sets(day DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_strength_sets_activity ON strength_sets(activity_id);

-- Vilka pass vi redan hämtat och tittat i originalfilen för.
--
-- Synken avgjorde tidigare "har vi importerat det här passet?" genom att
-- fråga om det fanns rader i strength_sets. Det svaret är fel för ett pass
-- vars fil INTE innehåller några set (Strava-import, TCX-fil, eller ett
-- gympass där klockan inte loggade set): ingenting sparades, frågan
-- fortsatte svara nej, och filen laddades ner på nytt varje timme så länge
-- passet låg kvar i synkfönstret — trots att svaret aldrig kunde ändras.
--
-- Den här tabellen skiljer "inte tittat än" från "tittat, hittade inget".
-- sets_found=0 är alltså ett giltigt och slutgiltigt resultat.
CREATE TABLE IF NOT EXISTS strength_import_attempts (
    activity_id TEXT PRIMARY KEY,
    day TEXT,
    sets_found INTEGER NOT NULL,
    checked_at TEXT NOT NULL
);

-- Atletens pulszoner per sport, hämtade från Intervals sport-settings.
--
-- Varje pass bär redan sina egna zongränser i raw_json (icu_hr_zones) och
-- sin tid i varje zon (icu_hr_zone_times). Det som saknades var NAMNEN:
-- payloaden till Claude innehöll två parallella talarrayer
-- ([144,152,161,...] och [305,955,774,...]) medan prompten uttryckligen ber
-- om "tid i varje pulszon". Vilken zon som var vilken gick bara att gissa,
-- och gissningen syntes i analyserna.
--
-- Zonerna skiljer sig MELLAN sporter — löpningens zon 1 slutar vid 144,
-- cyklingens och styrkans vid 137 — så namnen måste slås upp på passets
-- typ och kan inte hårdkodas eller delas mellan pass.
--
-- Hela tabellen ersätts vid varje synk (se replace_sport_settings):
-- Intervals returnerar alltid samtliga sporter i ett svar, och en sport
-- som tagits bort där ska försvinna här också.
CREATE TABLE IF NOT EXISTS sport_settings (
    id TEXT PRIMARY KEY,          -- Intervals eget id för inställningen
    types TEXT NOT NULL,          -- JSON-lista, t.ex. ["Run","VirtualRun"]
    max_hr INTEGER,
    lthr INTEGER,                 -- tröskelpuls
    hr_zone_names TEXT,           -- JSON-lista, ett namn per zon
    last_synced TEXT NOT NULL
);

-- Schemalagda jobbs egna markörer: små värden som måste överleva en
-- omstart men som inte hör hemma i någon av datatabellerna.
--
-- Finns för att morgonanalysens spärr behöver minnas VILKET underlag den
-- senaste analysen byggde på. Den frågan gick tidigare att ställa mot
-- wellness.last_synced, men det fältet är en synktidsstämpel som skrivs
-- om vid varje upsert även när inget värde ändrats — se
-- _morning_data_snapshot i scheduler.py.
--
-- REGELN FÖR NYCKLAR: en nyckel per sorts markör, som skrivs över. Inte
-- en nyckel per dygn. Stegkontrollen lade in "evening_summary_steps:DAG"
-- och "evening_summary_recheck_done:DAG" — två rader per dygn, som
-- ingen läste igen efter morgonen därpå och ingenting tog bort. Behöver
-- en markör veta vilket dygn den gäller får dygnet ligga i värdet, som
-- _EVENING_UNDERLAG_KEY gör. Tabellen ska vara en handfull rader stor,
-- inte växa så länge tjänsten kör.
CREATE TABLE IF NOT EXISTS job_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


# Hur länge en skrivning väntar på att en annan ska bli klar innan den ger
# upp med "database is locked". sqlite3:s standardvärde är 5 sekunder.
#
# Appen har två skribenter som INTE kan se varandras trådlås, eftersom de
# är olika processer: webbservern (garmin-ai-pi.service, med sin egen
# schemalagda sync) och CLI:t (`python -m src.main sync`, import-strength
# och de andra), som körs medan tjänsten är igång. En sync som dessutom
# laddar ner och parsar FIT-filer för nya styrkepass håller anslutningen
# längre än 5 sekunder är bekvämt med. WAL-läget (se _conn) släpper igenom läsare parallellt; det
# här handlar bara om att en väntande SKRIVNING ska vänta ut den pågående
# istället för att kasta ett fel som ser ut som ett riktigt haveri.
_BUSY_TIMEOUT_SECONDS = 30.0


def _now_iso() -> str:
    return datetime.now().isoformat()


# Övningar som bytt svenskt namn i fit_strength._EXERCISE_VARIANT_NAMES_SV
# efter att rader redan skrivits. Tillämpas en gång per start i init().
# Ligger HÄR och inte i fit_strength för att hålla store fri från beroenden
# uppåt; namnen är redan normaliserade (gemener, se normalize_exercise).
RENAMED_EXERCISES = {
    # Klockan kallar atletens knäböj "barbell_hack_squat". Samma lyft —
    # det översätts numera till "knäböj", och de gamla raderna följer med
    # dit så att serien inte delas i två.
    "hack squat": "knäböj",
}


def normalize_exercise(name: str) -> str:
    """Övningens namn i jämförbar form: trimmat, gement, ett mellanslag.

    Namnet är fritext från två källor som inte kan komma överens av sig
    själva. FIT-importen bygger det ur Garmins enum och får alltid
    "marklyft"; chatten får det av Claude, som ombeds skriva gemener men
    lika gärna kan skicka "Marklyft" i början av en mening.

    Utan normalisering är de två olika övningar överallt: dubbelloggs-
    spärren i pipeline.py jämför strängarna rakt av och släpper igenom den
    andra stavningen (3 600 kg volym där 1 800 lyftes), fmt_strength_sessions
    grupperar dem var för sig, och get_strength_history matchar med
    `exercise IN (...)` och hittar aldrig historiken till den ena.

    Gemener i PYTHON och inte i SQL: SQLites inbyggda lower() rör bara
    ASCII, så "KNÄBÖJ" hade blivit "knÄbÖj". Svenska övningsnamn är fulla
    av å, ä och ö.
    """
    return " ".join(str(name).split()).lower()


# En rättelse gäller en övning en bestämd dag: antingen stryks den, eller
# så byter den namn. None betyder stryk.
StrengthCorrections = dict[tuple[str, str], str | None]


def parse_strength_corrections(entries: Iterable[str]) -> StrengthCorrections:
    """Tolkar rättelser på formen "2026-05-05:bänkpress[=nytt namn]".

    Klockan skriver ibland ner något annat än det som hände. Två fall,
    båda verkliga och båda funna genom att atleten läste sitt eget kort:

    1. Ett programmerat pass fyller i den FÖRESKRIVNA vikten när steget
       aldrig justeras. Bänkpressen 5 och 14 maj 2026 blev åtta identiska
       set om 3 × 120 kg — 50 % över atletens 35 andra bänkpressdagar,
       som alla ligger mellan 40 och 80 kg. Den vikten låg aldrig på en
       stång, men den blev personligt rekord, eftersom ett rekord är ett
       maxvärde och därmed maximalt känsligt för en enda felaktig siffra.
    2. Programsteget bär fel ÖVNING. De två sista marklyftsseten 21 april
       2026 märktes straight_leg_deadlift (FIT-subtyp 25) mitt i ett
       block med vanligt marklyft på samma vikt — 120 kg, medan atletens
       raka marklyft annars ligger på 40–70.

    Därför konfiguration och inte kod. En automatisk avvikelserensning
    hade varit fel verktyg: ett verkligt personbästa ÄR en avvikelse, och
    ett filter som slänger 120 kg idag slänger nästa riktiga rekord i
    morgon. Bara den som lyfte vet vad som hände.

    Poster som inte går att tolka faller tyst bort. Listan skrivs för
    hand i .env, och en felskriven rad ska inte hindra appen från att
    starta — de rätta raderna gör fortfarande sitt.
    """
    corrections: StrengthCorrections = {}
    for entry in entries:
        dag, _, resten = entry.partition(":")
        if not resten.strip():
            continue
        ovning, likhetstecken, nytt = resten.partition("=")
        ovning = normalize_exercise(ovning)
        if not dag.strip() or not ovning:
            continue
        # "dag:övning" stryker; "dag:övning=namn" döper om. Ett tomt namn
        # efter likhetstecknet stryker också — "=" utan fortsättning är
        # rimligast att läsa som "det här ska bort".
        nytt_namn = normalize_exercise(nytt) if likhetstecken else ""
        corrections[(dag.strip(), ovning)] = nytt_namn or None
    return corrections


def _cutoff_day(days: int) -> str:
    """Första dagen i ett fönster på `days` dagar som slutar idag.

    days=7 ger alltså dagens datum minus sex dagar — sju kalenderdagar
    inklusive idag, inte åtta. list_self_reports och list_strength_sets
    räknade tidigare `today - days`, vilket gav en dag för mycket.
    """
    return (datetime.now().date() - timedelta(days=max(0, days - 1))).isoformat()


class Store:
    """Tunt lager ovanpå sqlite3. En anslutning per anrop för trådsäkerhet."""

    def __init__(
        self,
        db_path: Path | str,
        strength_corrections: Iterable[str] = (),
    ) -> None:
        self.db_path = str(db_path)
        # Se parse_strength_corrections. Tillämpas på VÄGEN IN, i
        # add_strength_sets, och en gång på befintliga rader i init().
        # Att rätta vid skrivning och inte vid läsning är det som gör att
        # rättelsen överlever en omsynk: raderna skrivs om varje gång ett
        # styrkepass importeras på nytt, och ett filter på läsvägen hade
        # dessutom behövt sitta på sex ställen där ett kunde glömmas.
        self._strength_corrections = parse_strength_corrections(strength_corrections)

    @contextmanager
    def _conn(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """En anslutning med en transaktion runt blocket.

        `immediate=True` tar skrivlåset direkt, INNAN blocket kör sin
        första sats. Det behövs så fort ett block läser ett värde och
        skriver utifrån det (läs-modifiera-skriv), för annars är läsningen
        oskyddad: sqlite3 kör i sitt äldre läge (isolation_level=""), och
        där börjar en transaktion först vid INSERT/UPDATE/DELETE — en
        ensam SELECT går i autocommit. Uppmätt: conn.in_transaction är
        False efter SELECT och True först efter INSERT.

        Två skrivare kan alltså läsa samma värde och båda skriva utifrån
        det. Appen har dem: chattens _log_strength_session (webbtråd) och
        FIT-importen i sync_activities (schemaläggartråd), plus
        systemd-timern som är en helt egen process.

        Väntan på skrivlåset täcks av _BUSY_TIMEOUT_SECONDS, samma som för
        alla andra skrivningar.
        """
        conn = sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        # NORMAL, inte SQLites standard FULL. I WAL-läge betyder FULL att
        # varje commit fsync:ar WAL-filen; NORMAL låter operativsystemet
        # skriva ut den och synkar först vid checkpoint. Skillnaden i
        # hållbarhet är att ett STRÖMAVBROTT kan tappa de sista
        # transaktionerna — en processkrasch kan det inte, WAL:en är
        # fortfarande konsistent. Appen kör på ett SD-kort, tar en
        # backup varje natt, och synkar om det som ändå kommer från
        # Intervals. Att fsync:a varje timvis synk för den risken är fel
        # avvägning: det är slitage utan motsvarande nytta.
        conn.execute("PRAGMA synchronous=NORMAL;")
        # Här stod PRAGMA foreign_keys=ON, körd vid varje anslutning.
        # Schemat har noll främmande nycklar (verifierat mot
        # PRAGMA foreign_key_list för samtliga tabeller), så den gjorde
        # ingenting alls. Lägger någon till en FOREIGN KEY måste raden
        # tillbaka — SQLite har den avstängd som standard.
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Lägg till kolumner som saknas i äldre databaser (idempotent)."""
        existing = {r[1] for r in conn.execute("PRAGMA table_info(wellness)").fetchall()}
        additions = {
            "ramp_rate": "REAL",
            "sleep_score": "INTEGER",
            "sleep_quality": "INTEGER",
            "avg_sleeping_hr": "INTEGER",
            "resting_hr": "INTEGER",
            "hrv": "REAL",
            "hrv_sdnn": "REAL",
            "stress": "INTEGER",
            "respiration": "REAL",
            "spO2": "INTEGER",
            "systolic": "INTEGER",
            "diastolic": "INTEGER",
            "hydration": "REAL",
            "soreness": "INTEGER",
            "motivation": "INTEGER",
            "injury": "INTEGER",
            "vo2max": "REAL",
            "steps": "INTEGER",
            "kcal_consumed": "REAL",
        }
        for col, coltype in additions.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE wellness ADD COLUMN {col} {coltype}")

        # Normalisera övningsnamn som skrevs innan add_strength_sets
        # gjorde det. Görs i Python och inte med SQL:s lower(), som bara
        # rör ASCII — se normalize_exercise.
        namn = [r[0] for r in conn.execute(
            "SELECT DISTINCT exercise FROM strength_sets"
        ).fetchall()]
        for gammalt in namn:
            nytt = normalize_exercise(gammalt)
            if nytt != gammalt:
                conn.execute(
                    "UPDATE strength_sets SET exercise=? WHERE exercise=?",
                    (nytt, gammalt),
                )

        # Döp om övningar som bytt namn i översättningstabellen. Utan den
        # här raden ligger gamla rader kvar under sitt gamla namn medan
        # nya skrivs under det nya, och progressionen delas i två serier
        # som var för sig ser ut att stanna av. En omsynk är inget svar:
        # ett pass från maj hämtas aldrig mer.
        for gammalt, nytt in RENAMED_EXERCISES.items():
            conn.execute(
                "UPDATE strength_sets SET exercise=? WHERE exercise=?",
                (nytt, gammalt),
            )

        # Rätta styrkeset som redan ligger i tabellen. Behövs utöver
        # spärren i add_strength_sets: de felaktiga raderna skrevs innan
        # rättelsen fanns, och att vänta på att passen synkas om igen är
        # inget svar — ett pass från maj hämtas aldrig mer.
        for (dag, ovning), rattat_namn in self._strength_corrections.items():
            if rattat_namn is None:
                conn.execute(
                    "DELETE FROM strength_sets WHERE day=? AND exercise=?",
                    (dag, ovning),
                )
            else:
                conn.execute(
                    "UPDATE strength_sets SET exercise=? WHERE day=? AND exercise=?",
                    (rattat_namn, dag, ovning),
                )

        # Numrera om set som fått samma set_number två gånger. Körs EFTER
        # rättelserna ovan, för det är de som skapar dubbletterna: två av
        # klockans poster döps om till samma övning, och båda numrerades
        # från 1 av den add_strength_sets som gällde då. Se den metoden
        # för hur nya rader numreras.
        dubblerade = {
            (r["day"], r["exercise"], r["activity_id"])
            for r in conn.execute(
                """
                SELECT day, exercise, activity_id FROM strength_sets
                GROUP BY day, exercise, activity_id, set_number
                HAVING COUNT(*) > 1
                """
            ).fetchall()
        }
        for dag, ovning, pass_id in sorted(dubblerade, key=lambda t: (t[0], t[1])):
            rader = conn.execute(
                """
                SELECT id FROM strength_sets
                WHERE day=? AND exercise=? AND activity_id IS ?
                ORDER BY created_at, set_number, id
                """,
                (dag, ovning, pass_id),
            ).fetchall()
            conn.executemany(
                "UPDATE strength_sets SET set_number=? WHERE id=?",
                [(nr, r["id"]) for nr, r in enumerate(rader, start=1)],
            )

        # Flytta schemaläggarens gamla en-nyckel-per-dygn-markörer till en
        # rad var. Stegkontrollen skrev "evening_summary_steps:DAG" och
        # "evening_summary_recheck_done:DAG" — två rader per dygn som ingen
        # tog bort och ingen läste igen efter morgonen därpå. Se REGELN FÖR
        # NYCKLAR i SCHEMA och _EVENING_UNDERLAG_KEY i scheduler.py.
        #
        # Den nyaste av varje sort flyttas över i stället för att bara
        # slängas: kontrollen kör 09:00 och läser gårdagens markör, så en
        # uppgradering klockan åtta hade annars raderat själva underlaget
        # den skulle jämföra mot och tappat dagens kontroll helt.
        for prefix, ny_nyckel in (
            ("evening_summary_steps:", "evening_summary_underlag"),
            ("evening_summary_recheck_done:", "evening_summary_recheck_done"),
        ):
            gamla = conn.execute(
                "SELECT key, value FROM job_state WHERE key LIKE ? ORDER BY key DESC",
                (prefix + "%",),
            ).fetchall()
            if not gamla:
                continue
            redan_flyttad = conn.execute(
                "SELECT 1 FROM job_state WHERE key=?", (ny_nyckel,)
            ).fetchone()
            if not redan_flyttad:
                nyaste = gamla[0]
                # Dygnet satt i nyckeln och ska nu ligga i värdet.
                # Done-markören höll en naken tidsstämpel och inte JSON
                # alls, så den ger bara ett dygn — vilket är allt som läses.
                try:
                    varde = json.loads(nyaste["value"])
                except ValueError:
                    varde = {}
                if not isinstance(varde, dict):
                    varde = {}
                conn.execute(
                    "INSERT INTO job_state (key, value, updated_at) VALUES (?, ?, ?)",
                    (
                        ny_nyckel,
                        json.dumps({"day": nyaste["key"][len(prefix):], **varde}),
                        _now_iso(),
                    ),
                )
            conn.executemany(
                "DELETE FROM job_state WHERE key=?", [(r["key"],) for r in gamla]
            )

        # Fyll i TSB där det saknas (TSB = CTL - ATL).
        conn.execute(
            """
            UPDATE wellness
            SET tsb = ROUND(COALESCE(ctl, 0) - COALESCE(atl, 0), 2)
            WHERE tsb IS NULL AND ctl IS NOT NULL AND atl IS NOT NULL
            """
        )

    # --- Aktiviteter -----------------------------------------------------
    def upsert_activity(self, a: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO activities (
                    id, name, type, sport, start_time, duration_seconds, distance_meters,
                    average_heart_rate, max_heart_rate, average_watts, normalized_watts,
                    average_cadence, average_speed, tss, intensity, raw_json, last_synced
                ) VALUES (
                    :id, :name, :type, :sport, :start_time, :duration_seconds, :distance_meters,
                    :average_heart_rate, :max_heart_rate, :average_watts, :normalized_watts,
                    :average_cadence, :average_speed, :tss, :intensity, :raw_json, :last_synced
                )
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, type=excluded.type, sport=excluded.sport,
                    start_time=excluded.start_time, duration_seconds=excluded.duration_seconds,
                    distance_meters=excluded.distance_meters,
                    average_heart_rate=excluded.average_heart_rate,
                    max_heart_rate=excluded.max_heart_rate,
                    average_watts=excluded.average_watts,
                    normalized_watts=excluded.normalized_watts,
                    average_cadence=excluded.average_cadence,
                    average_speed=excluded.average_speed, tss=excluded.tss,
                    intensity=excluded.intensity, raw_json=excluded.raw_json,
                    last_synced=excluded.last_synced
                """,
                a,
            )

    def list_activities(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM activities ORDER BY start_time DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_activity(self, activity_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
            return dict(row) if row else None

    def latest_activity_start(self) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT MAX(start_time) AS s FROM activities").fetchone()
            return row["s"] if row and row["s"] else None

    def activity_sync_dates(self, since: str) -> dict[str, str]:
        """id -> icu_sync_date för lagrade pass som startat på/efter `since`.

        Intervals stämplar varje pass med icu_sync_date, och fältet följer
        med redan i LIST-svaret. Synken kan därför avgöra vilka pass som
        faktiskt ändrats sedan förra körningen utan att hämta detaljerna
        för vart och ett — se sync_activities.

        En fråga för hela fönstret i stället för en get_activity per pass:
        synken kör varje timme och fönstret rymmer ett tiotal pass.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, json_extract(raw_json, '$.icu_sync_date') AS synced
                FROM activities WHERE start_time >= ?
                """,
                (since,),
            ).fetchall()
            return {str(r["id"]): str(r["synced"]) for r in rows if r["synced"]}

    def activity_ids_since(self, since: str) -> set[str]:
        """Id för alla lagrade pass som startat på/efter `since`.

        Samma fönster som activity_sync_dates, men utan krav på att passet
        bär en icu_sync_date — frågan här är vilka pass vi HAR, inte vilka
        som ändrats. Se remove_deleted_activities i intervals_client.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id FROM activities WHERE start_time >= ?", (since,)
            ).fetchall()
            return {str(r["id"]) for r in rows}

    def delete_activity(
        self, activity_id: str, move_chat_sets_to: str | None = None
    ) -> None:
        """Tar bort ett pass som inte längre finns i Intervals.

        Styrkeset importerade ur passets FIT-fil försvinner med det, liksom
        noteringen om att filen lästs: de beskrev en fil som inte längre
        finns. Set du loggat i chatten ligger kvar — de är det enda
        exemplaret av något du sagt — och flyttas till `move_chat_sets_to`,
        eller lämnas okopplade (activity_id NULL) som set loggade innan ett
        pass synkats.

        Passets analyser rörs inte. De går inte att nå utan passet, men de
        är betalda och ligger kvar i databasen och i nattens backup om
        passet skulle komma tillbaka.

        En transaktion: ett avbrott halvvägs ska inte lämna set som pekar
        på ett pass som inte finns.
        """
        with self._conn(immediate=True) as conn:
            conn.execute(
                """
                UPDATE strength_sets SET activity_id=?
                WHERE activity_id=? AND COALESCE(source, 'chat') != 'fit'
                """,
                (move_chat_sets_to, activity_id),
            )
            conn.execute(
                "DELETE FROM strength_sets WHERE activity_id=? AND source='fit'",
                (activity_id,),
            )
            conn.execute(
                "DELETE FROM strength_import_attempts WHERE activity_id=?",
                (activity_id,),
            )
            conn.execute("DELETE FROM activities WHERE id=?", (activity_id,))

    # --- Wellness --------------------------------------------------------
    # SQL:en ligger som konstant och inte inuti metoden nedan: den
    # delades tidigare med en enradsvariant, och en bulkvariant hade
    # annars behövt en andra kopia av 30 kolumnnamn.
    _UPSERT_WELLNESS_SQL = """
                INSERT INTO wellness (
                    day, rest_day, ctl, atl, tsb, ramp_rate, sleep_seconds, sleep_score,
                    sleep_quality, avg_sleeping_hr, weight, resting_hr, hrv, hrv_sdnn,
                    stress, respiration, spO2, systolic, diastolic, hydration, soreness,
                    fatigue, mood, motivation, injury, readiness, vo2max, steps,
                    kcal_consumed, raw_json, last_synced
                ) VALUES (
                    :day, :rest_day, :ctl, :atl, :tsb, :ramp_rate, :sleep_seconds, :sleep_score,
                    :sleep_quality, :avg_sleeping_hr, :weight, :resting_hr, :hrv, :hrv_sdnn,
                    :stress, :respiration, :spO2, :systolic, :diastolic, :hydration, :soreness,
                    :fatigue, :mood, :motivation, :injury, :readiness, :vo2max, :steps,
                    :kcal_consumed, :raw_json, :last_synced
                )
                ON CONFLICT(day) DO UPDATE SET
                    rest_day=excluded.rest_day, ctl=excluded.ctl, atl=excluded.atl,
                    tsb=excluded.tsb, ramp_rate=excluded.ramp_rate,
                    sleep_seconds=excluded.sleep_seconds, sleep_score=excluded.sleep_score,
                    sleep_quality=excluded.sleep_quality,
                    avg_sleeping_hr=excluded.avg_sleeping_hr, weight=excluded.weight,
                    resting_hr=excluded.resting_hr, hrv=excluded.hrv,
                    hrv_sdnn=excluded.hrv_sdnn, stress=excluded.stress,
                    respiration=excluded.respiration, spO2=excluded.spO2,
                    systolic=excluded.systolic, diastolic=excluded.diastolic,
                    hydration=excluded.hydration, soreness=excluded.soreness,
                    fatigue=excluded.fatigue, mood=excluded.mood,
                    motivation=excluded.motivation, injury=excluded.injury,
                    readiness=excluded.readiness, vo2max=excluded.vo2max,
                    steps=excluded.steps, kcal_consumed=excluded.kcal_consumed,
                    raw_json=excluded.raw_json, last_synced=excluded.last_synced
                """

    def upsert_wellness_many(self, rows: list[dict[str, Any]]) -> int:
        """Skriver wellness-dagar i EN transaktion. Returnerar antalet.

        Enda vägen in i wellness-tabellen. Synken skrev tidigare rad för
        rad via en enradsvariant, och varje anrop öppnade en egen
        SQLite-anslutning, satte två PRAGMA:n, körde en transaktion och
        stängde igen. Med 366 dagar per synk och en synk i timmen blev det
        ~8 800 anslutningar och transaktioner per dygn — uppmätt i
        journalen på Pi:n — för att i praktiken uppdatera en handfull
        rader. På ett SD-kort är det slitage utan nytta.

        Enradsvarianten blev kvar efteråt, oanvänd av allt utom testerna,
        med signaturen upsert_wellness(day, w) — där `day` aldrig lästes.
        Dagen kommer ur raden själv (`:day` i SQL:en nedan), så anropet
        såg ut att bestämma något det inte bestämde: skickade ett test in
        en dag som inte matchade radens hade raden ändå hamnat på radens
        datum, tyst. Testerna bygger sina rader via upsert_wellness_many
        nu, samma väg som produktionen.
        """
        if not rows:
            return 0
        with self._conn() as conn:
            conn.executemany(self._UPSERT_WELLNESS_SQL, rows)
        return len(rows)

    def latest_weight(self) -> tuple[float, str] | None:
        """Senast kända vikt som (kg, dag), eller None om ingen finns.

        Väger samman båda källorna och tar den färskaste: wellness.weight
        (synkas från Intervals, t.ex. från en uppkopplad våg) och
        self_reports med kategorin 'weight' (det du själv sagt i chatten).
        Vilken som är nyast går inte att veta i förväg — vägde du dig och
        berättade det i chatten är chattvärdet färskast, annars vågens.

        Används för atletprofilen i systemprompten, som annars hade en
        fast siffra oavsett hur vikten faktiskt utvecklats.

        Vid SAMMA dag vinner chattvärdet. Sorteringen var tidigare bara
        `ORDER BY day DESC`, utan något att skilja två träffar från samma
        dag åt — vilket i praktiken lät wellness-raden vinna, eftersom den
        kommer först i UNION:en. Rapporterade man en ny vikt i chatten
        samma dag som vågen synkat ett äldre värde gick alltså den nyare
        siffran förlorad, tvärtemot vad den här metoden utger sig för att
        göra. Vågen mäter på morgonen; säger man något i chatten är det
        med i stort sett alltid senare på dagen.

        Vid samma dag OCH samma källa avgör tidsstämpeln. Utan den tredje
        nyckeln lämnade frågan valet mellan två viktrapporter samma dag
        osagt, och svaret kom att bero på vilken ordning SQLite råkade
        läsa raderna i — vilket i praktiken blev rätt, men bara därför att
        idx_self_reports_day sorterar på created_at DESC. Ett index är
        ingen plats att gömma ett affärsregelbeslut i: byts eller stryks
        det ändras svaret tyst.

        `typeof(...)` och inte bara IS NOT NULL: SQLite har dynamiska
        typer, så en REAL-kolumn lagrar "82 kg" som TEXT utan att knota.
        float() på den raden kastade — och den här metoden anropas från
        AnalysisPipeline.system_prompt(), alltså av varenda analys OCH
        varje chattmeddelande. En enda sådan rad gjorde hela appen
        obrukbar, och eftersom chatten låg nere gick den inte att rätta
        därifrån heller. Verktygen validerar numera på vägen in (se
        _tool_number i analysis/pipeline.py); det här är spärren för
        rader som redan hunnit skrivas.
        """
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT weight AS w, day, 0 AS from_chat, last_synced AS at
                    FROM wellness
                    WHERE typeof(weight) IN ('integer', 'real')
                UNION ALL
                SELECT value AS w, day, 1 AS from_chat, created_at AS at
                    FROM self_reports
                    WHERE category = 'weight'
                      AND typeof(value) IN ('integer', 'real')
                ORDER BY day DESC, from_chat DESC, at DESC LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            return float(row["w"]), str(row["day"])

    def list_wellness(self, days: int = 90) -> list[dict[str, Any]]:
        """Senaste N dagarnas wellness, nyast först.

        Filtrerar på datum, inte LIMIT N rader. Skillnaden märks så fort
        det finns luckor i datan: med LIMIT gav days=7 de sju senaste
        raderna som fanns, vilket kunde spänna över tio kalenderdagar och
        alltså visa ett annat tidsfönster än det som stod i diagrammets
        rubrik. Samma semantik som list_self_reports och
        list_strength_sets.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM wellness WHERE day >= ? ORDER BY day DESC",
                (_cutoff_day(days),),
            ).fetchall()
            return [dict(r) for r in rows]

    # --- Analyser --------------------------------------------------------
    def save_analysis(
        self, analysis_type: str, markdown: str, ref_id: str | None, model: str
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO analyses (analysis_type, ref_id, created_at, model, markdown)
                VALUES (?, ?, ?, ?, ?)
                """,
                (analysis_type, ref_id, _now_iso(), model, markdown),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def latest_analysis(
        self, analysis_type: str, ref_id: str | None = None
    ) -> dict[str, Any] | None:
        with self._conn() as conn:
            if ref_id is not None:
                row = conn.execute(
                    """
                    SELECT * FROM analyses
                    WHERE analysis_type=? AND ref_id=?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (analysis_type, ref_id),
                ).fetchone()
            else:
                # Returnera senaste analysen av denna typ, oavsett ref_id.
                row = conn.execute(
                    """
                    SELECT * FROM analyses
                    WHERE analysis_type=?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (analysis_type,),
                ).fetchone()
            return dict(row) if row else None

    def latest_analysis_before(
        self, analysis_type: str, day: str
    ) -> dict[str, Any] | None:
        """Senaste analysen av en typ som gäller en dag FÖRE `day`.

        Finns för att kvällssammanfattningen ska kunna falla tillbaka på
        gårdagens när dagens inte hunnit få något underlag — se
        api_evening_analysis i web/app.py. Sorterar på ref_id, inte på
        created_at: det är vilken DAG analysen handlar om som avgör vilken
        som är närmast i tiden, inte när den råkade skrivas.
        """
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM analyses
                WHERE analysis_type=? AND ref_id IS NOT NULL AND ref_id < ?
                ORDER BY ref_id DESC, created_at DESC LIMIT 1
                """,
                (analysis_type, day),
            ).fetchone()
            return dict(row) if row else None

    def latest_activity_analysis(self, activity_id: str) -> dict[str, Any] | None:
        return self.latest_analysis("activity", activity_id)

    def get_wellness_day(self, day: str) -> dict[str, Any] | None:
        """Hämta wellness för en specifik dag (YYYY-MM-DD)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM wellness WHERE day=?", (day,)
            ).fetchone()
            return dict(row) if row else None

    def latest_wellness_day(self) -> dict[str, Any] | None:
        """Senaste wellness-raden som finns, oavsett vilken dag det är.

        Används som fallback när dagens synk ännu inte kommit in: hellre
        gårdagens siffror än en dashboard full av "-" fram till dess att
        synkjobbet kört (var 60:e minut, se scheduler.py). get_wellness_day
        kräver ett exakt datum och ger None så fort dagens rad saknas —
        den här frågar inte efter en specifik dag alls.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM wellness ORDER BY day DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def get_activities_for_date(self, day: str) -> list[dict[str, Any]]:
        """Hämta aktiviteter som startade ett visst datum (YYYY-MM-DD)."""
        with self._conn() as conn:
            # start_time är ISO 8601; jämför datum-prefixet.
            pattern = day + "%"
            rows = conn.execute(
                "SELECT * FROM activities WHERE start_time LIKE ? ORDER BY start_time",
                (pattern,),
            ).fetchall()
            return [dict(r) for r in rows]

    # --- Självrapporterad data (vikt/sjukdom/alkohol/skada/anteckning) ---
    def add_self_report(
        self, day: str, category: str, value: float | None = None, note: str | None = None
    ) -> int:
        """Loggar en självrapporterad uppgift. Append-only — flera per dag+kategori är ok."""
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO self_reports (day, category, value, note, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (day, category, value, note, _now_iso()),
            )
            assert cur.lastrowid is not None
            return int(cur.lastrowid)

    def list_self_reports(self, days: int = 30) -> list[dict[str, Any]]:
        """Senaste N dagarnas självrapporter, nyast först — för historik/trender."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM self_reports WHERE day >= ?
                ORDER BY day DESC, created_at DESC
                """,
                (_cutoff_day(days),),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_self_reports_for_date(self, day: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM self_reports WHERE day=? ORDER BY created_at",
                (day,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_self_report(self, report_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM self_reports WHERE id=?", (report_id,)
            ).fetchone()
            return dict(row) if row else None

    # Vilka fält en självrapport går att rätta i efterhand. En vitlista och
    # inte fria **kwargs: nycklarna byggs in i SET-satsen, och en osållad
    # sådan är en injektionsväg. created_at står medvetet utanför — den
    # säger när uppgiften registrerades, inte vad den påstår.
    _EDITABLE_SELF_REPORT_FIELDS = ("day", "category", "value", "note")

    def update_self_report(self, report_id: int, **fields: Any) -> bool:
        """Rättar en tidigare rapporterad uppgift. True om raden fanns.

        Tabellen var append-only, vilket räckte så länge den bara skrevs
        av chatten och aldrig lästes tillbaka av någon som ville ändra
        sig. Men en felaktig rad går inte att ta tillbaka: fel vikt eller
        fel dag följer med i morgon- och kvällsanalysen i fjorton dagar
        och i träningsanalysen i trettio, och det enda botemedlet var
        sqlite3 på kommandoraden.

        Bara de fält som faktiskt skickas med skrivs om. Att utelämna ett
        fält betyder "lämna som det är", inte "sätt till null" — annars
        hade en rättning av vikten raderat anteckningen bredvid.
        """
        updates = {
            key: value
            for key, value in fields.items()
            if key in self._EDITABLE_SELF_REPORT_FIELDS
        }
        if not updates:
            return False
        assignments = ", ".join(f"{key}=?" for key in updates)
        with self._conn() as conn:
            cur = conn.execute(
                f"UPDATE self_reports SET {assignments} WHERE id=?",
                (*updates.values(), report_id),
            )
            return cur.rowcount > 0

    def delete_self_report(self, report_id: int) -> bool:
        """Tar bort en självrapport. True om raden fanns.

        För det som inte går att rätta till något vettigt — en uppgift som
        aldrig skulle ha loggats alls.
        """
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM self_reports WHERE id=?", (report_id,))
            return cur.rowcount > 0

    # --- Styrketräning (övning/vikt/reps per set) ------------------------
    def resolved_exercise(self, day: str, exercise: str) -> str | None:
        """Namnet en övning FAKTISKT får i tabellen en viss dag.

        Alltså efter normalisering (se normalize_exercise) och efter
        rättelselistan (se parse_strength_corrections). None betyder att
        övningen stryks den dagen och inte skrivs alls.

        Ligger som en egen metod därför att två anropare utanför
        add_strength_sets behöver samma svar INNAN raderna finns:

        - import_strength_sets rensar chattrader för de övningar
          klockans fil täcker, och måste veta vilket namn de ligger under.
        - chattens kvittens ska säga vad som sparades, inte vad Claude
          skickade in. Den sa tidigare "Sparat: raka marklyft" om rader
          som hamnade under "marklyft", och "Sparat: 0 set, volym 1920 kg"
          om en övning rättelselistan strök helt.
        """
        namn = normalize_exercise(exercise)
        if (day, namn) in self._strength_corrections:
            return self._strength_corrections[(day, namn)]
        return namn

    def add_strength_sets(
        self,
        day: str,
        exercise: str,
        sets: list[dict[str, Any]],
        activity_id: str | None = None,
        note: str | None = None,
        source: str = "chat",
    ) -> int:
        """Loggar en övnings alla set. Returnerar antal sparade rader.

        `sets` är en lista i utförd ordning, ett element per set, med de
        valfria nycklarna reps, weight_kg och rpe. set_number sätts här
        istället för att lita på att anroparen numrerar rätt, och räknas
        vidare från de set övningen redan har den dagen — inte om från 1.

        Att numrera om från 1 vid varje anrop gav dubbletter så fort samma
        övning skrevs i två omgångar. Skarpt fall 2026-04-21: klockan hade
        loggat två poster som en rättelse i .env döpte om till samma namn,
        FIT-importen anropade den här metoden en gång per post, och
        aktivitetssidan visade set 1, 2, 1, 2 för marklyft. Numret ska
        svara på "vilket set i ordningen", och det svaret finns inte i ett
        enskilt anrop. Befintliga rader rättas en gång i _migrate.

        `exercise` normaliseras (se normalize_exercise) så att klockans
        "marklyft" och chattens "Marklyft" blir samma övning.
        """
        if not sets:
            return 0
        # Namnet löses HÄR och inte hos anroparen: det här är den enda
        # vägen in i tabellen, och invarianten "en övning har ett namn"
        # ska gälla oavsett vem som skriver. Se resolved_exercise.
        rattat = self.resolved_exercise(day, exercise)
        if rattat is None:
            return 0
        exercise = rattat
        now = _now_iso()
        # immediate=True: uppslaget nedan och insättningen måste ligga i
        # samma transaktion, annars kan två samtidiga skrivare läsa samma
        # maxvärde och båda skriva från N+1 — exakt de dubbletter
        # _migrate finns för att städa. Se _conn för varför en vanlig
        # SELECT inte räcker.
        with self._conn(immediate=True) as conn:
            # Räknas per pass och inte per dag, för att omimporten ska
            # landa på samma nummer varje gång: FIT-importen raderar bara
            # sina EGNA rader för passet (se
            # delete_strength_sets_for_activity), så ett annat pass samma
            # dag hade annars puttat numren uppåt vid varje omsynk.
            # `IS ?` och inte `= ?` — activity_id är NULL för set loggade
            # i chatten utan pass att koppla till, och NULL = NULL är
            # aldrig sant i SQL.
            hittills = conn.execute(
                """
                SELECT COALESCE(MAX(set_number), 0) AS n FROM strength_sets
                WHERE day=? AND exercise=? AND activity_id IS ?
                """,
                (day, exercise, activity_id),
            ).fetchone()["n"]
            rows = [
                (
                    day,
                    activity_id,
                    exercise,
                    index,
                    s.get("reps"),
                    s.get("weight_kg"),
                    s.get("rpe"),
                    note,
                    source,
                    now,
                )
                for index, s in enumerate(sets, start=int(hittills) + 1)
            ]
            conn.executemany(
                """
                INSERT INTO strength_sets (
                    day, activity_id, exercise, set_number, reps, weight_kg,
                    rpe, note, source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def list_strength_sets(self, days: int = 30) -> list[dict[str, Any]]:
        """Senaste N dagarnas styrkeset, nyast först — för historik/progression."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM strength_sets WHERE day >= ?
                ORDER BY day DESC, created_at DESC, set_number
                """,
                (_cutoff_day(days),),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_strength_sets_for_date(self, day: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM strength_sets WHERE day=? ORDER BY created_at, set_number",
                (day,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_strength_sets_for_activity(self, activity_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM strength_sets WHERE activity_id=? ORDER BY created_at, set_number",
                (activity_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_strength_history(
        self,
        exercises: list[str],
        before_day: str,
        sessions_per_exercise: int = 8,
    ) -> list[dict[str, Any]]:
        """Tidigare set för utvalda övningar, till progressionsjämförelser.

        Fönstret räknas i PASS per övning, inte i kalenderdagar. Ett tak i
        dagar ger ojämn historik: knäböj körs var femte dag och skulle få
        gott om jämförelsepunkter, medan olympiska lyft (9 pass på ett år)
        oftast skulle falla utanför fönstret helt och aldrig gå att bedöma.
        De N senaste passen med samma övning finns alltid, hur glest den än
        körs.

        `before_day` utesluts (>, inte >=): passet som analyseras skickas
        redan med som sina egna set, och samma siffror två gånger i samma
        payload läser Claude som två pass.
        """
        if not exercises or not before_day:
            return []
        placeholders = ",".join("?" for _ in exercises)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT day, exercise, set_number, reps, weight_kg, rpe FROM (
                    SELECT day, exercise, set_number, reps, weight_kg, rpe,
                           DENSE_RANK() OVER (
                               PARTITION BY exercise ORDER BY day DESC
                           ) AS session_rank
                    FROM strength_sets
                    WHERE exercise IN ({placeholders}) AND day < ?
                )
                WHERE session_rank <= ?
                ORDER BY day DESC, exercise, set_number
                """,
                (*exercises, before_day, sessions_per_exercise),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_exercises(
        self, min_sessions: int = 1, exclude: Sequence[str] = ()
    ) -> list[dict[str, Any]]:
        """Alla övningar med antal pass och senaste dag, flest pass först.

        Underlaget till övningsväljaren. `min_sessions` filtrerar bort det
        som bara provats en gång: skarpt har 29 övningar loggats, men nio
        av dem i ett eller två pass — de har ingen utveckling att visa och
        gör listan svårare att leta i.

        `exclude` tar bort namngivna övningar (se hidden_exercises i
        config.py) — accessoarlyft och FIT-poster vars namn inte gick att
        tolka. Namnen normaliseras här och inte hos anroparen, så en
        .env-rad får skrivas med versaler och extra mellanslag.
        """
        gomda = {normalize_exercise(namn) for namn in exclude if namn}
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT exercise,
                       COUNT(DISTINCT day) AS sessions,
                       MAX(day) AS last_day,
                       MIN(day) AS first_day
                FROM strength_sets
                GROUP BY exercise
                HAVING COUNT(DISTINCT day) >= ?
                ORDER BY sessions DESC, exercise
                """,
                (max(1, min_sessions),),
            ).fetchall()
            return [dict(r) for r in rows if r["exercise"] not in gomda]

    def list_sets_for_exercise(
        self, exercise: str, sessions: int | None = 12
    ) -> list[dict[str, Any]]:
        """Råa set för en övnings N senaste pass, äldsta först.

        Fönstret räknas i PASS och inte i kalenderdagar, av samma skäl som
        get_strength_history: knäböj körs var femte dag och skulle få gott
        om punkter, medan frivändningar (7 pass på ett år) oftast skulle
        falla utanför ett dagfönster helt och aldrig gå att bedöma.

        Namnet normaliseras på vägen in. Chatten skickar det Claude skrev,
        webben det som stod i en URL — och tabellen innehåller bara gemener
        (se normalize_exercise).

        Äldsta först: det här är underlag för en kurva, och en kurva läses
        från vänster.

        `sessions=None` ger hela historiken. Det behövs för personliga
        rekord: räknade man dem ur kurvans fönster fick bänkpressen
        rekordet 89,4 kg medan det riktiga, 120 kg från 5 maj, låg utanför
        och aldrig syntes.
        """
        exercise = normalize_exercise(exercise)
        if not exercise:
            return []
        with self._conn() as conn:
            if sessions is None:
                rows = conn.execute(
                    """
                    SELECT day, set_number, reps, weight_kg, rpe, note, source
                    FROM strength_sets WHERE exercise = ?
                    ORDER BY day, set_number
                    """,
                    (exercise,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT day, set_number, reps, weight_kg, rpe, note, source
                    FROM (
                        SELECT *, DENSE_RANK() OVER (ORDER BY day DESC) AS session_rank
                        FROM strength_sets
                        WHERE exercise = ?
                    )
                    WHERE session_rank <= ?
                    ORDER BY day, set_number
                    """,
                    (exercise, max(1, sessions)),
                ).fetchall()
            return [dict(r) for r in rows]

    def record_strength_import(
        self, activity_id: str, day: str | None, sets_found: int
    ) -> None:
        """Noterar att vi hämtat och tittat i passets originalfil.

        sets_found=0 betyder "tittat, filen innehöll inga set" — ett
        slutgiltigt svar, inte ett misslyckande att försöka igen.
        """
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO strength_import_attempts
                    (activity_id, day, sets_found, checked_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(activity_id) DO UPDATE SET
                    day=excluded.day, sets_found=excluded.sets_found,
                    checked_at=excluded.checked_at
                """,
                (activity_id, day, sets_found, _now_iso()),
            )

    def mark_strength_import_empty(self, activity_id: str, day: str | None) -> None:
        """Kortform för record_strength_import(..., sets_found=0)."""
        self.record_strength_import(activity_id, day, 0)

    def has_checked_strength_import(self, activity_id: str) -> bool:
        """Har vi redan hämtat och tittat i det här passets originalfil?

        Skiljer sig från has_strength_sets_for_activity: den frågar om det
        FINNS set, den här om vi TITTAT. Ett pass utan set i filen svarar
        nej på den första för alltid, vilket fick synken att ladda ner
        samma fil varje timme utan att något kunde ändras.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM strength_import_attempts WHERE activity_id=? LIMIT 1",
                (activity_id,),
            ).fetchone()
            return row is not None

    def has_strength_sets_for_activity(self, activity_id: str, source: str) -> bool:
        """Har vi redan set från den här källan för passet?

        Används av synken för att slippa ladda ner och parsa samma
        FIT-fil varje timme — bara pass vi inte redan importerat hämtas.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM strength_sets WHERE activity_id=? AND source=? LIMIT 1",
                (activity_id, source),
            ).fetchone()
            return row is not None

    def delete_strength_sets_for_exercise(
        self, day: str, exercise: str, source: str
    ) -> int:
        """Raderar en dags set för EN övning från EN källa. Antal rader.

        Används av FIT-importen för att rensa chattloggade dubbletter av
        de övningar klockans fil täcker — se import_strength_sets.

        Dag och inte pass: chattloggade set får bara ett activity_id när
        dagen har exakt ETT styrkepass (se _find_strength_activity_id i
        analysis/pipeline.py), så en dubblett loggad en dag med två pass
        ligger på activity_id=NULL och hade inte gått att hitta via
        passet. Samma avgränsning som spärren åt andra hållet redan
        använder, som frågar get_strength_sets_for_date.

        `exercise` förutsätts vara det upplösta namnet (se
        resolved_exercise) — tabellen innehåller inga andra.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM strength_sets WHERE day=? AND exercise=? AND source=?",
                (day, exercise, source),
            )
            return cur.rowcount

    def delete_strength_sets_for_activity(self, activity_id: str, source: str) -> int:
        """Raderar ett passets set från EN källa. Returnerar antal rader.

        Gör om-import idempotent: FIT-importen kan rensa sina egna rader
        och skriva nya utan att röra det du loggat manuellt i chatten
        (source='chat'), som aldrig går att återskapa från någon fil.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM strength_sets WHERE activity_id=? AND source=?",
                (activity_id, source),
            )
            return cur.rowcount

    # --- Sportinställningar (pulszoner) ----------------------------------
    def replace_sport_settings(self, rows: list[dict[str, Any]]) -> int:
        """Skriver om hela sport_settings. Returnerar antal sparade rader.

        Ersättning och inte upsert: listan är atletens fullständiga
        uppsättning sporter, och tas en sport bort i Intervals ska den inte
        ligga kvar här och namnge zoner som inte längre finns.

        Ett tomt svar rör ingenting. Det skyddar mot att ett tillfälligt
        trasigt API-svar tömmer tabellen och tar zonnamnen med sig — se
        sync_sport_settings, som hellre behåller gamla namn än inga alls.
        """
        if not rows:
            return 0
        now = _now_iso()
        with self._conn() as conn:
            conn.execute("DELETE FROM sport_settings")
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO sport_settings
                        (id, types, max_hr, lthr, hr_zone_names, last_synced)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(row.get("id")),
                        json.dumps(row.get("types") or [], ensure_ascii=False),
                        row.get("max_hr"),
                        row.get("lthr"),
                        json.dumps(row.get("hr_zone_names") or [], ensure_ascii=False),
                        now,
                    ),
                )
        return len(rows)

    def list_sport_settings(self) -> list[dict[str, Any]]:
        """Alla sporters zoninställningar, med JSON-kolumnerna uppackade."""
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM sport_settings ORDER BY id").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            for key in ("types", "hr_zone_names"):
                try:
                    d[key] = json.loads(d[key]) if d[key] else []
                except (TypeError, ValueError):
                    d[key] = []
            result.append(d)
        return result

    def hr_zone_names(self, activity_type: str | None) -> list[str] | None:
        """Zonnamnen som gäller för en passtyp, eller None om vi inte vet.

        Intervals grupperar sporter: en rad täcker ["Run","VirtualRun",
        "TrailRun"], en annan ["WeightTraining"]. Uppslagningen är exakt på
        typen, med raden för "Other" som sista utväg — det är den Intervals
        självt faller tillbaka på för en sport utan egna inställningar.

        None och inte en tom lista: skillnaden mellan "vi har inte synkat
        zonnamnen" och "sporten har inga zoner" ska synas hos anroparen.
        """
        if not activity_type:
            return None
        reserv: list[str] | None = None
        for row in self.list_sport_settings():
            namn = row.get("hr_zone_names") or None
            if activity_type in (row.get("types") or []):
                return namn
            if "Other" in (row.get("types") or []):
                reserv = namn
        return reserv

    # --- Schemalagda jobbs tillstånd -------------------------------------
    def get_job_state(self, key: str) -> str | None:
        """Läser en markör satt av set_job_state, eller None om den saknas."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM job_state WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def set_job_state(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO job_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, _now_iso()),
            )

    def prune_analyses(self, keep_days: int) -> int:
        """Rensar överflödiga versioner av analyser. Returnerar antal rader.

        Tabellen växer och inget tar bort ur den. Det mesta av tillväxten
        är inte nya dygn utan OMSKRIVNINGAR av samma dygn: uppmätt skarpt
        låg 75 av 133 rader (145 kB av 265) på en (analysis_type, ref_id)
        som redan hade en nyare version — som mest åtta versioner av samma
        morgonrekommendation, från tiden innan spärren mot dubbelkörning
        var på plats.

        Den nyaste versionen av varje (typ, ref_id) sparas ALLTID, hur
        gammal den än är. Den är det enda exemplaret: Intervals har rådatan
        kvar, men en genererad analys går inte att återskapa utan att
        betala för den igen, och dashboarden visar just den raden. Det som
        gallras är alltså bara versioner som redan ersatts av en nyare.

        `keep_days` skyddar de senaste dygnens omskrivningar, så att en
        analys man just genererat om går att jämföra med den den ersatte.
        0 eller mindre stänger av gallringen helt.

        Obs: filen KRYMPER inte av det här. SQLite återanvänder de frigjorda
        sidorna i stället för att lämna tillbaka dem, och backup-API:et
        kopierar dem med sig — uppmätt: 2 101 248 byte både före och efter.
        Gallringen bromsar tillväxten, den tar inte tillbaka utrymme. Det
        gör VACUUM, som anroparen får köra separat när något faktiskt
        raderats (se run_backup i scheduler.py).
        """
        if keep_days <= 0:
            return 0
        cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """
                DELETE FROM analyses WHERE id IN (
                    SELECT id FROM (
                        SELECT id, created_at,
                               ROW_NUMBER() OVER (
                                   PARTITION BY analysis_type, ref_id
                                   ORDER BY created_at DESC, id DESC
                               ) AS rn
                        FROM analyses
                    )
                    WHERE rn > 1 AND created_at < ?
                )
                """,
                (cutoff,),
            )
            return cur.rowcount

    def vacuum(self) -> None:
        """Ger tillbaka frigjort utrymme till filsystemet.

        Egen anslutning med isolation_level=None: pythons sqlite3 öppnar
        annars en transaktion åt oss inför nästa sats, och VACUUM går inte
        att köra inuti en transaktion.

        Skriver om hela databasen, alltså ett par megabyte till SD-kortet.
        Anropas därför bara när en gallring faktiskt raderat något — inte
        varje natt för säkerhets skull.
        """
        conn = sqlite3.connect(self.db_path, timeout=_BUSY_TIMEOUT_SECONDS,
                               isolation_level=None)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()

    # --- Backup ----------------------------------------------------------
    def backup(self, dest: Path | str) -> Path:
        """Skriver en konsistent kopia av hela databasen till `dest`.

        Använder sqlite3:s inbyggda backup-API i stället för att kopiera
        filen rakt av. Databasen körs i WAL-läge (se _conn), och då ligger
        de senaste skrivningarna kvar i en separat -wal-fil tills de
        checkpointas — en `cp training.db` kan alltså missa dem helt och ge
        en kopia som ser hel ut men saknar de sista dagarnas data.
        Backup-API:et läser genom en riktig anslutning och får med allt som
        är committat, även om en synk skriver samtidigt.

        Skriver över en befintlig fil med samma namn: två backuper samma
        dygn ska ge en fil, inte två.

        Kopian ställs om till journal_mode=DELETE innan den stängs.
        Backup-API:et tar med källans filhuvud, WAL-läget inkluderat, och
        en kopia i WAL-läge får en -wal- och en -shm-fil bredvid sig så
        fort någon öppnar den för att titta i den. Sidofilerna städas inte
        alltid bort (en readonly-anslutning kan inte checkpointa vid
        stängning), och rotationen letar bara efter *.db — de blev alltså
        liggande kvar för evigt. En arkivkopia har inga samtidiga
        skribenter och har ingen nytta av WAL. Innehållet är detsamma, och
        _conn slår ändå på WAL igen om filen någon gång tas i bruk.

        Skrivningen går till en temporärfil som byts in på plats först när
        den är klar.

        Inte för att en avbruten kopiering skulle förstöra en BEFINTLIG
        målfil — backup-API:et är transaktionellt mot målet och rullar
        tillbaka det orört (uppmätt: samma storlek och alla rader kvar
        efter ett avbrott mitt i). Skälet är att filnamnet bär dygnets
        datum, så varje natt skriver till ett namn som inte fanns förut,
        och där finns ingen tidigare version att rulla tillbaka TILL.
        Avbröts kopieringen (fullt SD-kort, strömavbrott, omstart) blev en
        TOM fil kvar under slutnamnet — uppmätt 0 byte, och
        "no such table: self_reports" när den öppnas. Rotationen letar
        efter training-*.db, räknade den som en giltig backup, och knuffade
        därmed ut en kopia som faktiskt gick att läsa.

        Temporärnamnet matchar inte rotationens mönster, så en .part som
        blivit liggande efter en hård processdöd städas bort vid nästa
        körning i stället för att räknas.
        """
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = dest_path.with_name(dest_path.name + ".part")
        temp_path.unlink(missing_ok=True)
        try:
            with self._conn() as conn:
                target = sqlite3.connect(str(temp_path))
                try:
                    conn.backup(target)
                    target.execute("PRAGMA journal_mode=DELETE").fetchall()
                finally:
                    target.close()
            # os.replace-semantik: byter in filen atomiskt och skriver över
            # en befintlig backup från samma dygn, så två körningar samma
            # dag ger en fil och inte två.
            temp_path.replace(dest_path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        return dest_path
