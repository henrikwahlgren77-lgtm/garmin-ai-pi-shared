"""Schemaläggning av sync och daglig analys med APScheduler."""
from __future__ import annotations

import json
import logging
import threading
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI

from analysis.claude import ClaudeClient
from config import Settings, parse_time_of_day
from sync.intervals_client import (
    IntervalsClient,
    sync_activities,
    sync_lock,
    sync_sport_settings,
    sync_wellness,
)
from sync.store import Store

log = logging.getLogger(__name__)

# APScheduler skippar tyst ett jobb om schemaläggarens interna klocktråd
# hinner bli mer försenad än detta innan den kommer runt till att köra
# det — standardvärdet är bara 1 sekund. Det upptäcktes efter att
# kvällssammanfattningen (23:59) missades helt en natt: loggen visade
# "was missed by 0:03:45" utan något fel i själva appen — schemaläggaren
# hade bara inte hunnit reagera inom sin (orimligt snäva) standardmarginal.
# En timmes marginal täcker vanliga förseningar (hög systemlast, en
# tillfälligt upptagen tråd) utan att låta ett jobb köras så sent att
# resultatet blir missvisande.
_CRON_MISFIRE_GRACE_SECONDS = 3600

# Hur länge nedstängningen väntar in uppstarts-catchupen (se
# _run_startup_catchups). Den är klar långt innan en normal nedstängning
# sker; taket finns bara så att en omstart som råkar komma sekunderna
# efter en start inte blir hängande bakom ett pågående Claude-anrop.
_CATCHUP_JOIN_TIMEOUT_SECONDS = 10.0


# Morgonrekommendationen görs om varje heltimme så här många timmar efter
# den schemalagda starttiden. Sömndatan från klockan kommer inte alltid in
# punktligt, och run_morning_recommendation hoppar över sig själv tills
# det finns ny data — fönstret ger den alltså flera chanser. Fyra timmar
# efter starten motsvarar de fem försök (07-11) som tidigare låg
# hårdkodade i triggern.
_MORNING_RETRY_HOURS = 4

# Nyckeln i job_state där underlaget för den senaste morgonanalysen sparas.
_MORNING_STATE_KEY = "morning_recommendation_data"

# Nycklarna i job_state för kvällskontrollen (se
# run_evening_summary_recheck). _EVENING_UNDERLAG_KEY minns vilket
# synkat underlag kvällssammanfattningen byggde på,
# _EVENING_RECHECK_DONE_KEY att kontrollen redan körts, så den bara sker
# en gång oavsett hur många gånger jobbet eller uppstarts-catchupen
# anropas samma morgon.
#
# EN rad var, som skrivs över — vilket dygn de gäller ligger i värdet
# ("day"), inte i nyckeln. Här stod tidigare "evening_summary_steps:DAG"
# och "evening_summary_recheck_done:DAG", alltså två nya rader per dygn
# som ingen läste igen efter morgonen därpå och ingenting städade: 730
# rader om året i en tabell som ska rymma en handfull. Båda markörerna
# frågas bara om GÅRDAGEN, en enda gång, så det finns inget att spara.
# Gamla rader tas bort i Store._migrate.
_EVENING_UNDERLAG_KEY = "evening_summary_underlag"
_EVENING_RECHECK_DONE_KEY = "evening_summary_recheck_done"

# Hur stor avvikelse i eftersynkat underlag som motiverar att göra om
# kvällssammanfattningen. Måste vara både en relativ OCH en absolut
# tröskel: en ren procentsats hade gjort om sammanfattningen för en dag på
# 200 steg som blev 210 (+5%, betydelselöst), och ett rent absoluttal hade
# missat en dag på 500 steg som blev 1000. Uppmätt skarpt: en vanlig
# efterjustering ligger på någon procent (12 540 -> 12 670, +1%) och ska
# INTE trigga; den som upptäcktes (19 674 -> 22 125, +12%) ska.
_STEPS_DRIFT_RELATIVE = 0.05
_STEPS_DRIFT_ABSOLUTE = 300

# Samma sorts tröskel för sömnen. Garmin räknar om natten när klockan
# synkat klart, och skillnaden mellan 6h55m och 7h00m är inget att skriva
# om texten för — femton minuter är det. Kvällssammanfattningen har
# sömnen som sin andra halva ("dygnets träning + nattens sömn"), så en
# omräkning där slår lika hårt mot texten som stegtalet gör.
_SLEEP_DRIFT_RELATIVE = 0.05
_SLEEP_DRIFT_ABSOLUTE = 900

# Lås runt "avgör om analysen behövs, generera, notera att den är gjord"
# för de tre dagliga analyserna.
#
# Var och en av dem har TVÅ anropare i samma process: sitt cron-jobb och
# uppstarts-catchupen (se _run_startup_catchups). Spärren som ska hindra
# dubbelarbete läser ett resultat som skrivs först EFTER Claude-anropet,
# och det anropet tar en halv minut. Startas tjänsten om under den sista
# halvminuten före en schemalagd tid hinner catchupen alltså in i anropet
# innan cron brinner, cron läser en spärr som fortfarande pekar på
# gårdagen, och båda genererar. Två betalda analyser och två rapporter
# för samma dygn.
#
# Fönstret är brett nog att träffa i praktiken: en omstart var som helst i
# de dryga trettio sekunderna före 07:00, 12:00 eller 23:59 räcker, och vi
# startar om tjänsten ofta.
#
# Att sätta spärren FÖRE analysen i stället vore fel åt andra hållet:
# misslyckas Claude-anropet ska nästa körning försöka igen, inte tro att
# jobbet är gjort.
#
# Låset måste omsluta både kontrollen och skrivningen. Läses spärren
# utanför låset ändrar låset ingenting — då hinner båda läsa "inte gjord"
# innan någon av dem hunnit skriva.
#
# Ett trådlås, inte ett processlås — samma avvägning som _sync_lock i
# sync/intervals_client.py. Webbens knappar för att generera om går
# medvetet förbi det helt: de anropar pipelinen direkt och är avsiktliga
# handlingar, inte schemalagda.
_analysis_lock = threading.Lock()


