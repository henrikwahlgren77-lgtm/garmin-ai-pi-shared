"""Orkestrering av analyser: aggregerar data från SQLite och anropar Claude."""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any

from analysis.claude import ClaudeClient
from analysis.prompts import MAX_ANALYSIS_CHARS, get_prompt
from analysis.strength import personal_records, session_progression
from config import AthleteProfile
from sync.fit_strength import is_strength_activity
from sync.store import Store, normalize_exercise

log = logging.getLogger(__name__)

# Modulens publika yta, alltså det webblagret och schemaläggaren får
# använda. Listan var tidigare bara två namn medan web/app.py importerade
# fem understrecksprefixade funktioner och anropade två privata metoder på
# pipelinen. De VAR modulens gränssnitt i praktiken; understrecket sa
# motsatsen, och det gjorde det oklart vad som gick att ändra fritt.
#
# MAX_ANALYSIS_CHARS bor i prompts.py, i samma fil som prompttexten som
# nämner siffran (LENGTH_LIMIT), och återexporteras här eftersom det är
# härifrån den skickas till ClaudeClient som backstop. Prompten ber Claude
# hålla sig under gränsen; backstoppen garanterar den oavsett.
__all__ = [
    "CORRECT_SELF_REPORT_TOOL",
    "DELETE_SELF_REPORT_TOOL",
    "EVENING_CONTENT_KEY",
    "LOG_SELF_REPORT_TOOL",
    "LOG_STRENGTH_SESSION_TOOL",
    "MAX_ANALYSIS_CHARS",
    "MAX_ANALYSIS_TOKENS",
    "SELF_REPORT_CATEGORIES",
    "AnalysisPipeline",
    "day_has_data",
    "fmt_hr_zones",
    "fmt_self_reports",
    "fmt_strength_sessions",
    "fmt_wellness_history",
    "swedish_date_label",
    "swedish_weekday_genitive",
    "wellness_for_payload",
]

# Tokentaket ska rymma BÅDE modellens tänkande och den synliga texten, med
# marginal, så att max_chars alltid är det som binder — då klipps ett för
# långt svar städat av _truncate_markdown i stället för rått av API:et
# mitt i en mening.
#
# Taket var tidigare satt till MAX_ANALYSIS_CHARS (3500) med motiveringen
# att en token aldrig motsvarar färre än ett tecken. Det stämmer, men
# räknar inte med att Sonnet 5 returnerar thinking-block utan att man ber
# om det, och att de tokenen dras från samma max_tokens som svaret.
# Morgonanalysen kapades därför mitt i — innan den hann fram till själva
# rekommendationen, alltså hela poängen med den. Fyra gånger på en vecka
# på Pi:n; inte varje gång, eftersom tänkandets längd varierar per anrop.
#
# Uppmätt på den skarpa morgonprompten (4958 tecken systemprompt, 8673
# tecken payload) mot claude-sonnet-5:
#
#   thinking  1405 tokens  (54 % av svaret)
#   text      1211 tokens  -> 2447 tecken  (~2,0 tecken/token på svenska)
#   totalt    2616 tokens
#
# Med det gamla taket 3500 räckte det precis — tills ett tyngre
# tänkandepass tog ~2900 tokens och lämnade under 600 till texten.
#
# 8000 ger 3500 tecken text (~1750 tokens, avrundat uppåt till ~2200 för
# markdown, siffror och emoji som tokeniseras sämre) plus omkring 5800
# tokens tänkande innan taket ens kan bli det som binder.
MAX_ANALYSIS_TOKENS = 8000

_WEEKDAYS_SV = ["Måndag", "Tisdag", "Onsdag", "Torsdag", "Fredag", "Lördag", "Söndag"]
_MONTHS_SV = [
    "januari", "februari", "mars", "april", "maj", "juni",
    "juli", "augusti", "september", "oktober", "november", "december",
]


def swedish_date_label(value: str | date | datetime) -> str:
    """Formaterar ett datum som 'Torsdag 20 augusti 2026'.

    Beräknas i Python istället för att be Claude räkna ut veckodagen
    själv från en ISO-sträng — språkmodeller är inte alltid pålitliga på
    datumaritmetik, och detta är dessutom oberoende av om sv_SE-locale
    råkar vara installerad på servern (som strftime('%A') annars kräver).
    """
    if isinstance(value, datetime):
        d = value.date()
    elif isinstance(value, str):
        d = datetime.fromisoformat(value.split("T")[0]).date()
    else:
        d = value
    return f"{_WEEKDAYS_SV[d.weekday()]} {d.day} {_MONTHS_SV[d.month - 1]} {d.year}"


def swedish_weekday_genitive(value: str | date | datetime) -> str:
    """Veckodagen i bestämd genitiv: 'Fredagens', 'Torsdagens'.

    För rubriker av typen "Fredagens sömndata". Alla svenska veckodagar
    slutar på -dag, så bestämd form + genitiv är alltid ändelsen -ens
    (fredag -> fredagen -> fredagens) — inget specialfall behövs.

    Bor här hos swedish_date_label så att det bara finns EN lista med
    svenska veckodagar; webblagret importerar den härifrån i stället för
    att hålla en egen kopia som kan glida isär.
    """
    if isinstance(value, datetime):
        d = value.date()
    elif isinstance(value, str):
        d = datetime.fromisoformat(value.split("T")[0]).date()
    else:
        d = value
    return f"{_WEEKDAYS_SV[d.weekday()]}ens"


def fmt_hr_zones(
    bounds: list[Any] | None,
    times: list[Any] | None,
    names: list[str] | None = None,
) -> list[dict[str, Any]] | None:
    """Slår ihop zongränser, tid i zon och zonnamn till en rad per zon.

    Payloaden innehöll tidigare de två arrayerna var för sig:

        "hr_zones":      [144, 152, 161, 170, 175, 180, 189]
        "hr_zone_times": [305, 955, 774, 80, 0, 0, 0]

    Att de hör ihop parvis stod ingenstans, och namnen fanns inte alls —
    trots att prompten ber analysen redovisa "tid i varje pulszon".
    Modellen fick para ihop dem själv och hitta på vad zonerna heter.

    Zoner utan tid utelämnas. Ett styrkepass ligger i praktiken helt i zon
    1 ([3256, 0, 0, 0, 0, 0, 0]), och sex rader nollor per pass är brus i
    en payload som redan är stor. Att en zon saknas betyder att den inte
    berördes.

    Gränserna är ÖVRE gränser: zon 1 är upp till bounds[0], zon 2 från
    bounds[0]+1 till bounds[1], och så vidare. Sista zonens övre gräns är
    maxpulsen, som ett pass mycket väl kan överskrida — den skickas ändå
    som den står, eftersom det är den gräns Intervals räknat tiderna mot.
    """
    if not times:
        return None
    bounds = bounds or []
    names = names or []
    zones: list[dict[str, Any]] = []
    for i, seconds in enumerate(times):
        if not seconds:
            continue
        zone: dict[str, Any] = {"zone": i + 1}
        if i < len(names) and names[i]:
            zone["name"] = names[i]
        if i < len(bounds):
            zone["min_bpm"] = (bounds[i - 1] + 1) if i > 0 else 0
            zone["max_bpm"] = bounds[i]
        zone["seconds"] = seconds
        zones.append(zone)
    return zones or None


