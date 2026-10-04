"""Tester för jobbens utfall (alerts.py) och backupens integritetskontroll."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import alerts
from sync.store import Store
from tests.conftest import make_settings

_NU = datetime(2026, 10, 5, 14, 0)


@pytest.fixture
def notiser(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Fångar varje notis som skulle ha skickats till ntfy."""
    skickade: list[dict[str, Any]] = []

    class _Svar:
        def raise_for_status(self) -> None:
            pass

    def _post(url: str, json: dict[str, Any], timeout: float) -> _Svar:
        skickade.append({"url": url, **json})
        return _Svar()

    monkeypatch.setattr(alerts.httpx, "post", _post)
    return skickade


def _settings(tmp_path: Path, **overrides: Any) -> Any:
    db = tmp_path / "test.db"
    Store(db).init()
    values = {"db_path": db, "ntfy_url": "https://ntfy.example/hemligt-amne"}
    return make_settings(**{**values, **overrides})


def test_the_hourly_sync_alerts_once_per_incident_not_once_per_hour(
    tmp_path: Path, notiser: list[dict[str, Any]]
) -> None:
    settings = _settings(tmp_path)
    fel = ConnectionError("intervals.icu svarar inte")

    alerts.record(settings, "sync", fel)
    assert notiser == [], "ett enstaka synkfel rättar nästa timme till själv"

    alerts.record(settings, "sync", fel)
    assert [n["title"] for n in notiser] == ["Synken misslyckades"]

    for _ in range(5):
        alerts.record(settings, "sync", fel)
    assert len(notiser) == 1, "samma incident ska inte larma varje timme"

    alerts.record(settings, "sync", None)
    assert [n["title"] for n in notiser][-1] == "Synken fungerar igen"
    alerts.record(settings, "sync", None)
    assert len(notiser) == 2


def test_a_daily_job_alerts_on_its_first_failure(
    tmp_path: Path, notiser: list[dict[str, Any]]
) -> None:
    alerts.record(_settings(tmp_path), "backup", OSError("No space left on device"))
    assert [n["title"] for n in notiser] == ["Backupen misslyckades"]


def test_the_notification_names_the_error_type_but_never_its_message(
    tmp_path: Path, notiser: list[dict[str, Any]]
) -> None:
    """Felmeddelanden kan bära värden ur hälsodatan, och ntfy.sh är en
    tredje part. Bara typen går iväg; detaljerna finns i journalen."""
    alerts.record(_settings(tmp_path), "coaching", ValueError("vilopuls 48, HRV 61"))
    (notis,) = notiser
    assert "ValueError" in notis["message"]
    assert "48" not in notis["message"] and "HRV" not in str(notis)


def test_the_notification_is_json_to_the_server_root_with_the_topic(
    tmp_path: Path, notiser: list[dict[str, Any]]
) -> None:
    """Rubriken bär å/ä/ö, som inte får stå i ett HTTP-huvud. JSON till
    serverns rot är ntfys sätt att skicka den ändå."""
    alerts.record(_settings(tmp_path), "evening_summary", RuntimeError())
    (notis,) = notiser
    assert notis["url"] == "https://ntfy.example/"
    assert notis["topic"] == "hemligt-amne"
    assert notis["title"] == "Kvällssammanfattningen misslyckades"


def test_nothing_is_sent_without_ntfy_url(
    tmp_path: Path, notiser: list[dict[str, Any]]
) -> None:
    alerts.record(_settings(tmp_path, ntfy_url=""), "backup", OSError())
    assert notiser == []


def test_a_failing_ntfy_does_not_break_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _nere(*args: Any, **kwargs: Any) -> None:
        raise OSError("ntfy nere")

    monkeypatch.setattr(alerts.httpx, "post", _nere)
    alerts.record(_settings(tmp_path), "backup", OSError())  # kastar inte