def _already_generated_at_scheduled_time(
    latest: dict[str, Any] | None, day: str, scheduled: tuple[int, int]
) -> bool:
    """Har `latest` verkligen genererats för `day`, vid eller efter dess tid?

    "Redan genererad idag" avgjordes tidigare bara av ref_id == idag. Det
    räckte inte: en manuell "Generera om" strax efter midnatt (t.ex. under
    testning) sparar en rad med DAGENS ref_id, fast timmar innan dagens
    träning och sömn ens finns. Den raden såg då ut precis som en riktig
    kvällskörning, och det schemalagda 23:59-jobbet hoppade tyst över sig
    självt — kvällssammanfattningen uteblev helt den dagen, trots att allt
    underlag fanns när klockan väl slog 23:59. Inträffade skarpt
    2026-08-30.

    Jämför därför mot dygnets schemalagda ÖGONBLICK och inte mot klockslag
    lösryckt från datum. Skillnaden märks vid uppsamling: gårdagens
    sammanfattning skriven 00:05 idag ligger efter gårdagens 23:59 och
    räknas som gjord, medan samma jämförelse på bara (timme, minut) hade
    läst 00:05 som "före 23:59" och genererat om den vid varje omstart.
    """
    if not latest or latest.get("ref_id") != day:
        return False
    try:
        created = datetime.fromisoformat(latest["created_at"])
        d = date.fromisoformat(day)
    except (KeyError, TypeError, ValueError):
        return False
    return created >= datetime(d.year, d.month, d.day, scheduled[0], scheduled[1])


def _value_has_drifted(
    then: float | None, now: float | None, relative: float, absolute: float
) -> bool:
    """Har ett mätvärde ändrats nog att motivera en ny analys?

    Se trösklarna ovan för varför både den relativa och den absoluta
    måste passeras.

    `then=None` betyder att sammanfattningen skrevs utan värdet alls —
    wellness-raden hade inte synkat kl 23:59. Texten skrev då "ingen
    stegdata tillgänglig", och kommer siffran in under natten är det det
    STARKASTE skälet att göra om den, inte det svagaste. Absoluttröskeln
    gäller ändå: en dag som visar sig ha 40 steg är inte värd en betald
    analys till.

    `now=None` gör aldrig något. Värdet kan försvinna ur svaret (en dag
    Intervals inte längre rapporterar), och en sammanfattning som säger
    ett tal är bättre än en som säger ingenting.
    """
    if now is None:
        return False
    if then is None:
        return now >= absolute
    diff = abs(now - then)
    return diff >= absolute and diff >= then * relative


def _steps_have_drifted(then: int | None, now: int | None) -> bool:
    """Stegtröskeln — se _value_has_drifted."""
    return _value_has_drifted(then, now, _STEPS_DRIFT_RELATIVE, _STEPS_DRIFT_ABSOLUTE)


def _sleep_has_drifted(then: int | None, now: int | None) -> bool:
    """Sömntröskeln — se _value_has_drifted."""
    return _value_has_drifted(then, now, _SLEEP_DRIFT_RELATIVE, _SLEEP_DRIFT_ABSOLUTE)


def _job_state_json(store: Store, key: str) -> dict[str, Any] | None:
    """Läser en JSON-markör ur job_state. None om den saknas eller är trasig.

    En markör som inte går att tolka behandlas som frånvarande i stället
    för att kastas vidare. Ett halvskrivet värde hade annars fått
    json.loads att kasta, jobbet att fångas av sitt yttre except, och
    samma fel att upprepas vid varje körning — utan att markören någonsin
    hann skrivas om till något giltigt.
    """
    raw = store.get_job_state(key)
    if not raw:
        return None
    try:
        varde = json.loads(raw)
    except ValueError:
        log.warning("Markören %s i job_state gick inte att tolka som JSON.", key)
        return None
    return varde if isinstance(varde, dict) else None


def _job_state_day(store: Store, key: str) -> str | None:
    """Vilket dygn en markör gäller, eller None om den saknas."""
    noterat = _job_state_json(store, key)
    return noterat.get("day") if noterat else None


def _mark_recheck_done(store: Store, day: str, now: datetime) -> None:
    """Noterar att kvällskontrollen är avklarad för `day`."""
    store.set_job_state(
        _EVENING_RECHECK_DONE_KEY, json.dumps({"day": day, "at": now.isoformat()})
    )


def _evening_underlag(store: Store, day: str) -> dict[str, Any]:
    """Det synkade underlaget en kvällssammanfattning för `day` vilar på.

    Bara det som kommer från Intervals och kan ändras i efterhand utan
    att någon rör appen. Det du själv skriver in (self_reports, set
    loggade i chatten) står inte här: ändrar du något där gör du det
    medvetet, och en analys som plötsligt skrev om sig själv efteråt hade
    varit förvirrande snarare än hjälpsam.
    """
    wellness = store.get_wellness_day(day) or {}
    return {
        "steps": wellness.get("steps"),
        "sleep_seconds": wellness.get("sleep_seconds"),
        "activities": sorted(
            str(a["id"]) for a in store.get_activities_for_date(day) if a.get("id")
        ),
    }


def _underlaget_har_andrats(
    then: dict[str, Any], now: dict[str, Any]
) -> str | None:
    """Vad som ändrats sedan sammanfattningen skrevs, i klartext — eller None.

    Returnerar en mening att logga snarare än True/False, så journalen
    säger VARFÖR en analys gjordes om. Kontrollen fanns först bara för
    stegtalet, men samma sorts efterjustering drabbar dygnets andra
    hälfter: ett pass som laddas upp från klockan först på morgonen
    saknas helt i en sammanfattning skriven 23:59, och Garmin räknar om
    natten när den synkat klart.

    Bara fält markören FAKTISKT noterade jämförs. En markör skriven av en
    äldre version känner bara till "steps", och ett fält den aldrig såg
    kan inte ha ändrats sedan dess — att behandla det som en ändring hade
    gjort om varje sammanfattning en gång vid uppgraderingen.
    """
    if "steps" in then and _steps_have_drifted(then["steps"], now.get("steps")):
        return f"stegtalet {then['steps']} -> {now.get('steps')}"

    if "sleep_seconds" in then and _sleep_has_drifted(
        then["sleep_seconds"], now.get("sleep_seconds")
    ):
        return (
            f"sömnen {then['sleep_seconds']} -> {now.get('sleep_seconds')} sekunder"
        )

    if "activities" in then:
        # Pass jämförs exakt, utan tröskel: ett pass finns eller finns
        # inte, och en sammanfattning som missar ett helt pass är fel på
        # ett sätt inget procenttal mildrar.
        foran = set(then["activities"] or [])
        efter = set(now.get("activities") or [])
        if foran != efter:
            delar = []
            if efter - foran:
                delar.append(f"nya pass {', '.join(sorted(efter - foran))}")
            if foran - efter:
                delar.append(f"borttagna pass {', '.join(sorted(foran - efter))}")
            return " och ".join(delar)

    return None