def _fmt_activity_for_claude(
    a: dict[str, Any],
    raw: dict[str, Any] | None = None,
    hr_zone_names: list[str] | None = None,
) -> dict[str, Any]:
    """Extraherar relevanta fält för analys, inkl. data från raw_json.

    `raw` kan skickas in färdigparsad av anroparen för att undvika att
    raw_json json.loads:as flera gånger för samma aktivitet.

    `hr_zone_names` kommer från store.hr_zone_names(passets typ) och är
    det enda som inte går att läsa ur passet självt — se fmt_hr_zones.
    """
    sport = a.get("sport") or a.get("type")
    distance_m = a.get("distance_meters")

    if raw is None:
        raw = {}
        if a.get("raw_json"):
            try:
                raw = json.loads(a["raw_json"])
            except Exception:
                raw = {}

    data: dict[str, Any] = {
        # Grundläggande
        "id": a.get("id"),
        "name": a.get("name"),
        "type": a.get("type"),
        "sport": sport,
        "start_time": a.get("start_time"),
        "device_name": raw.get("device_name"),
        "source": raw.get("source"),
        # Distans, tid, höjd
        "duration_seconds": a.get("duration_seconds"),
        "elapsed_time_seconds": raw.get("elapsed_time"),
        "moving_time_seconds": raw.get("moving_time"),
        "distance_meters": distance_m,
        "total_elevation_gain_m": raw.get("total_elevation_gain"),
        "total_elevation_loss_m": raw.get("total_elevation_loss"),
        "max_altitude_m": raw.get("max_altitude"),
        "min_altitude_m": raw.get("min_altitude"),
        # Puls & hjärtdata
        "average_heart_rate": a.get("average_heart_rate"),
        "max_heart_rate": a.get("max_heart_rate"),
        "lactate_threshold_hr": raw.get("lthr"),
        "resting_hr": raw.get("icu_resting_hr"),
        "athlete_max_hr": raw.get("athlete_max_hr"),
        "hr_load_hrss": raw.get("hr_load"),
        "trimp": raw.get("trimp"),
        "hr_zones": fmt_hr_zones(
            raw.get("icu_hr_zones"), raw.get("icu_hr_zone_times"), hr_zone_names
        ),
        # Effekt (för cykel)
        "average_watts": a.get("average_watts"),
        "normalized_watts": a.get("normalized_watts"),
        # Träningsbelastning
        "tss": a.get("tss"),
        "intensity_factor": a.get("intensity"),
        "ctl": raw.get("icu_ctl"),
        "atl": raw.get("icu_atl"),
        # Löpningsspecifikt
        "pace_ms": raw.get("pace"),
        "gap_ms": raw.get("gap"),
        "average_cadence": a.get("average_cadence"),
        "average_step_length_mm": raw.get("average_step_length"),
        "average_stride_m": raw.get("average_stride"),
        "average_stance_time_ms": raw.get("average_stance_time"),
        "average_stance_time_percent": raw.get("average_stance_time_percent"),
        "average_stance_time_balance": raw.get("average_stance_time_balance"),
        "average_vertical_oscillation_mm": raw.get("average_vertical_oscillation"),
        "average_vertical_ratio": raw.get("average_vertical_ratio"),
        "average_speed": a.get("average_speed"),
        "max_speed": raw.get("max_speed"),
        # Kalorier & energi
        "calories": raw.get("calories"),
        # Omgivningstemperatur. Klockan mäter den på 156 av 160 pass och
        # den låg oläst i raw_json. Värme höjer pulsen vid samma arbete,
        # så utan den läser en pulsanalys ett varmt gym (mätt: 27-28,5 °C
        # året om) som sämre form. Avrundad: klockan rapporterar
        # 28.201132 °C, en precision mätningen inte har.
        "average_temp_c": (
            round(raw["average_temp"], 1)
            if isinstance(raw.get("average_temp"), int | float)
            else None
        ),
        "min_temp_c": raw.get("min_temp"),
        "max_temp_c": raw.get("max_temp"),
        # Varv & strukturer
        "lap_count": raw.get("icu_lap_count"),
        "warmup_time_seconds": raw.get("icu_warmup_time"),
        "cooldown_time_seconds": raw.get("icu_cooldown_time"),
        # Vikt vid passet
        "weight_at_activity": raw.get("icu_weight"),
        # Intervaller/sektorer om de finns
        "intervals": raw.get("icu_intervals"),
        "interval_summary": raw.get("interval_summary"),
    }

    # För löpning: uppskatta steg vid 1,1 m/steg.
    data["estimated_run_steps"] = (
        int(distance_m / 1.1)
        if sport and "Run" in str(sport) and distance_m
        else None
    )

    # Ta bort nycklar med None-värden för att hålla datan kompakt.
    return {k: v for k, v in data.items() if v is not None}




# Kolumner som aldrig ska följa med när en wellness-rad läggs i en payload
# till Claude.
#
# raw_json är hela Intervals-svaret för dygnet — exakt samma mätvärden som
# kolumnerna bredvid, bara under Intervals egna camelCase-namn. Det var
# ren dubblering, och inte en liten: uppmätt på skarp data utgjorde
# raw_json 28 582 av coachinganalysens 49 507 tecken wellness, alltså
# 58 %, varje dag. Den äldre dagsanalysen (90 dagar, numera borttagen)
# skickade 148 506 tecken av samma skäl.
#
# last_synced är en synktidsstämpel som skrivs om vid varje upsert och
# säger ingenting om kroppen.
_WELLNESS_PAYLOAD_SKIP = ("raw_json", "last_synced")


def wellness_for_payload(
    wellness: dict[str, Any] | None, include_steps: bool = True
) -> dict[str, Any] | None:
    """En wellness-rad redo att skickas till Claude.

    Alla riktiga mätvärden behålls — bara rådatadubbletten och
    synkstämpeln faller bort (se _WELLNESS_PAYLOAD_SKIP).

    `include_steps=False` för morgonanalysen: den läser dygnet innan det
    hänt, och dagens rad innehåller då ett par hundra steg från nattens
    toalettbesök som modellen läste som en dagssiffra ("endast 180 steg
    registrerat — total vilodag"). Steg säger ingenting om hur natten
    gick, vilket är det morgonanalysen handlar om, så de utelämnas helt i
    stället för att kräva att prompten förklarar bort dem.
    Kvällssammanfattningen behåller stegen — där är dygnet slut och
    siffran betyder något.
    """
    if wellness is None:
        return None
    skip = set(_WELLNESS_PAYLOAD_SKIP)
    if not include_steps:
        skip.add("steps")
    return {k: v for k, v in wellness.items() if k not in skip}


# Här låg _wellness_rows_for_payload, som körde wellness_for_payload över
# en hel dagserie. Den hade två anropare — coaching() och den äldre
# dagsanalysen, numera borttagen — och båda skickade därmed 30 respektive
# 90 kompletta wellness-rader till Claude. Majoriteten av fälten var null.
# coaching() använder numera fmt_wellness_history för serien och
# wellness_for_payload för dagens enda rad, precis som morgon- och
# kvällsanalysen alltid gjort.


# Vad som gör ett dygn värt att sammanfatta: nattens sömn är registrerad,
# eller ett pass är loggat. Kvällssammanfattningen handlar om dagens
# träning och sömn — saknas båda finns det ingenting att sammanfatta.
#
# steps, kcal_consumed, hydration och stress står medvetet utanför: de
# börjar fyllas i direkt efter midnatt och kan därför inte skilja "dygnet
# har börjat" från "dygnet har innehåll".
DAY_SUMMARISABLE_FIELDS = (
    "sleep_seconds", "sleep_score", "sleep_quality", "avg_sleeping_hr",
)

# job_state-nyckel: vilken dag den senaste kvällssammanfattningen gällde,
# och om det dygnet hade något att sammanfatta när den skrevs.
EVENING_CONTENT_KEY = "daily_summary_content"


def day_has_data(store: Any, day: str) -> bool:
    """Har dygnet något att sammanfatta — ett pass eller en registrerad natt?"""
    if store.get_activities_for_date(day):
        return True
    wellness = store.get_wellness_day(day)
    if not wellness:
        return False
    return any(wellness.get(field) is not None for field in DAY_SUMMARISABLE_FIELDS)


def _fmt_activity_summary(a: dict[str, Any]) -> dict[str, Any]:
    """Lättvikts-sammanfattning för morgon/kväll-analyser (ej full raw_json)."""
    sport = a.get("sport") or a.get("type")
    distance_m = a.get("distance_meters")
    return {
        "name": a.get("name"),
        "sport": sport,
        "start_time": a.get("start_time"),
        "duration_seconds": a.get("duration_seconds"),
        "distance_meters": distance_m,
        "average_heart_rate": a.get("average_heart_rate"),
        "max_heart_rate": a.get("max_heart_rate"),
        "tss": a.get("tss"),
        "intensity": a.get("intensity"),
    }


