"""Wrapper runt Anthropic Messages API (Claude Sonnet 5)."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal, cast

from anthropic import Anthropic

from config import Settings

log = logging.getLogger(__name__)

# Skydd mot en oändlig verktygs-loop om Claude av någon anledning fortsätter
# anropa verktyg om och om igen istället för att ge ett slutgiltigt svar.
MAX_TOOL_ITERATIONS = 4

# Hur djupt modellen får tänka innan den svarar.
#
# Sonnet 5 kör ADAPTIVT tänkande när `thinking` utelämnas — det är alltså
# inte något appen har valt, utan modellens standardläge. Tänkandet dras
# från samma max_tokens som den synliga texten, och syns inte i svaret
# (_extract_text läser bara text-block), så budgeten var i praktiken både
# osynlig och ostyrd.
#
# Vad det kostade, uppmätt på den skarpa morgonprompten mot claude-sonnet-5:
#
#   thinking  1405 tokens  (54 % av svaret)
#   text      1211 tokens
#
# Det var det som tvingade upp MAX_ANALYSIS_TOKENS från 3500 till 8000, och
# som ändå kapade fyra analyser på en vecka när ett tyngre tänkandepass åt
# upp utrymmet för texten.
#
# "medium" i stället för standardens "high": analyserna tolkar en handfull
# mätvärden mot en baslinje, inte ett problem som kräver full utredning.
# Tänkandet behålls (det höjer rimligtvis kvaliteten på just den
# tolkningen) men blir ett medvetet val i stället för en gratisbiljett, och
# max_chars återfår rollen som det som faktiskt binder svarslängden.
#
# Ändra med mätning, inte på känsla: log-raden i analyze() skriver ut hur
# många tänkandetokens varje anrop faktiskt använde.
#
# Typad som Literal, inte str: SDK:ns output_config tar exakt de fem
# nivåerna, och mypy fångar då en felstavning här i stället för att
# API:et gör det med en 400 i produktion.
THINKING_EFFORT: Literal["low", "medium", "high", "xhigh", "max"] = "medium"


def _log_token_usage(label: str, resp: Any, max_tokens: int) -> None:
    """Loggar hur svarsbudgeten faktiskt gick åt.

    Tänkandetokens fanns tidigare bara som en gissning i en kodkommentar.
    Utan den här raden går det inte att se om THINKING_EFFORT ovan är rätt
    satt, eller om ett kapat svar berodde på tänkandet eller på texten.
    """
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    details = getattr(usage, "output_tokens_details", None)
    thinking = getattr(details, "thinking_tokens", None) if details else None
    log.info(
        "%s: %s output-tokens av %d (varav %s tänkande), stop_reason=%s.",
        label,
        getattr(usage, "output_tokens", "?"),
        max_tokens,
        thinking if thinking is not None else "okänt antal",
        getattr(resp, "stop_reason", "okänd"),
    )


def _extract_text(content: Any) -> str:
    """Extrahera text från Claude:s content-blocks (hanterar Anthropics breda union-typer)."""
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts).strip()


_TOO_LONG_SUFFIX = "\n\n*(avkortat — svaret var för långt)*"
_OUT_OF_ROOM_SUFFIX = "\n\n*(avkortat — modellens svarsutrymme tog slut)*"


def _cut_at_safe_boundary(text: str, budget: int) -> str:
    """Klipper till högst `budget` tecken vid en radbrytning eller ett
    mellanslag, aldrig mitt i ett ord eller en **fetstil**-markör."""
    cut = text[:budget]
    last_newline = cut.rfind("\n")
    if last_newline > budget * 0.5:
        return cut[:last_newline]
    last_space = cut.rfind(" ")
    if last_space > 0:
        return cut[:last_space]
    return cut


def _truncate_markdown(text: str, max_chars: int) -> str:
    """Klipper markdown-text till max_chars tecken vid en säker gräns.

    Prompterna ber Claude hålla svaren korta, men det är en instruktion —
    inget hårt tak. Den här funktionen är backstoppet som garanterar
    gränsen oavsett vad modellen faktiskt skriver, och lägger till en
    tydlig markering så det syns att texten är medvetet avkortad (inte
    bara "ser trasig ut").
    """
    if len(text) <= max_chars:
        return text

    budget = max_chars - len(_TOO_LONG_SUFFIX)
    if budget <= 0:
        return text[:max_chars]
    return _cut_at_safe_boundary(text, budget).rstrip() + _TOO_LONG_SUFFIX


def _finish_incomplete(text: str, max_chars: int | None = None) -> str:
    """Städar upp ett svar som API:et kapat mitt i en mening.

    Vid stop_reason='max_tokens' tar texten slut var som helst — mitt i
    ett ord, en punktlista eller en **fetstil**-markör. Det svaret är
    KORTARE än max_chars, så _truncate_markdown ovan slår aldrig till och
    texten visades tidigare precis som den kom: tvärt avbruten utan att
    något antydde att det saknades text. Nu klipps den tillbaka till
    senaste hela rad och markeras.

    `max_chars` måste skickas med av samma skäl som _truncate_markdown
    räknar bort sitt suffix. Markeringen är 48 tecken och lades tidigare
    till OVANPÅ en text som redan kunde vara exakt max_chars lång: ett
    svar på 3 500 tecken kom ut som 3 547, alltså 47 över det tak
    anroparen bett om. Taket var hårt överallt utom just här, i den gren
    som körs när modellens utrymme tagit slut.
    """
    budget = len(text)
    if max_chars is not None:
        budget = min(budget, max_chars - len(_OUT_OF_ROOM_SUFFIX))
        if budget <= 0:
            # Inte plats för ens markeringen. Samma utväg som
            # _truncate_markdown: hellre ett rakt klipp än ett brutet tak.
            return text[:max_chars]
    return _cut_at_safe_boundary(text, budget).rstrip() + _OUT_OF_ROOM_SUFFIX


class ClaudeClient:
    def __init__(self, settings: Settings) -> None:
        self.model = settings.anthropic_model
        self._client = Anthropic(api_key=settings.anthropic_api_key)

    def analyze(
        self,
        system_prompt: str,
        user_data: str,
        max_tokens: int = 2048,
        max_chars: int | None = None,
    ) -> str:
        """Skickar system-prompt + aggregerad användardata och returnerar Markdown-svar.

        `max_chars`, om satt, är en hård gräns (se _truncate_markdown) —
        oberoende av prompt-instruktioner som "håll svaret under 2000
        tecken", som Claude inte alltid följer exakt.
        """
        log.info("Anropar Claude (%s), ~%d tecken user-data.", self.model, len(user_data))
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_data}],
            # Se THINKING_EFFORT ovan: tänkandet är på som standard och
            # åt tidigare drygt halva svarsbudgeten utan att någon valt det.
            output_config={"effort": THINKING_EFFORT},
        )
        result = _extract_text(resp.content)
        stop_reason = getattr(resp, "stop_reason", "okänd")
        _log_token_usage("Analys", resp, max_tokens)
        log.info("Claude svarade med %d tecken.", len(result))
        if not result:
            log.warning(
                "Claude returnerade tomt svar. user-data: %d tecken, stop_reason: %s",
                len(user_data), stop_reason,
            )
            # Ett tomt svar ska INTE behandlas som lyckat — det har hänt att
            # stop_reason='max_tokens' ger 0 tecken text (hela budgeten tog
            # slut innan något synligt hann skrivas). Anroparen (t.ex.
            # AnalysisPipeline.morning_recommendation) sparar annars en tom
            # analys och rapporterar det som lyckat, vilket dels döljer
            # felet, dels blockerar "redan genererat idag"-spärren från att
            # låta nästa schemalagda försök (nästa heltimme) faktiskt köra.
            raise RuntimeError(
                f"Claude gav ett tomt svar (stop_reason={stop_reason}, "
                f"max_tokens={max_tokens}). Troligen för lågt max_tokens "
                "för det här anropet."
            )
        if max_chars is not None and len(result) > max_chars:
            log.info(
                "Svaret var %d tecken, klipper till max %d (backstop).",
                len(result), max_chars,
            )
            result = _truncate_markdown(result, max_chars)
        elif stop_reason == "max_tokens":
            # Svaret ryms under max_chars men API:et hann ta slut på
            # tokens ändå, så texten slutar mitt i en mening. Anroparen
            # bör höja max_tokens; under tiden ska det åtminstone synas
            # att svaret är ofullständigt.
            log.warning(
                "Claude kapades av max_tokens=%d efter %d tecken — höj taket.",
                max_tokens, len(result),
            )
            result = _finish_incomplete(result, max_chars)
        return result

    def chat(
        self,
        history: list[dict[str, Any]],
        system_prompt: str,
        max_tokens: int = 1024,
        max_chars: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_executor: Callable[[str, dict[str, Any]], str] | None = None,
    ) -> str:
        """Chatt med atleten; behåller konversationshistorik.

        `max_chars`, om satt, applicerar samma trunkerings-backstop som
        analyze() (se _truncate_markdown) — annars klipps svaret bara rått
        av API:et mitt i en mening när max_tokens nås, utan varken en
        tydlig markering eller ett städat stopp.

        `tools`/`tool_executor`: om båda anges kan Claude anropa verktyg
        (t.ex. för att logga självrapporterad data). `tool_executor` körs
        synkront för varje tool_use-block Claude begär (namn + input-dict
        in, textsträng ut som skickas tillbaka som tool_result), i en loop
        tills Claude ger ett slutgiltigt textsvar eller MAX_TOOL_ITERATIONS
        nås. ClaudeClient känner medvetet inte till Store eller databasen
        — anroparen (AnalysisPipeline) äger den faktiska verktygslogiken,
        det här är bara mekaniken för att köra Anthropics tool-use-loop.
        """
        messages = list(history)

        for _ in range(MAX_TOOL_ITERATIONS):
            kwargs: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": cast("list[Any]", messages),
                # Samma skäl som i analyze() — se THINKING_EFFORT.
                "output_config": {"effort": THINKING_EFFORT},
            }
            if tools:
                kwargs["tools"] = tools

            resp = self._client.messages.create(**kwargs)
            _log_token_usage("Chatt", resp, max_tokens)

            if resp.stop_reason != "tool_use" or not tool_executor:
                result = _extract_text(resp.content)
                if not result:
                    # Samma resonemang som i analyze(): ett tomt svar (t.ex.
                    # stop_reason='max_tokens' utan synlig text) ska inte
                    # tyst returneras som ett giltigt, tomt chattsvar.
                    raise RuntimeError(
                        f"Claude gav ett tomt svar (stop_reason={resp.stop_reason}, "
                        f"max_tokens={max_tokens})."
                    )
                if max_chars is not None and len(result) > max_chars:
                    result = _truncate_markdown(result, max_chars)
                elif resp.stop_reason == "max_tokens":
                    # Se analyze(): svaret ryms under max_chars men slutar
                    # ändå mitt i en mening.
                    log.warning(
                        "Chattsvaret kapades av max_tokens=%d efter %d tecken.",
                        max_tokens, len(result),
                    )
                    result = _finish_incomplete(result, max_chars)
                return result

            # Claude vill anropa ett eller flera verktyg. Kör dem och
            # skicka tillbaka resultaten som en ny user-turn, sedan loopa
            # för att ge Claude chansen att svara (eller anropa fler
            # verktyg) med resultaten som kontext.
            messages.append({"role": "assistant", "content": resp.content})

            tool_results = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                try:
                    output = tool_executor(block.name, block.input)
                except Exception:
                    # Undantagets text stannar i loggen, den går inte in i
                    # modellens kontext. Samma skäl som _server_error i
                    # web/app.py: texten är skriven för den som felsöker
                    # och bär med sig det som fanns i felet — ett
                    # sqlite3-fel innehåller SQL:en som misslyckades, ett
                    # httpx-fel hela URL:en med query. Härifrån hade den
                    # dessutom en väg vidare ut i chattsvaret, eftersom
                    # Claude läser den och skriver sitt svar utifrån den.
                    #
                    # Verktygen som FINNS rapporterar sina egna,
                    # begripliga fel som vanliga returvärden ("Fel:
                    # 'exercise' saknas ..." i pipeline.py). Det som når
                    # hit är alltså inget atleten kan rätta till — bara
                    # något som gick sönder.
                    log.exception("Verktygsanrop %s misslyckades.", block.name)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": (
                            "Verktyget gick inte att köra. Säg till atleten att "
                            "det inte gick att spara och att felet finns i "
                            "tjänstens logg — påstå inte att något sparades, och "
                            "hitta inte på vad som gick fel."
                        ),
                        "is_error": True,
                    })
                    continue
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                })
            messages.append({"role": "user", "content": tool_results})

        log.warning("Nådde MAX_TOOL_ITERATIONS (%d) utan slutgiltigt svar.", MAX_TOOL_ITERATIONS)
        return "Jag fastnade i för många verktygsanrop — kan du omformulera frågan?"
