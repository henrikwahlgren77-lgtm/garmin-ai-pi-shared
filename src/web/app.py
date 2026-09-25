"""FastAPI-app: mobil-först webbgränssnitt.

Alla routes (utom /static) är async def eller def beroende på om de
behöver await:a något (t.ex. request.json()). De flesta anropar bara
synkron, blockerande kod (SQLite, httpx, Anthropic-klienten) och är
därför vanliga `def`-routes — FastAPI/Starlette kör dem automatiskt i en
trådpool, så de blockerar inte den delade event-loopen. Bara /chat (som
faktiskt await:ar request.json()) är async, och wrappar sitt blockerande
Claude-anrop i run_in_threadpool av samma anledning.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from analysis.claude import ClaudeClient
from analysis.pipeline import (
    CORRECT_SELF_REPORT_TOOL,
    DELETE_SELF_REPORT_TOOL,
    EVENING_CONTENT_KEY,
    LOG_SELF_REPORT_TOOL,
    LOG_STRENGTH_SESSION_TOOL,
    AnalysisPipeline,
    fmt_self_reports,
    fmt_strength_sessions,
    fmt_wellness_history,
    swedish_date_label,
    swedish_weekday_genitive,
    wellness_for_payload,
)
from analysis.prompts import MAX_CHAT_CHARS
from analysis.strength import measurable_sets, personal_records, session_progression
from config import load_settings, parse_time_of_day
from scheduler import make_lifespan
from sync.intervals_client import (
    IntervalsClient,
    sync_activities,
    sync_lock,
    sync_sport_settings,
    sync_wellness,
)
from sync.store import Store, normalize_exercise

log = logging.getLogger(__name__)

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_STATIC = Path(__file__).parent / "static"

# Hur många dagar formbandet och baslinjerna bygger på.
#
# Var 30, sänktes till 7 därför att den gamla grafen ritade 30 punkter så
# trångt på mobil att varken datumetiketter eller linjeform gick att läsa.
# Formbandet ritar samma 30 punkter läsbart (uppmätt ner till 320px
# skärmbredd): det skalar höjden efter containerns faktiska bredd i
# stället för att krympa proportionellt, och glesar ut datumetiketterna
# när det är trångt.
#
# Sju dagar räcker inte längre heller: baslinjerna ska visa var dagens
# sömn och HRV ligger i din normala variation, och en veckas mätvärden är
# för få för att kalla ett snitt.
_LOAD_HISTORY_DAYS = 30

# Så många meddelanden av chattens historik som skickas med till Claude.
# Hela konversationen skickades tidigare vid varje nytt meddelande, så
# kostnaden växte kvadratiskt med samtalets längd och skulle till slut
# spränga kontextfönstret. Tio utbyten räcker gott för att hålla tråden;
# äldre fakta om vikt, sjukdom och träning finns ändå kvar i kontexten
# via self_reports och strength.
_MAX_CHAT_HISTORY_MESSAGES = 20

# Chattsvarens längd. Gränsen bor i analysis/prompts.py, i samma fil som
# prompttexten som nämner siffran, så att instruktionen till Claude och
# backstoppen här alltid talar om samma tal — texten hade tidigare "2500"
# inskrivet för hand medan konstanten låg här.
#
# Tokentaket måste rymma både tänkande och synlig text — se den utförliga
# härledningen vid MAX_ANALYSIS_TOKENS i analysis/pipeline.py. Kort: Sonnet
# 5 returnerar thinking-block utan att man ber om det, och de dras från
# samma max_tokens som svaret, så ett tak satt till teckengränsen räcker
# inte.
#
# Chatten har inte kapats i praktiken (0 gånger på en vecka på Pi:n, mot
# fyra för analyserna) eftersom svaren är kortare och prompten enklare —
# men marginalen fanns inte, den råkade bara inte ta slut. 6000 ger 2500
# tecken text (~1250 tokens) plus rikligt med tänkandeutrymme.
_MAX_CHAT_CHARS = MAX_CHAT_CHARS
_MAX_CHAT_TOKENS = 6000

# Största inkommande chattmeddelande, och största enskilda meddelande i
# den historik klienten skickar med.
#
# Kroppen lästes tidigare utan någon storleksgräns alls: ett meddelande på
# en megabyte gick rakt vidare till Anthropic och blev en megabyte betald
# prompt. Antalet meddelanden var begränsat (_MAX_CHAT_HISTORY_MESSAGES),
# men inte deras längd — taket var alltså 20 × oändligt.
#
# 8000 tecken är rejält tilltaget mot vad man faktiskt skriver i ett
# textfält (och mot _MAX_CHAT_CHARS = 2500, taket för Claudes egna svar
# som utgör halva historiken). Det är en spärr mot missbruk och
# klistrade textmassor, inte en redigeringsgräns.
_MAX_CHAT_INPUT_CHARS = 8000

# Tak för hur många chattmeddelanden som får skickas per tidsfönster.
#
# Varje meddelande är ett betalt Anthropic-anrop, och det fanns inget tak
# alls. Någon annan på nätverket stoppas av WEB_ACCESS_TOKEN (servern
# vägrar starta utan den när den lyssnar på mer än den egna maskinen, se
# _open_to_the_network_without_token i main.py). Det taket skyddar mot är
# en klient som fastnar i en omförsöksloop: den är redan inloggad, och
# kommer förbi vilken inloggning som helst.
#
# Trettio i minuten är långt över vad en människa skriver (två, tre) och
# långt under vad en loop hinner. Gränsen är en spärr mot skenande
# kostnad, inte en användningsbegränsning.
#
# Räknas per process och inte per avsändare: appen har en användare, och
# ett IP-baserat tak hade ändå kunnat kringgås av det enda fall som
# spelar roll här — samma klient som gör om samma anrop.
_CHAT_RATE_LIMIT = 30
_CHAT_RATE_WINDOW_SECONDS = 60.0
_chat_calls: deque[float] = deque()
_chat_rate_lock = threading.Lock()


def _chat_rate_limit_exceeded(now: float | None = None) -> bool:
    """Registrerar ett chattanrop och säger om taket är passerat.

    Glidande fönster: gamla tidsstämplar faller ur i takt med att tiden
    går, i stället för att alla nollställas på en fast minutgräns — annars
    hade det gått att skicka 2 × taket över en gränsövergång.
    """
    stamp = time.monotonic() if now is None else now
    with _chat_rate_lock:
        while _chat_calls and stamp - _chat_calls[0] > _CHAT_RATE_WINDOW_SECONDS:
            _chat_calls.popleft()
        if len(_chat_calls) >= _CHAT_RATE_LIMIT:
            return True
        _chat_calls.append(stamp)
        return False


# Hälsningsfrasen i hero-rutan, efter klockslag. Gränserna följer dygnsrytm
# snarare än jämna intervall: "God morgon" känns fel långt in på
# förmiddagen, och "God kväll" fel före sen eftermiddag.
#
# Natten (00-06) får neutrala "Hej" och inte "God natt" — den som tittar på
# sin träningsdata klockan tre på natten hälsas inte godnatt, hen är vaken.
_MORNING_FROM, _MORNING_UNTIL = 6, 10
_EVENING_FROM, _EVENING_UNTIL = 17, 24


def _greeting(name: str = "", now: datetime | None = None) -> str:
    """Tidsanpassad hälsning, t.ex. 'God morgon Anna!'.

    `now` går att skicka in för att kunna testa dygnets alla lägen utan att
    behöva mocka klockan globalt.

    Namnet kommer från ATHLETE_NAME i .env (se AthleteProfile i config.py).
    Är det tomt hälsar sidan utan namn ('God morgon!') istället för att
    lämna ett hängande mellanslag eller hälsa fel person — templaten hade
    tidigare 'Hej Anna!' inskrivet för hand.
    """
    hour = (now or datetime.now()).hour
    if _MORNING_FROM <= hour < _MORNING_UNTIL:
        greeting = "God morgon"
    elif _EVENING_FROM <= hour < _EVENING_UNTIL:
        greeting = "God kväll"
    else:
        greeting = "Hej"

    name = name.strip()
    return f"{greeting} {name}!" if name else f"{greeting}!"


def _open_analysis(morning_time: str, now: datetime | None = None) -> str:
    """Vilket analysblock som står utfällt när dashboarden öppnas.

    Morgonanalysen är dagens plan och står öppen från att den körts
    (MORNING_RECOMMENDATION_TIME) till midnatt. Före dess är den gårdagens,
    och det färskaste som finns är kvällssammanfattningen som skrevs 23:59
    — den står då öppen i stället. Förut stod morgonanalysen öppen dygnet
    runt, också klockan två på natten när den handlade om i går.

    `now` går att skicka in för att testa dygnets lägen, som i _greeting.
    """
    hour, minute = parse_time_of_day(morning_time, "MORNING_RECOMMENDATION_TIME")
    now = now or datetime.now()
    return "evening" if (now.hour, now.minute) < (hour, minute) else "morning"


def _fmt_clock(iso: str | None) -> str:
    """'2026-08-27T22:45:44' -> '22:45'. Tom sträng om tiden saknas."""
    if not iso:
        return "–"
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except ValueError:
        return "–"


def _analysis_payload(analysis: dict[str, Any] | None) -> dict[str, Any]:
    """Svarsformat för analysrutorna på dashboarden.

    `created_at` följer med för att rubriken i analysen bara säger vilken
    DAG den gäller, aldrig när den faktiskt skrevs. Det är inte samma sak:
    en kvällssammanfattning man genererar om strax efter midnatt får
    rubriken "Söndag 30 augusti" men sammanfattar ett dygn som är sexton
    minuter gammalt — och rapporterar förstås varken steg eller träning.
    Utan klockslaget ser det ut som att datan saknas.

    `ref_id` är dagen analysen GÄLLER (YYYY-MM-DD för morgon, coaching och
    kväll). Frontend jämför den mot created_at för att avgöra om datumet
    behöver skrivas ut vid klockslaget: kördes analysen samma dag som den
    handlar om säger rubriken redan vilken dag det är.
    """
    if not analysis:
        return {"markdown": "", "created_at": None, "ref_id": None}
    return {
        "markdown": analysis["markdown"],
        "created_at": analysis.get("created_at"),
        "ref_id": analysis.get("ref_id"),
    }


class ActivitySummary(BaseModel):
    """Vad passlistan på dashboarden faktiskt läser — inte hela tabellraden.

    Store.list_activities gör `SELECT *`, och routen returnerade raderna
    rakt av. Då följde raw_json med: hela det sammanslagna Intervals-svaret
    per pass, som passlistan inte rör vid en enda gång. Uppmätt mot en
    kopia av den skarpa databasen, tio pass (det dashboarden hämtar):

        55 960 byte  ->  1 815 byte    (97 % var rådata ingen läser)

    Samma sorts dubblering som togs bort ur analysernas payload till
    Claude, fast mot telefonen i stället för mot API:et.

    Deklarerad som en modell och inte som en handplockad dict i routen:
    FastAPI filtrerar då svaret mot modellen, så en ny kolumn i tabellen
    kan inte läcka ut i API:et bara för att ingen tänkte på det. Det ger
    /api/activities ett kontrakt på köpet.
    """

    id: str
    name: str | None = None
    start_time: str | None = None
    duration_seconds: int | None = None
    distance_meters: float | None = None
    average_heart_rate: int | None = None
    tss: float | None = None


class ActivityList(BaseModel):
    activities: list[ActivitySummary]


class StrengthPass(BaseModel):
    """Ett enskilt gympass inom en vecka, så stapeln kan länkas.

    activity_id är valfritt: set loggade via chatten innan passet synkats
    har inget pass att länka till. Volymen räknas ändå in i veckan.
    """

    activity_id: str | None
    day: str            # YYYY-MM-DD
    volume_kg: float


class StrengthWeek(BaseModel):
    start: str          # måndagen i veckan, YYYY-MM-DD
    volume_kg: float
    sessions: int
    passes: list[StrengthPass]


class TopLift(BaseModel):
    exercise: str
    weight_kg: float


class ExerciseSummary(BaseModel):
    exercise: str
    sessions: int
    first_day: str
    last_day: str


class BestSet(BaseModel):
    """Passets bästa set, mätt i skattat maxlyft — inte i vikt på stången."""

    weight_kg: float
    reps: int


class ExerciseSession(BaseModel):
    """Ett pass med EN övning, som progressionskurvan ritar det."""

    day: str
    sets: int
    measured_sets: int      # set med både vikt och reps; resten går inte att räkna på
    reps_total: int
    top_weight_kg: float | None
    volume_kg: float
    estimated_1rm: float | None
    best_set: BestSet | None


class ExerciseRecord(BaseModel):
    value: float
    day: str
    # Repsen vikten lyftes för. Bara tyngsta-vikten-rekordet har dem: det
    # är det enda av måtten som beskriver ett lyft som faktiskt utfördes,
    # och "120 kg" utan reps är bara halva beskedet.
    reps: int | None = None


class ExerciseProgression(BaseModel):
    """Underlaget till progressionskurvan för en övning.

    Dashboarden visade bara total volym per vecka. Den säger hur mycket du
    lyft, inte om bänkpressen går uppåt — och databasen har ett års
    underlag per övning som ingenting läste. Se analysis/strength.py för
    varför ett skattat maxlyft behövs vid sidan av tyngsta stången.
    """

    exercise: str
    sessions: list[ExerciseSession]     # äldst först, som en kurva läses
    # Rekord över HELA historiken, inte över de pass kurvan visar. Räknade
    # ur fönstret fick bänkpressen rekordet 89,4 kg medan det riktiga —
    # 120 kg den 5 maj — låg utanför och aldrig syntes.
    records: dict[str, ExerciseRecord | None]
    # VILKA rekord det senaste passet satte, inte om det satte något.
    # Ett ensamt ja/nej gjorde kortet motsägelsefullt: push press
    # 2026-09-03 tog rekordet i tyngsta stången men inte i skattat
    # maxlyft, och noten skrev "nytt rekord" ovanför en avläsning som
    # visade 60,5 mot rekordets 63. Båda talen var rätta — påståendet
    # sa bara inte vilket av dem det gällde.
    records_set: list[str]


class StrengthVolume(BaseModel):
    """Underlaget till styrkevolymen på dashboarden.

    Finns för att CTL/ATL — det formbandet ritar — räknas ur pulsen, och
    puls är ett dåligt mått på att lyfta tungt. Uppmätt på skarp data över
    90 dagar: styrketräningen är 51 % av träningstiden men 22 % av
    belastningen (53 TSS/timme mot löpningens 213), eftersom snittpulsen i
    ett gympass ligger på 94 mot löpningens 142. Intervals har varken
    power_load, pace_load eller RPE för de passen, bara HRSS.

    Halva träningen syntes alltså knappt i den enda bilden dashboarden gav
    av den. Volym (reps × vikt) är måttet som faktiskt beskriver arbetet.
    """

    weeks: list[StrengthWeek]           # äldst först, tomma veckor inkluderade
    average_volume_kg: float | None     # snitt över AVSLUTADE veckor
    top_lift: TopLift | None


_BASELINE_FIELDS = ("sleep_score", "hrv", "resting_hr", "steps")

# Nycklar /api/wellness redan använder till annat. Baslinjeserierna sprids
# in i samma svarsdict, så ett fältnamn som krockar hade tyst skrivit över
# formbandets data i stället för att ge ett fel någon märker.
_RESERVED_WELLNESS_KEYS = frozenset(
    {"latest", "stale", "days", "ctl", "atl", "tsb", "baselines"}
)
assert not (_RESERVED_WELLNESS_KEYS & set(_BASELINE_FIELDS)), (
    "Ett fält i _BASELINE_FIELDS krockar med en nyckel /api/wellness redan "
    "använder — serien hade skrivit över den utan att något klagat."
)


def _baselines(history: list[dict[str, Any]]) -> dict[str, dict[str, float | int]]:
    """Snitt, lägsta och högsta per mätvärde över historiken.

    Dashboarden visar dagens siffra mot det här spannet i stället för
    ensam. min/max behövs för att kunna placera dagens värde i spannet
    visuellt — utan dem går det att skriva ut avvikelsen men inte att
    visa var i sin normala variation kroppen ligger.

    Dagar som saknar ett värde hoppas över per fält (Intervals levererar
    inte alltid alla mätvärden varje dygn), så en lucka i HRV inte
    förstör baslinjen för sömnen.
    """
    out: dict[str, dict[str, float | int]] = {}
    for field in _BASELINE_FIELDS:
        values = [
            float(row[field]) for row in history
            if row.get(field) is not None
        ]
        if not values:
            continue
        out[field] = {
            "mean": round(sum(values) / len(values), 1),
            "min": min(values),
            "max": max(values),
            "days": len(values),
        }
    return out


# Hur många kalenderveckor styrkevolymen visas över.
#
# Veckor och inte dagar: du lyfter 2-3 gånger i veckan, så en dagserie
# hade varit mest nollor. Tolv veckor ger tolv staplar — nog för att en
# trend ska synas, få nog för att gå att läsa på en telefon. Ett år hade
# gett 52.
_STRENGTH_WEEKS = 12

# Hur många pass progressionskurvan visar, och hur få pass en övning får
# ha för att komma med i väljaren.
#
# Tolv pass är ungefär ett halvårs bänkpress (37 pass på tretton månader)
# och en dryg månads push press (41 pass). Fönstret räknas i pass och inte
# i veckor just därför — en övning som körs sällan ska ändå ha en kurva.
_PROGRESSION_SESSIONS = 12
_PROGRESSION_MIN_SESSIONS = 3


def _week_start(day: date) -> date:
    """Måndagen i `day`:s vecka."""
    return day - timedelta(days=day.weekday())


def _weekly_strength_volume(
    rows: list[dict[str, Any]], weeks: int, today: date | None = None
) -> list[dict[str, Any]]:
    """Volym och antal pass per kalendervecka, äldst först.

    Volym = summan av reps × vikt — samma formel som fmt_strength_sessions
    i analysis/pipeline.py och _strength_total_volume_kg här, alltså ett
    mått på utfört arbete och inte tre.

    TOMMA veckor kommer med, med volume_kg = 0. Utan dem hade en vecka du
    inte lyfte försvunnit ur serien i stället för att synas som ett hål,
    och staplarna hade ljugit om hur jämnt du tränat: fyra pass på fyra
    veckor sett likadant ut som fyra pass på två. Det finns gott om sådana
    veckor i historiken.

    Ett pass = en aktivitet, inte en dag. Det spelar roll: en dag med två
    separata gympass (förmiddag och kväll), och räknat per dag
    blev det ett — vilket både gav fel antal i "du har lyft N veckor" och
    gjorde det omöjligt att länka stapeln till rätt pass.

    Varje vecka bär därför sina 'passes': aktivitets-id, dag och volym, så
    stapeln kan delas i en klickbar del per pass.
    """
    today = today or datetime.now().date()
    first = _week_start(today) - timedelta(weeks=weeks - 1)
    buckets: dict[date, dict[str, Any]] = {
        first + timedelta(weeks=i): {
            "start": (first + timedelta(weeks=i)).isoformat(),
            "volume_kg": 0.0,
            "sessions": 0,
            "passes": [],
        }
        for i in range(weeks)
    }
    # Passen i veckan, nycklade på aktivitet. Ett set utan activity_id (t.ex.
    # chattloggat innan passet synkats) kan inte länkas till någon sida, men
    # volymen ska ändå med — sådana rader samlas per dag i stället.
    seen: dict[date, dict[tuple[str, str | None], dict[str, Any]]] = {
        start: {} for start in buckets
    }

    for row in rows:
        raw_day = row.get("day")
        if not raw_day:
            continue
        try:
            day = date.fromisoformat(str(raw_day))
        except ValueError:
            continue
        start = _week_start(day)
        bucket = buckets.get(start)
        if bucket is None:
            continue

        activity_id = row.get("activity_id") or None
        key = (str(raw_day), activity_id)
        session = seen[start].get(key)
        if session is None:
            session = {
                "activity_id": activity_id,
                "day": str(raw_day),
                "volume_kg": 0.0,
            }
            seen[start][key] = session
            bucket["sessions"] += 1

        reps, weight = row.get("reps"), row.get("weight_kg")
        if reps is not None and weight is not None:
            bucket["volume_kg"] += reps * weight
            session["volume_kg"] += reps * weight

    result = [buckets[first + timedelta(weeks=i)] for i in range(weeks)]
    for week in result:
        week["volume_kg"] = round(week["volume_kg"], 1)
        # Kronologiskt, så staplarnas delar staplas i den ordning passen
        # faktiskt kördes.
        week["passes"] = [
            {**s, "volume_kg": round(s["volume_kg"], 1)}
            for s in sorted(
                seen[date.fromisoformat(week["start"])].values(),
                key=lambda s: (s["day"], s["activity_id"] or ""),
            )
        ]
    return result


def _average_completed_week(weeks: list[dict[str, Any]]) -> float | None:
    """Snittvolym över de AVSLUTADE veckorna i serien.

    Den sista veckan är den pågående och utelämnas. Räknas den med sjunker
    snittet varje måndag och stiger igen mot söndagen — en referenslinje
    som rör sig med veckodagen jämför inte längre någonting.

    Nollveckor räknas MED: de är riktiga veckor du inte lyfte, och ett
    snitt som hoppar över dem svarar på frågan "hur mycket lyfter jag när
    jag lyfter?" i stället för "hur mycket lyfter jag?".
    """
    completed = weeks[:-1]
    if not completed:
        return None
    return round(sum(w["volume_kg"] for w in completed) / len(completed), 1)


def _top_lift(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Tyngsta enskilda lyftet i fönstret, och i vilken övning.

    Volymen säger hur mycket arbete som utförts, men inte om du blivit
    starkare — två lättare pass kan ge samma volym som ett tungt. Det här
    är den andra halvan av svaret, och den enda siffran i kortet som inte
    går att läsa ur staplarna.

    Bara set med både vikt och reps räknas (measurable_sets), samma regel
    som progressionen och analyserna. Ett set klockan inte repsräknade
    är inget lyft du vet att du gjorde.
    """
    best: dict[str, Any] | None = None
    for row in measurable_sets(rows):
        weight = float(row["weight_kg"])
        exercise = row.get("exercise")
        if not exercise:
            continue
        if best is None or weight > best["weight_kg"]:
            best = {"exercise": str(exercise), "weight_kg": weight}
    return best