def _up_to_day(rows: list[dict[str, Any]], day: str) -> list[dict[str, Any]]:
    """Rader till och med `day` — historik får inte innehålla framtiden.

    Fönstren (14 dagar bakåt) räknas från IDAG, medan en uppsamlings-
    körning strax efter midnatt sammanfattar GÅRDAGEN. Utan den här
    filtreringen hade den nya dygnets halvtomma rader legat överst i
    trendserien som analysen ska läsa bakåt ur.
    """
    return [r for r in rows if str(r.get("day") or "") <= day]


def fmt_self_reports(
    reports: list[dict[str, Any]], include_ids: bool = False
) -> list[dict[str, Any]]:
    """Kompakt format av självrapporter (vikt/sjukdom/alkohol/etc, se
    LOG_SELF_REPORT_TOOL) för att skicka med i analys-payloads.

    `include_ids=True` bara för chattens kontext. Verktygen som rättar och
    raderar (CORRECT_SELF_REPORT_TOOL, DELETE_SELF_REPORT_TOOL) adresserar
    en rad med dess id, och utan id i kontexten kan Claude inte veta
    vilket det är — den hade fått gissa, vilket är precis fel sak att
    gissa om. Analyserna får dem inte: där är id:t brus som inte går att
    tolka, och de har inga verktyg att använda det med.
    """
    result = []
    for r in reports:
        entry = {
            "day": r.get("day"),
            "category": r.get("category"),
            "value": r.get("value"),
            "note": r.get("note"),
        }
        row = {k: v for k, v in entry.items() if v is not None}
        if include_ids and r.get("id") is not None:
            row["id"] = r["id"]
        result.append(row)
    return result


def fmt_wellness_history(
    rows: list[dict[str, Any]], include_steps: bool = True
) -> list[dict[str, Any]]:
    """Kompakt dagserie av wellness (hrv, vilopuls, sömn, belastning) för
    trendfrågor — samma fältval morgon- och kvällsanalysen redan skickar,
    så en fråga i chatten om t.ex. HRV över tid kan grundas i riktiga
    siffror i stället för bara dagens enda värde."""
    result = []
    for w in rows:
        entry = {
            "day": w.get("day"),
            "hrv": w.get("hrv"),
            "resting_hr": w.get("resting_hr"),
            "sleep_seconds": w.get("sleep_seconds"),
            "sleep_score": w.get("sleep_score"),
            "ctl": w.get("ctl"),
            "atl": w.get("atl"),
            "tsb": w.get("tsb"),
        }
        if include_steps:
            entry["steps"] = w.get("steps")
        result.append(entry)
    return result


# Hur många tidigare pass per övning som följer med en per-passanalys som
# jämförelseunderlag. Åtta räcker för att se en trend utan att svälla
# payloaden: åtta axelpressar sträcker sig ~10 veckor bakåt, åtta marklyft
# ~9 — långt nog för att 50 -> 52.5 -> 55 kg ska synas som en progression
# och inte som brus.
STRENGTH_HISTORY_SESSIONS = 8