def _evening_target_day(now: datetime, scheduled: tuple[int, int]) -> str | None:
    """Vilket dygn kvällssammanfattningen gäller just nu, eller None.

    Två fall ger ett svar, och de är inte samma sak:

    1. Klockan har passerat dagens schemalagda tid — då är det idag.
    2. Klockan har inte det, men vi ligger inom misfire-fönstret efter
       GÅRDAGENS schemalagda tid — då är det gårdagen som ska samlas upp.

    Fall 2 finns därför att _CRON_MISFIRE_GRACE_SECONDS inte gjorde någon
    nytta alls för det klockslag appen faktiskt kör på. Nåden är en timme,
    men jobbet självt avvisade sig på (timme, minut) mot 23:59 — och varje
    försening från 23:59 passerar midnatt. Uppmätt före rättelsen: i tid
    och 30 s sent genererades, 4 min och 46 min sent hoppades över. Nåden
    var alltså 60 sekunder i praktiken, inte 3 600.

    Utanför båda fallen: None. En omstart mitt på dagen ska inte
    sammanfatta ett dygn som knappt börjat, och gårdagen är sedan länge
    förlorad — evening_summary kan skriva den, men underlaget den skulle
    beskriva har hunnit bli ett halvt dygn gammalt.
    """
    hour, minute = scheduled
    dagens = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= dagens:
        return now.date().isoformat()
    gardagens = dagens - timedelta(days=1)
    if (now - gardagens).total_seconds() <= _CRON_MISFIRE_GRACE_SECONDS:
        return gardagens.date().isoformat()
    return None

# Vilka wellness-fält som avgör om morgonanalysen behöver göras om.
#
# Spärren jämförde tidigare wellness.last_synced med analysens
# created_at. Det såg rimligt ut men kunde aldrig fungera: last_synced
# sätts till datetime.now() vid VARJE upsert (se _map_wellness i
# intervals_client), oavsett om något värde faktiskt ändrats. Med
# SYNC_INTERVAL_HOURS=1 skrevs den alltså om varje timme, jämförelsen blev
# sann varje gång, och morgonanalysen gjordes om vid varje försök i
# fönstret. Uppmätt på Pi5:an: 5-8 morgonanalyser per dygn i stället för
# en, alla utom den första betalda i onödan — och den rekommendation du
# läste 07:15 byttes tyst ut mot en annan 08:00, eftersom dashboarden
# visar den senaste.
#
# Fälten nedan är de som beskriver natten och belastningen: de sätts en
# gång när klockan synkat färdigt och ändras sedan inte under dagen.
#
# Medvetet UTELÄMNADE: steps, kcal_consumed, hydration och stress ändras
# under dagen — de tre första är dygnssummor som växer från noll vid
# midnatt till sina 10-15 tusen på kvällen, den fjärde ett medelvärde som
# räknas om. Tas de med börjar spärren släppa igenom igen, fast av en
# anledning som inte har någonting med morgonens återhämtning att göra.
# last_synced och raw_json är uteslutna av samma skäl som gjorde den gamla
# spärren verkningslös.
_MORNING_DATA_FIELDS = (
    "sleep_seconds",
    "sleep_score",
    "sleep_quality",
    "avg_sleeping_hr",
    "hrv",
    "hrv_sdnn",
    "resting_hr",
    "respiration",
    "spO2",
    "readiness",
    "soreness",
    "fatigue",
    "mood",
    "motivation",
    "injury",
    "weight",
    "rest_day",
    "ctl",
    "atl",
    "tsb",
    "ramp_rate",
)


def _morning_window(settings: Settings) -> tuple[tuple[int, int], int]:
    """Morgonfönstret som ((starttimme, startminut), sista timme).

    Fönstret är en och samma sak sett från två håll: cron-triggern i
    make_lifespan brinner varje heltimme inuti det, och
    run_morning_recommendation vägrar köra utanför det. Det räknades
    tidigare bara på ett ställe — i triggern — så jobbet självt visste
    inte att det fanns ett fönster. Uppstarts-catchupen, som anropar
    funktionen direkt vid varje omstart, gick därmed rakt förbi det.

    Klipps vid midnatt i stället för att svepa runt till nästa dygn: en
    morgonanalys som körs kl 02 skulle sammanfatta fel natt.
    """
    hour, minute = parse_time_of_day(
        settings.morning_recommendation_time, "MORNING_RECOMMENDATION_TIME"
    )
    return (hour, minute), min(hour + _MORNING_RETRY_HOURS, 23)


def _within_morning_window(now: datetime, settings: Settings) -> bool:
    """Är klockan inne i morgonfönstret?

    Sista timmen räknas HEL, inte fram till startminuten: cron-jobbet
    brinner på minuten men får skjutas upp en timme av
    _CRON_MISFIRE_GRACE_SECONDS, och en körning som skulle skett 11:00
    men kommer 11:45 är fortfarande en morgonanalys. Nästa dygns 06:59
    är det inte.
    """
    (hour, minute), last_hour = _morning_window(settings)
    if (now.hour, now.minute) < (hour, minute):
        return False
    return now.hour <= last_hour


def _morning_measured_fields(wellness: dict[str, Any]) -> list[str]:
    return sorted(f for f in _MORNING_DATA_FIELDS if wellness.get(f) is not None)


def _morning_data_snapshot(day: str, wellness: dict[str, Any]) -> str:
    """Sammanfattar dagens underlag som en jämförbar sträng.

    Noterar VILKA fält som har ett värde, inte vilka värden de har. Det är
    andra försöket, och skillnaden är hela poängen.

    Första versionen jämförde värdena. Den gick från 5-8 analyser per dygn
    ner till två — 07:00 och 08:00 den 30 augusti, båda loggade som "ny
    sömndata" trots att natten redan var registrerad 07:00. Klockan
    fortsätter nämligen skicka in justeringar efter första synken: hrv
    45 -> 46, vilopuls 61 -> 62, sömnscore som räknas om. Varje sådan
    småjustering kostade en hel ny analys.

    Fönstret 07-11 finns för att vänta in att nattens data DYKER UPP, inte
    för att följa den medan den finjusteras. Så fort ett mätvärde finns är
    analysen gjord; kommer ett fält som saknades helt (sömnen synkade sent,
    HRV kom först vid nio) räknas det som nytt underlag och analysen görs
    om.

    Sparas som JSON och inte som en hash: raden är ändå kort, och den som
    undrar varför analysen kördes om kan läsa svaret direkt ur job_state.

    Dagen ingår så att ett nytt dygn alltid ser ut som nytt underlag.
    """
    return json.dumps(
        {"day": day, "fields": _morning_measured_fields(wellness)}, sort_keys=True
    )


