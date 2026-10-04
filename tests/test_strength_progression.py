"""Tester för analysis/strength.py och progressions-API:et.

Modulen räknar på riktig data (1 273 set, 29 övningar sedan augusti 2025),
och siffrorna nedan är hämtade därifrån — inte påhittade — så ett test som
faller pekar på ett fall som faktiskt finns i databasen.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from analysis.strength import (
    EPLEY_MAX_REPS,
    estimated_1rm,
    personal_records,
    session_progression,
)
from sync.store import Store, parse_strength_corrections

# --- Skattat maxlyft -----------------------------------------------------


def test_a_single_rep_is_the_max_lift_not_an_estimate() -> None:
    """Epley ger 103 % av en vikt som faktiskt lyftes en gång. Det vore ett
    rekord som är omöjligt att slå genom att göra om samma lyft."""
    assert estimated_1rm(130, 1) == 130.0


def test_more_reps_on_less_weight_can_be_the_heavier_lift() -> None:
    """Hela skälet till att skatta: 5 reps på 120 kg är tyngre än 1 rep på
    130, men talet på stången säger tvärtom. Utan en gemensam skala läser
    en analys det andra passet som det bättre."""
    assert estimated_1rm(120, 5) == 140.0
    assert estimated_1rm(130, 1) == 130.0
    assert estimated_1rm(120, 5) > estimated_1rm(130, 1)


def test_a_set_that_is_really_endurance_gets_no_estimate() -> None:
    """Formeln är linjär i reps: 20 reps skattas till 167 % av vikten. Den
    siffran beskriver uthållighet, inte styrka, och skulle sätta ett rekord
    som aldrig går att slå."""
    assert estimated_1rm(40, EPLEY_MAX_REPS) is not None
    assert estimated_1rm(40, EPLEY_MAX_REPS + 1) is None
    assert estimated_1rm(40, 20) is None


@pytest.mark.parametrize(
    ("vikt", "reps"),
    [
        (None, 5),      # kroppsviktsövning: 37 set skarpt
        (60, 0),        # klockan loggade ett set utan att räcka reps: 44 set
        (60, None),
        (0, 5),
        ("x", 5),
    ],
)
def test_an_unmeasurable_set_gives_no_estimate(vikt: object, reps: object) -> None:
    """None och inte en gissning — ett skattat maxlyft ur ingenting är ett
    påstående om något som inte mätts."""
    assert estimated_1rm(vikt, reps) is None


# --- Sessioner -----------------------------------------------------------


def _set(day: str, vikt: float | None, reps: int, nr: int = 1) -> dict:
    return {"day": day, "set_number": nr, "weight_kg": vikt, "reps": reps}


def test_a_session_carries_three_different_answers() -> None:
    """Topp, skattat max och volym kan peka åt olika håll, och gör det i
    riktiga data: marklyft 2026-08-20 gav 120 kg topp och 3 000 kg volym,
    2026-08-27 gav 130 kg topp och 1 950 kg. Tyngre lyft, mindre arbete."""
    rader = session_progression([
        _set("2026-08-27", 130, 5, 1),
        _set("2026-08-27", 130, 5, 2),
        _set("2026-08-27", 130, 5, 3),
        _set("2026-08-20", 120, 5, 1),
        _set("2026-08-20", 120, 5, 2),
    ])

    nyast, aldst = rader
    assert nyast["day"] == "2026-08-27"
    assert nyast["top_weight_kg"] == 130.0
    assert nyast["volume_kg"] == 1950.0
    assert aldst["volume_kg"] == 1200.0
    assert nyast["estimated_1rm"] > aldst["estimated_1rm"]


def test_the_best_set_is_the_heaviest_lift_not_the_heaviest_bar() -> None:
    """Bästa setet väljs på skattat max. Annars hade ett tungt singelförsök
    alltid vunnit över det set som faktiskt var det tyngsta lyftet."""
    rader = session_progression([
        _set("2026-09-03", 130, 1, 1),
        _set("2026-09-03", 120, 5, 2),
    ])

    assert rader[0]["best_set"] == {"weight_kg": 120.0, "reps": 5}
    assert rader[0]["top_weight_kg"] == 130.0, "tyngsta stången är fortfarande 130"


def test_sets_the_watch_logged_without_counting_are_left_out() -> None:
    """reps=0 är klockans "här hände något men jag räknade inte" — 44 set
    skarpt. De får inte dra ned volymen på ett pass som var helt normalt,
    men de ska synas i antalet loggade set."""
    rader = session_progression([
        _set("2026-08-25", 60, 5, 1),
        _set("2026-08-25", 60, 0, 2),
    ])

    assert rader[0]["sets"] == 2
    assert rader[0]["measured_sets"] == 1
    assert rader[0]["volume_kg"] == 300.0


def test_reps_are_counted_even_without_a_weight() -> None:
    """Armhävningar och hängande rodd har ingen vikt loggad alls. Volym och
    maxlyft går inte att räkna, men reps beskriver ändå vad som gjordes —
    och utan dem blev kurvan en rak nolla över fyra pass."""
    rader = session_progression([
        _set("2026-08-27", None, 12, 1),
        _set("2026-08-27", None, 12, 2),
    ])

    assert rader[0]["reps_total"] == 24
    assert rader[0]["estimated_1rm"] is None
    assert rader[0]["volume_kg"] == 0.0


# --- Rekord --------------------------------------------------------------


def test_a_record_belongs_to_the_day_it_was_first_reached() -> None:
    """Tangeras ett rekord är det fortfarande satt den första gången."""
    rader = session_progression([
        _set("2026-06-02", 130, 5),
        _set("2026-08-27", 130, 5),
    ])

    assert personal_records(rader)["estimated_1rm"]["day"] == "2026-06-02"


def test_the_three_records_can_belong_to_three_different_days() -> None:
    """Skarpt för marklyft: tyngsta stången 2026-04-21, bästa skattade max
    2026-06-02, största volym 2026-08-20. Ett kort som bara säger "rekord"
    utan att säga vilket blir motsägelsefullt — push press 2026-09-03 tog
    rekordet i tyngsta stången men inte i maxlyft, och kortet stod och
    påstod "nytt rekord" ovanför siffran 60,5 mot rekordets 63."""
    rader = session_progression([
        _set("2026-04-21", 150, 1),
        _set("2026-06-02", 130, 5),
        _set("2026-08-20", 120, 5, 1),
        _set("2026-08-20", 120, 5, 2),
        _set("2026-08-20", 120, 5, 3),
    ])
    rekord = personal_records(rader)

    assert rekord["top_weight_kg"]["day"] == "2026-04-21"
    assert rekord["estimated_1rm"]["day"] == "2026-06-02"
    assert rekord["volume_kg"]["day"] == "2026-08-20"


def test_zero_is_not_a_record() -> None:
    """En kroppsviktsövning har volymen 0 i varje pass. Utan spärren
    rapporterade kortet "volymrekord: 0 kg" med ett datum — ett påstående
    om ingenting."""
    rader = session_progression([_set("2026-08-27", None, 12)])

    rekord = personal_records(rader)
    assert rekord["volume_kg"] is None
    assert rekord["estimated_1rm"] is None


# --- Lagret --------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.init()
    return s


def test_the_window_counts_sessions_not_days(store: Store) -> None:
    """Frivändningar kördes 7 gånger på ett år. Ett fönster i kalenderdagar
    hade lämnat övningen utan jämförelsepunkter helt."""
    for dag in ("2025-01-01", "2025-06-01", "2026-01-01", "2026-06-01"):
        store.add_strength_sets(dag, "frivändning", [{"reps": 3, "weight_kg": 40}])

    assert len(store.list_sets_for_exercise("frivändning", sessions=2)) == 2
    dagar = [r["day"] for r in store.list_sets_for_exercise("frivändning", sessions=2)]
    assert dagar == ["2026-01-01", "2026-06-01"], "de SENASTE passen, äldst först"


def test_the_exercise_name_is_normalised_on_the_way_in(store: Store) -> None:
    """Namnet kommer från en URL eller från chatten; tabellen innehåller
    bara gemener (se normalize_exercise). Utan normalisering svarar
    "Bänkpress" tomt på en övning som har 37 pass."""
    store.add_strength_sets("2026-09-01", "bänkpress", [{"reps": 5, "weight_kg": 70}])

    assert store.list_sets_for_exercise("  BÄNKPRESS ") != []
    assert store.list_sets_for_exercise("") == []


def test_exercises_tried_once_are_left_out_of_the_picker(store: Store) -> None:
    """29 övningar är loggade, nio av dem i ett eller två pass. De har
    ingen utveckling att visa och gör listan svårare att leta i."""
    store.add_strength_sets("2026-09-01", "hopprep", [{"reps": 50}])
    for dag in ("2026-08-01", "2026-08-08", "2026-08-15"):
        store.add_strength_sets(dag, "marklyft", [{"reps": 5, "weight_kg": 130}])

    namn = [r["exercise"] for r in store.list_exercises(min_sessions=3)]
    assert namn == ["marklyft"]
    assert "hopprep" in [r["exercise"] for r in store.list_exercises(min_sessions=1)]


# --- API -----------------------------------------------------------------


def _app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("INTERVALS_API_KEY", "x")
    monkeypatch.setenv("INTERVALS_ATHLETE_ID", "i1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    monkeypatch.setenv("WEB_ACCESS_TOKEN", "")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    sys.modules.pop("web.app", None)
    return importlib.import_module("web.app").create_app()


def _fyll(tmp_path: Path) -> None:
    s = Store(tmp_path / "t.db")
    s.init()
    # Tre pass där det SISTA tar rekordet i tyngsta stången men inte i
    # skattat maxlyft — exakt fallet push press 2026-09-03 utgjorde.
    s.add_strength_sets("2026-08-13", "push press", [{"reps": 3, "weight_kg": 50}])
    s.add_strength_sets("2026-08-27", "push press", [{"reps": 6, "weight_kg": 52.5}])
    s.add_strength_sets("2026-09-03", "push press", [{"reps": 3, "weight_kg": 55}])


def test_the_progression_endpoint_reports_which_records_were_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fyll(tmp_path)
    app = _app(monkeypatch, tmp_path)

    with TestClient(app) as client:
        svar = client.get("/api/strength/progression", params={"exercise": "push press"})

    assert svar.status_code == 200
    data = svar.json()
    assert [s["day"] for s in data["sessions"]] == [
        "2026-08-13", "2026-08-27", "2026-09-03",
    ], "äldst först — en kurva läses från vänster"
    # 3x55 = 60,5 skattat; 6x52,5 = 63,0. Sista passet har tyngsta stången
    # men inte det tyngsta lyftet.
    assert data["records_set"] == ["top_weight_kg"]
    assert data["records"]["estimated_1rm"]["day"] == "2026-08-27"


def test_an_unknown_exercise_answers_404(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fyll(tmp_path)
    app = _app(monkeypatch, tmp_path)

    with TestClient(app) as client:
        assert client.get(
            "/api/strength/progression", params={"exercise": "stavhopp"}
        ).status_code == 404


def test_the_exercise_list_puts_the_most_trained_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Väljaren öppnar på första posten, alltså den övning som har mest att
    visa."""
    _fyll(tmp_path)
    s = Store(tmp_path / "t.db")
    for dag in ("2026-08-01", "2026-08-08", "2026-08-15", "2026-08-22"):
        s.add_strength_sets(dag, "marklyft", [{"reps": 5, "weight_kg": 130}])
    app = _app(monkeypatch, tmp_path)

    with TestClient(app) as client:
        namn = [r["exercise"] for r in client.get("/api/strength/exercises").json()]

    assert namn == ["marklyft", "push press"]