def _strength_source_label(rows: list[dict[str, Any]]) -> str:
    """Var kom passets styrkeset ifrån? Rubrik till tabellen på passidan.

    Stod tidigare som den fasta texten "Loggat via chatten", vilket var
    sant när manuell chattloggning var enda källan. Sedan FIT-importen
    (source='fit') kom till märks automatiskt hämtade set fel — de kommer
    från klockan, inte från något du skrivit.
    """
    sources = {row.get("source") or "chat" for row in rows}
    if sources == {"fit"}:
        return "Importerat från klockans träningsfil"
    if sources == {"chat"}:
        return "Loggat via chatten"
    return "Från klockans träningsfil och chatten"


def _strength_total_volume_kg(rows: list[dict[str, Any]]) -> float | None:
    """Total volym (reps × vikt, summerat över alla set) för ett pass.

    Styrkepassets motsvarighet till distans på ett löppass — samma
    volymformel som fmt_strength_sessions i analysis/pipeline.py, men
    summerad över hela passet i stället för per övning. None om inget set
    har både reps och vikt, så gauge:n kan döljas helt i stället för att
    visa "0 kg".
    """
    total = 0.0
    has_value = False
    for row in rows:
        reps = row.get("reps")
        weight = row.get("weight_kg")
        if reps is not None and weight is not None:
            total += reps * weight
            has_value = True
    return round(total, 1) if has_value else None


