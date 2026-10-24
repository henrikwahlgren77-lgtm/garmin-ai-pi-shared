"""Styrkeprogression: skattat maxlyft, sessionsrader och rekord.

Databasen har ett års styrkeset — 1 273 set i 29 övningar sedan augusti
2025 — men ingenting läste dem som en utveckling. Dashboarden visade
total volym per vecka, vilket säger hur mycket du lyft men inte om
bänkpressen går uppåt, och analyserna fick råa set och ombads räkna själva.

Modulen är avsiktligt ren: den tar rader och ger tal tillbaka, utan att
känna till vare sig databasen eller Claude. Både webblagret och
analyspipelinen räknar därför på exakt samma sätt, och reglerna nedan
(vilka set som duger, hur ett maxlyft skattas) står på ett ställe.
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "EPLEY_MAX_REPS",
    "estimated_1rm",
    "measurable_sets",
    "personal_records",
    "session_progression",
]


# Över så här många reps slutar Epleys formel vara meningsfull. Den är
# linjär i reps, så ett set om 20 skattas som 167 % av vikten på stången —
# en siffra som inte beskriver något maxlyft utan uthållighet.
#
# Skarpt spelar gränsen liten roll: 15 av 1 273 set ligger över den. Den
# finns för att de få som gör det inte ska sätta ett falskt rekord som
# sedan aldrig går att slå.
EPLEY_MAX_REPS = 12


def estimated_1rm(weight_kg: Any, reps: Any) -> float | None:
    """Skattat maxlyft för ett set, enligt Epley. None om det inte går.

    1RM = vikt × (1 + reps/30). Formeln finns för att göra set med olika
    upplägg jämförbara: 5 reps på 120 kg (skattat 140) är ett tyngre lyft
    än 1 rep på 130 (130), och utan en gemensam skala ser det andra ut som
    det bättre passet bara för att talet på stången är högre.

    Ett rep ÄR maxlyftet och skattas inte: formeln ger annars 103 % av en
    vikt som faktiskt lyftes, alltså ett rekord som är omöjligt att slå
    med samma lyft en gång till.

    Returnerar None när underlaget saknas i stället för att gissa:
    kroppsviktsövningar har ingen vikt (37 set), klockan loggar ibland ett
    set utan repsräkning (44 set med reps=0), och över EPLEY_MAX_REPS
    säger formeln inget om styrka.
    """
    try:
        vikt = float(weight_kg)
        antal = int(reps)
    except (TypeError, ValueError):
        return None
    if vikt <= 0 or antal <= 0 or antal > EPLEY_MAX_REPS:
        return None
    if antal == 1:
        return round(vikt, 1)
    return round(vikt * (1 + antal / 30), 1)


def _reps(rad: dict[str, Any]) -> int:
    """Repsantalet i ett set, eller 0 om det inte gick att läsa.

    reps=0 är klockans sätt att säga "här hände något men jag räknade
    inte" — 44 set skarpt. De ska varken räknas som arbete eller dra ned
    ett pass som i övrigt var helt normalt.
    """
    try:
        return max(0, int(rad.get("reps")))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def measurable_sets(rader: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Set som bär både en vikt och ett repsantal.

    Publik, för att dashboardens "Tyngsta lyftet" ska räkna med samma
    regel som progressionen och analyserna. Den räknade tidigare varje
    set med en vikt, och ett set på 130 kg som klockan aldrig
    repsräknade blev dashboardens tyngsta lyft medan progressionskortet
    kallade samma set "ingen mätbar vikt".

    Kravet på vikt gäller volym och maxlyft — det är de talen som blir
    meningslösa utan den. Repsräkningen har ett eget, lösare krav (se
    _reps): armhävningar och hängande rodd har ingen vikt loggad alls,
    men reps beskriver ändå vad som gjordes, och ett tomt diagram är ett
    sämre svar än rätt mått med rätt etikett.
    """
    matbara = []
    for rad in rader:
        if _reps(rad) <= 0:
            continue
        try:
            vikt = float(rad.get("weight_kg"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if vikt > 0:
            matbara.append(rad)
    return matbara


def session_progression(sets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Råa set för EN övning till en rad per träningsdag, nyast först.

    Varje rad bär tre olika svar på "hur gick det", eftersom de kan peka åt
    olika håll och tillsammans säger mer än var för sig:

    - `top_weight_kg`: tyngsta stången. Det man minns från passet.
    - `estimated_1rm`: bästa setet omräknat till ett maxlyft, så pass med
      olika upplägg går att jämföra (se estimated_1rm).
    - `volume_kg`: reps × vikt summerat. Arbetet, inte styrkan.

    Skarpt exempel på varför alla tre behövs: marklyft 2026-08-20 gav 120 kg
    topp och 3 000 kg volym, 2026-08-27 gav 130 kg topp och 1 950 kg. Tyngre
    lyft, mindre arbete — vilket av passen som var "bättre" beror på frågan.

    `measured_sets` räknar de set som gick att räkna volym och maxlyft
    på, och `sets` alla loggade. Skiljer de sig har klockan loggat set
    utan reps, eller så är övningen en kroppsviktsövning. `reps_total`
    räknar båda sorterna — reps beskriver arbetet även utan en vikt.
    """
    per_dag: dict[str, list[dict[str, Any]]] = {}
    for rad in sets:
        dag = str(rad.get("day") or "")
        if dag:
            per_dag.setdefault(dag, []).append(rad)

    rader: list[dict[str, Any]] = []
    for dag in sorted(per_dag, reverse=True):
        alla = per_dag[dag]
        matbara = measurable_sets(alla)
        session: dict[str, Any] = {
            "day": dag,
            "sets": len(alla),
            "measured_sets": len(matbara),
            "reps_total": sum(_reps(r) for r in alla),
            "top_weight_kg": None,
            "volume_kg": 0.0,
            "estimated_1rm": None,
            "best_set": None,
            "top_set": None,
        }
        if matbara:
            session["top_weight_kg"] = max(float(r["weight_kg"]) for r in matbara)
            # Setet bakom tyngsta stången, med sina reps. Det är DET som är
            # ett personligt rekord — ett lyft som faktiskt utfördes — och
            # utan repsen går det inte att skriva ut som det gjordes
            # ("120 kg × 3"). Flest reps vinner vid samma vikt.
            tyngsta = max(
                (r for r in matbara
                 if float(r["weight_kg"]) == session["top_weight_kg"]),
                key=_reps,
            )
            session["top_set"] = {
                "weight_kg": float(tyngsta["weight_kg"]),
                "reps": _reps(tyngsta),
            }
            session["volume_kg"] = round(
                sum(float(r["weight_kg"]) * _reps(r) for r in matbara), 1
            )
            # Bästa setet är det med högst SKATTAT max, inte det tyngsta:
            # 5x120 slår 1x130. Set över EPLEY_MAX_REPS ger None och faller
            # bort här — de kan inte vinna, men de räknas i volymen.
            skattade = [
                (estimated_1rm(r["weight_kg"], r["reps"]), r) for r in matbara
            ]
            basta = max(
                ((v, r) for v, r in skattade if v is not None),
                default=None,
                key=lambda par: par[0],
            )
            if basta is not None:
                session["estimated_1rm"] = basta[0]
                session["best_set"] = {
                    "weight_kg": float(basta[1]["weight_kg"]),
                    "reps": int(basta[1]["reps"]),
                }
        rader.append(session)
    return rader


def personal_records(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Bästa noteringen per mått — med datum, och reps för tyngsta vikten.

    Bara `top_weight_kg` och `reps_total` beskriver något som faktiskt
    hänt. `estimated_1rm` är en RÄKNING på ett lyft, inte ett lyft: den
    som presenterar den som ett personligt rekord påstår att atleten lyft
    en vikt som aldrig legat på stången. Skarpt blev bänkpressens rekord
    89,4 kg en dag då tyngsta stången vägde 72,5.

    Räknas ur de sessioner som skickas in, inte ur hela historiken: den
    som vill ha ett rekord "genom alla tider" måste skicka in ALLA
    sessioner. Ett fönster på tolv pass gav bänkpressen rekordet 89,4 kg
    medan det riktiga — 120 kg — låg utanför och aldrig syntes.
    Vid lika värden vinner det ÄLDSTA datumet — ett rekord sattes när det
    sattes första gången, inte när det tangerades.

    Varje post är None när ingen session har ett värde att bidra med. En
    kroppsviktsövning får bara `reps_total`; ett pass där klockan loggat
    set utan att räkna dem får ingenting.
    """
    def basta(falt: str, extra: str | None = None) -> dict[str, Any] | None:
        # Noll och nedåt är inget rekord. En kroppsviktsövning har volymen
        # 0 i varje pass, och utan det här hade kortet rapporterat
        # "volymrekord: 0 kg" med ett datum — ett påstående om ingenting.
        kandidater = [
            s for s in sessions
            if s.get(falt) is not None and float(s[falt]) > 0
        ]
        if not kandidater:
            return None
        toppvarde = max(float(s[falt]) for s in kandidater)
        # Äldsta dagen som nådde värdet.
        traffar = [s for s in kandidater if float(s[falt]) == toppvarde]
        forst = min(traffar, key=lambda s: str(s["day"]))
        post: dict[str, Any] = {"value": toppvarde, "day": str(forst["day"])}
        # Repsen som vikten lyftes för. Ett personligt rekord är ett lyft,
        # och "120 kg" utan reps är bara halva det.
        if extra and forst.get(extra):
            post["reps"] = forst[extra].get("reps")
        return post

    return {
        "top_weight_kg": basta("top_weight_kg", extra="top_set"),
        "estimated_1rm": basta("estimated_1rm"),
        "volume_kg": basta("volume_kg"),
        # Flest reps i ett pass. Enda rekordet en kroppsviktsövning kan
        # sätta: armhävningar har varken vikt, volym eller maxlyft att
        # räkna på, men "24 reps mot 22 förra gången" är ett rekord i den
        # enda enhet övningen mäts i.
        "reps_total": basta("reps_total"),
    }