def test_a_bodyweight_exercise_can_still_set_a_record() -> None:
    """Armhävningar har varken vikt, volym eller maxlyft att räkna på. Flest
    reps i ett pass är det enda rekord övningen kan sätta — och utan det
    stod kortet med tre streck där rekordet skulle stå."""
    rader = session_progression([
        _set("2025-09-04", None, 11, 1),
        _set("2025-09-04", None, 11, 2),
        _set("2026-08-27", None, 12, 1),
        _set("2026-08-27", None, 12, 2),
    ])
    rekord = personal_records(rader)

    assert rekord["reps_total"] == {"value": 24.0, "day": "2026-08-27"}
    assert rekord["estimated_1rm"] is None, "det finns ingen vikt att skatta ur"


def test_reps_are_a_record_for_weighted_lifts_too() -> None:
    """Fältet räknas alltid. Vilket rekord kortet lyfter fram avgörs av vad
    kurvan visar, inte av vad som råkar finnas."""
    rader = session_progression([
        _set("2026-08-20", 120, 5, 1),
        _set("2026-08-20", 120, 5, 2),
        _set("2026-08-27", 130, 5, 1),
    ])

    assert personal_records(rader)["reps_total"]["day"] == "2026-08-20"


# --- Dolda övningar ------------------------------------------------------


