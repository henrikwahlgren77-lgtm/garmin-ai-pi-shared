"""Jobbens utfall: notis till telefonen när något slutar fungera.

Ett misslyckat jobb syntes tidigare bara i journalen, och journalen läser
man när något redan är fel. En backup som slutat skrivas, eller en synk
som fått 401 sedan nyckeln byttes, kunde alltså pågå i veckor.

Varje schemalagt jobb noterar här hur det gick. Utfallen sparas i
job_state (en rad per jobb), och av dem räknas två saker ut:

- **Notiser** via ntfy (NTFY_URL i .env). EN notis per incident: när ett
  jobb börjar fela, och en när det fungerar igen. Inte en per misslyckad
  körning — synken körs varje timme, och ett larm som ljuder tjugo gånger
  för samma fel slutar man läsa.
- **Dashboardens varning** (se problems), som står kvar tills felet är
  borta och alltså syns även om notisen missades.

Notisen innehåller jobbets namn och felets TYP, aldrig felmeddelandet:
det kan bära en adress, ett datum eller ett värde ur hälsodatan, och
ntfy.sh är en tredje part. Detaljerna finns i journalen.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from config import Settings
from sync.store import Store

log = logging.getLogger(__name__)

# Jobben som rapporterar, med namnet de har i notiser och på dashboarden.
JOB_LABELS = {
    "sync": "Synken",
    "morning_recommendation": "Morgonanalysen",
    "evening_summary": "Kvällssammanfattningen",
    "evening_summary_recheck": "Kvällskontrollen",
    "coaching": "Träningsanalysen",
    "backup": "Backupen",
}

# Hur många misslyckanden i rad innan det räknas som en incident. Synken
# körs varje timme och Intervals har enstaka tillfälliga fel; ett enda
# missat försök rättar nästa timme till själv. De andra jobben körs en
# gång om dagen, så där är redan första felet ett uteblivet resultat.
_FAILURES_BEFORE_ALERT = {"sync": 2}

# Hur gammal den senaste lyckade körningen får vara innan dashboarden
# varnar, för jobb som ska lyckas regelbundet. Analyserna saknas med flit:
# de hoppar legitimt över sig själva (ingen ny sömndata, redan gjord), så
# en gammal tidsstämpel betyder ingenting där. Ett FEL syns ändå.
#
# Synken: tre missade intervall (SYNC_INTERVAL_HOURS, se problems).
# Backupen: två nätter, samma gräns som uppladdningsskriptet. En enstaka
# missad natt tar run_backup_catchup igen vid nästa start.
_MAX_AGE = {"backup": timedelta(hours=50)}
_SYNC_INTERVALS_BEFORE_OVERDUE = 3

_STATE_PREFIX = "status:"
_NTFY_TIMEOUT_SECONDS = 10.0


def _load(store: Store, job: str) -> dict[str, Any]:
    raw = store.get_job_state(_STATE_PREFIX + job)
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def _save(store: Store, job: str, state: dict[str, Any]) -> None:
    store.set_job_state(_STATE_PREFIX + job, json.dumps(state, sort_keys=True))


def record(settings: Settings, job: str, failure: BaseException | None) -> None:
    """Noterar hur en körning gick. Anropas från jobbets finally-block.

    En körning som hoppar över sig själv (ingen ny sömndata, redan gjord
    idag) räknas som lyckad: jobbet fungerar, det hade bara inget att göra.
    """
    if failure is None:
        record_success(settings, job)
    else:
        record_failure(settings, job, failure)


def record_success(settings: Settings, job: str, now: datetime | None = None) -> None:
    """Noterar en lyckad körning. Skickar "fungerar igen" efter en incident.

    Kastar aldrig: jobbet har redan lyckats, och ett fel i bokföringen får
    inte göra om det till ett misslyckande.
    """
    try:
        now = now or datetime.now()
        store = Store(settings.db_path, settings.strength_corrections)
        state = _load(store, job)
        was_alerting = state.get("failures", 0) >= _threshold(job)
        state.update(ok=now.isoformat(timespec="seconds"), failures=0)
        _save(store, job, state)
        if was_alerting:
            send(settings, f"{JOB_LABELS[job]} fungerar igen",
                 f"Lyckades {_when(now, now)}.", priority=3, tags=["white_check_mark"])
    except Exception:
        log.exception("Kunde inte notera att %s lyckades.", job)


def record_failure(
    settings: Settings, job: str, exc: BaseException, now: datetime | None = None
) -> None:
    """Noterar ett misslyckande. Skickar en notis när det blir en incident.

    Kastar aldrig. Går databasen inte att skriva till — vilket i sig kan
    vara felet — skickas notisen ändå, utan räkning: hellre en notis för
    mycket än ingen när databasen är det som gått sönder.
    """
    now = now or datetime.now()
    failures = 1
    try:
        store = Store(settings.db_path, settings.strength_corrections)
        state = _load(store, job)
        failures = int(state.get("failures", 0)) + 1
        state.update(
            failed=now.isoformat(timespec="seconds"),
            failures=failures,
            error=type(exc).__name__,
        )
        _save(store, job, state)
    except Exception:
        log.exception("Kunde inte notera att %s misslyckades.", job)
        failures = _threshold(job)
    if failures == _threshold(job):
        send(
            settings,
            f"{JOB_LABELS[job]} misslyckades",
            f"{type(exc).__name__}"
            + (f", {failures} gånger i rad" if failures > 1 else "")
            + ". Detaljer: journalctl -u garmin-ai-pi",
            priority=4,
            tags=["warning"],
        )


def _threshold(job: str) -> int:
    return _FAILURES_BEFORE_ALERT.get(job, 1)


def problems(
    store: Store, now: datetime | None = None, sync_interval_hours: int = 1
) -> list[str]:
    """Meningar till dashboarden om jobb som inte fungerar just nu.

    Tom lista när allt är som det ska. Ett jobb som aldrig rapporterat
    (ny installation, eller före första körningen) räknas inte som fel.
    """
    now = now or datetime.now()
    max_age = {
        **_MAX_AGE,
        "sync": timedelta(hours=_SYNC_INTERVALS_BEFORE_OVERDUE * sync_interval_hours),
    }
    found: list[str] = []
    for job, label in JOB_LABELS.items():
        state = _load(store, job)
        if not state:
            continue
        ok = _parse(state.get("ok"))
        if state.get("failures", 0) >= _threshold(job):
            when = _parse(state.get("failed"))
            last_ok = f" Lyckades senast {_when(ok, now)}." if ok else ""
            found.append(f"{label} misslyckades {_when(when, now)}.{last_ok}")
        elif ok and job in max_age and now - ok > max_age[job]:
            found.append(f"{label} lyckades senast {_when(ok, now)}.")
    return found


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _when(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "okänt"
    if moment.date() == now.date():
        return f"kl {moment:%H:%M}"
    if moment.date() == (now - timedelta(days=1)).date():
        return f"i går kl {moment:%H:%M}"
    return f"{moment.day}/{moment.month} kl {moment:%H:%M}"


def send(
    settings: Settings,
    title: str,
    message: str,
    priority: int = 3,
    tags: list[str] | None = None,
) -> bool:
    """Skickar en notis via ntfy. Gör ingenting om NTFY_URL saknas.

    NTFY_URL är ämnets hela adress, t.ex. https://ntfy.sh/mitt-hemliga-amne.
    Meddelandet skickas som JSON till serverns rot, inte som rå text till
    ämnet: rubriken bär å, ä och ö, och i ett HTTP-huvud (ntfys andra sätt
    att ange rubrik) är de inte tillåtna.

    Kastar aldrig. En notis som inte når fram får inte fälla jobbet som
    försökte rapportera.
    """
    url = getattr(settings, "ntfy_url", "")
    if not url:
        return False
    try:
        parts = urlsplit(url)
        base, _, topic = parts.path.rstrip("/").rpartition("/")
        if not topic:
            raise ValueError("NTFY_URL saknar ämne (t.ex. https://ntfy.sh/mitt-amne)")
        server = urlunsplit((parts.scheme, parts.netloc, base + "/", "", ""))
        response = httpx.post(
            server,
            json={
                "topic": topic,
                "title": title,
                "message": message,
                "priority": priority,
                "tags": tags or [],
            },
            timeout=_NTFY_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        # Typen räcker; adressen innehåller ämnet, som fungerar som lösenord.
        log.warning("Notisen kunde inte skickas (%s).", type(exc).__name__)
        return False