def run_sync(settings: Settings, full: bool = False) -> bool:
    """Kör en Intervals-sync. Returnerar True vid lyckad sync, annars False.

    Returvärdet gör att anropare (t.ex. CLI:t) kan avsluta med fel exit-kod
    om syncen misslyckas, istället för att felet bara försvinner i loggen.

    `full=True` hämtar hela wellness-historiken i stället för bara det
    rullande fönstret (se sync_wellness) — för att fylla i luckor bakåt.
    """
    try:
        store = Store(settings.db_path, settings.strength_corrections)
        store.init()
        # Låset delas med webbens /sync-route (se sync_lock i
        # sync/intervals_client.py) så en manuell synk och den schemalagda
        # inte skriver till databasen samtidigt. Här väntar vi hellre in
        # den pågående än hoppar över körningen.
        with sync_lock(), IntervalsClient(settings) as client:
            sync_activities(client, store)
            sync_wellness(client, store, full=full)
            sync_sport_settings(client, store)

        log.info("Sync klar.")
        return True
    except Exception:
        log.exception("Sync misslyckades.")
        return False


def run_morning_recommendation(settings: Settings) -> None:
    """Generera morgonrekommendation när underlaget för idag ändrats.

    Körs varje heltimme i morgonfönstret (se _morning_window), och hoppar
    över sig själv av två skäl.

    1. Klockan ligger utanför fönstret. Kvälls- och coachingjobbet har
       haft en sådan spärr hela tiden; det här hade ingen alls, och
       _run_startup_catchups anropar funktionen vid VARJE omstart —
       alltså vid varje uppdatering, oavsett klockslag. Räckte det att
       ett wellness-fält som saknades i morse hade dykt upp under dagen
       skrevs en ny "morgonrekommendation", och eftersom dashboarden
       visar den senaste byttes dagens plan tyst ut mot en skriven på
       kvällen. Inträffade skarpt: 2026-09-03 kl 00:15:28, en sekund
       efter en omstart, för ett dygn som var femton minuter gammalt.
    2. Nattens mätvärden är desamma som när den senaste analysen skrevs
       — se _MORNING_DATA_FIELDS för vad som räknas som ändrat underlag.

    Tidsspärren ligger först, före både databas och synk: den beror inte
    på någon data och ska inte kosta en anslutning för att svara nej.
    """
    try:
        now = datetime.now()
        if not _within_morning_window(now, settings):
            (start_hour, start_minute), last_hour = _morning_window(settings)
            log.info(
                "Klockan %02d:%02d ligger utanför morgonfönstret "
                "%02d:%02d-%02d:59, hoppar över morgonanalysen.",
                now.hour, now.minute, start_hour, start_minute, last_hour,
            )
            return

        store = Store(settings.db_path, settings.strength_corrections)
        store.init()

        today = now.date().isoformat()

        # Synka FÖRST, jämför sedan. Ordningen var tvärtom tidigare, med
        # två följder: spärren avgjorde frågan "har det kommit ny
        # sömndata?" på data som kunde vara en timme gammal, och när
        # svaret blev nej returnerade funktionen innan den hann köra sin
        # egen synk — den synken var alltså onåbar i precis det läge där
        # den hade gjort nytta. Synken tar en halv sekund; Claude-anropet
        # den skyddar mot tar trettio.
        #
        # Samma delade lås som run_sync och webbens /sync — se sync_lock
        # i intervals_client.
        #
        # Ett synkfel stoppar INTE analysen. Det gjorde det tidigare: felet
        # fångades av funktionens yttre except, och var Intervals nere hela
        # morgonfönstret blev det ingen morgonanalys alls — trots att den
        # timvisa synken redan hämtat nattens data. Synken här finns för
        # att få med det allra senaste, inte för att analysen ska vänta på
        # den; spärren nedan avgör ändå på det som faktiskt ligger i
        # databasen.
        try:
            with sync_lock(), IntervalsClient(settings) as client:
                sync_activities(client, store)
                sync_wellness(client, store)
                sync_sport_settings(client, store)
        except Exception:
            log.warning(
                "Synken före morgonanalysen misslyckades — fortsätter med "
                "data från senaste lyckade synk.",
                exc_info=True,
            )

        # Spärren läses och skrivs under samma lås — se _analysis_lock.
        # Wellness läses också här inne: den andra anroparen kan ha synkat
        # in ny data medan vi väntade, och beslutet ska fattas på det som
        # gäller nu.
        with _analysis_lock:
            wellness = store.get_wellness_day(today)
            if not wellness:
                log.info("Ingen wellness-data för idag än, väntar med morgonanalys.")
                return

            snapshot = _morning_data_snapshot(today, wellness)
            previous = store.get_job_state(_MORNING_STATE_KEY)
            latest = store.latest_analysis("morning_recommendation")
            if latest and latest.get("ref_id") == today and previous == snapshot:
                log.info("Ingen ny sömndata sedan senaste morgonanalysen, hoppar över.")
                return

            # Skriv ut VAD som var nytt. Utan den här raden gick det inte
            # att se varför en körning valde att generera om — loggen sa
            # bara "ny sömndata", vilket krävde detektivarbete i databasen
            # när spärren betedde sig oväntat.
            if latest and latest.get("ref_id") == today and previous:
                try:
                    innan = set(json.loads(previous).get("fields", []))
                except ValueError:
                    innan = set()
                nya = sorted(set(_morning_measured_fields(wellness)) - innan)
                log.info(
                    "Nya mätvärden sedan förra morgonanalysen: %s.",
                    ", ".join(nya) or "inga",
                )

            claude = ClaudeClient(settings)
            from analysis.pipeline import AnalysisPipeline

            AnalysisPipeline(store, claude, settings.athlete).morning_recommendation()
            # Efter analysen, inte före: misslyckas Claude-anropet ska nästa
            # heltimme försöka igen i stället för att tro att jobbet är gjort.
            store.set_job_state(_MORNING_STATE_KEY, snapshot)
            log.info("Morgonrekommendation genererad baserat på ny sömndata.")
    except Exception:
        log.exception("Morgonanalys misslyckades.")