def fmt_strength_sessions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregerar råa set-rader (se Store.add_strength_sets) per dag + övning.

    Skickar man alla enskilda set till Claude växer payloaden fort (ett
    gympass är lätt 20+ rader) utan att tillföra mycket — det som är
    intressant för analys och progression är antal set, total reps, tyngsta
    vikt och volym. Volym = summan av reps x vikt över setsen, vilket är
    det gängse måttet på hur mycket arbete som utförts.

    `estimated_1rm` är passets bästa set omräknat till ett maxlyft, så
    pass med olika upplägg går att jämföra. Fältet utelämnas när inget set
    gick att skatta — kroppsviktsövningar, eller set klockan loggat utan
    repsräkning.

    SJÄLVA RÄKNANDET görs av session_progression i analysis/strength.py,
    inte här. Det stod tidigare en egen kopia av aritmetiken i den här
    funktionen, och kopiorna var oense om vad som räknas som ett mätbart
    set: strength.py kräver både vikt och reps > 0, medan den här räknade
    en vikt så fort den fanns. Skarpt gav det två svar på samma fråga ur
    samma två rader — däckvältning 2026-05-05, två set på 130 kg som
    klockan aldrig repsräknade, blev "tyngsta vikt 130 kg" i payloaden
    till Claude och "ingen mätbar vikt" på progressionskortet. Databasen
    har 26 sådana set. Ett tal som betyder olika saker beroende på vem som
    frågar är värre än inget tal alls, och det är samma familj av fel som
    de påhittade personbästa.

    `measured_sets` följer med när den skiljer sig från `sets`, alltså
    just när klockan loggat set den inte räknat. Utan den ser ett pass med
    "2 set, 0 reps, volym 0" ut som saknad data i stället för som det som
    faktiskt står i filen.

    Ordningen från indata bevaras (nyast först från list_strength_sets).
    """
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, str]] = []
    for r in rows:
        day = r.get("day")
        exercise = r.get("exercise")
        if not day or not exercise:
            continue
        key = (day, exercise)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(r)

    sessions = []
    for day, exercise in order:
        rader = grouped[(day, exercise)]
        # En grupp är en övning en dag, så session_progression ger exakt
        # en rad tillbaka.
        (pass_,) = session_progression(rader)
        entry: dict[str, Any] = {
            "day": day,
            "exercise": exercise,
            "sets": pass_["sets"],
            "reps_total": pass_["reps_total"],
            "top_weight_kg": pass_["top_weight_kg"],
            "volume_kg": pass_["volume_kg"],
            "estimated_1rm": pass_["estimated_1rm"],
        }
        if pass_["measured_sets"] != pass_["sets"]:
            entry["measured_sets"] = pass_["measured_sets"]

        # reps_total är en SUMMA över passets set, men "3 set, 15 reps" är
        # tvetydigt nog att läsas som 3x15 — och gjorde det: en analys
        # beskrev 3 set x 5 reps som "3x15 @ 130 kg", ett pass atleten
        # aldrig kört. Kördes alla set på samma antal reps (det vanliga)
        # skickas den siffran med explicit, så det inte behöver gissas.
        #
        # Kravet "alla set har reps" (len(reps) == sets), inte bara "de
        # som har reps är lika". Utan det kom tvetydigheten tillbaka från
        # andra hållet: tre set där ett saknar reps gav sets=3 och
        # reps_per_set=8, som läses som 3x8 = 24, medan reps_total sa 16.
        # Två av siffrorna kunde inte båda stämma.
        #
        # Och noll är inget repsantal. Ett pass där klockan loggat set utan
        # att räkna dem skrev "reps_per_set: 0" — vilket läses som "2 set x
        # 0 reps på 130 kg", ett påstående om hur passet gick till som inte
        # står någonstans i filen.
        reps: list[Any] = [r["reps"] for r in rader if r.get("reps") is not None]
        if len(reps) == entry["sets"] and len(set(reps)) == 1 and reps[0] > 0:
            entry["reps_per_set"] = reps[0]
        sessions.append({k: v for k, v in entry.items() if v is not None})
    return sessions


# Verktyg Claude kan anropa i chatten (se ClaudeClient.chat) för att spara
# självrapporterad data som inte kommer från Intervals.icu-synken. Schemat
# och exekveringen (AnalysisPipeline.execute_tool) hör ihop — ändra båda
# tillsammans.
# Kategorierna en självrapport kan ha. Delas av verktyget som skapar en
# rapport och det som rättar den — två kopior av samma enum hade kunnat
# glida isär, och då blir en kategori möjlig att skriva men omöjlig att
# rätta till.
SELF_REPORT_CATEGORIES = [
    "weight", "illness", "alcohol", "injury", "mood", "note",
]

_CATEGORY_HELP = (
    "weight=vikt, illness=sjukdom/förkylning/krämpa, "
    "alcohol=alkoholkonsumtion, injury=skada/ömhet, "
    "mood=humör/stress, note=annat värt att notera."
)

LOG_SELF_REPORT_TOOL = {
    "name": "log_self_report",
    "description": (
        "Spara en självrapporterad uppgift från atleten som INTE kommer "
        "från Intervals.icu-synken — t.ex. vikt, sjukdom, alkoholkonsumtion, "
        "skada, humör eller annan anteckning som bör vägas in i framtida "
        "träningsanalyser. Anropa ALLTID detta verktyg när atleten nämner "
        "något sådant i chatten, även i förbigående ('lite snorig idag', "
        "'tog en öl igår kväll', 'vägde mig, 91.2'). Anropa det flera "
        "gånger i samma svar om atleten nämner flera saker."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": SELF_REPORT_CATEGORIES,
                "description": _CATEGORY_HELP,
            },
            "value": {
                "type": "number",
                "description": "Numeriskt värde om relevant, t.ex. vikt i kg. Utelämna annars.",
            },
            "note": {
                "type": "string",
                "description": (
                    "Kort fritext med detaljer, t.ex. 'två öl till middag' "
                    "eller 'halsont, lite feber'."
                ),
            },
            "day": {
                "type": "string",
                "description": (
                    "ISO-datum (YYYY-MM-DD) som uppgiften gäller. Utelämna "
                    "för idag — ange bara om atleten pratar om ett annat "
                    "datum (t.ex. 'igår')."
                ),
            },
        },
        "required": ["category"],
    },
}

# Verktyg för att rätta respektive ta bort en självrapport.
#
# self_reports var append-only: loggade Claude fel vikt, eller registrerade
# "förkyld" på fel dag, gick det inte att ta tillbaka från vare sig chatten
# eller webben. Raden följde med i morgon- och kvällsanalysen i fjorton
# dagar och i träningsanalysen i trettio, och enda boteboten var sqlite3 på
# kommandoraden.
#
# Två verktyg och inte ett med ett "delete"-flagga: beskrivningarna blir
# entydiga, och ett verktyg som både kan skriva om och radera är lättare
# att anropa fel än två som gör var sin sak.
#
# Id:t kommer från 'self_reports' i chattkontexten, som skickas med
# include_ids=True just för det här (se fmt_self_reports).
CORRECT_SELF_REPORT_TOOL = {
    "name": "correct_self_report",
    "description": (
        "Rätta en självrapport som redan sparats. Använd det när atleten "
        "korrigerar sig ('nej förresten, det var 121,8', 'det där var "
        "igår, inte idag', 'det var inte alkohol utan sömn'). Ta id:t "
        "från 'self_reports' i kontexten — "
        "gissa aldrig ett id. Är du osäker på vilken rad som avses, fråga "
        "atleten i stället för att chansa. Bara de fält du skickar med "
        "skrivs om; de du utelämnar lämnas orörda."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "report_id": {
                "type": "integer",
                "description": "id från 'self_reports' i kontexten.",
            },
            "value": {
                "type": "number",
                "description": "Nytt numeriskt värde, t.ex. en rättad vikt.",
            },
            "note": {"type": "string", "description": "Ny fritext."},
            "day": {
                "type": "string",
                "description": "Nytt datum (YYYY-MM-DD), om uppgiften låg på fel dag.",
            },
            "category": {
                "type": "string",
                "enum": SELF_REPORT_CATEGORIES,
                "description": (
                    "Ny kategori, om uppgiften hamnade under fel sort. "
                    + _CATEGORY_HELP
                ),
            },
        },
        "required": ["report_id"],
    },
}

DELETE_SELF_REPORT_TOOL = {
    "name": "delete_self_report",
    "description": (
        "Ta bort en självrapport helt. Använd det bara när atleten ber om "
        "det, eller när en uppgift är felaktig på ett sätt som inte går "
        "att rätta ('strunta i det där', 'det blev fel, ta bort det'). "
        "Ta id:t från 'self_reports' i kontexten — gissa aldrig ett id, "
        "och radera aldrig något atleten inte bett dig radera. Ska en "
        "uppgift bara justeras, använd correct_self_report i stället."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "report_id": {
                "type": "integer",
                "description": "id från 'self_reports' i kontexten.",
            },
        },
        "required": ["report_id"],
    },
}

# Verktyg för att logga ett styrkepass. Egen tabell och eget verktyg
# istället för en 'strength'-kategori i log_self_report, eftersom fritext i
# ett note-fält inte går att räkna volym eller progression på — se
# strength_sets i sync/store.py. Schemat och AnalysisPipeline.execute_tool
# hör ihop — ändra båda tillsammans.
LOG_STRENGTH_SESSION_TOOL = {
    "name": "log_strength_session",
    "description": (
        "Spara en styrkeövning som atleten utfört, med alla set. Anropa "
        "det när atleten nämner en styrkeövning med vikter eller reps "
        "('bänkpress 3x8 på 80 kg', 'körde marklyft, 5 reps på 120'), EN "
        "GÅNG PER ÖVNING — nämner atleten tre övningar gör du tre anrop. "
        "Expandera kompakt notation själv: '3x8 @ 80 kg' betyder tre set "
        "med reps=8 och weight_kg=80. Varierar repsen ('8/7/5 på 80') blir "
        "det tre set med olika reps men samma vikt. "
        "VIKTIGT: pass som registrerats på klockan importeras automatiskt "
        "från Garmin-filen och syns redan i 'strength' i kontexten. Logga "
        "INTE en övning som redan finns där för samma dag med ungefär "
        "samma vikter — det dubbelräknar volymen."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "exercise": {
                "type": "string",
                "description": (
                    "Övningens namn på svenska, gemener, t.ex. 'bänkpress', "
                    "'marklyft', 'knäböj'. Normalisera stavningen så samma "
                    "övning heter likadant mellan pass — annars går det inte "
                    "att följa progression över tid."
                ),
            },
            "sets": {
                "type": "array",
                "description": (
                    "Ett element per utfört set, i den ordning de gjordes."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "reps": {
                            "type": "integer",
                            "description": "Antal repetitioner i setet.",
                        },
                        "weight_kg": {
                            "type": "number",
                            "description": (
                                "Vikt i kilo. Utelämna för kroppsviktsövningar."
                            ),
                        },
                        "rpe": {
                            "type": "number",
                            "description": (
                                "Upplevd ansträngning 1-10, om atleten nämner "
                                "det. Utelämna annars."
                            ),
                        },
                    },
                },
            },
            "day": {
                "type": "string",
                "description": (
                    "ISO-datum (YYYY-MM-DD) som passet utfördes. Utelämna "
                    "för idag — ange bara om atleten pratar om en annan dag."
                ),
            },
            "note": {
                "type": "string",
                "description": (
                    "Kort fritext om övningen, t.ex. 'kändes tungt sista "
                    "setet' eller 'ny PB'. Utelämna om inget sagts."
                ),
            },
        },
        "required": ["exercise", "sets"],
    },
}


# Verktygens JSON-schema är en INSTRUKTION till modellen, inte en spärr.
# Anthropic validerar inte tool_input mot det, så allt som kommer in i
# execute_tool är otypat och ska behandlas som yttre indata — precis som
# en HTTP-kropp. Filen visste redan det på ett ställe (se _report_id:
# "Claude skickar ibland heltal som strängar"), men slutsatsen drogs bara
# för report_id.
#
# Tre fel som alla gick att reproducera innan de här hjälparna fanns:
#
# 1. reps som strängen "8" skrev raderna men kraschade summeringen i
#    _log_strength_session efteråt (sum() på str). claude.py fångar det
#    och säger åt Claude att INTE påstå att något sparades — fast det var
#    sparat. Atleten loggar då om passet, och chattspärren släpper igenom
#    det (den spärrar bara source='fit'), så volymen dubbelräknas.
# 2. value="82 kg" på en viktrapport lagras som TEXT i en REAL-kolumn.
#    store.latest_weight() gör float() på den och kastar — och den
#    anropas från system_prompt(), alltså av varje analys OCH varje
#    chattmeddelande. En rad gjorde hela appen obrukbar, utan väg
#    tillbaka: chatten var nere, så verktygen för att rätta raden gick
#    inte att nå.
# 3. day="igår" eller "2026-13-45" passerar rakt in i en kolumn som alla
#    fönsterfrågor jämför som STRÄNG. De sorterar över varje verkligt
#    datum och faller därför aldrig ur ett rullande fönster — de följer
#    med i varje betald analyspayload i all framtid. Åt andra hållet
#    försvinner "20 september 2026" tyst, eftersom den sorterar under.
#
# Gemensamt för alla tre: ett värde Claude kan rätta till ska ge ett
# begripligt "Fel: ..." tillbaka, så modellen kan göra om anropet. Det är
# samma mönster som verktygen redan använder för saknad `exercise`.
def _tool_day(value: Any) -> str | None:
    """Ett ISO-datum (YYYY-MM-DD) ur ett verktygsanrop. None = ogiltigt.

    Strikt, och inte date.fromisoformat() rakt av: den godtar från
    Python 3.11 även former som "20260920" och "2026-W38-5", och de
    sorterar inte som resten av kolumnen.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.isoformat() == text else None