def test_hidden_exercises_are_kept_out_of_the_picker(store: Store) -> None:
    """Accessoarlyft och FIT-poster vars namn inte gick att tolka ("okänd
    övning", 13 pass skarpt) fyller väljaren utan att ha någon utveckling
    att visa."""
    for dag in ("2026-08-01", "2026-08-08", "2026-08-15"):
        store.add_strength_sets(dag, "marklyft", [{"reps": 5, "weight_kg": 130}])
        store.add_strength_sets(dag, "okänd övning", [{"reps": 5, "weight_kg": 130}])

    namn = [
        r["exercise"]
        for r in store.list_exercises(min_sessions=3, exclude=["okänd övning"])
    ]
    assert namn == ["marklyft"]


def test_the_hidden_list_tolerates_how_it_was_typed(store: Store) -> None:
    """Listan skrivs för hand i .env. Versaler och extra mellanslag ska inte
    tyst göra en rad verkningslös."""
    for dag in ("2026-08-01", "2026-08-08", "2026-08-15"):
        store.add_strength_sets(dag, "slädpush", [{"reps": 1, "weight_kg": 240}])

    assert store.list_exercises(min_sessions=3, exclude=["  SLÄDPUSH "]) == []
    assert store.list_exercises(min_sessions=3, exclude=["", "annat"]) != []