def _fmt_number(value: float | None, decimals: int = 1, *, fixed: bool = False) -> str:
    """Ett tal på svenska: mellanslag mellan tusental, komma som decimaltecken.

    Samma form som dashboardens toLocaleString('sv-SE'), med hårt
    mellanslag (U+00A0) så att "5 145" aldrig bryts över två rader.
    Passidan skrev "5145 kg", "82.5 kg" och "28.8 km/h" medan dashboarden
    skrev "1 354 kg" och "82,5 kg".

    Utan fixed fälls decimalerna bort när de är noll ("130", inte
    "130,0"), precis som %g gjorde för vikterna. Med fixed står de alltid
    ("28,0 km/h"), för mått där antalet decimaler är en del av formen.
    """
    if value is None:
        return "–"
    rounded = round(float(value), decimals)
    places = decimals if fixed or rounded != int(rounded) else 0
    text = f"{rounded:,.{places}f}"
    return text.replace(",", " ").replace(".", ",")


def _fmt_duration(seconds: int | None) -> str:
    """Passets längd som "1 h 18 min", "1 h" eller "48 min".

    Samma regel som duration() i dashboardens passlista, så ett pass har
    samma längd på båda sidorna. Här stod "1h 18m" och "48m 12s" medan
    listan skrev "1 h 18 min" och "48 min" om samma pass. Avrundas till
    hela minuter; bara ett pass under en halv minut anges i sekunder.
    """
    if not seconds:
        return "–"
    # + 0.5 och inte round(): Python avrundar halvor till jämnt tal (150 s
    # -> 2 min), JavaScripts Math.round uppåt (-> 3). Listan och passidan
    # ska inte kunna skilja sig på en minut.
    minutes = int(int(seconds) / 60 + 0.5)
    if minutes == 0:
        return f"{int(seconds)} s"
    h, m = divmod(minutes, 60)
    if not h:
        return f"{m} min"
    return f"{h} h" if not m else f"{h} h {m} min"


def _fmt_distance(meters: float | None) -> str:
    """Distans som "14,20 km" — två decimaler och komma, som passlistan.

    Stod som "14.2 km": decimalpunkt och en decimal, mot listans "14,20
    km" för samma pass."""
    if meters is None:
        return "–"
    return f"{_fmt_number(meters / 1000.0, 2, fixed=True)} km"