def _tool_number(value: Any) -> float | None:
    """Ett tal ur ett verktygsanrop, eller None om det inte är ett tal.

    Accepterar siffror i strängform ("82", "82.5") eftersom modellen
    skickar sådana, men inte text med enhet ("82 kg") — den skulle lagras
    som TEXT i en REAL-kolumn och spränga nästa float() som läser den.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


# Felsvaren. Namnger både fältet och det som faktiskt kom in, så att
# Claude kan göra om anropet rätt i stället för att gissa — och säger
# uttryckligen att ingenting sparades, eftersom den annars gärna
# kvitterar ändå.
_BAD_SETS = (
    "Fel: 'sets' måste vara en lista med set, ett objekt per set. Inget "
    "sparades. Fick {varde!r}. Expandera upplägget själv: 3x8 @ 80 blir "
    '[{{"reps": 8, "weight_kg": 80}}, {{"reps": 8, "weight_kg": 80}}, '
    '{{"reps": 8, "weight_kg": 80}}].'
)
_BAD_SET_ROW = (
    "Fel: set nummer {nr} är {varde!r} och inget objekt. Inget sparades. "
    'Varje set skrivs som {{"reps": 8, "weight_kg": 80}} — reps och '
    "weight_kg får utelämnas var för sig, men själva setet måste vara "
    "ett objekt."
)
_BAD_REPS = (
    "Fel: reps={varde!r} är inget helt antal. Inget sparades. Ett halvt "
    "rep finns inte — avrunda till 3 eller 4, eller utelämna reps helt "
    "om klockan inte räknade dem."
)

_BAD_DAY = (
    "Fel: {varde!r} är inget giltigt datum. Inget sparades. Ange 'day' "
    "som YYYY-MM-DD med nollor (2026-03-05, inte 2026-3-5), eller utelämna "
    "fältet helt för idag. Räkna ut datumet själv om atleten sa 'igår' "
    "eller 'i måndags' — skriv aldrig in ordet."
)
_BAD_NUMBER = (
    "Fel: {falt}={varde!r} är inget tal. Inget sparades. Skicka bara "
    "siffran, utan enhet — 82 eller 82.5, inte '82 kg'."
)
_BAD_CATEGORY = (
    "Fel: {varde!r} är ingen giltig kategori. Inget sparades. Välj en av: "
    + ", ".join(SELF_REPORT_CATEGORIES)
    + "."
)
_BAD_NOTE = (
    "Fel: note={varde!r} är ingen text. Inget sparades. Skriv anteckningen "
    "som en vanlig sträng."
)
_FUTURE_DAY = (
    "Fel: {dag} ligger i framtiden — idag är {idag}. Inget sparades. Räkna "
    "om datumet: 'igår' är {igar}. Bara dagar som redan varit kan ha en "
    "uppgift."
)


def _future_day_error(day: str) -> str | None:
    """Felsvar om dagen ligger efter idag, annars None.

    Formatet kontrolleras av _tool_day, men ett korrekt skrivet datum kan
    ändå vara fel år — "2026-12-31" när atleten sa "i nyårsafton" i januari
    2027. Fönsterfrågorna räknar bakåt från idag och har inget tak framåt,
    så en framtida rad följde med i varje analys och i chatten tills
    datumet hunnit ikapp: en förkylning i ett år, eller styrkeset som
    fyllde en vecka som inte börjat.
    """
    idag = datetime.now().date()
    if day <= idag.isoformat():
        return None
    return _FUTURE_DAY.format(
        dag=day, idag=idag.isoformat(), igar=(idag - timedelta(days=1)).isoformat()
    )


def _bad_note(note: Any) -> str | None:
    """Felsvar om anteckningen inte är text, annars None.

    Schemat säger string, men det är en instruktion och ingen spärr. En
    lista eller ett objekt gick inte att skriva alls (sqlite3 vägrar binda
    det), och felet blev "Fel vid körning av verktyget" utan ledtråd till
    vad som var fel.
    """
    if note is None or isinstance(note, str):
        return None
    return _BAD_NOTE.format(varde=note)


class AnalysisPipeline:
    def __init__(
        self,
        store: Store,
        claude: ClaudeClient,
        athlete: AthleteProfile | None = None,
    ) -> None:
        self.store = store
        self.claude = claude
        # Kommer från ATHLETE_* i .env (se config.py). Utelämnas den blir
        # profilen tom, och prompten säger åt Claude att inte anta något
        # om ålder, kön eller hälsa i stället för att gissa.
        self.athlete = athlete or AthleteProfile()

    def system_prompt(self, analysis_type: str) -> str:
        """Systemprompt med atletprofilen från .env och senast kända vikt."""
        weight = self.store.latest_weight()
        if weight is None:
            return get_prompt(analysis_type, athlete=self.athlete)
        weight_kg, weight_day = weight
        return get_prompt(
            analysis_type,
            athlete=self.athlete,
            weight_kg=weight_kg,
            weight_day=weight_day,
        )

    def execute_tool(self, name: str, tool_input: dict[str, Any]) -> str:
        """Kör ett verktygsanrop Claude gjort i chatten (se LOG_SELF_REPORT_TOOL
        och LOG_STRENGTH_SESSION_TOOL).

        Returnerar en kort textsträng som skickas tillbaka till Claude som
        tool_result — inte till användaren direkt, men Claude brukar
        återberätta/bekräfta det i sitt svar.
        """
        if name == "log_self_report":
            # Kategorin kontrollerades bara vid rättning. Här skrevs vad
            # som helst in, och en rad under "sömn" eller "Weight" hamnar
            # utanför allt som läser kategorin — vikten i systemprompten
            # letar efter just "weight".
            category = tool_input.get("category") or "note"
            if category not in SELF_REPORT_CATEGORIES:
                return _BAD_CATEGORY.format(varde=category)
            note = tool_input.get("note")
            if fel := _bad_note(note):
                return fel

            day = self._tool_day_or_today(tool_input.get("day"))
            if day is None:
                return _BAD_DAY.format(varde=tool_input.get("day"))
            if fel := _future_day_error(day):
                return fel

            raw_value = tool_input.get("value")
            value = None
            if raw_value is not None:
                value = _tool_number(raw_value)
                if value is None:
                    return _BAD_NUMBER.format(falt="value", varde=raw_value)

            self.store.add_self_report(day=day, category=category, value=value, note=note)
            detail = f" ({value:g})" if value is not None else ""
            return f"Sparat: {category}{detail} för {day}."

        if name == "log_strength_session":
            return self._log_strength_session(tool_input)

        if name == "correct_self_report":
            return self._correct_self_report(tool_input)

        if name == "delete_self_report":
            return self._delete_self_report(tool_input)

        return f"Okänt verktyg: {name}"

    @staticmethod
    def _tool_day_or_today(raw: Any) -> str | None:
        """Dagen ett verktygsanrop gäller. None betyder ogiltig — inte idag.

        Ett utelämnat 'day' betyder "idag" enligt båda verktygens schema.
        Ett ifyllt men otolkbart betyder att modellen försökte säga något
        annat, och att tyst skriva dagens datum då hade lagt uppgiften på
        fel dag utan att någon fick veta det.
        """
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return datetime.now().date().isoformat()
        return _tool_day(raw)

    @staticmethod
    def _report_id(tool_input: dict[str, Any]) -> int | None:
        """Plockar ut report_id ur ett verktygsanrop, eller None.

        Claude skickar ibland heltal som strängar. Ett id som inte går att
        tolka ska ge ett begripligt svar tillbaka i chatten, inte ett
        ValueError som blir 'Fel vid körning av verktyget'.
        """
        raw = tool_input.get("report_id")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _describe_self_report(self, report: dict[str, Any]) -> str:
        """'vikt 122.3 för 2026-09-02' — till kvittensen i chatten."""
        delar = [str(report.get("category") or "note")]
        if report.get("value") is not None:
            delar.append(str(report["value"]))
        if report.get("note"):
            delar.append(f"”{report['note']}”")
        return f"{' '.join(delar)} för {report.get('day')}"

    def _correct_self_report(self, tool_input: dict[str, Any]) -> str:
        report_id = self._report_id(tool_input)
        if report_id is None:
            return "Fel: 'report_id' saknas eller är inte ett tal."

        before = self.store.get_self_report(report_id)
        if before is None:
            return (
                f"Fel: ingen självrapport med id {report_id}. Kontrollera "
                "id:t mot 'self_reports' i kontexten."
            )

        fields = {
            key: tool_input[key]
            for key in ("day", "value", "note", "category")
            if key in tool_input and tool_input[key] is not None
        }
        # Enumen i schemat är en instruktion till modellen, inte en spärr:
        # den som skickar något annat får ett svar den kan agera på i
        # stället för en rad som ingen analys vet vad den ska göra med.
        kategori = fields.get("category")
        if kategori is not None and kategori not in SELF_REPORT_CATEGORIES:
            return _BAD_CATEGORY.format(varde=kategori)
        if fel := _bad_note(fields.get("note")):
            return fel
        # Samma typkontroll som på vägen in (se log_self_report i
        # execute_tool). En rättelse skriver till samma kolumner, så ett
        # otolkbart datum eller en vikt med enhet hade kunnat komma in
        # bakvägen här och lämnat raden i precis det skick verktyget
        # finns för att laga.
        if "day" in fields:
            dag = _tool_day(fields["day"])
            if dag is None:
                return _BAD_DAY.format(varde=fields["day"])
            if fel := _future_day_error(dag):
                return fel
            fields["day"] = dag
        if "value" in fields:
            tal = _tool_number(fields["value"])
            if tal is None:
                return _BAD_NUMBER.format(falt="value", varde=fields["value"])
            fields["value"] = tal
        if not fields:
            return (
                "Fel: inget att ändra — skicka med minst ett av value, "
                "note, day eller category."
            )

        self.store.update_self_report(report_id, **fields)
        after = self.store.get_self_report(report_id) or {}
        return (
            f"Rättat: {self._describe_self_report(before)} -> "
            f"{self._describe_self_report(after)}."
        )

    def _delete_self_report(self, tool_input: dict[str, Any]) -> str:
        report_id = self._report_id(tool_input)
        if report_id is None:
            return "Fel: 'report_id' saknas eller är inte ett tal."

        # Läs raden FÖRE raderingen, så kvittensen kan säga vad som
        # faktiskt försvann. Efteråt finns den inte att beskriva, och
        # "Raderat id 12" säger ingenting för den som läser chatten.
        report = self.store.get_self_report(report_id)
        if report is None:
            return (
                f"Fel: ingen självrapport med id {report_id}. Kontrollera "
                "id:t mot 'self_reports' i kontexten."
            )

        self.store.delete_self_report(report_id)
        return f"Raderat: {self._describe_self_report(report)}."

    def _find_strength_activity_id(self, day: str) -> str | None:
        """Hittar dagens styrkepass för att kunna koppla loggade set till det.

        Kopplar bara när det finns exakt ETT styrkepass den dagen — med två
        pass går det inte att veta vilket setsen hörde till, och en gissning
        skulle visa fel data på fel aktivitetssida. Setsen sparas ändå (med
        activity_id=NULL) och syns i dagens/veckans analyser.
        """
        candidates = [
            a for a in self.store.get_activities_for_date(day) if is_strength_activity(a)
        ]
        if len(candidates) != 1:
            return None
        # `str(x) or None` stod här tidigare, vilket aldrig kan ge None:
        # str(None) är "None", en icke-tom och därmed sann sträng. Saknade
        # passet id hade setsen kopplats till aktiviteten "None" i stället
        # för att lämnas okopplade.
        activity_id = candidates[0].get("id")
        return str(activity_id) if activity_id else None

    def _log_strength_session(self, tool_input: dict[str, Any]) -> str:
        # Normaliserat direkt: spärren nedan jämför namn, och store-lagret
        # skriver ändå normaliserat (se normalize_exercise). Jämförde
        # tidigare rå sträng mot rå sträng, vilket lät "Marklyft" gå förbi
        # spärren och bli ett andra exemplar av samma pass.
        exercise = normalize_exercise(tool_input.get("exercise") or "")
        raw_sets = tool_input.get("sets") or []
        if not exercise:
            return "Fel: 'exercise' saknas — ange vilken övning det gäller."
        if not raw_sets:
            return f"Fel: inga set angivna för {exercise}."

        day = self._tool_day_or_today(tool_input.get("day"))
        if day is None:
            return _BAD_DAY.format(varde=tool_input.get("day"))
        if fel := _future_day_error(day):
            return fel
        note = tool_input.get("note")
        if fel := _bad_note(note):
            return fel

        # Är övningen redan importerad ur klockans FIT-fil för den här
        # dagen? Då är det här ett andra exemplar av samma arbete, och
        # volymen dubbelräknas överallt: staplarna på dashboarden blir
        # dubbelt så höga, "du har lyft N veckor" räknar två pass där det
        # fanns ett, och analyserna får siffran två gånger.
        #
        # Reproducerat: ett FIT-importerat marklyftspass plus samma pass
        # loggat i chatten gav 2 sessions och 1200 kg där 600 lyftes.
        #
        # Bara 'fit' spärrar. Redan chattloggade set gör det INTE: att
        # lägga till i efterhand ("jag glömde, det var fem set") är ett
        # riktigt användningsfall, och den som loggar för hand är den som
        # vet.
        redan_importerade = [
            r for r in self.store.get_strength_sets_for_date(day)
            if normalize_exercise(r.get("exercise") or "") == exercise
            and r.get("source") == "fit"
        ]
        if redan_importerade:
            vikter = [r["weight_kg"] for r in redan_importerade if r.get("weight_kg")]
            tyngst = f", tyngsta {max(vikter):g} kg" if vikter else ""
            return (
                f"Redan importerat från klockan: {exercise}, "
                f"{len(redan_importerade)} set för {day}{tyngst}. Sparade "
                "INTE en gång till — det hade dubbelräknat volymen. "
                "Bekräfta för atleten att passet redan finns, och fråga "
                "vad som behöver rättas om siffrorna inte stämmer."
            )

        # Ett set får sakna fält (kroppsvikt har ingen weight_kg, klockan
        # loggar ibland inga reps) — men det måste vara ett objekt.
        #
        # Talen typas om HÄR, före den första skrivningen. Görs det efteråt
        # — som summeringen längre ner gjorde — hinner raderna bli skrivna
        # innan felet upptäcks, och kvittensen säger då att inget sparades
        # trots att allt gjorde det. Se _tool_number.
        #
        # Formen på 'sets' kontrolleras av samma skäl som talen i den:
        # schemat säger "array of objects", men det är en instruktion till
        # modellen och ingen spärr. En sträng itererades tecken för tecken
        # ("3x8" blev tre tomma set) och en dict nyckel för nyckel — båda
        # skrev rader utan reps och utan vikt, och båda kvitterades som
        # "Sparat". Raderna räknas som pass på dashboarden och gör
        # övningen valbar i progressionsväljaren; de faller bara ur
        # volymen, eftersom den kräver en vikt.
        if not isinstance(raw_sets, (list, tuple)):
            return _BAD_SETS.format(varde=raw_sets)

        sets = []
        for nr, inkommande in enumerate(raw_sets, start=1):
            if not isinstance(inkommande, dict):
                return _BAD_SET_ROW.format(nr=nr, varde=inkommande)
            rent: dict[str, Any] = {}
            for falt in ("reps", "weight_kg", "rpe"):
                if inkommande.get(falt) is None:
                    continue
                tal = _tool_number(inkommande[falt])
                if tal is None:
                    return _BAD_NUMBER.format(falt=falt, varde=inkommande[falt])
                if falt == "reps":
                    # int() avrundade tyst nedåt: 2.7 reps blev 2, och
                    # kvittensen påstod att det var vad atleten sa.
                    if tal != int(tal):
                        return _BAD_REPS.format(varde=inkommande[falt])
                    rent[falt] = int(tal)
                else:
                    rent[falt] = tal
            sets.append(rent)

        # Vilket namn raderna faktiskt hamnar under, och om de skrivs alls.
        # Kvittensen byggdes tidigare enbart av det Claude skickade IN, och
        # de två sakerna är inte samma: rättelselistan i .env (se
        # parse_strength_corrections i sync/store.py) kan både stryka en
        # övning en bestämd dag och döpa om den.
        #
        # Utfallet var en kvittens som ljög, och Claude läser den och
        # berättar för atleten vad som hänt. En struken övning gav
        # "Sparat: bänkpress, 0 set, 24 reps, volym 1920 kg" trots att noll
        # rader skrevs; en omdöpt gav "Sparat: raka marklyft" för rader som
        # ligger under "marklyft" — och nästa fråga om raka marklyft hittar
        # då ingenting.
        sparat_som = self.store.resolved_exercise(day, exercise)
        if sparat_som is None:
            return (
                f"Inte sparat: {exercise} är struken för {day} i "
                "rättelselistan (STRENGTH_SET_CORRECTIONS i .env), så den "
                "filtreras bort med flit. Säg att den inte sparades — "
                "påstå inte motsatsen — och att listan behöver ändras om "
                "det var fel."
            )

        activity_id = self._find_strength_activity_id(day)
        count = self.store.add_strength_sets(
            day=day,
            exercise=exercise,
            sets=sets,
            activity_id=activity_id,
            note=note,
            source="chat",
        )

        reps_total = sum(s.get("reps") or 0 for s in sets)
        volume = sum(
            (s.get("reps") or 0) * (s.get("weight_kg") or 0) for s in sets
        )
        summary = f"Sparat: {sparat_som}, {count} set, {reps_total} reps"
        if volume:
            summary += f", volym {round(volume, 1)} kg"
        summary += f" för {day}."
        if sparat_som != exercise:
            summary += (
                f" Obs: sparat under namnet {sparat_som}, inte {exercise} — "
                "rättelselistan döper om den för den här dagen. Använd det "
                "namnet när du refererar till passet."
            )
        return summary

    def analyze_activity(self, activity_id: str) -> str:
        """Genererar en djupdykningsanalys för ett pass.

        Genererar alltid när den anropas (precis som coaching och
        evening_summary) — anropas bara från explicita "analysera"-actions
        (CLI: analyze-activity, webb: /analyze/activity/{id}), aldrig från
        passiv visning (den läser store.latest_activity_analysis direkt).
        Tidigare returnerades en cachad analys permanent utan att någonsin
        generera på nytt, vilket gjorde "generera om"-knappen verkningslös
        efter första gången.
        """
        a = self.store.get_activity(activity_id)
        if not a:
            raise ValueError(f"Aktivitet saknas: {activity_id}")

        raw: dict[str, Any] = {}
        if a.get("raw_json"):
            try:
                raw = json.loads(a["raw_json"])
            except Exception:
                raw = {}

        # _fmt_activity_for_claude läser redan ut sektortider (icu_intervals)
        # ur raw_json. "laps" finns inte i den generella mappningen, så den
        # läggs till separat här om Intervals returnerat det.
        # Zonnamnen slås upp på passets TYP: löpningens zon 1 slutar vid
        # 144, styrkans vid 137, och Intervals namnger dem per sportgrupp.
        data = _fmt_activity_for_claude(
            a, raw=raw, hr_zone_names=self.store.hr_zone_names(a.get("type"))
        )
        if "laps" in raw:
            data["laps"] = raw["laps"]
        if a.get("start_time"):
            data["date_label"] = swedish_date_label(a["start_time"])

        # Styrkeset loggade för just det här passet (Intervals skickar ingen
        # sådan data — se strength_sets i store.py). Här skickas de RÅA setsen,
        # inte den aggregerade formen som dagsanalyserna använder, eftersom
        # en per-passanalys ska kunna kommentera enskilda set.
        strength_sets = self.store.get_strength_sets_for_activity(activity_id)
        if strength_sets:
            data["strength_sets"] = [
                {
                    k: s.get(k)
                    for k in ("exercise", "set_number", "reps", "weight_kg", "rpe", "note")
                    if s.get(k) is not None
                }
                for s in strength_sets
            ]
            # Tidigare pass med SAMMA övningar. Utan dem kunde analysen inte
            # svara på det både den här prompten och BASE uttryckligen ber om
            # ("hur det står sig mot tidigare pass med samma övningar") —
            # payloaden innehöll bara passets egna set, så svaret blev
            # "utan historik för dessa övningar kan jag inte bedöma
            # progression över tid" fast databasen har ett års underlag
            # (marklyft 48 pass, axelpress 46).
            #
            # Dagen tas ur setsen, inte ur start_time: start_time kan vara
            # None (se _first_not_none i _map_activity), och raderna bär sin
            # egen dag ändå.
            day = max((s["day"] for s in strength_sets if s.get("day")), default=None)
            exercises = sorted({s["exercise"] for s in strength_sets if s.get("exercise")})
            if day and exercises:
                history = self.store.get_strength_history(
                    exercises, day, sessions_per_exercise=STRENGTH_HISTORY_SESSIONS
                )
                if history:
                    tidigare = fmt_strength_sessions(history)
                    data["strength_history"] = tidigare
                    # Bästa noteringen per övning, räknad åt Claude i
                    # stället för åt den. Maxvärdet ur en lista med tolv
                    # JSON-objekt är precis den sortens aritmetik en modell
                    # gör fel på, och prompten ber uttryckligen om ett
                    # besked om progressionen — samma skäl som
                    # reps_per_set finns av.
                    #
                    # Räknas ur HELA historiken för övningen, inte ur de
                    # åtta pass som skickas med. Ett rekord ur ett fönster
                    # är inget rekord: bänkpressen fick 89,4 kg medan det
                    # riktiga, 120 kg från 5 maj, låg utanför.
                    #
                    # Bara tyngsta vikten och flest reps följer med. Ett
                    # skattat maxlyft är en räkning PÅ ett lyft och blir en
                    # osanning i det ögonblick det presenteras som en
                    # notering atleten satt.
                    rekord = {}
                    for ovning in exercises:
                        historik = session_progression(
                            self.store.list_sets_for_exercise(ovning, sessions=None)
                        )
                        poster = {
                            k: v
                            for k, v in personal_records(historik).items()
                            if v is not None and k in ("top_weight_kg", "reps_total")
                        }
                        if poster:
                            rekord[ovning] = poster
                    if rekord:
                        data["strength_records"] = rekord

        prompt = self.system_prompt("activity")
        # Per-passanalysen saknade tidigare både max_tokens och max_chars
        # och körde alltså på ClaudeClient.analyze:s standardvärden, utan
        # backstop. Prompten beställer sju sektioner utan att nämna någon
        # längdgräns, så svaret kunde växa tills API:et kapade det rått
        # mitt i en mening — utan ens markeringen "(avkortat)" som de
        # andra analyserna får. Nu samma tak och samma städade avkortning
        # som morgon-, kvälls- och coachinganalysen.
        markdown = self.claude.analyze(
            prompt,
            json.dumps(data, default=str, ensure_ascii=False),
            max_tokens=MAX_ANALYSIS_TOKENS,
            max_chars=MAX_ANALYSIS_CHARS,
        )
        self.store.save_analysis("activity", markdown, activity_id, self.claude.model)
        return markdown

    def coaching(self) -> str:
        """Coachande rekommendationer. Genererar alltid när den anropas."""
        today = datetime.now().date().isoformat()
        activities = self.store.list_activities(limit=20)
        wellness = self.store.list_wellness(days=30)
        payload = {
            "recent_activities": [_fmt_activity_summary(a) for a in activities],
            # Samma form som morgon- och kvällsanalysen: dagens fulla rad
            # plus en slimmad dagserie. Här låg tidigare 30 HELA rader,
            # och det var mest tomhet — uppmätt på skarp data var 499 av
            # 870 fält (57 %) null, eftersom Garmin/Intervals inte
            # levererar stress, spO2, blodtryck, readiness, soreness och
            # ett dussin till i den här synkkedjan. 15 917 tecken för det
            # morgon- och kvällsanalysen klarar på 4 856.
            #
            # Fältet hette dessutom "wellness" medan prompterna
            # konsekvent talar om "wellness_history" — det som gick att
            # be modellen jämföra mot fanns alltså under ett annat namn
            # än det den ombads leta efter.
            #
            # Dagens rad hämtas på datum, inte som seriens första rad. Den
            # första raden är den SENASTE, och har dagens synk inte kommit
            # in är det gårdagens — som då skickades som "wellness_today".
            # Saknas dagens rad blir fältet tomt; gårdagen finns ändå
            # överst i wellness_history, med sitt riktiga datum.
            "wellness_today": wellness_for_payload(self.store.get_wellness_day(today)),
            "wellness_history": fmt_wellness_history(wellness),
            "self_reports": fmt_self_reports(self.store.list_self_reports(days=30)),
            "strength": fmt_strength_sessions(self.store.list_strength_sets(days=30)),
            # Utan date_label skrev Claude datumet på egen hand utifrån
            # generated_at nedan, och landade i "26 augusti 2026" — utan
            # veckodag, och i ett annat format än de andra analyserna.
            "date_label": swedish_date_label(datetime.now()),
            "generated_at": datetime.now().isoformat(),
        }
        prompt = self.system_prompt("coaching")
        markdown = self.claude.analyze(
            prompt,
            json.dumps(payload, default=str, ensure_ascii=False),
            max_tokens=MAX_ANALYSIS_TOKENS,
            max_chars=MAX_ANALYSIS_CHARS,
        )
        # Dagens datum som ref_id, inte None. Coaching-analysen blickar
        # framåt och har egentligen inget "sitt" datum, men utan ett går
        # det inte att avgöra om den redan genererats idag — vilket
        # run_coaching i scheduler.py behöver för att inte generera om den
        # vid varje omstart efter kl 12.
        self.store.save_analysis("coaching", markdown, today, self.claude.model)
        return markdown

    def morning_recommendation(self) -> str:
        """Morgonanalys: gårdagens sömn + träning -> rekommendation för idag.

        Körs på morgonen efter att sömndata registrerats (t.ex. från Garmin).
        Schemaläggaren (run_morning_recommendation) ansvarar för att kontrollera om
        ny sömndata kommit in; denna metod genererar alltid när den anropas.
        Vid manuell anrop via webb/CLI genereras också på nytt.
        """
        # Intervals lägger sömnen på den dag man vaknar, so "natten till idag"
        # finns på DAGENS datum. Gårdagens träning finns på gårdagens datum.
        today = datetime.now().date().isoformat()
        yesterday = (datetime.now() - timedelta(days=1)).date().isoformat()
        # 14 dagars wellness-historik för trendanalys (baslinje).
        wellness_history = self.store.list_wellness(days=14)
        payload = {
            "today_date": today,
            "last_night_sleep": wellness_for_payload(
                self.store.get_wellness_day(today), include_steps=False
            ),
            "yesterday_date": yesterday,
            "date_label": swedish_date_label(today),
            "yesterday_activities": [
                _fmt_activity_summary(a)
                for a in self.store.get_activities_for_date(yesterday)
            ],
            "recent_activities": [
                _fmt_activity_summary(a) for a in self.store.list_activities(limit=7)
            ],
            # Utan steps, till skillnad från kvällens historik. Morgonen
            # läser dygnet innan det hänt: dagens rad har ett par hundra
            # steg från natten, och modellen tolkade dem som en dagssiffra
            # ("endast 180 steg registrerat — total vilodag"). Steg säger
            # ingenting om hur natten gick, vilket är vad morgonanalysen
            # handlar om.
            "wellness_history": fmt_wellness_history(wellness_history, include_steps=False),
            "self_reports": fmt_self_reports(self.store.list_self_reports(days=14)),
            "strength": fmt_strength_sessions(self.store.list_strength_sets(days=14)),
            "generated_at": datetime.now().isoformat(),
        }
        prompt = self.system_prompt("morning_recommendation")
        markdown = self.claude.analyze(
            prompt,
            json.dumps(payload, default=str, ensure_ascii=False),
            max_tokens=MAX_ANALYSIS_TOKENS,
            max_chars=MAX_ANALYSIS_CHARS,
        )
        self.store.save_analysis(
            "morning_recommendation", markdown, today, self.claude.model
        )
        return markdown

    def evening_summary(self, day: str | None = None) -> str:
        """Kvällssammanfattning av ett dygns träning + nattens sömn.

        Körs kl 23:59 av schemaläggaren. Genererar alltid när den anropas
        (cachen används för visning på dashboarden, inte för att hoppa över
        generering). En tidigare manuell körning samma dag skrivs över med
        den senaste versionen som inkluderar dagens kompletta data.

        `day` (YYYY-MM-DD) anger vilket dygn som sammanfattas; utelämnad
        betyder idag. Parametern finns för schemaläggarens uppsamling: ett
        23:59-jobb som blir några minuter försenat kör efter midnatt, och
        utan möjligheten att peka ut gårdagen kunde det bara välja mellan
        att sammanfatta fel dygn eller att avstå helt — se
        _evening_target_day i scheduler.py.
        """
        day = day or datetime.now().date().isoformat()
        # Historiken klipps vid det dygn analysen gäller: fönstren räknas
        # från idag, och vid en uppsamling efter midnatt är "idag" en dag
        # analysen inte handlar om.
        wellness_history = _up_to_day(self.store.list_wellness(days=14), day)
        payload = {
            "today_date": day,
            "today_activities": [
                _fmt_activity_summary(a) for a in self.store.get_activities_for_date(day)
            ],
            "last_night_sleep": wellness_for_payload(self.store.get_wellness_day(day)),
            "date_label": swedish_date_label(day),
            "wellness_history": fmt_wellness_history(wellness_history),
            "self_reports": fmt_self_reports(
                _up_to_day(self.store.list_self_reports(days=14), day)
            ),
            "strength": fmt_strength_sessions(
                _up_to_day(self.store.list_strength_sets(days=14), day)
            ),
            "generated_at": datetime.now().isoformat(),
        }
        prompt = self.system_prompt("daily_summary")
        markdown = self.claude.analyze(
            prompt,
            json.dumps(payload, default=str, ensure_ascii=False),
            max_tokens=MAX_ANALYSIS_TOKENS,
            max_chars=MAX_ANALYSIS_CHARS,
        )
        self.store.save_analysis("daily_summary", markdown, day, self.claude.model)
        # Notera om dygnet hade något att sammanfatta NÄR den här texten
        # skrevs. Dashboarden kan inte räkna ut det i efterhand: trycker
        # man "Generera om" 00:16 blir sammanfattningen tom, och när
        # sömnen synkar vid sju ser dygnet plötsligt fullt ut igen. Frågan
        # "hade den här rapporten något att gå på?" kan bara besvaras här.
        self.store.set_job_state(
            EVENING_CONTENT_KEY,
            f"{day}:{1 if day_has_data(self.store, day) else 0}",
        )
        return markdown

