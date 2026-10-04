"""CLI-kommandona i main.py som läser och skriver databasen."""
from __future__ import annotations

import contextlib
from datetime import date
from pathlib import Path

import pytest

from sync.store import Store


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "t.db")
    s.init()
    return s


def test_self_report_lists_a_row_with_text_in_the_value_column(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """Äldre rader kan ha text i värdekolumnen — "82 kg" gick att spara
    innan chattverktygen kontrollerade talen. `:g` på en sträng kastade,
    så kommandot som finns för att hitta och ta bort sådana rader
    kraschade på just dem."""
    import main

    idag = date.today().isoformat()
    store.add_self_report(day=idag, category="weight", value="82 kg")  # type: ignore[arg-type]
    store.add_self_report(day=idag, category="weight", value=81.5)

    assert main._self_report(store, None, False, 30) == 0
    ut = capsys.readouterr().out
    assert "82 kg" in ut
    assert "81.5" in ut


def test_import_strength_survives_an_activity_without_start_time(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """start_time får vara NULL, och get('start_time', '') ger None när
    nyckeln finns. None[:10] kraschade kommandot mitt i körningen — efter
    att passets set redan skrivits."""
    import main
    import sync.intervals_client as intervals_client

    store.upsert_activity({
        "id": "i1", "name": "Styrka", "type": "WeightTraining", "sport": "WeightTraining",
        "start_time": None, "duration_seconds": 1800, "distance_meters": None,
        "average_heart_rate": None, "max_heart_rate": None, "average_watts": None,
        "normalized_watts": None, "average_cadence": None, "average_speed": None,
        "tss": None, "intensity": None, "raw_json": "{}", "last_synced": None,
    })
    monkeypatch.setattr(
        intervals_client, "IntervalsClient",
        lambda settings: contextlib.nullcontext(object()),
    )
    monkeypatch.setattr(
        intervals_client, "import_strength_sets",
        lambda client, store, activity, force=False: 4,
    )

    assert main._import_strength(None, store, "i1", True) == 0  # type: ignore[arg-type]
    assert "i1 (): 4 set" in capsys.readouterr().out