def test_a_broken_database_still_sends_the_notification(
    tmp_path: Path, notiser: list[dict[str, Any]]
) -> None:
    """Är databasen själva felet går räkningen inte att spara. Då hellre en
    notis för mycket än ingen."""
    settings = make_settings(
        db_path=tmp_path / "finns-inte" / "x.db", ntfy_url="https://ntfy.example/amne"
    )
    alerts.record(settings, "sync", sqlite3.OperationalError("unable to open"))
    assert [n["title"] for n in notiser] == ["Synken misslyckades"]


def _status(store: Store, job: str, **state: Any) -> None:
    import json

    store.set_job_state(f"status:{job}", json.dumps(state))


def test_the_dashboard_is_quiet_when_everything_works_or_nothing_has_run(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "test.db")
    store.init()
    assert alerts.problems(store, _NU) == [], "en ny installation är inget fel"

    _status(store, "sync", ok=(_NU - timedelta(minutes=40)).isoformat(), failures=0)
    _status(store, "backup", ok=(_NU - timedelta(hours=10)).isoformat(), failures=0)
    _status(store, "coaching", ok=(_NU - timedelta(days=9)).isoformat(), failures=0)
    assert alerts.problems(store, _NU) == [], (
        "analyserna hoppar legitimt över sig själva; en gammal tidsstämpel är inget fel"
    )


def test_the_dashboard_names_failing_and_overdue_jobs(tmp_path: Path) -> None:
    store = Store(tmp_path / "test.db")
    store.init()
    _status(
        store, "morning_recommendation",
        ok=(_NU - timedelta(days=1)).replace(hour=9, minute=0).isoformat(),
        failed=_NU.replace(hour=11, minute=0).isoformat(),
        failures=1, error="APITimeoutError",
    )
    _status(store, "backup", ok=datetime(2026, 10, 2, 3, 30).isoformat(), failures=0)
    _status(store, "sync", ok=_NU.isoformat(), failed=_NU.isoformat(), failures=1)

    assert alerts.problems(store, _NU) == [
        "Morgonanalysen misslyckades kl 11:00. Lyckades senast i går kl 09:00.",
        "Backupen lyckades senast 2/10 kl 03:30.",
    ], "ett enstaka synkfel är ingen incident än"


def test_the_scheduled_jobs_report_their_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scheduler

    rapporter: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        alerts, "record",
        lambda settings, job, failure: rapporter.append(
            (job, type(failure).__name__ if failure else None)
        ),
    )

    class _Trasig:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(scheduler, "Store", _Trasig)
    settings = make_settings(db_path=tmp_path / "x.db", backup_dir=tmp_path / "b")
    assert scheduler.run_sync(settings) is False
    assert scheduler.run_backup(settings) is None
    scheduler.run_coaching(settings)
    assert ("sync", "OperationalError") in rapporter
    assert ("backup", "OperationalError") in rapporter


def test_a_corrupt_database_never_becomes_a_backup(tmp_path: Path) -> None:
    """SQLites backup-API kopierar sidorna troget, även trasiga — uppmätt:
    kopieringen lyckas och felet syns först när kopian läses. Utan
    kontrollen hade en trasig kopia fått sitt slutnamn och räknats av
    rotationen."""
    db = tmp_path / "training.db"
    store = Store(db)
    store.init()
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE fyll (x TEXT)")
        conn.execute("CREATE INDEX fyll_x ON fyll (x)")
        conn.executemany(
            "INSERT INTO fyll VALUES (?)", [(f"rad{i:05d}" * 4,) for i in range(3000)]
        )
    conn.close()
    size = db.stat().st_size
    with open(db, "r+b") as f:
        f.seek(size - 4096 * 3 + 100)
        f.write(b"\xff" * 64)

    dest = tmp_path / "backups" / "training-2026-10-05.db"
    with pytest.raises(sqlite3.DatabaseError):
        store.backup(dest)
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


def test_a_healthy_backup_passes_the_check(tmp_path: Path) -> None:
    store = Store(tmp_path / "training.db")
    store.init()
    dest = store.backup(tmp_path / "backups" / "training-2026-10-05.db")
    assert dest.is_file()
