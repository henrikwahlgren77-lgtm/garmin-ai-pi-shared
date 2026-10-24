"""Tester för analysis/prompts.py — konventionerna som gäller ALLA analyser.

Sektionsprompterna (_MORNING_BODY, _ACTIVITY_BODY, ...) beskriver vad varje
analys ska innehålla. Regler som gäller allihop hör hemma i BASE, inte
kopierade per sektion — kopior glider isär, och den här filen finns för att
fånga det när de gör det.
"""
from __future__ import annotations

# --- Vanlig svenska ---------------------------------------------------


_JARGONG = {
    "CTL": "Fitness",
    "ATL": "Trötthet",
    "TSB": "Form",
    "TSS": "belastning",
    "HRSS": "pulsbaserad belastning",
    "TRIMP": "träningsdos",
    "HRV": "variation i hjärtrytmen",
    "GAP": "gradjusterat tempo",
    "LTHR": "tröskelpuls",
    "RPE": "upplevd ansträngning",
    "1RM": "maxlyft",
}


def test_base_translates_every_abbreviation_it_asks_for() -> None:
    """Regeln och ordlistan måste följas åt. Står en förkortning i en
    sektionsrubrik men saknas i ordlistan har modellen inget svenskt ord
    att använda, och skriver ut förkortningen naken."""
    from analysis.prompts import _base

    base = _base()
    assert "SPRÅK:" in base
    for abbr, svenska in _JARGONG.items():
        assert f"{abbr} = {svenska}" in base, f"{abbr} saknas i ordlistan"


def test_no_analysis_prompt_uses_a_bare_abbreviation() -> None:
    """Konventionen fanns tidigare bara i morgon- och kvällsanalysen,
    kopierad per sektion. Passanalysen fick den aldrig och bad rakt ut om
    "HRSS, TRIMP" och "TSS, intensitetsfaktor, CTL/ATL" — vilket också är
    precis vad den skrev ut till atleten.

    Förkortningar får förekomma, men bara inom parentes efter det vanliga
    ordet, eller i BASE:s egen ordlista.
    """
    import re

    from analysis import prompts

    bodies = {
        name: value
        for name, value in vars(prompts).items()
        if name.endswith("_BODY") and isinstance(value, str)
    }
    assert bodies, "hittade inga promptkroppar att granska"

    nakna = []
    for name, body in sorted(bodies.items()):
        for abbr in _JARGONG:
            for match in re.finditer(rf"(?<![\w-]){re.escape(abbr)}(?![\w-])", body):
                if not body[: match.start()].endswith("("):
                    rad = body[: match.start()].count("\n") + 1
                    nakna.append(f"{name} rad {rad}: {abbr}")

    assert not nakna, "naken förkortning: " + ", ".join(nakna)


def test_no_prompt_asks_for_an_icon_in_a_heading() -> None:
    """Rubrikerna i analyserna bar en varningsikon ('⚠️ Risker'), och var
    de enda rubrikerna i hela gränssnittet som hade en ikon alls. En
    ensam ikon mitt bland rena rubriker läser som ett felmeddelande.

    Gäller prompterna, inte chattens felbubblor — där bär ikonen faktiskt
    en varning och står inte i en rubrik.
    """
    from analysis import prompts

    text = "\n".join(
        value for value in vars(prompts).values() if isinstance(value, str)
    )
    assert "⚠" not in text


def test_base_requires_headings_not_bold_for_sections() -> None:
    """Avsnittsnamnen renderades olika beroende på analys: passanalysen
    skrev '## Passöversikt' men kvällssammanfattningen '**Dagens
    sammanfattning**'. Fetstil i en serif-brödtext blir en fet mening,
    inte en rubrik — den får varken eget typsnitt eller eget avstånd, och
    flyter ihop med texten omkring."""
    from analysis.prompts import _base

    base = _base()
    assert "## " in base
    assert "aldrig som fetstil" in base


def _analysis_bodies() -> dict[str, str]:
    """Promptkropparna för de fyra ANALYSERNA — chatten är undantagen.

    Chatten svarar i löpande text och har inga fasta avsnitt; reglerna
    nedan gäller strukturerade analyser.
    """
    from analysis import prompts

    bodies = {
        name: value
        for name, value in vars(prompts).items()
        if name.endswith("_BODY") and isinstance(value, str) and name != "_CHAT_BODY"
    }
    assert len(bodies) == 4, f"väntade fyra analyskroppar, hittade {sorted(bodies)}"
    return bodies