# Intervals passtyper på svenska. Bara visning: en typ som saknas här står
# kvar som Intervals skriver den, hellre än att gissas fram. Passidan
# visade tidigare typen rakt av, i versaler: "WEIGHTTRAINING".
_SPORT_LABELS = {
    "Run": "Löpning",
    "TrailRun": "Terränglöpning",
    "VirtualRun": "Löpning inomhus",
    "Ride": "Cykling",
    "VirtualRide": "Cykling inomhus",
    "MountainBikeRide": "Mountainbike",
    "GravelRide": "Grusvägscykling",
    "EBikeRide": "Elcykel",
    "Walk": "Promenad",
    "Hike": "Vandring",
    "Swim": "Simning",
    "OpenWaterSwim": "Simning i öppet vatten",
    "WeightTraining": "Styrketräning",
    "Workout": "Träningspass",
    "Crossfit": "Crossfit",
    "Yoga": "Yoga",
    "Pilates": "Pilates",
    "Rowing": "Rodd",
    "VirtualRow": "Rodd inomhus",
    "Elliptical": "Crosstrainer",
    "StairStepper": "Trappmaskin",
    "NordicSki": "Längdskidor",
    "AlpineSki": "Utförsåkning",
    "RockClimbing": "Klättring",
    "Kayaking": "Kajak",
}


def _sport_label(sport: str | None) -> str:
    """Passtypen på svenska, eller Intervals egen text om den är okänd."""
    if not sport:
        return "Pass"
    return _SPORT_LABELS.get(sport, sport)


_static_stamps: dict[str, tuple[float, int, str]] = {}


def _static_url(path: str) -> str:
    """URL till en statisk fil med en innehållsstämpel i query-strängen.

    Utan stämpeln serverar webbläsaren gammal CSS och JS efter en
    uppdatering tills man gör en hård omladdning — det har inträffat vid
    varje deploy, och en ändring som fungerar på servern ser då ut att
    inte ha slagit igenom.

    Stämpeln är en hash av filens innehåll, inte av starttiden: URL:en
    ändras alltså bara när filen faktiskt ändrats. En stämpel per start
    hade tvingat webbläsaren att hämta om allt vid varje omstart av
    tjänsten, även när ingenting rörts.

    Resultatet cachas på filens mtime och storlek, så hashen räknas om
    först när filen skrivits — inte vid varje sidvisning.
    """
    file = _STATIC / path
    try:
        stat = file.stat()
    except OSError:
        # Saknad fil ska inte fälla sidrenderingen; låt den 404:a som
        # vanligt i stället.
        log.warning("Statisk fil saknas: %s", path)
        return f"/static/{path}"

    cached = _static_stamps.get(path)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return f"/static/{path}?v={cached[2]}"

    digest = hashlib.sha256(file.read_bytes()).hexdigest()[:10]
    _static_stamps[path] = (stat.st_mtime, stat.st_size, digest)
    return f"/static/{path}?v={digest}"


def _trim_chat_history(history: Any) -> list[dict[str, str]]:
    """Rensar och kortar historiken som webbläsaren skickar med.

    Två skäl:

    1. Längden. Hela konversationen skickades tidigare med vid varje nytt
       meddelande, så antalet tokens (och kostnaden) växte kvadratiskt
       med samtalets längd, och ett tillräckligt långt samtal hade till
       slut spräckt kontextfönstret.
    2. Innehållet. Historiken kommer från klienten och gick rakt in i
       Anthropic-anropet utan kontroll — ett felformat meddelande blev
       ett 500-fel i stället för ett begripligt svar.

    Anthropic kräver att konversationen börjar med ett user-meddelande,
    så efter avkortningen kastas eventuella inledande assistant-svar.

    Meddelanden över _MAX_CHAT_INPUT_CHARS hoppas över, av samma skäl som
    felformade gör det: en historik är 20 poster men hade ingen gräns för
    hur långa de fick vara, och en välartad klient kan aldrig komma över
    taket (våra egna svar kapas vid _MAX_CHAT_CHARS, användarens
    meddelanden avvisas av routen).
    """
    if not isinstance(history, list):
        return []

    cleaned: list[dict[str, str]] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if (
            role in ("user", "assistant")
            and isinstance(content, str)
            and content
            and len(content) <= _MAX_CHAT_INPUT_CHARS
        ):
            cleaned.append({"role": role, "content": content})

    trimmed = cleaned[-_MAX_CHAT_HISTORY_MESSAGES:]
    while trimmed and trimmed[0]["role"] != "user":
        trimmed.pop(0)
    return trimmed


def _fmt_pace(speed_ms: float | None) -> str | None:
    """Tempo i min/km från hastighet i m/s, t.ex. 2.6 -> '6:25 /km'."""
    if not speed_ms or speed_ms <= 0:
        return None
    seconds_per_km = 1000.0 / speed_ms
    minutes, seconds = divmod(int(round(seconds_per_km)), 60)
    return f"{minutes}:{seconds:02d} /km"


# Sporter där tempo (min/km) är det naturliga måttet. För cykling anges
# hastighet i km/h i stället.
_PACE_SPORTS = ("run", "walk", "hike", "trail")


def _activity_detail_rows(a: dict[str, Any]) -> list[tuple[str, str]]:
    """Rader till passdetaljtabellen — bara de som faktiskt har ett värde.

    Tabellen visade tidigare samma sex rader för alla sporter, så ett
    styrkepass fick fyra rader med bara "–" (Effekt, NP, Intensitet och
    Kadens är cykel- och löpvärden). En rubrik utan utfall ser ut som
    saknad data snarare än som ett mått som inte finns för sporten.

    Tempo saknades samtidigt helt trots att average_speed fanns i
    databasen — för ett löppass är det den mest intressanta siffran.
    """
    sport = f"{a.get('sport') or ''} {a.get('type') or ''}".lower()
    rows: list[tuple[str, str]] = []

    def add(label: str, value: Any, suffix: str = "") -> None:
        if value is not None:
            rows.append((label, f"{value}{suffix}"))

    add("Snittpuls", a.get("average_heart_rate"), " bpm")
    add("Maxpuls", a.get("max_heart_rate"), " bpm")

    speed = a.get("average_speed")
    if speed:
        if any(s in sport for s in _PACE_SPORTS):
            add("Tempo", _fmt_pace(speed))
        else:
            add("Snittfart", _fmt_number(speed * 3.6, 1, fixed=True), " km/h")

    cadence = a.get("average_cadence")
    if cadence is not None:
        # Varv/min är aldrig meningsfullt med decimaler.
        add("Kadens", int(round(cadence)))

    # Talen på svenska, med decimalkomma — se _fmt_number.
    for label, key in (("Effekt", "average_watts"), ("NP", "normalized_watts")):
        if a.get(key) is not None:
            add(label, _fmt_number(a[key], 0), " W")
    if a.get("intensity") is not None:
        add("Intensitet", _fmt_number(a["intensity"], 2))
    return rows


def _fmt_date(iso: str | None) -> str:
    """Passets tidpunkt som "Lördag 19 september · 07:00".

    Stod som "2026-09-19 07:00" — maskinens format, på en sida där
    dashboarden bredvid skriver "Måndag 21 september". Årtalet skrivs bara
    ut när passet inte är från i år, som i progressionens datum.

    Klockslaget är det som står i tidsstämpeln, utan omräkning: Intervals
    start_date_local är redan lokal tid, även när den bär en offset.
    Veckodagar och månader kommer från swedish_date_label, så det finns
    en enda lista med svenska namn.
    """
    if not iso:
        return "–"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:16].replace("T", " ")
    label = swedish_date_label(dt)
    if dt.year == date.today().year:
        label = label.rsplit(" ", 1)[0]
    if "T" not in iso:
        return label
    return f"{label} · {dt:%H:%M}"


_TEMPLATES.env.filters["duration"] = _fmt_duration
_TEMPLATES.env.filters["distance"] = _fmt_distance
_TEMPLATES.env.filters["fmtdate"] = _fmt_date
_TEMPLATES.env.filters["tal"] = _fmt_number
_TEMPLATES.env.filters["sporttyp"] = _sport_label


# Här låg tidigare filtret `md`, som renderade en analys till HTML med
# Python-Markdown och märkte resultatet som säker HTML via Markup().
#
# Det var en XSS-lucka. Python-Markdown släpper igenom rå HTML i indata,
# och Markup() stänger av Jinjas escaping — så allt det som
# static/markdown.js byggdes för att stoppa gick rakt igenom på
# aktivitetssidan. Analysens text kommer från Claude men är byggd av data
# vi inte äger: passnamn från Intervals och notes från chattloggade set.
# Ett pass som heter '<img src=x onerror=...>' hamnar i analysens
# underlag, ekas i texten, och kördes av webbläsaren. Verifierat skarpt.
#
# Aktivitetssidan lägger nu markdownen som TEXT i elementet (Jinja escapar
# den) och låter renderAnalysisElement i markdown.js parsa och sanera den,
# precis som dashboardens tre analysrutor redan gjorde. En renderare, en
# sanerare — det var två vägar som gjorde att den här luckan kunde
# överleva att den stängdes på det andra stället.

# Mallarna länkar statiska filer via static_url(), inte med en rå sökväg,
# så webbläsaren hämtar om dem när de ändrats.
_TEMPLATES.env.globals["static_url"] = _static_url



# Vilka block ett misslyckat "Generera" kan peka tillbaka på: värdet i
# ?fel= på dashboarden. Allt annat i parametern ignoreras.
_ANALYSIS_ERROR_KEYS = ("morning", "coaching", "evening")