def run_evening_summary(settings: Settings) -> None:
    """Generera kvällssammanfattning av dagens träning + sömn.

    Anropas av både det schemalagda cron-jobbet (kl settings.
    evening_summary_time) och en engångs-catchup vid uppstart (se
    make_lifespan) — samma mönster som run_morning_recommendation redan
    använder. Catchupen fångar fallet att tjänsten startades om precis
    runt den schemalagda tiden och alltså missade den; kombinerat med
    _CRON_MISFIRE_GRACE_SECONDS (som fångar den vanligare varianten: en
    kortare fördröjning i schemaläggarens egen klocktråd) täcker det in
    orsaken till att kvällsanalysen en gång uteblev helt en natt.

    Två spärrar gör att catchupen inte springer i väg vid en helt vanlig
    omstart mitt på dagen:
    1. Klockan måste ligga efter dagens schemalagda tid, eller inom
       misfire-fönstret efter gårdagens (se _evening_target_day) — annars
       skulle en omstart klockan 14 generera en nästan tom sammanfattning
       av en dag som knappt börjat.
    2. Ingen sammanfattning ska redan finnas sparad för det dygnet,
       genererad vid eller efter dess schemalagda tid (se
       _already_generated_at_scheduled_time) — annars skulle varje omstart
       efter kl 23:59 (t.ex. flera uppdateringar samma kväll) generera om
       den i onödan. En manuell "Generera om" tidigare på dygnet räknas
       INTE som redan gjord, så den riktiga körningen klockan 23:59 sker
       ändå.

    Ett dygn som missats helt kan repareras så länge förseningen ryms i
    misfire-fönstret: evening_summary tar emot vilket dygn den ska
    sammanfatta, så en körning 00:05 skriver gårdagens rapport och inte en
    tom rapport om ett fem minuter gammalt dygn. Längre bort än så
    repareras ingenting — underlaget finns kvar, men rapporten hade
    beskrivit ett dygn ingen längre väntar på.
    """
    try:
        store = Store(settings.db_path, settings.strength_corrections)
        store.init()

        now = datetime.now()
        scheduled = parse_time_of_day(
            settings.evening_summary_time, "EVENING_SUMMARY_TIME"
        )
        day = _evening_target_day(now, scheduled)
        if day is None:
            log.info(
                "Klockan %02d:%02d ligger varken efter dagens schemalagda tid "
                "%s eller inom uppsamlingsfönstret efter gårdagens — hoppar "
                "över kvällssammanfattningen.",
                now.hour, now.minute, settings.evening_summary_time,
            )
            return

        # Frågan gäller ETT dygn, så slå upp just det i stället för den
        # senaste av alla: vid en uppsamling efter midnatt kan en manuell
        # körning för det nya dygnet redan ligga överst, och den säger
        # ingenting om huruvida gårdagens är skriven.
        # Kontroll och generering under samma lås — se _analysis_lock.
        with _analysis_lock:
            latest = store.latest_analysis("daily_summary", day)
            if _already_generated_at_scheduled_time(latest, day, scheduled):
                log.info(
                    "Kvällssammanfattning redan genererad för %s, hoppar över.", day
                )
                return

            # Underlaget läses FÖRE analysen, inte efter.
            #
            # Markören ska minnas vad texten byggde på, och pipelinen läser
            # sin data när den börjar. Claude-anropet tar en halv minut,
            # och den timvisa synken tar inte det här låset — en synk som
            # landar i det fönstret hade alltså skrivit in siffror
            # sammanfattningen aldrig sett. Morgonkontrollen hade då
            # jämfört mot fel baslinje, sett noll drift, och tyst missat
            # precis den efterjustering den finns för att fånga.
            underlag = _evening_underlag(store, day)

            claude = ClaudeClient(settings)
            from analysis.pipeline import AnalysisPipeline

            AnalysisPipeline(store, claude, settings.athlete).evening_summary(day)
            log.info("Kvällssammanfattning genererad för %s.", day)

            # Skrivs efter analysen, av samma skäl som spärrarna gör det:
            # misslyckas Claude-anropet ska ingen markör påstå att det
            # finns en sammanfattning att kontrollera i morgon.
            store.set_job_state(
                _EVENING_UNDERLAG_KEY, json.dumps({"day": day, **underlag})
            )
    except Exception:
        log.exception("Kvällsanalys misslyckades.")