def test_no_analysis_body_names_its_sections_in_bold() -> None:
    """Regeln i BASE gällde bara på pappret.

    BASE sa "Sätt varje avsnittsnamn som en RUBRIK med '## ', aldrig som
    fetstil på egen rad" — och sedan beskrev alla fyra promptkroppar sina
    avsnitt som '1. **Nattens sömn** - ...'. Alltså 25 konkreta exempel
    på precis det format regeln förbjuder, mot en enda abstrakt mening
    som förbjöd det. Modellen följde exemplen: kvällssammanfattningens
    avsnittsnamn kom ut som fetstil och flöt ihop med brödtexten, vilket
    ingen ändring i CSS:en kunde rätta till — en `**fetstil**` blir aldrig
    en `<h3>`.

    Fetstil MITT i en rad är fortfarande tillåten och efterfrågad: BASE
    ber uttryckligen om '**fetstil** för nyckeltal', och punktlistorna i
    morgon- och kvällsanalysen använder '- **Fitness (CTL):** ...'. Det
    här testet fångar bara avsnittsnamn som står som fetstil i stället
    för som rubrik.
    """
    import re

    fetstil = []
    for name, body in sorted(_analysis_bodies().items()):
        for match in re.finditer(r"^[ \t]*(?:\d+\.[ \t]*)?\*\*[^*\n]+\*\*[ \t]*$|"
                                 r"^[ \t]*\d+\.[ \t]+\*\*", body, re.M):
            rad = body[: match.start()].count("\n") + 1
            fetstil.append(f"{name} rad {rad}: {match.group().strip()}")

    assert not fetstil, (
        "avsnittsnamn som fetstil i stället för '## '-rubrik: " + ", ".join(fetstil)
    )


def test_every_analysis_body_shows_its_sections_as_headings() -> None:
    """Motsatsen till testet ovan: det räcker inte att fetstilen är borta,
    kropparna måste faktiskt visa formatet de vill ha tillbaka.

    En abstrakt regel i BASE räckte inte förra gången. Det som styr
    utdatan är exemplet, så varje kropp ska innehålla riktiga
    '## '-rubriker."""
    import re

    for name, body in sorted(_analysis_bodies().items()):
        rubriker = re.findall(r"^## \S", body, re.M)
        assert len(rubriker) >= 4, (
            f"{name} har {len(rubriker)} '## '-rubriker — beskriv avsnitten "
            "som rubriker, inte som en numrerad lista"
        )


def test_risk_section_is_always_called_risker() -> None:
    """BASE ber om 'en rubrik Risker'. Coachinganalysen kallade sin
    'Varningar', så samma sorts avsnitt hette två saker beroende på
    vilken ruta man läste på dashboarden.

    Alla analyser BEHÖVER inte ett riskavsnitt — passanalysen har aldrig
    haft något, den landar i 'Bedömning' i stället, och det är rimligt
    för en djupdykning i ett pass som redan är kört. Kravet är att den
    som har ett kallar det samma sak.
    """
    import re

    from analysis.prompts import _base

    assert "rubrik 'Risker'" in _base()

    med_risker = []
    for name, body in sorted(_analysis_bodies().items()):
        rubriker = re.findall(r"^## (.+)$", body, re.M)
        avvikande = [r for r in rubriker if "varning" in r.lower()]
        assert not avvikande, f"{name}: kalla riskavsnittet 'Risker', inte {avvikande}"
        if "Risker" in rubriker:
            med_risker.append(name)

    # De tre analyserna som blickar framåt eller sammanfattar ett dygn.
    assert len(med_risker) == 3, f"väntade tre kroppar med riskavsnitt, fick {med_risker}"


# --- Prompten får bara peka på fält som faktiskt finns ------------------


def test_no_analysis_asks_about_a_step_goal_that_is_not_in_the_data() -> None:
    """Kvällsprompten bad om "om målet nåddes". Det finns inget stegmål
    någonstans — varken i payloaden eller i .env — så den enda vägen till
    ett svar var att modellen hittade på ett tal att jämföra mot."""
    for name, body in sorted(_analysis_bodies().items()):
        assert "målet nåddes" not in body, (
            f"{name}: ber om ett stegmål som inte finns i datan"
        )


def test_the_evening_prompt_names_the_field_it_wants_compared() -> None:
    """Den skrev "jämfört med recent" — 'recent' är inget fält i någon
    payload. Samma sorts fel som när coachinganalysen bad om "wellness"
    medan fältet hette wellness_history: det som gick att jämföra mot
    fanns under ett annat namn än det modellen ombads leta efter."""
    from analysis.prompts import _DAILY_SUMMARY_BODY

    assert "wellness_history" in _DAILY_SUMMARY_BODY
    assert "recent" not in _DAILY_SUMMARY_BODY, (
        "'recent' är inget fält i kvällens payload — heter det något, ska "
        "det vara namnet payloaden faktiskt bär"
    )