def _analysis_failed(path: str, which: str) -> RedirectResponse:
    """Tillbaka till sidan, med ett felbesked i analysens block.

    Ett misslyckat "Generera" visade FastAPI:s felsvar rakt i webbläsaren
    — en rå JSON-rad på en vit sida, utan väg tillbaka. Nu landar man där
    man tryckte (formulärets #ankare följer med genom omdirigeringen) och
    blocket säger vad som hände. Undantaget loggas av anroparen; beskedet
    säger bara att det finns i loggen, precis som _server_error.

    Omdirigeringen bär ingen token. Den gjorde det tidigare, och varje
    "Generera" lämnade då tokenet i adressfältet och i historiken. Kakan
    följer med av sig själv.
    """
    return RedirectResponse(url=f"{path}?fel={which}", status_code=303)


# Kakan som låter en enhet slippa bära token i varje adress.
#
# Utan den måste ?token=... finnas med i varje bokmärke, på varje enhet,
# och adressen är obrukbar om man bara skriver pi5:8000 i adressfältet.
# Kakan är också det enda som bär inloggningen inne i appen: sidorna,
# länkarna och fetch-anropen innehåller inget token. Se
# _trade_token_for_cookie i create_app.
#
# Ett år: det här är en bekvämlighetsnyckel på ett hemnätverk, inte en
# session. Byter du WEB_ACCESS_TOKEN blir gamla kakor ogiltiga av sig
# själva — jämförelsen sker mot värdet i .env, inte mot något sparat.
_TOKEN_COOKIE = "garmin_ai_token"
_TOKEN_COOKIE_MAX_AGE = 365 * 24 * 3600


def _token_matches(supplied: str | None, expected: str) -> bool:
    """Konstant-tidsjämförelse av en token mot den förväntade.

    Jämför BYTES, inte str. hmac.compare_digest vägrar jämföra strängar
    med icke-ASCII-tecken ("TypeError: comparing strings with non-ASCII
    characters is not supported"), så en token med t.ex. å, ä eller ö gav
    ett ohanterat 500-fel i stället för 401 — och en sådan token i .env
    hade gjort hela sajten obrukbar.
    """
    if not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _make_token_checker(expected_token: str):
    """Skapar en FastAPI-dependency som kräver token om WEB_ACCESS_TOKEN
    är satt — antingen som ?token=... eller som kakan _TOKEN_COOKIE.

    Registreras en gång som global dependency på hela appen (se create_app)
    istället för att varje route implementerar sin egen kopia av kontrollen
    — annars är det lätt att glömma bort den på en ny route i framtiden.

    Kakan SÄTTS inte här utan i middlewaren nedan. Anledningen är inte
    stilistisk: FastAPI slår bara ihop en dependency-Response headers med
    svaret när routen returnerar ett värde som ska serialiseras. Våra
    sidor returnerar färdiga Response-objekt (TemplateResponse,
    RedirectResponse), och för dem kastas dependency-headers bort. En
    Set-Cookie härifrån hade alltså tyst försvunnit på just de svar den
    behövdes för.
    """

    def _check_token(
        request: Request, token: str | None = Query(default=None)
    ) -> None:
        if not expected_token:
            return
        if _token_matches(token, expected_token):
            return
        if _token_matches(request.cookies.get(_TOKEN_COOKIE), expected_token):
            return
        raise HTTPException(status_code=401, detail="Ogiltig token")

    return _check_token


def _address_without_token(request: Request) -> str:
    """Samma adress som förfrågan, utan ?token=. Övriga parametrar
    (fel=...) står kvar.

    Relativ, så omdirigeringen stannar på den värd och port webbläsaren
    redan använder. Inledande snedstreck slås ihop till ett: en sökväg
    som //exempel.se (eller /\\exempel.se, som webbläsare läser likadant)
    hade annars blivit en adress till en annan sajt.
    """
    path = "/" + request.url.path.lstrip("/\\")
    rest = [(k, v) for k, v in request.query_params.multi_items() if k != "token"]
    return path + ("?" + urlencode(rest) if rest else "")


# Vad klienten får veta när något gått sönder på serversidan.
#
# Här skickades undantagets str(exc) rakt ut i sju routes. Den texten är
# skriven för den som felsöker, inte för den som tittar på sidan, och bär med
# sig det som fanns i felet: ett fel från Anthropic-klienten innehåller
# request-id och delar av anropet, ett httpx-fel den fullständiga URL:en
# med query, ett sqlite3-fel den SQL som misslyckades. Två av vägarna
# visar dessutom texten rakt av — /sync alertar `detail` i dashboarden,
# och /analyze/* renderas som FastAPI:s felsida i webbläsaren.
#
# Meddelandet nedan säger vad som inte gick och var man hittar resten.
# Hela undantaget med traceback loggas oförändrat av log.exception på
# raden ovanför varje anrop, och journalen är åtkomlig för den som redan
# har tillgång till maskinen.
def _server_error(vad: str) -> HTTPException:
    return HTTPException(
        status_code=500,
        detail=f"{vad} misslyckades. Felet finns i tjänstens logg.",
    )


