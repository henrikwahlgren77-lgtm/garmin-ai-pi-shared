"""Konfiguration läst från miljövariabler / .env."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

# Ladda .env från projektroten om den finns.
_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_ROOT / ".env")


@dataclass(frozen=True)
class AthleteProfile:
    """Uppgifter om atleten som läggs i Claudes systemprompt.

    Låg tidigare hårdkodad i analysis/prompts.py. Det gjorde dels att
    koden innehöll personuppgifter — inklusive hälsodata — som följde med
    i varje klon och i hela git-historiken, dels att den som vill köra
    appen måste redigera Python för att beskriva sig själv. Det här är
    konfiguration, inte kod.

    Alla fält är frivilliga. De som saknas utelämnas ur prompten i stället
    för att fyllas med gissningar.
    """

    # Tilltalsnamnet. Används både i webbens hälsningsfras ("God morgon
    # Anna!") och i systemprompten, så att analyserna tilltalar atleten
    # vid namn. Stod hårdkodat i index.html, vilket hälsade fel person så
    # fort någon annan körde appen. Lämnas det tomt hälsar sidan utan namn
    # och prompten utelämnar raden helt.
    name: str = ""
    sex: str = ""
    birth_date: date | None = None
    height_cm: int | None = None
    medical: str = ""
    routine: str = ""
    goal: str = ""


def _env(key: str) -> str:
    return os.environ.get(key, "").strip()


def parse_time_of_day(value: str, var_name: str = "tiden") -> tuple[int, int]:
    """Tolkar 'HH:MM' till (timme, minut).

    Felmeddelandet namnger miljövariabeln. Utan det gav en felskriven tid
    (t.ex. EVENING_SUMMARY_TIME=2359 utan kolon) en ren
    "ValueError: not enough values to unpack (expected 2, got 1)" när
    schemaläggaren startade — ett fel som varken säger vilken inställning
    som är trasig eller hur den ska se ut, och som stoppar hela tjänsten
    från att starta.
    """
    try:
        hour_str, minute_str = value.split(":")
        hour, minute = int(hour_str), int(minute_str)
    except ValueError:
        raise RuntimeError(
            f"{var_name}={value!r} går inte att tolka som ett klockslag. "
            "Ange HH:MM, t.ex. 07:00."
        ) from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise RuntimeError(
            f"{var_name}={value!r} ligger utanför dygnet. "
            "Timmen ska vara 0-23 och minuten 0-59."
        )
    return hour, minute


def _time_setting(var_name: str, default: str) -> str:
    """Läser och validerar en HH:MM-inställning; returnerar den normaliserad.

    Valideras redan här, vid inläsningen, i stället för långt senare när
    schemaläggaren ska bygga sin trigger — då hade felet dykt upp som en
    krasch vid uppstart utan koppling till vilken variabel som var fel.
    """
    raw = _env(var_name) or default
    hour, minute = parse_time_of_day(raw, var_name)
    return f"{hour:02d}:{minute:02d}"


def _list_setting(var_name: str) -> tuple[str, ...]:
    """Läser en kommaseparerad lista ur miljön.

    Tomma poster faller bort, så en avslutande komma eller en rad med
    extra mellanslag inte blir en post som aldrig matchar något.
    """
    raw = os.environ.get(var_name, "")
    return tuple(del_.strip() for del_ in raw.split(",") if del_.strip())


def _bool_setting(var_name: str, default: bool) -> bool:
    """Läser en av/på-inställning ur miljön.

    Accepterar både sifferformen och ord, i båda riktningarna, eftersom
    .env-filer skrivs för hand: 1/0, true/false, yes/no, on/off. Allt
    annat — inklusive en tom sträng — ger defaultvärdet, så en bortkommen
    rad inte tyst stänger av något.
    """
    raw = os.environ.get(var_name, "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def _int_setting(var_name: str, default: int, minimum: int | None = None) -> int:
    """Läser ett heltal ur miljön och validerar det, likt _time_setting.

    Klockslagen fick tidig validering med ett felmeddelande som namnger
    variabeln; siffrorna gjorde inte det. En felskrivning i WEB_PORT,
    SYNC_INTERVAL_HOURS eller BACKUP_KEEP gav en naken
    "ValueError: invalid literal for int() with base 10: 'åtta'" ur
    load_settings — utan att nämna vilken inställning som var trasig, och
    med hela tjänsten nere.

    `minimum` klämmer värdet uppåt i stället för att fela: ett för lågt
    tal är entydigt (BACKUP_KEEP=0 är en rotation som raderar backupen
    direkt efter att den skrivits), medan ett tal som inte går att tolka
    inte är det.
    """
    raw = _env(var_name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(
            f"{var_name}={raw!r} går inte att tolka som ett heltal. "
            f"Ange en siffra, t.ex. {default}."
        ) from None
    if minimum is not None and value < minimum:
        log.warning(
            "%s=%d är lägre än minimum %d — använder %d.",
            var_name, value, minimum, minimum,
        )
        return minimum
    return value


def _load_athlete() -> AthleteProfile:
    birth_date = None
    raw_birth = _env("ATHLETE_BIRTH_DATE")
    if raw_birth:
        try:
            birth_date = date.fromisoformat(raw_birth)
        except ValueError:
            log.warning(
                "ATHLETE_BIRTH_DATE=%r går inte att tolka (väntar YYYY-MM-DD). "
                "Åldern utelämnas ur atletprofilen.",
                raw_birth,
            )

    height_cm = None
    raw_height = _env("ATHLETE_HEIGHT_CM")
    if raw_height:
        try:
            height_cm = int(float(raw_height))
        except ValueError:
            log.warning("ATHLETE_HEIGHT_CM=%r är inte ett tal.", raw_height)

    return AthleteProfile(
        name=_env("ATHLETE_NAME"),
        sex=_env("ATHLETE_SEX"),
        birth_date=birth_date,
        height_cm=height_cm,
        medical=_env("ATHLETE_MEDICAL"),
        routine=_env("ATHLETE_ROUTINE"),
        goal=_env("ATHLETE_GOAL"),
    )


@dataclass(frozen=True)
class Settings:
    intervals_api_key: str
    intervals_athlete_id: str
    anthropic_api_key: str
    anthropic_model: str
    web_access_token: str
    web_host: str
    web_port: int
    db_path: Path
    sync_interval_hours: int
    morning_recommendation_time: str
    evening_summary_time: str
    # Tid (HH:MM) då gårdagens kvällssammanfattning kontrolleras mot
    # eftersynkat underlag och görs om vid behov. Se
    # run_evening_summary_recheck i scheduler.py för varför: Intervals.icu
    # är inte alltid färdigsynkad med dygnet till 23:59 (Garmin
    # efterjusterar stegen, räknar om natten, och ett pass som låg kvar i
    # klockan laddas upp först på morgonen), så kvällssammanfattningen kan
    # ha låst in siffror som inte var facit.
    evening_summary_recheck_time: str
    # Träningsanalysen (coaching) kördes tidigare bara när man tryckte på
    # knappen i webben. Nu genereras den automatiskt mitt på dagen, när
    # dagens sömn- och wellness-data hunnit synkas men innan man planerat
    # kvällens pass.
    coaching_time: str
    # Nattlig kopia av databasen (se run_backup i scheduler.py). Databasen
    # är den enda platsen där något av det här finns: Intervals håller
    # rådatan, men självrapporterat från chatten, loggade styrkeset och
    # varenda genererad analys existerar bara här.
    backup_dir: Path
    backup_time: str
    backup_keep: int
    # Hur länge ERSATTA versioner av en analys sparas (se prune_analyses i
    # sync/store.py). Den nyaste versionen av varje dygn sparas alltid —
    # den går inte att återskapa utan att betala för den igen. 0 stänger
    # av gallringen.
    analysis_keep_days: int
    # Övningar som inte ska erbjudas i progressionsväljaren. Tillfälliga
    # lyft, uppvärmningsmoment och FIT-poster vars övningsnamn inte gick
    # att tolka ("okänd övning") fyller listan utan att ha någon
    # utveckling att visa.
    #
    # Konfiguration och inte kod: vilka lyft som är accessoarer och vilka
    # som är huvudövningar är atletens sak, precis som atletprofilen
    # ovan. Ett direktanrop mot /api/strength/progression fungerar
    # fortfarande — det här filtrerar väljaren, det raderar ingenting.
    hidden_exercises: tuple[str, ...] = ()
    # Set som klockan skrev ner fel, på formen "2026-05-05:bänkpress" för
    # att stryka och "2026-04-21:raka marklyft=marklyft" för att döpa om.
    # Se parse_strength_corrections i sync/store.py för varför de finns
    # och varför det är en handskriven lista och ingen automatik.
    strength_corrections: tuple[str, ...] = ()
    # Kör de dagliga analyserna en gång direkt vid uppstart (se
    # _run_startup_catchups i scheduler.py). Sant i drift: en omstart
    # strax efter en schemalagd tid ska inte betyda att dagens analys
    # uteblir.
    #
    # Finns som inställning för att den ska gå att stänga av. Varje
    # TestClient(app) startar en lifespan, och lifespan startade förr
    # catchup-tråden villkorslöst — 114 testuppstarter som var och en
    # försökte synka mot Intervals och anropa Claude på riktigt. Det
    # hölls ofarligt bara av två spärrar i conftest, och de kastar inne i
    # en tråd vars fel sväljs av ett except Exception. En trasig spärr
    # hade alltså inte synts som ett rött test utan som en oväntad
    # räkning. Nu startas tråden inte alls under test.
    #
    # Nyttig även utanför testerna: den som startar appen lokalt för att
    # titta på gränssnittet vill sällan att den genererar betalda
    # analyser vid varje omstart.
    startup_catchup: bool = True
    # Falskt ger en ren webbserver: inga schemalagda jobb och ingen
    # uppstarts-catchup. För en andra instans som delar databas med en
    # som redan kör jobben — två processer som synkar och analyserar
    # samma dygn hade dubblat både skrivningarna och Claude-räkningen.
    scheduler_enabled: bool = True
    athlete: AthleteProfile = AthleteProfile()

    @property
    def db_path_str(self) -> str:
        return str(self.db_path)


def _require(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(f"Miljövariabeln {key} saknas. Konfigurera .env (se .env.example).")
    return val


def load_settings() -> Settings:
    db_path = Path(os.environ.get("DB_PATH", "data/training.db"))
    if not db_path.is_absolute():
        db_path = _ROOT / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Default bredvid databasen. Sätt BACKUP_DIR till en monterad disk
    # eller nätverksplats för att faktiskt överleva att lagringsmediet dör
    # — en kopia på samma SD-kort skyddar mot tappade skrivningar och
    # felaktiga raderingar, inte mot att kortet slutar svara.
    raw_backup_dir = os.environ.get("BACKUP_DIR", "").strip()
    backup_dir = Path(raw_backup_dir) if raw_backup_dir else db_path.parent / "backups"
    if not backup_dir.is_absolute():
        backup_dir = _ROOT / backup_dir

    return Settings(
        intervals_api_key=_require("INTERVALS_API_KEY"),
        intervals_athlete_id=_require("INTERVALS_ATHLETE_ID"),
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        anthropic_model=(os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5").strip()
                         or "claude-sonnet-5"),
        web_access_token=os.environ.get("WEB_ACCESS_TOKEN", "").strip(),
        web_host=os.environ.get("WEB_HOST", "0.0.0.0").strip() or "0.0.0.0",
        web_port=_int_setting("WEB_PORT", 8000, minimum=1),
        db_path=db_path,
        sync_interval_hours=_int_setting("SYNC_INTERVAL_HOURS", 1, minimum=1),
        morning_recommendation_time=_time_setting("MORNING_RECOMMENDATION_TIME", "07:00"),
        evening_summary_time=_time_setting("EVENING_SUMMARY_TIME", "23:59"),
        evening_summary_recheck_time=_time_setting(
            "EVENING_SUMMARY_RECHECK_TIME", "09:00"
        ),
        coaching_time=_time_setting("COACHING_TIME", "12:00"),
        backup_dir=backup_dir,
        backup_time=_time_setting("BACKUP_TIME", "03:30"),
        # Minst en: en rotation som sparar noll filer är en backup som
        # raderar sig själv direkt efter att den skrivits.
        backup_keep=_int_setting("BACKUP_KEEP", 14, minimum=1),
        # Noll är tillåtet och betyder "gallra inte alls".
        analysis_keep_days=_int_setting("ANALYSIS_KEEP_DAYS", 30, minimum=0),
        hidden_exercises=_list_setting("PROGRESSION_HIDDEN_EXERCISES"),
        strength_corrections=_list_setting("STRENGTH_SET_CORRECTIONS"),
        startup_catchup=_bool_setting("STARTUP_CATCHUP", True),
        scheduler_enabled=_bool_setting("SCHEDULER_ENABLED", True),
        athlete=_load_athlete(),
    )
