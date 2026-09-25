"""Tester för analysis/claude.py: _truncate_markdown och max_chars-backstoppet."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from analysis.claude import _truncate_markdown
from tests.conftest import make_settings


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeToolUseBlock:
    def __init__(self, name: str, tool_input: dict, block_id: str = "tool_1") -> None:
        self.type = "tool_use"
        self.name = name
        self.input = tool_input
        self.id = block_id


class _FakeResponse:
    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.content = [_FakeBlock(text)]
        self.stop_reason = stop_reason


class _FakeToolUseResponse:
    def __init__(self, blocks: list, stop_reason: str = "tool_use") -> None:
        self.content = blocks
        self.stop_reason = stop_reason


def test_analyze_applies_max_chars_backstop(monkeypatch) -> None:
    """End-to-end: ClaudeClient.analyze() ska klippa svaret till max_chars
    även om Anthropic-svaret (fejkat här) är längre — oavsett vad
    prompten instruerar modellen att göra."""
    from analysis.claude import ClaudeClient

    settings = make_settings(anthropic_api_key="fake")
    client = ClaudeClient(settings)

    long_text = "\n".join(f"Rad {i} med lite text om träning." for i in range(300))
    assert len(long_text) > 2000

    monkeypatch.setattr(
        client._client.messages, "create", lambda **kwargs: _FakeResponse(long_text)
    )

    result = client.analyze("system", "data", max_chars=2000)
    assert len(result) <= 2000
    assert "avkortat" in result


def test_analyze_without_max_chars_returns_full_text(monkeypatch) -> None:
    """Om max_chars inte anges (t.ex. analyze_activity) ska inget klippas —
    det är bara morgon-/kvälls-/coaching-analyserna som har en gräns."""
    from analysis.claude import ClaudeClient

    settings = make_settings(anthropic_api_key="fake")
    client = ClaudeClient(settings)

    long_text = "\n".join(f"Rad {i} med lite text om träning." for i in range(300))
    monkeypatch.setattr(
        client._client.messages, "create", lambda **kwargs: _FakeResponse(long_text)
    )

    result = client.analyze("system", "data")
    assert result == long_text


def test_analyze_raises_on_empty_response_instead_of_returning_empty_string(
    monkeypatch,
) -> None:
    """Regressionstest: en riktig körning fick stop_reason='max_tokens' med
    0 tecken text (hela budgeten tog slut innan modellen hann skriva något
    synligt). analyze() returnerade tidigare tyst en tom sträng, vilket
    pipeline.py sparade som en 'lyckad' analys — det gjorde att 'redan
    genererat idag'-spärren blockerade nästa schemalagda försök samma
    timme, trots att inget användbart faktiskt genererats. Ett tomt svar
    ska nu ge ett fel istället, så anroparen (och systemd/journalctl)
    faktiskt ser att något gick fel."""
    import pytest

    from analysis.claude import ClaudeClient

    client = ClaudeClient(_make_settings())

    monkeypatch.setattr(
        client._client.messages,
        "create",
        lambda **kwargs: _FakeResponse("", stop_reason="max_tokens"),
    )

    with pytest.raises(RuntimeError, match="tomt svar"):
        client.analyze("system", "data", max_tokens=1024)


def test_chat_raises_on_empty_response(monkeypatch) -> None:
    """Samma skydd som analyze(), men för chat() (utan tool use)."""
    import pytest

    from analysis.claude import ClaudeClient

    client = ClaudeClient(_make_settings())

    monkeypatch.setattr(
        client._client.messages,
        "create",
        lambda **kwargs: _FakeResponse("", stop_reason="max_tokens"),
    )

    with pytest.raises(RuntimeError, match="tomt svar"):
        client.chat([{"role": "user", "content": "Hej"}], "system")


def test_truncate_markdown_leaves_short_text_untouched() -> None:
    text = "# Kort analys\nAllt är bra."
    assert _truncate_markdown(text, max_chars=2000) == text


def test_truncate_markdown_cuts_long_text_and_marks_it() -> None:
    # En lång text med tydliga radbrytningar, likt de riktiga analyserna.
    lines = [f"Rad {i}: lite text om träning och återhämtning." for i in range(200)]
    text = "\n".join(lines)
    assert len(text) > 2000

    result = _truncate_markdown(text, max_chars=2000)

    assert len(result) <= 2000
    assert "avkortat" in result, "Ska tydligt markera att texten är avkortad"
    # Ska inte klippa mitt i en rad — resultatet minus markeringen ska
    # sluta med en hel rad ur originaltexten.
    body = result.split("\n\n*(avkortat")[0]
    assert body in text


def test_truncate_markdown_never_exceeds_limit_even_with_no_good_break_point() -> None:
    # En enda extremt lång "rad" utan mellanslag eller radbrytningar.
    text = "x" * 5000
    result = _truncate_markdown(text, max_chars=2000)
    assert len(result) <= 2000


def _make_settings():

    return make_settings(anthropic_api_key="fake")


def test_chat_applies_max_chars_backstop(monkeypatch) -> None:
    """Regressionstest: chattens svar klipptes tidigare rått av API:et mitt
    i en mening (bara max_tokens, ingen max_chars-gräns fanns). chat()
    ska nu, precis som analyze(), applicera samma städade
    trunkerings-backstop när max_chars anges."""
    from analysis.claude import ClaudeClient

    client = ClaudeClient(_make_settings())

    long_text = "\n".join(f"Rad {i} med lite text om träning." for i in range(300))
    assert len(long_text) > 2500

    monkeypatch.setattr(
        client._client.messages, "create", lambda **kwargs: _FakeResponse(long_text)
    )

    result = client.chat([{"role": "user", "content": "Hej"}], "system", max_chars=2500)
    assert len(result) <= 2500
    assert "avkortat" in result


def test_chat_without_max_chars_returns_full_text(monkeypatch) -> None:
    from analysis.claude import ClaudeClient

    client = ClaudeClient(_make_settings())

    long_text = "\n".join(f"Rad {i} med lite text om träning." for i in range(300))
    monkeypatch.setattr(
        client._client.messages, "create", lambda **kwargs: _FakeResponse(long_text)
    )

    result = client.chat([{"role": "user", "content": "Hej"}], "system")
    assert result == long_text


def test_chat_executes_tool_use_and_returns_final_text(monkeypatch) -> None:
    """Regressionstest för tool-use-loopen: Claude begär ett verktygsanrop
    (log_self_report), ClaudeClient ska köra tool_executor, skicka
    resultatet tillbaka som tool_result, och returnera Claudes SLUTGILTIGA
    textsvar (inte tool-anropet självt)."""
    from analysis.claude import ClaudeClient

    client = ClaudeClient(_make_settings())

    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            # Första anropet: Claude vill använda verktyget.
            return _FakeToolUseResponse(
                [_FakeToolUseBlock("log_self_report", {"category": "weight", "value": 91.2})]
            )
        # Andra anropet (efter tool_result skickats tillbaka): slutgiltigt svar.
        return _FakeResponse("Noterat, 91.2 kg sparat!")

    monkeypatch.setattr(client._client.messages, "create", fake_create)

    executed: list[tuple[str, dict]] = []

    def fake_executor(name: str, tool_input: dict) -> str:
        executed.append((name, tool_input))
        return "Sparat: weight (91.2) för 2026-08-20."

    result = client.chat(
        [{"role": "user", "content": "Vägde mig, 91.2"}],
        "system",
        tools=[{"name": "log_self_report", "input_schema": {}}],
        tool_executor=fake_executor,
    )

    assert result == "Noterat, 91.2 kg sparat!"
    assert executed == [("log_self_report", {"category": "weight", "value": 91.2})]
    assert len(calls) == 2, "Ska göra exakt två API-anrop: ett som utlöser tool_use, ett efteråt"

    # Andra anropets messages ska innehålla tool_result kopplat till
    # rätt tool_use_id.
    second_call_messages = calls[1]["messages"]
    tool_result_turn = second_call_messages[-1]
    assert tool_result_turn["role"] == "user"
    assert tool_result_turn["content"][0]["tool_use_id"] == "tool_1"
    assert "Sparat" in tool_result_turn["content"][0]["content"]


def test_chat_without_tools_ignores_tool_use_stop_reason(monkeypatch) -> None:
    """Om inget tool_executor skickas in ska chat() inte försöka köra
    verktyg även om Claude (osannolikt utan tools i anropet) ändå skulle
    returnera stop_reason='tool_use' — bara returnera texten som finns."""
    from analysis.claude import ClaudeClient

    client = ClaudeClient(_make_settings())
    monkeypatch.setattr(
        client._client.messages,
        "create",
        lambda **kwargs: _FakeResponse("Vanligt svar", stop_reason="tool_use"),
    )

    result = client.chat([{"role": "user", "content": "Hej"}], "system")
    assert result == "Vanligt svar"


def test_chat_tool_executor_exception_sends_error_tool_result(monkeypatch) -> None:
    """Om tool_executor kastar ett fel ska det inte krascha hela chatten —
    felet skickas tillbaka till Claude som ett is_error-tool_result, så
    Claude kan förklara/be om ursäkt istället för att hela anropet dör."""
    from analysis.claude import ClaudeClient

    client = ClaudeClient(_make_settings())
    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _FakeToolUseResponse(
                [_FakeToolUseBlock("log_self_report", {"category": "weight"})]
            )
        return _FakeResponse("Något gick fel när jag skulle spara det.")

    monkeypatch.setattr(client._client.messages, "create", fake_create)

    def failing_executor(name: str, tool_input: dict) -> str:
        raise RuntimeError("databasen är låst")

    result = client.chat(
        [{"role": "user", "content": "Vägde mig"}],
        "system",
        tools=[{"name": "log_self_report", "input_schema": {}}],
        tool_executor=failing_executor,
    )

    assert result == "Något gick fel när jag skulle spara det."
    tool_result_turn = calls[1]["messages"][-1]
    assert tool_result_turn["content"][0]["is_error"] is True


def test_a_broken_tool_does_not_send_the_exception_text_to_claude(monkeypatch) -> None:
    """Undantagets text ska stanna i loggen, inte gå in i modellens kontext.

    Samma skäl som _server_error i web/app.py: texten är skriven för den
    som felsöker och bär med sig det som fanns i felet — ett sqlite3-fel
    innehåller SQL:en som misslyckades, ett httpx-fel hela URL:en med
    query. Härifrån har den dessutom en väg vidare ut i chattsvaret,
    eftersom Claude läser den och skriver sitt svar utifrån den.
    """
    from analysis.claude import ClaudeClient

    client = ClaudeClient(_make_settings())
    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return _FakeToolUseResponse(
                [_FakeToolUseBlock("log_strength_session", {"exercise": "marklyft"})]
            )
        return _FakeResponse("Det gick inte att spara.")

    monkeypatch.setattr(client._client.messages, "create", fake_create)

    hemlighet = (
        'no such column: hemlig_kolumn [SQL: SELECT * FROM strength_sets '
        'WHERE token=\'abc123\']'
    )

    def failing_executor(name: str, tool_input: dict) -> str:
        raise RuntimeError(hemlighet)

    client.chat(
        [{"role": "user", "content": "Marklyft 3x5"}],
        "system",
        tools=[{"name": "log_strength_session", "input_schema": {}}],
        tool_executor=failing_executor,
    )

    skickat = json.dumps(calls[1]["messages"], default=str)
    assert "hemlig_kolumn" not in skickat
    assert "abc123" not in skickat
    assert "SELECT" not in skickat

    innehall = calls[1]["messages"][-1]["content"][0]["content"]
    assert "logg" in innehall, (
        "Claude ska ändå kunna säga ATT det inte gick, och var felet finns"
    )


# --- Svar som API:et kapat mitt i en mening --------------------------


class _StopReasonResponse:
    """Svar där modellen slog i max_tokens innan den var klar."""

    def __init__(self, text: str, stop_reason: str = "max_tokens") -> None:
        block = type("B", (), {"type": "text", "text": text})()
        self.content = [block]
        self.stop_reason = stop_reason


def test_response_cut_by_max_tokens_is_marked_as_incomplete(monkeypatch) -> None:
    """Rapporterad bugg: analysen slutade mitt i en mening ("...träna
    inte") utan att något antydde att text saknades.

    max_chars slår bara till när svaret är för LÅNGT. Ett svar som API:et
    kapat är per definition kortare än gränsen, så backstoppen kördes
    aldrig och den råa avhuggningen visades rakt av."""
    from analysis.claude import ClaudeClient

    client = ClaudeClient(_make_settings())
    cut_off = (
        "## ⚠️ Risker\n\n"
        "* HRV-dropp till 27 + sömnscore 54 idag = tydlig recovery-varning, träna inte"
    )
    monkeypatch.setattr(
        client._client.messages, "create", lambda **kw: _StopReasonResponse(cut_off)
    )

    result = client.analyze("system", "data", max_tokens=100, max_chars=5000)

    assert "avkortat" in result, "det ska synas att svaret är ofullständigt"
    # Den halva meningen ska bort, inte stå kvar som om den vore komplett.
    assert "träna inte" not in result
    assert "⚠️ Risker" in result, "det som hann skrivas ska finnas kvar"


def test_complete_response_is_left_alone(monkeypatch) -> None:
    """Ett svar som fick skriva färdigt ska inte märkas som avkortat."""
    from analysis.claude import ClaudeClient

    client = ClaudeClient(_make_settings())
    monkeypatch.setattr(
        client._client.messages,
        "create",
        lambda **kw: _StopReasonResponse("# Klart\n\nAllt hann skrivas.", "end_turn"),
    )

    result = client.analyze("system", "data", max_tokens=100, max_chars=5000)

    assert result == "# Klart\n\nAllt hann skrivas."
    assert "avkortat" not in result


def test_too_long_response_still_uses_the_length_marker(monkeypatch) -> None:
    """De två fallen ska gå att skilja åt i efterhand: för långt svar
    respektive slut på svarsutrymme."""
    from analysis.claude import ClaudeClient

    client = ClaudeClient(_make_settings())
    long_text = "\n".join(f"Rad {i} om träning." for i in range(200))
    monkeypatch.setattr(
        client._client.messages,
        "create",
        lambda **kw: _StopReasonResponse(long_text, "end_turn"),
    )

    result = client.analyze("system", "data", max_tokens=5000, max_chars=500)

    assert len(result) <= 500
    assert "svaret var för långt" in result


def test_chat_marks_incomplete_replies_too(monkeypatch) -> None:
    from analysis.claude import ClaudeClient

    client = ClaudeClient(_make_settings())
    monkeypatch.setattr(
        client._client.messages,
        "create",
        lambda **kw: _StopReasonResponse("Kör ett lugnt pass idag, men undvik att"),
    )

    reply = client.chat([{"role": "user", "content": "Hej"}], "system", max_chars=5000)

    assert "avkortat" in reply


# --- Tänkandebudgeten är styrd, inte lämnad åt modellen (fynd 7) ------


def _settings_for_effort(tmp_path):  # noqa: ANN001, ANN202

    return make_settings(db_path=tmp_path / "t.db")


def test_analysis_calls_set_an_explicit_thinking_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sonnet 5 kör ADAPTIVT tänkande när `thinking` utelämnas — det är
    modellens standardläge, inte något appen valt. Tänkandet dras från
    samma max_tokens som den synliga texten och syns inte i svaret, så
    budgeten var både osynlig och ostyrd: uppmätt 1405 tänkandetokens mot
    1211 texttokens på morgonprompten, alltså 54 % av svaret. Det tvingade
    upp MAX_ANALYSIS_TOKENS från 3500 till 8000 och kapade ändå fyra
    analyser på en vecka."""
    from analysis.claude import THINKING_EFFORT, ClaudeClient

    seen: dict = {}

    class _Resp:
        content = [type("B", (), {"type": "text", "text": "# Rubrik\n\nText."})()]
        stop_reason = "end_turn"
        usage = None

    def _create(**kwargs):  # noqa: ANN003, ANN202
        seen.update(kwargs)
        return _Resp()

    client = ClaudeClient(_settings_for_effort(tmp_path))
    monkeypatch.setattr(client._client.messages, "create", _create)
    client.analyze("system", "data", max_tokens=8000, max_chars=3500)

    assert seen.get("output_config") == {"effort": THINKING_EFFORT}, (
        "analysanropet sätter ingen effort — tänkandet körs på modellens "
        "standard och äter av svarsbudgeten okontrollerat"
    )
    assert THINKING_EFFORT in ("low", "medium", "high", "xhigh", "max")


def test_chat_calls_set_the_same_thinking_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Chatten har samma osynliga tänkandebudget som analyserna."""
    from analysis.claude import THINKING_EFFORT, ClaudeClient

    seen: dict = {}

    class _Resp:
        content = [type("B", (), {"type": "text", "text": "Svar."})()]
        stop_reason = "end_turn"
        usage = None

    def _create(**kwargs):  # noqa: ANN003, ANN202
        seen.update(kwargs)
        return _Resp()

    client = ClaudeClient(_settings_for_effort(tmp_path))
    monkeypatch.setattr(client._client.messages, "create", _create)
    client.chat([{"role": "user", "content": "hej"}], "system", max_tokens=6000)

    assert seen.get("output_config") == {"effort": THINKING_EFFORT}


def test_token_usage_is_logged_so_the_effort_can_be_tuned_on_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Tänkandetokens fanns bara som en gissning i en kodkommentar. Utan en
    loggrad går det inte att se om THINKING_EFFORT är rätt satt, eller om
    ett kapat svar berodde på tänkandet eller på texten."""
    import logging

    from analysis.claude import ClaudeClient

    class _Resp:
        content = [type("B", (), {"type": "text", "text": "Text."})()]
        stop_reason = "end_turn"
        usage = type("U", (), {
            "output_tokens": 2616,
            "output_tokens_details": type("D", (), {"thinking_tokens": 1405})(),
        })()

    client = ClaudeClient(_settings_for_effort(tmp_path))
    monkeypatch.setattr(client._client.messages, "create", lambda **k: _Resp())

    with caplog.at_level(logging.INFO, logger="analysis.claude"):
        client.analyze("system", "data", max_tokens=8000)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "1405" in logged, "tänkandetokens loggas inte"
    assert "2616" in logged


def test_the_incomplete_marker_fits_inside_max_chars(monkeypatch) -> None:
    """max_chars var hårt överallt utom i den här grenen.

    _finish_incomplete lade sina 48 tecken markering OVANPÅ texten, och
    grenen körs just när svaret ryms under taket — alltså även när det
    ligger exakt på det. Uppmätt: ett svar på 3 500 tecken kom ut som
    3 547 med max_chars=3500.

    Det är samma avvägning som _truncate_markdown redan gjorde för sitt
    eget suffix; bara den här vägen hade glömts.
    """
    from analysis.claude import ClaudeClient

    client = ClaudeClient(_make_settings())
    # Precis på taket, kapad av modellens utrymme.
    exakt = ("ord " * 2000).strip()[:3500]
    monkeypatch.setattr(
        client._client.messages, "create", lambda **kw: _StopReasonResponse(exakt)
    )

    result = client.analyze("system", "data", max_tokens=100, max_chars=3500)

    assert len(result) <= 3500, f"{len(result)} tecken av 3500"
    assert "avkortat" in result, "markeringen ska fortfarande med"


def test_a_max_chars_too_small_for_the_marker_still_holds(monkeypatch) -> None:
    """Ryms inte ens markeringen är taket ändå ett tak.

    Samma utväg som _truncate_markdown: hellre ett rakt klipp utan
    markering än ett svar som är längre än anroparen bett om.
    """
    from analysis.claude import _OUT_OF_ROOM_SUFFIX, _finish_incomplete

    text = "x" * 200
    for tak in (len(_OUT_OF_ROOM_SUFFIX), len(_OUT_OF_ROOM_SUFFIX) - 1, 10, 1):
        assert len(_finish_incomplete(text, tak)) <= tak, tak


def test_the_chat_reply_respects_max_chars_when_it_is_cut_short(monkeypatch) -> None:
    """Chatten har samma gren som analyze och hade samma brist."""
    from analysis.claude import ClaudeClient

    client = ClaudeClient(_make_settings())
    exakt = ("ord " * 2000).strip()[:2500]
    monkeypatch.setattr(
        client._client.messages, "create", lambda **kw: _StopReasonResponse(exakt)
    )

    svar = client.chat(
        [{"role": "user", "content": "hej"}], "system",
        max_tokens=100, max_chars=2500,
    )

    assert len(svar) <= 2500, f"{len(svar)} tecken av 2500"
    assert "avkortat" in svar


def test_a_short_incomplete_reply_is_untouched_by_the_cap(monkeypatch) -> None:
    """Taket får inte klippa ett svar som har gott om plats kvar — det var
    ju därför markeringen behövdes från början."""
    from analysis.claude import ClaudeClient

    client = ClaudeClient(_make_settings())
    monkeypatch.setattr(
        client._client.messages,
        "create",
        lambda **kw: _StopReasonResponse("## Risker\n\nHRV-dropp, träna inte"),
    )

    result = client.analyze("system", "data", max_tokens=100, max_chars=5000)

    assert "## Risker" in result
    assert "träna inte" not in result, "den halva meningen ska fortfarande bort"
    assert "avkortat" in result