def create_app() -> FastAPI:
    settings = load_settings()
    store = Store(settings.db_path, settings.strength_corrections)
    store.init()
    intervals = IntervalsClient(settings)
    claude = ClaudeClient(settings)
    pipeline = AnalysisPipeline(store, claude, settings.athlete)

    app = FastAPI(
        title="Training Analyzer",
        docs_url=None,
        redoc_url=None,
        lifespan=make_lifespan(settings),
        # Global dependency: gäller alla routes på appen (utom /static, som
        # är en separat monterad ASGI-app och aldrig var tokenskyddad).
        dependencies=[Depends(_make_token_checker(settings.web_access_token))],
    )

    # Kom du in med en giltig ?token=... får enheten en kaka, och slipper
    # bära token i adressen därefter. Se _TOKEN_COOKIE och
    # _make_token_checker för varför kakan sätts här och inte i
    # dependencyn.
    #
    # En SIDA med token i adressen (dashboarden, ett pass) besvaras med en
    # omdirigering till samma adress utan token, och kakan följer med på
    # den. Adressfältet och en skärmdump visar därmed aldrig tokenet, och
    # sidorna man sedan går vidare till hamnar i historiken utan det.
    # Förut visades sidan på adressen med token, och sidan bar det vidare
    # i varje länk, formulär och fetch-anrop, trots att kakan redan fanns.
    #
    # /api/* och skrivande anrop besvaras som förut. De syns aldrig i
    # adressfältet, och ett skript utan kakburk (curl) hade annars fått en
    # omdirigering till en adress där det saknar inloggning.
    #
    # Kakan sätts bara på lyckade svar. Ett 401 ska inte dela ut en
    # inloggning, och ett 500 betyder att vi inte vet vad som hände.
    # Omdirigeringen räknas som lyckad: tokenet är redan kontrollerat.
    #
    # samesite=lax stramar dessutom åt skrivskyddet: en kaka med Lax följer
    # inte med på en POST från en annan sajt, så cross-site-formuläret som
    # _reject_cross_site_writes nedan stoppar skulle numera inte ens vara
    # autentiserat. httponly eftersom sidans JavaScript inte behöver läsa
    # den — fetch skickar med den av sig själv, och det som inte går att
    # läsa går inte att läcka via en XSS-lucka.
    #
    # secure sätts INTE: appen körs över vanlig http på ett hemnätverk, och
    # en secure-kaka hade aldrig skickats tillbaka alls.
    @app.middleware("http")
    async def _trade_token_for_cookie(request: Request, call_next: Any) -> Any:
        expected = settings.web_access_token
        if not expected or not _token_matches(
            request.query_params.get("token"), expected
        ):
            return await call_next(request)
        response: Any
        if request.method == "GET" and not request.url.path.startswith("/api/"):
            response = RedirectResponse(
                url=_address_without_token(request), status_code=303
            )
        else:
            response = await call_next(request)
            if response.status_code >= 400:
                return response
        # _token_matches, inte ==. Frågan här är ofarlig i sig (ska kakan
        # sättas om?), men det här var enda stället i filen som jämförde
        # en token med == medan de tre andra går via hjälparen. En
        # jämförelse som avbryts vid första olika tecknet läcker hur
        # många som stämde, och undantaget "just den här jämförelsen
        # spelar ingen roll" är precis det som är svårt att hålla i
        # huvudet nästa gång koden flyttas.
        if _token_matches(request.cookies.get(_TOKEN_COOKIE), expected):
            return response
        response.set_cookie(
            _TOKEN_COOKIE,
            expected,
            max_age=_TOKEN_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    # Skrivande förfrågningar ska komma från appens egna sidor, inte från
    # en annan sajt du råkar ha öppen i samma webbläsare.
    #
    # /analyze/* och /sync muterar databasen och kostar ett Claude-anrop
    # var. De låg på GET, och sidan krävde då inget token, så ett
    # <img src="http://raspberrypi.local:8000/analyze/coaching"> på
    # vilken sajt som helst räckte för att avfyra dem. Webbläsares
    # länkförhämtning kunde göra samma sak av misstag.
    #
    # De är POST nu, men POST ensamt stänger inte hålet: ett formulär får
    # posta tvärs över origin, det är bara SVARET som blir oläsbart för
    # angriparen — och här är det sidoeffekten som är nyttan, inte svaret.
    #
    # Sec-Fetch-Site är det som faktiskt stänger det. Huvudet sätts av
    # webbläsaren, inte av sidan, och kan därför inte förfalskas eller
    # utelämnas av en angripare. "cross-site" är precis det fallet vi vill
    # bort.
    #
    # Saknas huvudet helt släpps förfrågan igenom: curl, TestClient och
    # äldre webbläsare skickar det inte alls, och att kräva det hade brutit
    # dem utan att stoppa någon.
    @app.middleware("http")
    async def _reject_cross_site_writes(request: Request, call_next: Any) -> Any:
        if (
            request.method not in ("GET", "HEAD", "OPTIONS")
            and request.headers.get("sec-fetch-site") == "cross-site"
        ):
            log.warning(
                "Avvisade %s %s: förfrågan kom från en annan sajt.",
                request.method, request.url.path,
            )
            return JSONResponse(
                status_code=403,
                content={"detail": "Förfrågan kom från en annan sajt."},
            )
        return await call_next(request)

    app.state.settings = settings
    app.state.store = store
    app.state.intervals = intervals
    app.state.pipeline = pipeline

    _STATIC.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        """Dashboarden. Hämtar det mesta av sin data via /api/* i webbläsaren.

        Routen skickade tidigare med aktiviteter, 30 dagars wellness, tre
        senaste analyser och två wellness-dagar (varav båda dessutom
        hämtade samma dag, trots namnen). Inget av det användes av
        templaten — den läste bara `token` och `day_label` — så det var
        sju databasfrågor per sidladdning vars resultat kastades direkt.

        Hälsningen räknas ut per sidladdning (inte i webbläsaren) så att
        det bara finns en definition av vilka klockslag som är morgon och
        kväll. Den blir därmed stillastående om sidan lämnas öppen över en
        gräns — samma sak gäller redan all annan data här, som hämtas en
        gång vid laddning.
        """
        store = request.app.state.store
        today = datetime.now().date().isoformat()
        # Dagens synk (var 60:e minut, se scheduler.py) har kanske inte
        # kört än när sidan laddas — särskilt tidigt på morgonen, precis
        # efter midnatt. get_wellness_day(today) ger då None, och utan
        # fallback stod rubriken "Fredagens ..." över gårdagens tal (eller,
        # innan detta, över tomma "-"-värden hela förmiddagen). Samma dag
        # som faktiskt visas i hero-metrics (se /api/wellness) används här
        # för rubriken, så de två aldrig kan peka på olika datum.
        wellness = store.get_wellness_day(today) or store.latest_wellness_day()
        wellness_day = wellness["day"] if wellness else today
        return _TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                # Blocket vars "Generera om" nyss misslyckades, om något —
                # se _analysis_failed. Okända värden ignoreras.
                "analysis_error": (
                    fel if (fel := request.query_params.get("fel"))
                    in _ANALYSIS_ERROR_KEYS else None
                ),
                "greeting": _greeting(settings.athlete.name),
                "open_analysis": _open_analysis(settings.morning_recommendation_time),
                # Rubriken namnges efter DAGEN DATAN GÄLLER, inte
                # nödvändigtvis dagens datum — se kommentaren ovan.
                "day_label": swedish_weekday_genitive(wellness_day),
                # Fullt datum till "Kroppen"-rubriken. Samma format som
                # analyserna själva sätter som sin H1, så sidan inte har
                # två sätt att skriva ett datum. Gäller dagen DATAN är
                # från, inte nödvändigtvis idag.
                "wellness_date": swedish_date_label(wellness_day),
                "wellness_is_stale": wellness_day != today,
                # Klockslaget datan senast synkades, så det går att se hur
                # färsk sidan är utan att gissa.
                "synced_at": _fmt_clock(wellness.get("last_synced") if wellness else None),
                # Formbandets rubrik och baslinjernas etikett skrev "30
                # dagar" som fast text medan fönstret bor i
                # _LOAD_HISTORY_DAYS här. En ändring av konstanten hade
                # alltså tyst gjort rubrikerna osanna. Samma skäl som
                # MAX_ANALYSIS_CHARS och MAX_CHAT_CHARS flyttades för.
                "history_days": _LOAD_HISTORY_DAYS,
                # Samma skäl som history_days: rubriken över styrkevolymen
                # ska inte kunna säga ett annat antal veckor än serien
                # faktiskt innehåller.
                "strength_weeks": _STRENGTH_WEEKS,
            },
        )

    @app.get("/activity/{activity_id}", response_class=HTMLResponse)
    def activity(request: Request, activity_id: str) -> HTMLResponse:
        store = request.app.state.store
        a = store.get_activity(activity_id)
        if not a:
            raise HTTPException(status_code=404, detail="Aktivitet saknas")
        analysis = store.latest_activity_analysis(activity_id)
        # Styrkeset för det här passet — antingen importerade ur klockans
        # FIT-fil eller loggade via chatten (se strength_sets i
        # sync/store.py). Intervals eget API har ingen sådan data alls.
        strength_sets = store.get_strength_sets_for_activity(activity_id)
        return _TEMPLATES.TemplateResponse(
            request,
            "activity.html",
            {
                "activity": a,
                "analysis": analysis,
                "strength_sets": strength_sets,
                "strength_source_label": _strength_source_label(strength_sets),
                "strength_total_volume_kg": _strength_total_volume_kg(strength_sets),
                "detail_rows": _activity_detail_rows(a),
                # "Generera analys" misslyckades — se _analysis_failed.
                "analysis_error": request.query_params.get("fel") == "analys",
            },
        )

    @app.post("/analyze/activity/{activity_id}")
    def analyze_activity(request: Request, activity_id: str) -> RedirectResponse:
        pipeline = request.app.state.pipeline
        try:
            pipeline.analyze_activity(activity_id)
        except Exception:
            log.exception("Analys misslyckades")
            return _analysis_failed(f"/activity/{activity_id}", "analys")
        return RedirectResponse(url=f"/activity/{activity_id}", status_code=303)

    @app.post("/analyze/morning")
    def analyze_morning(request: Request) -> RedirectResponse:
        try:
            request.app.state.pipeline.morning_recommendation()
        except Exception:
            log.exception("Morgonanalys misslyckades")
            return _analysis_failed("/", "morning")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/analyze/evening")
    def analyze_evening(request: Request) -> RedirectResponse:
        """"Generera om" på kvällssammanfattningen.

        Kör evening_summary() — samma metod som det schemalagda jobbet
        (run_evening_summary kl EVENING_SUMMARY_TIME). Knappen pekade
        tidigare på /analyze/daily, en äldre dagsanalys med en helt annan
        payload (30 pass och 90 dagars wellness) som sparades under samma
        analystyp. Samma ruta på dashboarden visade då omväxlande två
        olika sorters analys beroende på om den kommit från schemat eller
        från knappen. Den äldre vägen är borttagen.
        """
        try:
            request.app.state.pipeline.evening_summary()
        except Exception:
            log.exception("Kvällssammanfattning misslyckades")
            return _analysis_failed("/", "evening")
        return RedirectResponse(url="/", status_code=303)

    @app.post("/analyze/coaching")
    def analyze_coaching(request: Request) -> RedirectResponse:
        try:
            request.app.state.pipeline.coaching()
        except Exception:
            log.exception("Coaching-analys misslyckades")
            return _analysis_failed("/", "coaching")
        return RedirectResponse(url="/", status_code=303)

    @app.get("/api/wellness")
    def api_wellness(request: Request) -> dict[str, Any]:
        """Senaste wellness-dagen plus en veckas serie för belastningen.

        `latest` driver hero-metrics överst på dashboarden. Serierna
        (days/ctl/atl/tsb) driver diagrammet och tabellen i Belastning-
        sektionen — de läste tidigare fält som endpointen aldrig
        returnerade, så diagrammet renderades alltid tomt och tabellen
        var permanent tom.

        `latest` faller tillbaka till senaste kända dag om dagens synk
        inte kommit in än, hellre än att visa "-" för sömnscore, HRV och
        vilopuls tills schemaläggarens sync-jobb hunnit köra (var 60:e
        minut). `stale` talar om för frontend att det som visas är
        gårdagens (eller äldre) tal, så den kan flagga det i stället för
        att tyst låtsas att det är dagens.
        """
        store = request.app.state.store
        today = datetime.now().date().isoformat()
        latest = store.get_wellness_day(today) or store.latest_wellness_day()
        # list_wellness ger nyast först; ett tidsdiagram vill ha stigande
        # datum.
        history = list(reversed(store.list_wellness(days=_LOAD_HISTORY_DAYS)))
        return {
            # Utan raw_json och last_synced — samma regel som analysernas
            # payload till Claude redan följer (wellness_for_payload i
            # analysis/pipeline.py), så det finns en definition av "en
            # wellness-rad utan rådatadubbletten" och inte två.
            #
            # raw_json är hela Intervals-svaret för dygnet, alltså exakt
            # samma mätvärden som kolumnerna bredvid under Intervals egna
            # namn. Uppmätt mot en kopia av den skarpa databasen var
            # dagens rad 1 654 byte, varav 89 är det dashboarden läser.
            #
            # Ingen response_model här: svaret har dynamiska nycklar (en
            # serie per fält i _BASELINE_FIELDS, se spreaden nedan), vilket
            # en modell inte kan beskriva utan att bli lösare än den dict
            # den skulle ersätta.
            "latest": wellness_for_payload(latest) or {},
            "stale": bool(latest) and latest.get("day") != today,
            "days": [w.get("day") for w in history],
            "ctl": [w.get("ctl") for w in history],
            "atl": [w.get("atl") for w in history],
            "tsb": [w.get("tsb") for w in history],
            # Samma 30-dagarsserie per baslinjefält, så varje gauge-kort kan
            # rita en egen trendlinje bredvid dagens siffra — inte bara
            # snittet. Ligger jämsides med "days" (samma index = samma dag),
            # med None där mätvärdet saknas den dagen, så frontend kan hoppa
            # över luckor i stället för att rita en felaktig punkt vid noll.
            **{field: [w.get(field) for w in history] for field in _BASELINE_FIELDS},
            # Baslinjer för sömnscore, HRV och vilopuls. Ett värde utan
            # jämförelse säger ingenting — "92" betyder olika saker för
            # olika kroppar, medan "92, tolv över ditt snitt" betyder
            # något direkt. Räknas här på servern i stället för i
            # webbläsaren, så det finns en enda definition av vad
            # baslinjen är.
            "baselines": _baselines(history),
        }

    @app.get("/api/strength", response_model=StrengthVolume)
    def api_strength(request: Request) -> dict[str, Any]:
        """Styrkevolym per vecka — se StrengthVolume för varför den finns.

        Hämtar exakt så många dagar som fönstret sträcker sig, räknat från
        måndagen i den äldsta veckan. Ett fast dagtal hade antingen missat
        början av den äldsta veckan eller hämtat dagar som ändå kastas.
        """
        store = request.app.state.store
        today = datetime.now().date()
        first = _week_start(today) - timedelta(weeks=_STRENGTH_WEEKS - 1)
        rows = store.list_strength_sets(days=(today - first).days + 1)
        weeks = _weekly_strength_volume(rows, _STRENGTH_WEEKS, today=today)
        return {
            "weeks": weeks,
            "average_volume_kg": _average_completed_week(weeks),
            "top_lift": _top_lift(rows),
        }

    @app.get("/api/strength/exercises", response_model=list[ExerciseSummary])
    def api_exercises(request: Request) -> list[dict[str, Any]]:
        """Övningarna som har tillräckligt med pass för en kurva.

        Minus dem som gömts i PROGRESSION_HIDDEN_EXERCISES. Bara väljaren
        filtreras — progressionen för en gömd övning går fortfarande att
        hämta direkt, och ingenting raderas.
        """
        store = request.app.state.store
        return store.list_exercises(
            min_sessions=_PROGRESSION_MIN_SESSIONS,
            exclude=settings.hidden_exercises,
        )

    @app.get("/api/strength/progression", response_model=ExerciseProgression)
    def api_progression(
        request: Request, exercise: str = Query(min_length=1, max_length=100)
    ) -> dict[str, Any]:
        """Utvecklingen för en övning: pass, rekord, och om det senaste slog.

        Rekorden räknas ur HELA historiken, kurvan visar de senaste
        _PROGRESSION_SESSIONS passen. Ett rekord ur fönstret är inget
        personligt rekord: bänkpressen fick 89,4 kg medan det riktiga,
        120 kg den 5 maj, låg utanför och aldrig syntes.

        Att läsa alla set kostar lite — den flitigaste övningen har 196
        rader — och sparar en andra fråga mot samma tabell.
        """
        store = request.app.state.store
        # Namnet normaliseras här också, inte bara inne i store-lagret:
        # svaret ekar tillbaka det som ETIKETT på kurvan, och ?exercise=
        # Knäböj gav då rubriken "Knäböj" över knäböjens data. Samma
        # normalisering som list_sets_for_exercise gör på vägen in.
        exercise = normalize_exercise(exercise)
        alla = session_progression(store.list_sets_for_exercise(exercise, sessions=None))
        if not alla:
            raise HTTPException(
                status_code=404, detail="Ingen sådan övning är loggad."
            )
        rekord = personal_records(alla)
        # session_progression ger nyast först; kurvan läses från vänster.
        rader = list(reversed(alla[:_PROGRESSION_SESSIONS]))
        senaste = rader[-1]
        return {
            "exercise": exercise,
            "sessions": rader,
            "records": rekord,
            "records_set": [
                falt
                for falt, post in rekord.items()
                if post is not None
                and post["day"] == senaste["day"]
                and senaste.get(falt) is not None
            ],
        }

    @app.get("/api/analyses/morning")
    def api_morning_analysis(request: Request) -> dict[str, Any]:
        store = request.app.state.store
        analysis = store.latest_analysis("morning_recommendation")
        return _analysis_payload(analysis)

    @app.get("/api/analyses/evening")
    def api_evening_analysis(request: Request) -> dict[str, Any]:
        store = request.app.state.store
        # Kvällssammanfattningen sparas under analystypen "daily_summary",
        # ett namn från en äldre dagsanalys som inte finns kvar. Namnet står
        # kvar eftersom varje redan sparad sammanfattning bär det.
        analysis = store.latest_analysis("daily_summary")

        # Faller tillbaka på gårdagens när dagens sammanfattning skrevs om
        # ett dygn som inte hade något att sammanfatta.
        #
        # Trycker man "Generera om" strax efter midnatt får man en korrekt
        # men innehållslös rapport ("ingen stegdata tillgänglig för idag"),
        # och eftersom rutan visar den SENASTE sammanfattningen tog den
        # över platsen från gårdagens riktiga — som ligger kvar i
        # databasen, skriven 23:59 med fullt underlag.
        #
        # Avgörandet läses ur markören som pipeline satte NÄR texten
        # skrevs, inte ur hur dygnet ser ut nu. Skillnaden är hela
        # poängen, och det är andra försöket: första versionen frågade
        # "har dygnet data?" vid varje sidladdning. Det fungerade fram till
        # 07:00, då sömnen synkade — dygnet såg plötsligt fullt ut, och
        # den tomma 00:16-rapporten kom tillbaka. Om rapporten hade något
        # att gå på kan bara avgöras när den skrivs.
        today = datetime.now().date().isoformat()
        if analysis and analysis.get("ref_id") == today:
            if store.get_job_state(EVENING_CONTENT_KEY) == f"{today}:0":
                earlier = store.latest_analysis_before("daily_summary", today)
                if earlier:
                    analysis = earlier
        return _analysis_payload(analysis)

    @app.get("/api/analyses/coaching")
    def api_coaching_analysis(request: Request) -> dict[str, Any]:
        store = request.app.state.store
        analysis = store.latest_analysis("coaching")
        return _analysis_payload(analysis)

    @app.get("/api/activities", response_model=ActivityList)
    def api_activities(request: Request) -> dict[str, Any]:
        """Passlistan på dashboarden.

        response_model filtrerar svaret mot ActivitySummary — se den för
        varför (raw_json utgjorde 97 % av svaret innan).
        """
        store = request.app.state.store
        activities = store.list_activities(limit=10)
        return {"activities": activities}

    @app.post("/sync")
    def sync(request: Request) -> dict[str, Any]:
        """Manuell synk från dashboardens "Synka Intervals"-knapp.

        Saknade tidigare felhantering helt, till skillnad från
        /analyze/*-routerna: ett nätverksfel eller en 401 mot Intervals
        bubblade upp som en oformaterad 500 utan att loggas här. Nu när
        synken dessutom laddar ner FIT-filer per nytt styrkepass tar den
        längre tid och har fler sätt att fela.

        Låset hindrar att en manuell synk krockar med den schemalagda som
        körs varje timme. Två samtidiga synkar skriver till samma
        SQLite-databas, och den andra hade fått "database is locked" efter
        timeout — ett förvirrande fel för något som egentligen bara
        behöver vänta.

        Låset ligger i sync/intervals_client.py och delas med schemaläggarens
        körningar. Det låg tidigare här i app.py, vilket innebar att bara
        den här routen faktiskt tog det — schemaläggarens run_sync gick
        rakt förbi, så krocken kommentaren beskriver kunde inträffa ändå.
        """
        with sync_lock(blocking=False) as acquired:
            if not acquired:
                raise HTTPException(
                    status_code=409,
                    detail="En synk pågår redan. Vänta tills den är klar.",
                )
            try:
                store = request.app.state.store
                intervals = request.app.state.intervals
                a = sync_activities(intervals, store)
                w = sync_wellness(intervals, store)
                sync_sport_settings(intervals, store)
                return {"activities": a, "wellness": w}
            except Exception as exc:
                log.exception("Manuell synk misslyckades")
                raise _server_error("Synkroniseringen") from exc


    # --- Chatt med Claude --------------------------------------------
    @app.post("/chat")
    async def chat_send(request: Request) -> dict[str, str]:
        # Enda routen som behöver vara async def: request.json() måste
        # await:as. Det blockerande Claude-anropet längre ner skickas
        # explicit till en trådpool (run_in_threadpool) så det inte fryser
        # event-loopen medan svaret genereras.
        # En trasig eller icke-JSON kropp är klientens fel, inte serverns —
        # den gav tidigare en ohanterad 500 via request.json().
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(
                status_code=400, detail="Kunde inte tolka meddelandet som JSON."
            ) from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Förväntade ett JSON-objekt.")

        # Typen kontrolleras innan .strip() körs. Kontrollen ovan täcker en
        # kropp som inte är ett objekt, men inte fältets typ: `(body.get(
        # "message") or "").strip()` anropar .strip() på vad som helst som
        # är sant, så {"message": {"a": 1}} gav en ohanterad AttributeError
        # och en 500 — samma trasiga indata, ett annat sätt att vara
        # trasig, motsatt svar.
        raw_msg = body.get("message")
        if raw_msg is not None and not isinstance(raw_msg, str):
            raise HTTPException(
                status_code=400, detail="'message' måste vara en sträng."
            )
        user_msg = raw_msg.strip() if raw_msg else ""
        history = _trim_chat_history(body.get("history"))
        if not user_msg:
            raise HTTPException(status_code=400, detail="Tomt meddelande")
        if len(user_msg) > _MAX_CHAT_INPUT_CHARS:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Meddelandet är för långt (max {_MAX_CHAT_INPUT_CHARS} tecken)."
                ),
            )
        # Efter valideringen, före Claude-anropet: ett trasigt meddelande
        # ska inte äta av budgeten, och ett giltigt ska inte nå Anthropic
        # om taket är passerat.
        if _chat_rate_limit_exceeded():
            log.warning(
                "Chattaket nått (%d anrop / %.0f s) — avvisar.",
                _CHAT_RATE_LIMIT, _CHAT_RATE_WINDOW_SECONDS,
            )
            raise HTTPException(
                status_code=429,
                detail=(
                    "För många meddelanden på kort tid. Vänta en stund "
                    "och försök igen."
                ),
            )

        store = request.app.state.store
        # Kontext: senaste wellness + aktiviteter.
        # Bugg: hämtade tidigare GÅRDAGENS datum (datetime.now() - 1 dag).
        # Intervals lägger sömnen på den dag man vaknar (samma konvention
        # som resten av appen använder, se morning_recommendation() i
        # pipeline.py), så "i natt"-sömnen ligger på DAGENS datum — chatten
        # visade alltså alltid data en dag för gammal.
        #
        # Samma fallback som dashboarden och /api/wellness: har den
        # timvisa synken inte kört sedan midnatt finns ingen rad för
        # idag, och utan fallback svarade chatten "jag har ingen data om
        # din sömn" hela förmiddagen — medan sidan bakom den visade
        # gårdagens siffror. Vilken dag talen gäller står i texten nedan,
        # så Claude kan säga "i natt" eller "i förrgår natt" korrekt.
        today = datetime.now().date().isoformat()
        wellness = store.get_wellness_day(today) or store.latest_wellness_day()
        wellness_day = wellness["day"] if wellness else today
        activities = store.list_activities(limit=5)

        context = (
            "KONTEXT (senaste data):\n"
            f"Senaste wellness (dagens datum {today}, "
            f"talen nedan gäller {wellness_day}): "
            + json.dumps(
                {
                    "sleep_seconds": wellness.get("sleep_seconds") if wellness else None,
                    "sleep_score": wellness.get("sleep_score") if wellness else None,
                    "hrv": wellness.get("hrv") if wellness else None,
                    "resting_hr": wellness.get("resting_hr") if wellness else None,
                    "stress": wellness.get("stress") if wellness else None,
                    "ctl": wellness.get("ctl") if wellness else None,
                    "atl": wellness.get("atl") if wellness else None,
                    "tsb": wellness.get("tsb") if wellness else None,
                },
                default=str,
                ensure_ascii=False,
            )
            + "\nSenaste aktiviteter: "
            + json.dumps(
                [
                    {
                        "name": a.get("name"),
                        "sport": a.get("sport"),
                        "start_time": a.get("start_time"),
                        "tss": a.get("tss"),
                    }
                    for a in activities
                ],
                default=str,
                ensure_ascii=False,
            )
            # Senaste 14 dagarnas självrapporter (vikt/sjukdom/alkohol/etc,
            # se log_self_report-verktyget) — så Claude minns vad du sagt i
            # TIDIGARE chattar/dagar, inte bara det du precis loggade i den
            # här konversationen.
            + "\nSenaste självrapporter (self_reports, från tidigare chattar): "
            + json.dumps(
                # include_ids: chattens verktyg för att rätta och
                # radera adresserar en rad med dess id, och utan id i
                # kontexten kan Claude inte veta vilket det är.
                fmt_self_reports(store.list_self_reports(days=14), include_ids=True),
                default=str,
                ensure_ascii=False,
            )
            # Loggade styrkepass (se log_strength_session-verktyget) av samma
            # skäl som self_reports ovan: utan detta kan Claude inte svara på
            # "vad lyfte jag förra gången?" eller föreslå en rimlig ökning.
            + "\nLoggad styrketräning (strength, aggregerat per dag och övning): "
            + json.dumps(
                fmt_strength_sessions(store.list_strength_sets(days=14)),
                default=str,
                ensure_ascii=False,
            )
            # 30 dagars wellness-historik (samma fönster som dashboardens
            # baslinjer, se _LOAD_HISTORY_DAYS) — utan den hade en fråga som
            # "hur har min HRV sett ut den senaste veckan?" bara kunnat
            # besvaras med dagens enda värde, inte en faktisk trend.
            + "\nWellness senaste 30 dagarna (dagserie, för trendfrågor om "
            "t.ex. HRV, vilopuls, sömn): "
            + json.dumps(
                fmt_wellness_history(store.list_wellness(days=_LOAD_HISTORY_DAYS)),
                default=str,
                ensure_ascii=False,
            )
        )

        pipeline = request.app.state.pipeline
        # _prompt fyller atletprofilen med senast kända vikt, så chatten
        # utgår från samma siffra som analyserna.
        prompt = pipeline.system_prompt("chat") + "\n\n" + context
        messages = history + [{"role": "user", "content": user_msg}]
        # Claude-anropet är blockerande (synkron anthropic-klient) — kör det
        # i en trådpool så det inte fryser den delade event-loopen under de
        # sekunder svaret tar att generera.
        # max_chars är det faktiska målet; max_tokens ska bara vara ett
        # tak. Taket låg tidigare på 2000 mot 2500 tecken, alltså UNDER
        # gränsen det skulle skydda — samma fel som i analyserna, där
        # tokenbudgeten tog slut först och svaret slutade mitt i en
        # mening. Se MAX_ANALYSIS_TOKENS i analysis/pipeline.py.
        # Samma felhantering som /analyze/*-routerna. Chatten saknade den
        # helt: ett API-fel mot Anthropic, ett tomt svar eller ett trasigt
        # verktygsanrop blev en 500 utan en enda rad i loggen, och det enda
        # spåret var "⚠️ Något gick fel (HTTP 500)" i chattbubblan.
        try:
            reply = await run_in_threadpool(
                pipeline.claude.chat,
                messages, prompt,
                max_tokens=_MAX_CHAT_TOKENS, max_chars=_MAX_CHAT_CHARS,
                tools=[
                    LOG_SELF_REPORT_TOOL,
                    LOG_STRENGTH_SESSION_TOOL,
                    CORRECT_SELF_REPORT_TOOL,
                    DELETE_SELF_REPORT_TOOL,
                ],
                tool_executor=pipeline.execute_tool,
            )
        except Exception as exc:
            log.exception("Chattsvar misslyckades")
            raise _server_error("Chattsvaret") from exc
        return {"reply": reply}

    return app


# Här låg `app = create_app()`, en modulnivå-sats som byggde hela appen vid
# IMPORT: läste .env, öppnade databasen, skapade Intervals- och
# Anthropic-klienten. Modulen gick alltså inte att importera alls utan
# fullständig konfiguration — inte ens för att komma åt en ren
# hjälpfunktion som _fmt_pace eller _weekly_strength_volume, och inte för
# att läsa den i ett skript.
#
# uvicorn startar den nu som en fabrik i stället (se `serve` i main.py),
# vilket är samma sak för tjänsten men gör importen biverkningsfri.
