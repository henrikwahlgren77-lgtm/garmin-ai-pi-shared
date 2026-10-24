"""HTTP-beteendet i IntervalsClient: återförsök och väntan."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest


def test_retry_after_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry-After följdes rakt av. "Retry-After: 3600" fick synken att sova
    en timme, med synklåset taget — och vid en manuell synk inne i
    webbens förfrågan."""
    import sync.intervals_client as ic

    sovit: list[float] = []
    monkeypatch.setattr(ic.time, "sleep", sovit.append)
    klient = ic.IntervalsClient(
        SimpleNamespace(intervals_athlete_id="i1", intervals_api_key="k")  # type: ignore[arg-type]
    )
    begaran = httpx.Request("GET", "https://intervals.icu/x")
    svar = iter([
        httpx.Response(429, headers={"Retry-After": "3600"}, request=begaran),
        httpx.Response(200, json=[], request=begaran),
    ])
    monkeypatch.setattr(klient._client, "request", lambda method, url, params=None: next(svar))

    assert klient.get("wellness") == []
    assert sovit == [ic.MAX_RETRY_AFTER_SECONDS]
    klient.close()


def test_a_short_retry_after_is_still_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    import sync.intervals_client as ic

    sovit: list[float] = []
    monkeypatch.setattr(ic.time, "sleep", sovit.append)
    klient = ic.IntervalsClient(
        SimpleNamespace(intervals_athlete_id="i1", intervals_api_key="k")  # type: ignore[arg-type]
    )
    begaran = httpx.Request("GET", "https://intervals.icu/x")
    svar = iter([
        httpx.Response(503, headers={"Retry-After": "7"}, request=begaran),
        httpx.Response(200, json=[], request=begaran),
    ])
    monkeypatch.setattr(klient._client, "request", lambda method, url, params=None: next(svar))

    assert klient.get("wellness") == []
    assert sovit == [7.0]
    klient.close()