def run_evening_summary_recheck(settings: Settings) -> None:
    """Gör om gårdagens kvällssammanfattning om underlaget hunnit ändras.

    Kvällssammanfattningen skrivs 23:59 med den data Intervals.icu har
    DÅ. Men Intervals är inte alltid färdigsynkad med dygnet vid
    midnatt — Garmin efterjusterar, och Intervals hämtar justeringen på
    sin egen synktakt. Skarpt fall 2026-09-06: sammanfattningen skrev
    "19 674 steg" kl 23:59, den verkliga, färdigräknade totalen var
    22 125 (+12%) och syntes i databasen först eftermiddagen därpå.

    Kontrollen gällde först bara stegtalet, men det var att laga ett
    exempel snarare än felet: samma efterjustering träffar sömnen (Garmin
    räknar om natten) och passen (ett pass som ligger kvar i klockan över
    natten laddas upp först på morgonen och saknas alltså helt i en
    sammanfattning skriven 23:59). Se _evening_underlag för vad som
    jämförs och _underlaget_har_andrats för hur.

    Körs en gång varje morgon (EVENING_SUMMARY_RECHECK_TIME, default
    09:00) — sent nog att Intervals normalt hunnit synka klart gårdagen,
    tidigt nog att det märks samma dag som kvällssammanfattningen
    faktiskt läses. Precis som de tre andra dagliga jobben har den en
    spärr mot att köra mer än en gång per dygn den gäller (se
    _EVENING_RECHECK_DONE_KEY) och en tidsspärr som gör den säker att
    anropa från uppstarts-catchupen oavsett klockslag.
    """
    try:
        now = datetime.now()
        scheduled = parse_time_of_day(
            settings.evening_summary_recheck_time, "EVENING_SUMMARY_RECHECK_TIME"
        )
        if (now.hour, now.minute) < scheduled:
            log.info(
                "Klockan %02d:%02d är före schemalagd tid %s för "
                "kvällskontrollen, hoppar över.",
                now.hour, now.minute, settings.evening_summary_recheck_time,
            )
            return

        store = Store(settings.db_path, settings.strength_corrections)
        store.init()
        day = (now.date() - timedelta(days=1)).isoformat()

        # Första passet: finns det något att kontrollera alls? Under låset,
        # eftersom grenen "inget att jämföra mot" skriver sin markör.
        with _analysis_lock:
            if _job_state_day(store, _EVENING_RECHECK_DONE_KEY) == day:
                log.info("Kvällskontroll redan gjord för %s, hoppar över.", day)
                return

            noterat = _job_state_json(store, _EVENING_UNDERLAG_KEY)
            if noterat is None or noterat.get("day") != day:
                # Ingen kvällssammanfattning skrevs för dygnet (eller den
                # skrevs innan den här funktionen fanns) — inget att
                # jämföra mot. Markera ändå gjort, annars körs den här
                # grenen på nytt varje gång jobbet triggas samma morgon.
                #
                # Loggas, för den här grenen var tyst: uppstarts-catchupen
                # 2026-09-07 18:23 körde kontrollen, skrev sin markör och
                # lämnade inte ett spår i journalen. Ett jobb som inte
                # syns när det kör går inte att felsöka när det inte gör
                # det den ska.
                log.info(
                    "Inget underlag noterat för %s — ingen kvällssammanfattning "
                    "att kontrollera. Markerar dygnet som klart.", day,
                )
                _mark_recheck_done(store, day, now)
                return
            then = {k: v for k, v in noterat.items() if k != "day"}

        # Synka FÖRST så jämförelsen sker mot färsk data, inte mot en
        # timme gammal cache — samma resonemang som i
        # run_morning_recommendation. Aktiviteterna måste med: det är just
        # det pass som laddades upp i morse som kontrollen ska kunna se,
        # och wellness-synken hämtar inga pass.
        #
        # UTANFÖR _analysis_lock, precis som run_morning_recommendation
        # gör det. Låg synken innanför blockerade den här funktionen de
        # tre andra dagliga analyserna under hela nätverksanropet — en
        # halv minut plus nedladdning av FIT-filer för nya styrkepass.
        # Morgonrekommendationens omförsök kör 09:00, samma minut som den
        # här, och hade fått vänta in något den inte har med att göra.
        with sync_lock(), IntervalsClient(settings) as client:
            sync_activities(client, store)
            sync_wellness(client, store)

        # Andra passet: jämför, generera, notera — under låset igen.
        with _analysis_lock:
            # Spärren läses om. Två anropare (cron-jobbet och uppstarts-
            # catchupen) kan ha tagit sig förbi första passet innan någon
            # av dem hunnit skriva markören, och då ska bara den ena
            # betala för en analys. Se _analysis_lock.
            if _job_state_day(store, _EVENING_RECHECK_DONE_KEY) == day:
                log.info(
                    "Kvällskontrollen för %s blev gjord medan synken kördes, "
                    "hoppar över.", day,
                )
                return

            nu = _evening_underlag(store, day)
            andring = _underlaget_har_andrats(then, nu)

            if andring:
                log.info(
                    "Underlaget för %s har ändrats (%s) sedan "
                    "kvällssammanfattningen skrevs, gör om den.",
                    day, andring,
                )
                claude = ClaudeClient(settings)
                from analysis.pipeline import AnalysisPipeline

                AnalysisPipeline(store, claude, settings.athlete).evening_summary(day)
                store.set_job_state(
                    _EVENING_UNDERLAG_KEY, json.dumps({"day": day, **nu})
                )
            else:
                log.info(
                    "Underlaget för %s oförändrat (%s), ingen ny "
                    "sammanfattning behövs.",
                    day, then,
                )

            _mark_recheck_done(store, day, now)
    except Exception:
        log.exception("Kvällskontroll misslyckades.")


def run_coaching(settings: Settings) -> None:
    """Generera träningsanalysen (coaching) för dagen.

    Körs kl settings.coaching_time (COACHING_TIME, default 12:00) och som
    en catchup vid uppstart, med samma två spärrar som
    run_evening_summary — för tidigt på dygnet, respektive redan
    genererad idag.

    Till skillnad från kvällssammanfattningen har coaching-analysen inget
    eget datum i sig (den blickar framåt), men den sparas ändå med dagens
    datum som ref_id så spärren nedan kan skilja dagens från gårdagens.
    coaching() i pipelinen sparade tidigare ref_id=None, vilket hade gjort
    "redan genererad idag" omöjlig att avgöra — se ändringen där.
    """
    try:
        store = Store(settings.db_path, settings.strength_corrections)
        store.init()

        now = datetime.now()
        today = now.date().isoformat()
        scheduled = parse_time_of_day(settings.coaching_time, "COACHING_TIME")
        if (now.hour, now.minute) < scheduled:
            log.info(
                "Klockan %02d:%02d är före schemalagd tid %s för "
                "träningsanalys, hoppar över.",
                now.hour, now.minute, settings.coaching_time,
            )
            return

        # Kontroll och generering under samma lås — se _analysis_lock.
        with _analysis_lock:
            latest = store.latest_analysis("coaching")
            if _already_generated_at_scheduled_time(latest, today, scheduled):
                log.info("Träningsanalys redan genererad för idag, hoppar över.")
                return

            # Utan någon wellness-data alls finns det ingenting att coacha
            # utifrån, och analysen skulle bli fri fantasi. Gäller en färsk
            # installation innan första synken hunnit köra.
            if store.latest_wellness_day() is None:
                log.info("Ingen wellness-data alls än, väntar med träningsanalys.")
                return

            claude = ClaudeClient(settings)
            from analysis.pipeline import AnalysisPipeline

            AnalysisPipeline(store, claude, settings.athlete).coaching()
            log.info("Träningsanalys genererad.")
    except Exception:
        log.exception("Träningsanalys misslyckades.")


# Backupfilernas namn. Datumet sist gör att en alfabetisk sortering också
# är en kronologisk — det är hela grunden för rotationen i _prune_backups.
#
# Bindestrecket i prefixet är inte kosmetiskt: det gör att mönstret aldrig
# kan matcha den levande databasen (training.db), ens om BACKUP_DIR pekas
# om till samma katalog som DB_PATH.
_BACKUP_PREFIX = "training-"
_BACKUP_SUFFIX = ".db"

# SQLite lägger sina sidofiler bredvid databasen med samma namn plus ett
# suffix. Backuper skrivs numera i journal_mode=DELETE just för att slippa
# dem (se Store.backup), men äldre kopior — och kopior som något annat
# verktyg hunnit öppna — kan ha dem liggande. De matchar inte *.db och blev
# därför aldrig städade när sin huvudfil roterades bort.
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _prune_backups(directory: Path, keep: int) -> list[Path]:
    """Tar bort alla utom de `keep` senaste backuperna. Returnerar de borttagna."""
    if keep < 1:
        return []
    existing = sorted(
        path
        for path in directory.glob(f"{_BACKUP_PREFIX}*{_BACKUP_SUFFIX}")
        if path.is_file()
    )
    removed = existing[:-keep]
    for path in removed:
        path.unlink()
        for suffix in _SQLITE_SIDECAR_SUFFIXES:
            sidecar = path.with_name(path.name + suffix)
            if sidecar.is_file():
                sidecar.unlink()
    return removed