def test_hiding_an_exercise_does_not_hide_its_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Väljaren filtreras, ingenting raderas: progressionen går fortfarande
    att hämta, och seten räknas kvar i veckovolymen."""
    _fyll(tmp_path)
    monkeypatch.setenv("PROGRESSION_HIDDEN_EXERCISES", "push press")
    app = _app(monkeypatch, tmp_path)

    with TestClient(app) as client:
        assert client.get("/api/strength/exercises").json() == []
        svar = client.get(
            "/api/strength/progression", params={"exercise": "push press"}
        )
    assert svar.status_code == 200
    assert len(svar.json()["sessions"]) == 3


def test_the_hidden_setting_survives_a_sloppy_env_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config import _list_setting

    monkeypatch.setenv("X_LISTA", " a , b ,, c, ")
    assert _list_setting("X_LISTA") == ("a", "b", "c")
    monkeypatch.setenv("X_LISTA", "")
    assert _list_setting("X_LISTA") == ()
    monkeypatch.delenv("X_LISTA")
    assert _list_setting("X_LISTA") == ()


# --- Ett rekord måste vara ett lyft som utförts --------------------------


def test_the_weight_record_carries_the_reps_it_was_lifted_for() -> None:
    """"120 kg" är halva beskedet — för ett rep och för tre är olika lyft."""
    rader = session_progression([
        _set("2026-05-05", 120, 3, 1),
        _set("2026-05-05", 120, 1, 2),
        _set("2026-09-01", 72.5, 7),
    ])

    assert personal_records(rader)["top_weight_kg"] == {
        "value": 120.0, "day": "2026-05-05", "reps": 3,
    }, "flest reps vinner vid samma vikt"


def test_an_estimate_is_never_presented_as_a_lift_that_happened() -> None:
    """Skarpt, och anmält av atleten: kortet påstod "Personligt rekord
    89,4 kg" i bänkpress för 1 september. Tyngsta stången den dagen vägde
    72,5 kg — 89,4 var Epleys räkning på ett set om 7 reps.

    Testet fäster att de två talen är olika saker och ligger i olika fält.
    Vilket av dem som får kallas rekord avgörs av den som presenterar, och
    det är tyngsta vikten.
    """
    rader = session_progression([
        _set("2026-09-01", 72.5, 3, 1),
        _set("2026-09-01", 72.5, 7, 2),
    ])
    rekord = personal_records(rader)

    assert rader[0]["estimated_1rm"] == 89.4
    assert rekord["top_weight_kg"]["value"] == 72.5
    assert rekord["top_weight_kg"]["value"] < rader[0]["estimated_1rm"]


def test_all_history_is_available_for_records(store: Store) -> None:
    """Rekordet räknas över hela historiken, kurvan visar ett fönster.

    Bänkpressen fick rekordet 89,4 kg ur tolv pass medan det riktiga —
    120 kg den 5 maj — låg utanför och aldrig syntes.
    """
    store.add_strength_sets("2026-05-05", "bänkpress", [{"reps": 3, "weight_kg": 120}])
    for i in range(1, 15):
        store.add_strength_sets(
            f"2026-08-{i:02d}", "bänkpress", [{"reps": 5, "weight_kg": 70}]
        )

    fonster = session_progression(store.list_sets_for_exercise("bänkpress", sessions=12))
    allt = session_progression(store.list_sets_for_exercise("bänkpress", sessions=None))

    assert len(fonster) == 12
    assert len(allt) == 15
    assert personal_records(fonster)["top_weight_kg"]["value"] == 70.0
    assert personal_records(allt)["top_weight_kg"] == {
        "value": 120.0, "day": "2026-05-05", "reps": 3,
    }


def test_the_endpoint_reports_the_all_time_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """API:et ska svara med rekordet, inte med fönstrets bästa."""
    s = Store(tmp_path / "t.db")
    s.init()
    s.add_strength_sets("2026-01-05", "marklyft", [{"reps": 1, "weight_kg": 150}])
    for i in range(1, 15):
        s.add_strength_sets(
            f"2026-08-{i:02d}", "marklyft", [{"reps": 5, "weight_kg": 100}]
        )
    app = _app(monkeypatch, tmp_path)

    with TestClient(app) as client:
        data = client.get(
            "/api/strength/progression", params={"exercise": "marklyft"}
        ).json()

    assert len(data["sessions"]) == 12, "kurvan visar fönstret"
    assert data["records"]["top_weight_kg"] == {
        "value": 150.0, "day": "2026-01-05", "reps": 1,
    }
    assert data["records_set"] == [], "senaste passet slog ingenting"


def test_the_card_does_not_call_an_all_time_record_a_window_record() -> None:
    """Noten skrev "inom de N pass som visas" om ett rekord som räknas över
    hela historiken (se testet ovan). Kvalifikationen blev kvar när
    rekorden byttes från fönstret till historiken, och gjorde ett riktigt
    personbästa till en notering inom tolv pass. Ett rekord som
    presenteras som mindre än det är, är samma sorts osanning som ett som
    presenteras som mer."""
    template = (
        Path(__file__).resolve().parents[1]
        / "src" / "web" / "templates" / "index.html"
    ).read_text(encoding="utf-8")
    # Kommentaren ovanför raden förklarar varför texten ändrades och nämner
    # den gamla formuleringen — leta bara i koden.
    kod = "\n".join(
        rad for rad in template.splitlines() if not rad.strip().startswith("//")
    )

    assert "pass som visas" not in kod, (
        "rekorden räknas över hela historiken, inte över de pass kurvan visar"
    )
    assert "satte personligt rekord i" in kod


# --------------------------------------------------------------------------
# Rättelser av set som klockan skrev ner fel.
#
# Två verkliga fall, båda funna genom att atleten läste sitt eget kort och
# sa "det där har jag inte lyft". Se parse_strength_corrections i store.py.


def test_a_correction_without_a_new_name_drops_the_sets(tmp_path: Path) -> None:
    """Bänkpressen 5 maj: åtta identiska set om 3 × 120 kg, vikten ett
    programsteg föreskrev och som aldrig låg på en stång."""
    store = Store(tmp_path / "t.db", ("2026-05-05:bänkpress",))
    store.init()
    store.add_strength_sets("2026-05-05", "bänkpress", [{"reps": 3, "weight_kg": 120.0}])
    store.add_strength_sets("2026-05-07", "bänkpress", [{"reps": 5, "weight_kg": 62.5}])

    rader = store.list_sets_for_exercise("bänkpress", sessions=None)
    assert [r["day"] for r in rader] == ["2026-05-07"]

    rekord = personal_records(session_progression(rader))
    assert rekord["top_weight_kg"]["value"] == 62.5


def test_a_correction_with_a_new_name_renames_the_sets(tmp_path: Path) -> None:
    """21 april: två marklyftsset märktes straight_leg_deadlift av ett
    programsteg, mitt i ett block med vanligt marklyft på samma vikt."""
    store = Store(tmp_path / "t.db", ("2026-04-21:raka marklyft=marklyft",))
    store.init()
    store.add_strength_sets(
        "2026-04-21", "raka marklyft", [{"reps": 5, "weight_kg": 120.0}]
    )

    assert store.list_sets_for_exercise("raka marklyft", sessions=None) == []
    flyttade = store.list_sets_for_exercise("marklyft", sessions=None)
    assert [(r["reps"], r["weight_kg"]) for r in flyttade] == [(5, 120.0)]


def test_a_correction_only_touches_the_day_it_names(tmp_path: Path) -> None:
    """Rättelsen är en dag och en övning, inte en vikt eller en övning.
    Samma övning andra dagar — och samma dags andra övningar — står kvar."""
    store = Store(tmp_path / "t.db", ("2026-05-05:bänkpress",))
    store.init()
    store.add_strength_sets("2026-05-05", "bänkpress", [{"reps": 3, "weight_kg": 120.0}])
    store.add_strength_sets("2026-05-05", "hack squat", [{"reps": 5, "weight_kg": 97.5}])
    store.add_strength_sets("2026-05-14", "bänkpress", [{"reps": 3, "weight_kg": 120.0}])

    assert len(store.list_sets_for_exercise("hack squat", sessions=None)) == 1
    kvar = store.list_sets_for_exercise("bänkpress", sessions=None)
    assert [r["day"] for r in kvar] == ["2026-05-14"]


def test_a_correction_also_fixes_rows_that_are_already_stored(tmp_path: Path) -> None:
    """Spärren på vägen in räcker inte: de felaktiga raderna skrevs innan
    rättelsen fanns, och ett pass från maj hämtas aldrig mer."""
    store = Store(tmp_path / "t.db")
    store.init()
    store.add_strength_sets("2026-05-05", "bänkpress", [{"reps": 3, "weight_kg": 120.0}])
    store.add_strength_sets(
        "2026-04-21", "raka marklyft", [{"reps": 5, "weight_kg": 120.0}]
    )

    rattad = Store(
        tmp_path / "t.db",
        ("2026-05-05:bänkpress", "2026-04-21:raka marklyft=marklyft"),
    )
    rattad.init()

    assert rattad.list_sets_for_exercise("bänkpress", sessions=None) == []
    assert rattad.list_sets_for_exercise("raka marklyft", sessions=None) == []
    assert len(rattad.list_sets_for_exercise("marklyft", sessions=None)) == 1


def test_a_correction_survives_the_session_being_synced_again(tmp_path: Path) -> None:
    """Det är därför rättelsen sitter på skrivvägen och inte på läsvägen.
    En radering i SQL hade kommit tillbaka vid nästa omsynk."""
    store = Store(tmp_path / "t.db", ("2026-05-05:bänkpress",))
    store.init()
    for _ in range(3):  # samma pass importerat om och om igen
        store.add_strength_sets(
            "2026-05-05", "bänkpress", [{"reps": 3, "weight_kg": 120.0}]
        )
    assert store.list_sets_for_exercise("bänkpress", sessions=None) == []


def test_a_correction_matches_regardless_of_spelling(tmp_path: Path) -> None:
    """Listan skrivs för hand. Den ska inte behöva matcha klockans exakta
    stavning — namnet normaliseras på båda sidor först."""
    store = Store(tmp_path / "t.db", ("2026-05-05:  BÄNKPRESS  ",))
    store.init()
    store.add_strength_sets("2026-05-05", "Bänkpress", [{"reps": 3, "weight_kg": 120.0}])
    assert store.list_sets_for_exercise("bänkpress", sessions=None) == []


@pytest.mark.parametrize("trasig", ["", "  ", "bara-text", "2026-05-05:", ":bänkpress"])
def test_an_unreadable_correction_is_ignored_instead_of_crashing(trasig: str) -> None:
    """En felskriven rad i .env ska inte hindra appen från att starta."""
    assert parse_strength_corrections([trasig]) == {}


def test_the_readable_corrections_survive_an_unreadable_one() -> None:
    """De rätta raderna gör fortfarande sitt."""
    rattelser = parse_strength_corrections(
        ["strunt", "2026-05-05:bänkpress", "2026-04-21:raka marklyft=marklyft"]
    )
    assert rattelser == {
        ("2026-05-05", "bänkpress"): None,
        ("2026-04-21", "raka marklyft"): "marklyft",
    }


def test_the_label_in_the_answer_is_the_name_the_data_is_stored_under(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regressionstest: svaret ekade tillbaka frågans stavning.

    list_sets_for_exercise normaliserar på vägen in, så "Push Press"
    hittade rätt rader — men fältet `exercise` i svaret var det som stod
    i URL:en, och det är fältet kurvan sätter som rubrik — en rubrik som
    stavas på ett sätt medan tabellen stavar det på ett annat.
    """
    _fyll(tmp_path)
    app = _app(monkeypatch, tmp_path)

    with TestClient(app) as client:
        svar = client.get(
            "/api/strength/progression", params={"exercise": "  Push   PRESS "}
        )

    assert svar.status_code == 200
    assert svar.json()["exercise"] == "push press"
