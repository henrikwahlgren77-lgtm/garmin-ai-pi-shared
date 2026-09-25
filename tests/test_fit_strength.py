"""Tester för FIT-parsningen av styrkepass (sync/fit_strength.py).

Testerna matar in avkodade set-meddelanden direkt istället för riktiga
FIT-filer — formen är hämtad från en verklig fil från användarens Garmin
(dump-fit på ett marklyftspass), så de speglar hur datan faktiskt ser ut:
category kommer som en lista med upprepade värden, och vilopauser ligger
som egna meddelanden mellan arbetsseten.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sync.fit_strength import (
    is_strength_activity,
    parse_set_messages,
    parse_strength_sets,
)


def test_strength_classifier_has_exactly_one_definition() -> None:
    """Regressionstest: samma regel för "är det här ett styrkepass?" fanns
    kopierad i både sync/intervals_client.py (avgör om FIT-filen ska
    hämtas) och analysis/pipeline.py (avgör vilket pass chattloggade set
    kopplas till). Två kopior betyder att en ny aktivitetstyp från
    Intervals måste läggas till på båda ställena — glöms det ena bort
    importeras styrkedata men kopplas aldrig till passet, eller tvärtom."""
    from analysis import pipeline
    from sync import intervals_client

    assert pipeline.is_strength_activity is is_strength_activity
    assert intervals_client.is_strength_activity is is_strength_activity


def test_strength_classifier_matches_intervals_activity_types() -> None:
    assert is_strength_activity({"type": "WeightTraining", "sport": None})
    assert is_strength_activity({"type": None, "sport": "Strength"})
    # Skiftlägesokänsligt, och matchar även sammansatta namn.
    assert is_strength_activity({"type": "weighttraining", "sport": ""})
    assert not is_strength_activity({"type": "Run", "sport": "Run"})
    assert not is_strength_activity({"type": None, "sport": None})
    assert not is_strength_activity({})


def _active_set(
    index: int,
    reps: int,
    weight: float,
    category: str = "deadlift",
    subtype: int | None = 0,
) -> dict:
    """Ett arbetsset. subtype=None utelämnar fältet helt, vilket händer på
    riktigt: 13 av användarens marklyftsset saknar subtyp i filen och får
    då falla tillbaka på kategorinamnet."""
    mesg = {
        "timestamp": datetime(2026, 8, 20, 17, 11, 26, tzinfo=timezone.utc),
        "duration": 29.541,
        "start_time": datetime(2026, 8, 20, 17, 11, 26, tzinfo=timezone.utc),
        "repetitions": reps,
        "weight": weight,
        # FIT:s array-fält: samma värde upprepat.
        "category": [category, category, category],
        "weight_display_unit": "kilogram",
        "message_index": index,
        "set_type": "active",
    }
    if subtype is not None:
        mesg["category_subtype"] = [subtype, subtype, subtype]
    return mesg


def _rest_set(index: int) -> dict:
    return {
        "timestamp": datetime(2026, 8, 20, 17, 11, 26, tzinfo=timezone.utc),
        "duration": 206.952,
        "start_time": datetime(2026, 8, 20, 17, 11, 55, tzinfo=timezone.utc),
        "message_index": index,
        "set_type": "rest",
    }


def test_parses_real_world_deadlift_session() -> None:
    """Tre marklyftsset med vilopauser emellan, precis som filen ser ut."""
    mesgs = [
        _active_set(0, reps=5, weight=120.0),
        _rest_set(1),
        _active_set(2, reps=5, weight=120.0),
        _rest_set(3),
        _active_set(4, reps=5, weight=120.0),
    ]

    result = parse_set_messages(mesgs)

    assert len(result) == 1
    assert result[0]["exercise"] == "marklyft"   # deadlift översatt
    assert result[0]["sets"] == [
        {"reps": 5, "weight_kg": 120.0},
        {"reps": 5, "weight_kg": 120.0},
        {"reps": 5, "weight_kg": 120.0},
    ]


def test_rest_periods_are_not_saved_as_sets() -> None:
    """Vilopauserna är egna set-meddelanden utan reps och vikt. Räknas de
    som set blir både antalet set och volymen fel."""
    mesgs = [_active_set(0, reps=8, weight=80.0), _rest_set(1), _rest_set(2)]

    result = parse_set_messages(mesgs)

    assert len(result) == 1
    assert len(result[0]["sets"]) == 1


def test_multiple_exercises_keep_performed_order() -> None:
    mesgs = [
        _active_set(0, reps=5, weight=120.0, category="deadlift", subtype=None),
        _rest_set(1),
        _active_set(2, reps=8, weight=80.0, category="bench_press", subtype=None),
        _active_set(3, reps=8, weight=80.0, category="bench_press", subtype=None),
        _active_set(4, reps=5, weight=100.0, category="squat", subtype=None),
    ]

    result = parse_set_messages(mesgs)

    assert [e["exercise"] for e in result] == ["marklyft", "bänkpress", "knäböj"]
    assert len(result[1]["sets"]) == 2


def test_sets_are_ordered_by_message_index_not_input_order() -> None:
    """message_index är den pålitliga ordningen i filen; meddelandena
    behöver inte komma sorterade."""
    mesgs = [
        _active_set(4, reps=3, weight=140.0),
        _active_set(0, reps=5, weight=120.0),
        _active_set(2, reps=5, weight=130.0),
    ]

    sets = parse_set_messages(mesgs)[0]["sets"]

    assert [s["weight_kg"] for s in sets] == [120.0, 130.0, 140.0]


def test_bodyweight_exercise_keeps_reps_without_weight() -> None:
    """Chins har reps men ingen vikt — setet ska sparas ändå, inte som 0 kg."""
    mesg = _active_set(0, reps=6, weight=0.0, category="pull_up", subtype=None)
    del mesg["weight"]

    result = parse_set_messages([mesg])

    assert result[0]["exercise"] == "chins"
    assert result[0]["sets"] == [{"reps": 6}]


def test_implausible_weights_are_discarded() -> None:
    """FIT:s 'ogiltigt värde'-sentinels ska inte bli 65535 kg marklyft."""
    result = parse_set_messages([_active_set(0, reps=5, weight=65535.0)])
    assert result[0]["sets"] == [{"reps": 5}]


def test_pounds_are_converted_to_kilos() -> None:
    mesg = _active_set(0, reps=5, weight=100.0)
    mesg["weight_display_unit"] = "pound"

    result = parse_set_messages([mesg])

    assert result[0]["sets"][0]["weight_kg"] == 45.36


def test_unknown_category_falls_back_to_readable_name() -> None:
    """Okända kategorier ska sparas läsbart, inte tappas bort."""
    mesg = _active_set(0, reps=10, weight=20.0, category="battle_rope", subtype=None)
    result = parse_set_messages([mesg])
    assert result[0]["exercise"] == "battle rope"

    # Även en kategori vi inte har någon översättning för alls.
    mesg2 = _active_set(
        0, reps=10, weight=20.0, category="some_new_lift", subtype=None
    )
    assert parse_set_messages([mesg2])[0]["exercise"] == "some new lift"


def test_category_may_arrive_as_plain_string() -> None:
    """SDK:n ger listor, men fältet är inte garanterat en lista."""
    mesg = _active_set(0, reps=5, weight=120.0, subtype=None)
    mesg["category"] = "squat"
    assert parse_set_messages([mesg])[0]["exercise"] == "knäböj"


# --- Övningsvarianter (category_subtype) -----------------------------
#
# Alla kategori/subtyp-par nedan är avlästa ur användarens riktiga
# FIT-filer, inte påhittade.


def test_subtype_separates_deadlift_variants() -> None:
    """Buggen som motiverade hela mappningen: 135 kg konventionellt marklyft
    och 40 kg raka marklyft sparades båda som "marklyft", så progressionen
    såg ut att svänga mellan 40 och 150 kg mellan passen."""
    tungt = _active_set(0, reps=3, weight=135.0, category="deadlift", subtype=0)
    rakt = _active_set(1, reps=5, weight=40.0, category="deadlift", subtype=1)

    result = parse_set_messages([tungt, rakt])

    assert [e["exercise"] for e in result] == ["marklyft", "raka marklyft"]


def test_subtype_separates_squat_variants() -> None:
    """Knäböjen var värst: vanlig knäböj (188 set, 40-115 kg) och frontböj
    (88 set, 40-70 kg) låg i samma serie.

    Subtyp 9 är Garmins barbell_hack_squat — etiketten klockan sätter på
    atletens knäböj. Den översätts till "knäböj", inte till Garmins namn.
    """
    knabojen = _active_set(0, reps=5, weight=107.5, category="squat", subtype=9)
    front = _active_set(1, reps=5, weight=40.0, category="squat", subtype=8)

    result = parse_set_messages([knabojen, front])

    assert [e["exercise"] for e in result] == ["knäböj", "frontböj"]


def test_subtype_numbers_mean_different_things_per_category() -> None:
    """Subtypsenumen är egna per kategori — samma nummer är olika övningar.
    Därför får mappningen inte slås upp på numret ensamt."""
    deadlift = _active_set(0, reps=5, weight=40.0, category="deadlift", subtype=1)
    bench = _active_set(1, reps=5, weight=80.0, category="bench_press", subtype=1)

    result = parse_set_messages([deadlift, bench])

    assert [e["exercise"] for e in result] == ["raka marklyft", "bänkpress"]


def test_garmins_duplicate_enum_values_merge_into_one_exercise() -> None:
    """barbell_row (45) och bent_over_row_with_barbell (46) är samma lyft med
    två enum-värden. Delade de rad skulle 19 respektive 69 set bli två
    halva serier istället för en hel."""
    a = _active_set(0, reps=5, weight=60.0, category="row", subtype=45)
    b = _active_set(1, reps=5, weight=70.0, category="row", subtype=46)

    result = parse_set_messages([a, b])

    assert [e["exercise"] for e in result] == ["skivstångsrodd"]
    assert len(result[0]["sets"]) == 2


def test_missing_subtype_falls_back_to_the_category_name() -> None:
    """13 av användarens marklyftsset (90-130 kg) har ingen subtyp alls.
    De ska bli "marklyft", inte tappas eller få ett påhittat variantnamn."""
    mesg = _active_set(0, reps=5, weight=130.0, category="deadlift", subtype=None)
    assert parse_set_messages([mesg])[0]["exercise"] == "marklyft"


def test_untranslated_variant_keeps_its_fit_name_instead_of_the_category() -> None:
    """En övning vi inte översatt ska ändå hållas isär från kategorin.
    squat/24 finns i FIT:s enum men inte i vår tabell — bättre ett läsbart
    engelskt namn än att blanda in det i "knäböj"."""
    mesg = _active_set(0, reps=5, weight=60.0, category="squat", subtype=24)
    name = parse_set_messages([mesg])[0]["exercise"]

    assert name != "knäböj"
    assert "_" not in name


def test_subtype_may_arrive_as_a_plain_scalar() -> None:
    """Samma sak som för category: fältet är inte garanterat en lista."""
    mesg = _active_set(0, reps=5, weight=40.0, category="deadlift")
    mesg["category_subtype"] = 1

    assert parse_set_messages([mesg])[0]["exercise"] == "raka marklyft"


def test_every_translated_variant_exists_in_the_fit_profile() -> None:
    """Skyddar mot stavfel i tabellen.

    Nycklarna är FIT-namn och slås upp mot SDK:ns profil; en felstavad
    nyckel skulle aldrig matcha någonting och tyst göra att övningen
    fortsätter kollapsa in i sin kategori — precis den bugg tabellen
    finns för att åtgärda.
    """
    from garmin_fit_sdk import Profile

    from sync.fit_strength import _EXERCISE_VARIANT_NAMES_SV

    known = {
        str(value)
        for name, enum in Profile["types"].items()
        if name.endswith("_exercise_name")
        for value in enum.values()
    }
    okända = sorted(set(_EXERCISE_VARIANT_NAMES_SV) - known)
    assert not okända, f"finns inte i FIT-profilen: {okända}"


def test_parse_strength_sets_ignores_non_fit_content() -> None:
    """Ett pass uppladdat som TCX/GPX ska ge en tom lista, inte krascha."""
    assert parse_strength_sets(b"<?xml version='1.0'?><TrainingCenterDatabase/>") == []
    assert parse_strength_sets(b"") == []


# --- Importflödet (nedladdning -> databas) ---------------------------


class _FakeClient:
    """Fejkad IntervalsClient som returnerar en förberedd FIT-fil."""

    def __init__(self, exercises: list | None = None) -> None:
        self.exercises = exercises
        self.download_count = 0

    def download_original_file(self, activity_id: str) -> bytes:
        self.download_count += 1
        return b"fake-fit-bytes"


def _patch_parser(monkeypatch, exercises: list) -> None:
    monkeypatch.setattr(
        "sync.intervals_client.parse_strength_sets", lambda _content: exercises
    )


def _strength_activity(activity_id: str = "gym1", day: str = "2026-08-20") -> dict:
    return {
        "id": activity_id, "name": "Styrka", "type": "WeightTraining",
        "sport": "WeightTraining", "start_time": f"{day}T17:00:00+00:00",
        "duration_seconds": 3600, "distance_meters": None,
        "average_heart_rate": 110, "max_heart_rate": 140, "average_watts": None,
        "normalized_watts": None, "average_cadence": None, "average_speed": None,
        "tss": 30.0, "intensity": None, "raw_json": "{}",
        "last_synced": "2026-08-20T18:00:00+00:00",
    }


def test_import_saves_sets_dated_to_the_activity(tmp_path, monkeypatch) -> None:
    from sync.intervals_client import import_strength_sets
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()
    _patch_parser(monkeypatch, [
        {"exercise": "marklyft", "sets": [{"reps": 5, "weight_kg": 120.0}] * 3},
    ])

    count = import_strength_sets(_FakeClient(), store, _strength_activity())

    assert count == 3
    rows = store.get_strength_sets_for_activity("gym1")
    assert len(rows) == 3
    # Dagen tas från passets starttid, inte från set-meddelandets UTC-tid,
    # så ett kvällspass inte hamnar på fel datum.
    assert {r["day"] for r in rows} == {"2026-08-20"}
    assert {r["source"] for r in rows} == {"fit"}
    assert [r["set_number"] for r in rows] == [1, 2, 3]


def test_import_is_skipped_when_already_imported(tmp_path, monkeypatch) -> None:
    """Synken kör varje timme. Utan den här spärren laddas samma FIT-fil
    ner om och om igen för varje pass som redan är importerat."""
    from sync.intervals_client import import_strength_sets
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()
    _patch_parser(monkeypatch, [
        {"exercise": "marklyft", "sets": [{"reps": 5, "weight_kg": 120.0}]},
    ])
    client = _FakeClient()

    import_strength_sets(client, store, _strength_activity())
    import_strength_sets(client, store, _strength_activity())

    assert client.download_count == 1
    assert len(store.get_strength_sets_for_activity("gym1")) == 1


def test_forced_reimport_replaces_instead_of_duplicating(tmp_path, monkeypatch) -> None:
    from sync.intervals_client import import_strength_sets
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()
    _patch_parser(monkeypatch, [
        {"exercise": "marklyft", "sets": [{"reps": 5, "weight_kg": 120.0}] * 3},
    ])
    client = _FakeClient()

    import_strength_sets(client, store, _strength_activity())
    import_strength_sets(client, store, _strength_activity(), force=True)

    assert client.download_count == 2
    assert len(store.get_strength_sets_for_activity("gym1")) == 3


def test_reimport_never_touches_manually_logged_sets(tmp_path, monkeypatch) -> None:
    """Chattloggade set går inte att återskapa från någon fil — en
    om-import får aldrig radera dem."""
    from sync.intervals_client import import_strength_sets
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()
    store.add_strength_sets(
        day="2026-08-20", exercise="hantelcurl", activity_id="gym1",
        sets=[{"reps": 10, "weight_kg": 15.0}], source="chat",
    )
    _patch_parser(monkeypatch, [
        {"exercise": "marklyft", "sets": [{"reps": 5, "weight_kg": 120.0}]},
    ])

    import_strength_sets(_FakeClient(), store, _strength_activity(), force=True)

    rows = store.get_strength_sets_for_activity("gym1")
    by_source = {r["source"]: r["exercise"] for r in rows}
    assert by_source == {"chat": "hantelcurl", "fit": "marklyft"}


def test_import_handles_file_without_set_data(tmp_path, monkeypatch) -> None:
    from sync.intervals_client import import_strength_sets
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()
    _patch_parser(monkeypatch, [])

    assert import_strength_sets(_FakeClient(), store, _strength_activity()) == 0
    assert store.get_strength_sets_for_activity("gym1") == []


def test_sync_continues_when_strength_import_fails(tmp_path, monkeypatch) -> None:
    """Ett Strava-importerat pass (där originalfilen inte stöds) eller ett
    nätverksfel ska inte fälla hela synken — passet i sig är redan sparat."""
    from sync import intervals_client
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()

    class _Client:
        def list_activities(self, oldest=None, newest=None):
            return [{"id": "gym1", "type": "WeightTraining", "sport": "WeightTraining",
                     "start_date_local": "2026-08-20T17:00:00"}]

        def get_activity(self, activity_id):
            return {}

        def download_original_file(self, activity_id):
            raise RuntimeError("404 Not Found (Strava-aktivitet)")

    count = intervals_client.sync_activities(_Client(), store)

    assert count == 1
    assert store.get_activity("gym1") is not None
    assert store.get_strength_sets_for_activity("gym1") == []


def test_sync_skips_file_download_for_non_strength_activities(tmp_path) -> None:
    """Ett löppass ska inte trigga en nedladdning av originalfilen."""
    from sync import intervals_client
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()
    downloads = []

    class _Client:
        def list_activities(self, oldest=None, newest=None):
            return [{"id": "run1", "type": "Run", "sport": "Run",
                     "start_date_local": "2026-08-20T07:00:00"}]

        def get_activity(self, activity_id):
            return {}

        def download_original_file(self, activity_id):
            downloads.append(activity_id)
            return b""

    intervals_client.sync_activities(_Client(), store)

    assert downloads == []


def test_a_pass_logged_in_chat_before_the_sync_is_not_counted_twice(
    tmp_path, monkeypatch
) -> None:
    """Den vanliga ordningen: du loggar passet på gymmet, den timvisa
    synken hämtar klockans fil en timme senare.

    Spärren fanns bara åt andra hållet — chatten vägrar logga en övning som
    redan importerats — så det här hållet gav två exemplar av samma arbete:
    6 set och 3 600 kg volym där 3 set och 1 800 kg utfördes, på
    dashboarden, på passidan och i varje payload till Claude. Dubbletterna
    överlevde dessutom varje omsynk, eftersom om-importen bara rensar sina
    egna rader.
    """
    from sync.intervals_client import import_strength_sets
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()
    store.add_strength_sets(
        day="2026-08-20", exercise="marklyft", activity_id="gym1",
        sets=[{"reps": 5, "weight_kg": 120.0}] * 3, source="chat",
    )
    _patch_parser(monkeypatch, [
        {"exercise": "marklyft", "sets": [{"reps": 5, "weight_kg": 120.0}] * 3},
    ])

    import_strength_sets(_FakeClient(), store, _strength_activity())

    rows = store.get_strength_sets_for_date("2026-08-20")
    assert len(rows) == 3, "klockans set ska ersätta chattens, inte läggas till"
    assert {r["source"] for r in rows} == {"fit"}
    # Numren ska börja om på 1 — annars ser passidan ut att sakna set 1-3.
    assert [r["set_number"] for r in rows] == [1, 2, 3]


def test_the_import_only_clears_the_exercises_the_file_contains(
    tmp_path, monkeypatch
) -> None:
    """Klockan registrerar inte allt. Ett set du kör efter att du stoppat
    passet, eller en övning du gjorde utan att trycka igång, finns bara i
    chatten — och går inte att återskapa från någon fil."""
    from sync.intervals_client import import_strength_sets
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()
    for ovning in ("marklyft", "hantelcurl"):
        store.add_strength_sets(
            day="2026-08-20", exercise=ovning, activity_id="gym1",
            sets=[{"reps": 8, "weight_kg": 40.0}], source="chat",
        )
    _patch_parser(monkeypatch, [
        {"exercise": "marklyft", "sets": [{"reps": 5, "weight_kg": 120.0}]},
    ])

    import_strength_sets(_FakeClient(), store, _strength_activity())

    kvar = {(r["exercise"], r["source"]) for r in store.get_strength_sets_for_date("2026-08-20")}
    assert kvar == {("marklyft", "fit"), ("hantelcurl", "chat")}


def test_chat_sets_without_a_linked_pass_are_also_replaced(
    tmp_path, monkeypatch
) -> None:
    """En dag med TVÅ gympass kopplar chatten inte setsen till något pass
    alls (activity_id blir NULL), eftersom det inte går att veta vilket de
    hörde till. Rensningen måste därför gå på dagen och övningen — går den
    på passet hittar den inte dubbletten."""
    from sync.intervals_client import import_strength_sets
    from sync.store import Store

    store = Store(tmp_path / "t.db")
    store.init()
    store.add_strength_sets(
        day="2026-08-20", exercise="marklyft", activity_id=None,
        sets=[{"reps": 5, "weight_kg": 120.0}] * 3, source="chat",
    )
    _patch_parser(monkeypatch, [
        {"exercise": "marklyft", "sets": [{"reps": 5, "weight_kg": 120.0}] * 3},
    ])

    import_strength_sets(_FakeClient(), store, _strength_activity())

    rows = store.get_strength_sets_for_date("2026-08-20")
    assert {r["source"] for r in rows} == {"fit"}
    assert len(rows) == 3


def test_the_import_clears_under_the_name_the_correction_gives(
    tmp_path, monkeypatch
) -> None:
    """Rättelselistan kan döpa om klockans övning. Chattraden ligger under
    det RÄTTADE namnet (den skrevs genom samma upplösning), så rensningen
    måste slå upp namnet i stället för att lita på det filen säger."""
    from sync.intervals_client import import_strength_sets
    from sync.store import Store

    store = Store(tmp_path / "t.db", ["2026-08-20:raka marklyft=marklyft"])
    store.init()
    store.add_strength_sets(
        day="2026-08-20", exercise="marklyft", activity_id="gym1",
        sets=[{"reps": 5, "weight_kg": 120.0}] * 3, source="chat",
    )
    _patch_parser(monkeypatch, [
        {"exercise": "raka marklyft", "sets": [{"reps": 5, "weight_kg": 120.0}] * 3},
    ])

    import_strength_sets(_FakeClient(), store, _strength_activity())

    rows = store.get_strength_sets_for_date("2026-08-20")
    assert {r["source"] for r in rows} == {"fit"}
    assert {r["exercise"] for r in rows} == {"marklyft"}
    assert len(rows) == 3