def run_backup(settings: Settings) -> Path | None:
    """Nattlig städning: kopiera databasen, gallra ersatta analyser.

    Databasen är den enda kopian av allt appen själv skapat: analyserna,
    det du rapporterat i chatten och styrkeseten. Intervals har rådatan
    kvar, men inget av det andra går att återskapa om filen försvinner.

    Kopian tas FÖRE gallringen, inte efter. Gallringen är den enda
    rutinen i appen som raderar något, och en kopia tagen i minuten innan
    är det starkaste skyddsnätet den kan få: går något fel i urvalet ligger
    raderna kvar i natten backup, och i de tretton dessförinnan.

    Returnerar sökvägen till kopian, eller None om något gick fel. Fel
    loggas men kastas inte vidare — en misslyckad backup ska inte ta ner
    tjänsten, och nästa natt gör ett nytt försök.
    """
    try:
        store = Store(settings.db_path, settings.strength_corrections)
        dest = settings.backup_dir / (
            f"{_BACKUP_PREFIX}{datetime.now().date().isoformat()}{_BACKUP_SUFFIX}"
        )
        # Samma lås som synken. Backup-API:et börjar om från början om
        # källan skrivs under kopieringen, så en backup mitt i en synk blir
        # korrekt men onödigt långsam.
        with sync_lock():
            store.backup(dest)
        removed = _prune_backups(settings.backup_dir, settings.backup_keep)
        log.info(
            "Backup skriven: %s (%.1f MB)%s",
            dest,
            dest.stat().st_size / 1_048_576,
            f", rensade {len(removed)} äldre." if removed else ".",
        )

        # Gallringen efter kopian, av samma skäl som står i docstringen.
        gallrade = store.prune_analyses(settings.analysis_keep_days)
        if gallrade:
            # VACUUM bara när något faktiskt raderats. En DELETE ger inte
            # tillbaka utrymmet av sig själv — SQLite återanvänder sidorna
            # — och att skriva om hela filen varje natt för sakens skull är
            # ren förslitning på ett SD-kort.
            store.vacuum()
            log.info("Gallrade %d ersatta analysversioner.", gallrade)
        return dest
    except Exception:
        log.exception("Backup misslyckades.")
        return None


def _latest_backup_day(directory: Path) -> str | None:
    """Datumet i den senaste backupens namn, eller None om det inte finns någon."""
    if not directory.is_dir():
        return None
    namn = sorted(
        p.name for p in directory.glob(f"{_BACKUP_PREFIX}*{_BACKUP_SUFFIX}") if p.is_file()
    )
    return namn[-1][len(_BACKUP_PREFIX):-len(_BACKUP_SUFFIX)] if namn else None


def run_backup_catchup(settings: Settings, now: datetime | None = None) -> Path | None:
    """Tar nattens backup nu, om den senaste schemalagda uteblev.

    Backupjobbet är ett cron-jobb som brinner vid BACKUP_TIME och inte
    annars. Var Pi:n avstängd, eller startade om, just då blev det ingen
    kopia det dygnet — och inget tog igen den. Analysjobben har haft sin
    uppsamling vid start länge; backupen, som skyddar allt de skriver,
    hade det inte.

    Den senaste schemalagda tidpunkten är idag vid BACKUP_TIME om den
    passerat, annars igår. Finns en kopia daterad den dagen eller senare
    görs ingenting. En manuell `backup` samma dygn räknas alltså också.
    """
    try:
        now = now or datetime.now()
        timme, minut = parse_time_of_day(settings.backup_time, "BACKUP_TIME")
        senast = now.replace(hour=timme, minute=minut, second=0, microsecond=0)
        if now < senast:
            senast -= timedelta(days=1)
        dag = senast.date().isoformat()
        finns = _latest_backup_day(settings.backup_dir)
        if finns is not None and finns >= dag:
            log.info("Backup för %s finns redan, hoppar över.", dag)
            return None
        log.info("Backupen för %s saknas, tar den nu.", dag)
        return run_backup(settings)
    except Exception:
        log.exception("Uppsamlingen av backupen misslyckades.")
        return None


def _run_startup_catchups(settings: Settings) -> None:
    """Kör de dagliga jobben en gång direkt vid uppstart.

    Cron-triggarna nedan brinner bara exakt vid sina tidpunkter (07-11:00,
    23:59, 09:00, ...). Startas tjänsten om strax efter en sådan tidpunkt —
    vilket händer ofta, varje uppdatering är en omstart — missas den helt
    till nästa gång. Alla funktionerna har egna spärrar (ingen ny sömndata
    / för tidigt på dygnet / redan genererad idag), så det är säkert att
    ropa på dem vid varje start oavsett klockslag.

    Kördes tidigare som två DateTrigger-jobb i schemaläggaren. Ett sådant
    jobb har ingen nästa körtid och raderar därför sig självt när det är
    klart — vilket kunde krocka med nedstängningen: APScheduler tömmer
    jobstore:n i shutdown() medan dess klocktråd står mitt i _process_jobs
    och är på väg att ta bort samma jobb, och tråden dog då med ett
    ohanterat JobLookupError. Cron- och intervall-jobb har alltid en nästa
    körtid och når aldrig den kodvägen, så en vanlig tråd här tar bort
    hela den kapplöpningen (och är dessutom enklare än ett schemalagt
    engångsjobb som ska köras omedelbart).

    Körs sekventiellt, inte parallellt: morgonkörningen kan göra en full
    Intervals-synk, och två samtidiga skribenter mot samma SQLite-databas
    är det vi försöker undvika på alla andra ställen också.
    """
    try:
        run_morning_recommendation(settings)
        run_coaching(settings)
        run_evening_summary(settings)
        run_evening_summary_recheck(settings)
        # Sist: analyserna är de som har bråttom, och en kopia tagen efter
        # dem får med det de nyss skrev.
        run_backup_catchup(settings)
    except Exception:
        # Funktionerna fångar redan sina egna fel; det här är bara ett
        # skyddsnät så att tråden inte dör med ett spårutskrivet
        # traceback om något oväntat läcker igenom.
        log.exception("Uppstarts-catchup misslyckades.")


def make_lifespan(settings: Settings):
    """Returnerar en FastAPI lifespan som startar/stannar schedulern."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not settings.scheduler_enabled:
            # Ren webbserver (SCHEDULER_ENABLED=0): varken jobb eller
            # catchup. Produktionsinstansen sköter det åt båda.
            app.state.scheduler = None
            app.state.startup_catchup = None
            try:
                yield
            finally:
                _close_intervals(app)
            return

        scheduler = BackgroundScheduler()
        # Timvis Intervals-sync (SYNC_INTERVAL_HOURS i .env).
        #
        # Hade tidigare next_run_time=None, vilket INTE betyder "använd
        # triggerns tid" utan är precis så APScheduler pausar ett jobb:
        # pause_job() är implementerat som modify_job(next_run_time=None),
        # och _real_add_job beräknar bara en körtid när attributet saknas
        # helt. Jobbet lades alltså till i pausat läge och kördes aldrig en
        # enda gång — SYNC_INTERVAL_HOURS var död konfiguration, och all
        # färsk data kom i praktiken från en systemd-timer (numera
        # borttagen) i en helt annan process.
        #
        # IntervalTrigger brinner första gången ett helt intervall efter
        # start, så en omstart utlöser ingen synk-storm.
        scheduler.add_job(
            run_sync,
            trigger=IntervalTrigger(hours=settings.sync_interval_hours),
            id="sync",
            args=[settings],
            replace_existing=True,
        )
        # Morgonrekommendation: körs varje heltimme i ett fönster som
        # börjar vid MORNING_RECOMMENDATION_TIME, och kollar varje gång om
        # ny sömndata kommit in. Hoppar över om ingen ny data finns.
        #
        # Fönstret låg tidigare hårdkodat som hour="7-11", minute=0 — alltså
        # helt frikopplat från MORNING_RECOMMENDATION_TIME, som lästes in i
        # config.py och dokumenterades i .env.example men aldrig användes
        # till något. Att sätta den till 05:00 gjorde ingenting alls.
        # Samma fönster som run_morning_recommendation själv spärrar mot
        # (se _morning_window) — annars kan triggern och jobbet glida isär.
        (morning_hour, morning_minute), last_hour = _morning_window(settings)
        morning_hours = (
            f"{morning_hour}-{last_hour}" if last_hour > morning_hour else str(morning_hour)
        )
        scheduler.add_job(
            run_morning_recommendation,
            trigger=CronTrigger(hour=morning_hours, minute=morning_minute),
            id="morning_recommendation",
            args=[settings],
            replace_existing=True,
            misfire_grace_time=_CRON_MISFIRE_GRACE_SECONDS,
        )
        # Kvällssammanfattning (EVENING_SUMMARY_TIME, default 23:59).
        evening_hour, evening_minute = parse_time_of_day(
            settings.evening_summary_time, "EVENING_SUMMARY_TIME"
        )
        scheduler.add_job(
            run_evening_summary,
            trigger=CronTrigger(hour=evening_hour, minute=evening_minute),
            id="evening_summary",
            args=[settings],
            replace_existing=True,
            misfire_grace_time=_CRON_MISFIRE_GRACE_SECONDS,
        )

        # Kvällskontroll (EVENING_SUMMARY_RECHECK_TIME, default 09:00) —
        # gör om gårdagens kvällssammanfattning om Intervals hunnit synka
        # ett annat underlag än det som stod till 23:59: fler steg, en
        # omräknad natt eller ett pass som laddades upp först i morse. Se
        # run_evening_summary_recheck.
        recheck_hour, recheck_minute = parse_time_of_day(
            settings.evening_summary_recheck_time, "EVENING_SUMMARY_RECHECK_TIME"
        )
        scheduler.add_job(
            run_evening_summary_recheck,
            trigger=CronTrigger(hour=recheck_hour, minute=recheck_minute),
            id="evening_summary_recheck",
            args=[settings],
            replace_existing=True,
            misfire_grace_time=_CRON_MISFIRE_GRACE_SECONDS,
        )

        # Träningsanalys (COACHING_TIME, default 12:00). Mitt på dagen:
        # dagens sömn- och wellness-data har hunnit synkas, och svaret
        # finns på plats innan man planerar kvällens pass. Genererades
        # tidigare bara manuellt via knappen i webben.
        coaching_hour, coaching_minute = parse_time_of_day(
            settings.coaching_time, "COACHING_TIME"
        )
        scheduler.add_job(
            run_coaching,
            trigger=CronTrigger(hour=coaching_hour, minute=coaching_minute),
            id="coaching",
            args=[settings],
            replace_existing=True,
            misfire_grace_time=_CRON_MISFIRE_GRACE_SECONDS,
        )

        # Nattlig backup (BACKUP_TIME, default 03:30). Ligger mellan
        # kvällssammanfattningen (23:59) och morgonfönstret (från 07:00),
        # så kopian tas när ingenting annat skriver.
        backup_hour, backup_minute = parse_time_of_day(
            settings.backup_time, "BACKUP_TIME"
        )
        scheduler.add_job(
            run_backup,
            trigger=CronTrigger(hour=backup_hour, minute=backup_minute),
            id="backup",
            args=[settings],
            replace_existing=True,
            misfire_grace_time=_CRON_MISFIRE_GRACE_SECONDS,
        )
        scheduler.start()
        app.state.scheduler = scheduler

        # Uppstarts-catchup för de dagliga analyserna (se
        # _run_startup_catchups). I en egen tråd eftersom morgonkörningen
        # kan göra en full Intervals-synk följd av ett Claude-anrop — det
        # får inte hålla uvicorns uppstart gisslan i en halv minut.
        #
        # Går att stänga av med STARTUP_CATCHUP=0. Testsviten gör det för
        # varje test (se conftest), utom de som handlar om catchupen
        # själv: tråden gjorde annars riktiga anrop mot både Intervals och
        # Anthropic vid varje TestClient-uppstart, och hölls ofarlig bara
        # av spärrar vars fel försvann i trådens except Exception.
        catchup = None
        if settings.startup_catchup:
            catchup = threading.Thread(
                target=_run_startup_catchups,
                args=(settings,),
                name="startup-catchup",
                daemon=True,
            )
            catchup.start()
        app.state.startup_catchup = catchup

        try:
            yield
        finally:
            scheduler.shutdown(wait=False)
            if catchup is not None:
                catchup.join(timeout=_CATCHUP_JOIN_TIMEOUT_SECONDS)
            _close_intervals(app)

    return lifespan


def _close_intervals(app: FastAPI) -> None:
    # HTTP-klienten mot Intervals skapas i web.app.create_app och levde
    # tidigare vidare med sin öppna anslutningspool tills processen dog —
    # lifespan är appens nedstängningshook, så den stängs här. getattr
    # eftersom make_lifespan i princip kan användas av en app som inte har
    # någon klient.
    intervals = getattr(app.state, "intervals", None)
    if intervals is not None:
        intervals.close()
