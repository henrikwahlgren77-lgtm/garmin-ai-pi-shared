"""CLI: kör sync och analyser manuellt, eller starta webbservern.

Användning:
    python -m src.main sync
    python -m src.main analyze-evening
    python -m src.main analyze-coaching
    python -m src.main analyze-activity <activity_id>
    python -m src.main serve
"""
from __future__ import annotations

import argparse
import ipaddress
import logging
import sys
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    # Bara för typkontroll: config importeras annars sent i main() så att
    # --help inte kräver en ifylld .env.
    from config import Settings
    from sync.store import Store


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# Query-parametrar vars värde aldrig ska hamna i en loggrad.
_HEMLIGA_PARAMETRAR = ("token",)
_REDIGERAT = "***"


class _RedactQuerySecrets(logging.Filter):
    """Maskar ?token=... i uvicorns access-loggrader.

    WEB_ACCESS_TOKEN får följa med som query-parameter — det är så första
    klivet in i appen fungerar innan kakan finns (se _TOKEN_COOKIE i
    web/app.py). uvicorn loggar hela sökvägen inklusive query, så varje
    sådan förfrågan skrev nyckeln i klartext i journalen, där den blev
    kvar så länge loggen roterar.

    Filtret och inte access_log=False: raderna är det enda som visar att
    någon faktiskt når tjänsten, och vilka vägar som svarar 401 eller 500.
    Att slänga hela diagnostiken för ett fälts skull är fel byte.

    uvicorn formaterar raden med args = (klient, metod, sökväg, version,
    status) och sökvägen på plats 2 (uppmätt). Ser posten inte ut så
    lämnas den orörd — ett filter som kastar tar ner loggningen, och det
    är värre än det det skulle skydda mot.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 3:
            return True
        path = args[2]
        if not isinstance(path, str) or "?" not in path:
            return True
        bas, _, query = path.partition("?")
        delar = []
        for del_ in query.split("&"):
            namn, likhet, _varde = del_.partition("=")
            delar.append(
                f"{namn}={_REDIGERAT}"
                if likhet and namn in _HEMLIGA_PARAMETRAR
                else del_
            )
        record.args = (*args[:2], f"{bas}?{'&'.join(delar)}", *args[3:])
        return True


def _open_to_the_network_without_token(settings: Settings) -> bool:
    """Skulle webbservern lyssna på nätverket utan WEB_ACCESS_TOKEN?

    Då når vem som helst som kommer åt porten både hälsodatan och /chat
    och /analyze/*, som kostar ett Anthropic-anrop var. Servern startade
    ändå, utan ett ord om det — och det är utgångsläget för den som följer
    .env.example, där tokenet står tomt och WEB_HOST är 0.0.0.0.

    Bara loopback räknas som lokalt. Ett annat värdnamn än localhost kan
    peka var som helst och räknas som nätverket.
    """
    if settings.web_access_token:
        return False
    if settings.web_host == "localhost":
        return False
    try:
        return not ipaddress.ip_address(settings.web_host).is_loopback
    except ValueError:
        return True


_OPEN_SERVER_REFUSAL = """\
Servern startar inte: WEB_ACCESS_TOKEN är tom, och WEB_HOST={host} gör
sidan nåbar för alla på nätverket, utan inloggning. Det gäller också
chatten och analyserna, som kostar ett Anthropic-anrop var.

Gör ett av två i .env:
  - Sätt WEB_ACCESS_TOKEN. Ett slumpat värde får du med
      python3 -c "import secrets; print(secrets.token_urlsafe(24))"
    Öppna sedan sidan en gång per enhet med ?token=<värdet> i adressen.
  - Eller lyssna bara på den här maskinen: WEB_HOST=127.0.0.1
"""


def _dump_fit(settings: Settings, activity_id: str) -> int:
    """Diagnostik: vad innehåller originalfilen för ett pass egentligen?

    Intervals.icu API har inga styrkefält alls (verifierat mot deras
    OpenAPI-spec), men originalfilen från klockan kan innehålla
    set-meddelanden med vikt och reps. Om de finns går de att importera
    automatiskt; om de inte gör det är chatt-loggning enda vägen. Det här
    kommandot avgör vilket — det skriver inget till databasen och sparar
    ingen fil.
    """
    from collections import Counter

    from garmin_fit_sdk import Decoder, Stream

    from sync.intervals_client import IntervalsClient

    with IntervalsClient(settings) as client:
        try:
            content = client.download_original_file(activity_id)
        except Exception as exc:
            print(f"Kunde inte hämta originalfilen: {exc}", file=sys.stderr)
            print(
                "Obs: endpointen stöds inte för pass som importerats från Strava.",
                file=sys.stderr,
            )
            return 1

    print(f"Hämtade {len(content)} byte.")
    # En FIT-fil har den bokstavliga strängen '.FIT' på position 8-12.
    if content[8:12] != b".FIT":
        print(
            "Filen ser inte ut att vara en FIT-fil (saknar '.FIT'-signaturen) — "
            "troligen TCX/GPX, som inte kan innehålla set-data.",
            file=sys.stderr,
        )
        return 1

    raw_messages, errors = Decoder(Stream.from_byte_array(content)).read()
    # SDK:n typar resultatet som en TypedDict med ett fält per känd
    # meddelandetyp. Här slås typerna upp dynamiskt (vi vet inte i förväg
    # vad filen innehåller — det är hela poängen med kommandot), vilket en
    # TypedDict inte tillåter, så den behandlas som en vanlig dict.
    messages = cast("dict[str, list[Any]]", raw_messages)
    if errors:
        print(f"Avkodningsvarningar: {len(errors)} (visar de tre första)")
        for err in errors[:3]:
            print(f"  {err}")

    print("\nMeddelandetyper i filen:")
    counts = Counter({name: len(rows) for name, rows in messages.items() if rows})
    for name, count in counts.most_common():
        print(f"  {name}: {count}")

    # Det vi faktiskt letar efter.
    for key in ("set_mesgs", "exercise_title_mesgs"):
        rows = messages.get(key) or []
        print(f"\n{key}: {len(rows)} st")
        for row in rows[:5]:
            print(f"  {row}")
        if len(rows) > 5:
            print(f"  ... och {len(rows) - 5} till")

    if not messages.get("set_mesgs"):
        print(
            "\nSlutsats: filen innehåller inga set-meddelanden — övningar och "
            "vikter går inte att importera automatiskt. Logga via chatten."
        )
    else:
        print(
            "\nSlutsats: filen innehåller set-data. Automatisk import är möjlig."
        )
    return 0


def _import_strength(
    settings: Settings, store: Store, activity_id: str | None, force: bool
) -> int:
    """Efterhandsimport av styrkedata för pass som redan ligger i databasen.

    Den vanliga synken importerar nya styrkepass automatiskt, men den
    rör bara pass den hämtar. Det här kommandot går igenom historiken.
    """
    from sync.fit_strength import is_strength_activity
    from sync.intervals_client import IntervalsClient, import_strength_sets

    if activity_id:
        activity = store.get_activity(activity_id)
        if not activity:
            print(f"Aktivitet saknas: {activity_id}", file=sys.stderr)
            return 1
        activities = [activity]
    else:
        # list_activities defaultar till 50; styrkepass 2-3 ggr/vecka i ett
        # års historik ryms med god marginal i 1000.
        activities = [
            a for a in store.list_activities(limit=1000) if is_strength_activity(a)
        ]
        if not activities:
            print("Inga styrkepass i databasen — kör 'sync' först.")
            return 0

    total_sets = 0
    imported = 0
    skipped = 0
    failed = 0
    with IntervalsClient(settings) as client:
        for activity in activities:
            act_id = str(activity.get("id"))
            # Samma spärr som synken: "har vi tittat?", inte "finns det
            # set?". Annars räknas ett pass vars fil saknar set-data som
            # oimporterat för alltid och hämtas om vid varje körning.
            if not force and (
                store.has_checked_strength_import(act_id)
                or store.has_strength_sets_for_activity(act_id, "fit")
            ):
                skipped += 1
                continue
            try:
                count = import_strength_sets(client, store, activity, force=force)
            except Exception as exc:
                print(f"  {act_id}: misslyckades — {exc}", file=sys.stderr)
                failed += 1
                continue
            if count:
                imported += 1
                total_sets += count
                # `or ""` och inte get(..., ""): nyckeln finns alltid, men
                # kan vara None, och None[:10] kraschade kommandot mitt i
                # körningen — efter att setsen redan skrivits.
                print(f"  {act_id} ({(activity.get('start_time') or '')[:10]}): {count} set")

    print(
        f"\nKlart: {total_sets} set från {imported} pass "
        f"({skipped} redan importerade, {failed} misslyckades)."
    )
    return 0


def _fmt_report_value(value: Any) -> str:
    """Värdet i listan: ett tal utan onödiga decimaler, annat som det står.

    Äldre rader kan ha text i värdekolumnen — "82 kg" gick att spara innan
    chattverktygen började kontrollera talen, och SQLite lagrar det som
    TEXT trots att kolumnen är REAL. `:g` på en sträng kastade, så just det
    kommando som finns för att hitta och ta bort sådana rader kraschade på
    dem.
    """
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:g}"
    return str(value)


def _self_report(store: Store, report_id: int | None, delete: bool, days: int) -> int:
    """Listar och raderar självrapporter från kommandoraden.

    Chatten kan rätta och radera via sina verktyg, men det förutsätter att
    Claude hittar rätt rad — och att chatten över huvud taget svarar. Det
    här är säkerhetsnätet: det som tidigare krävde `sqlite3` för hand mot
    den skarpa databasen.
    """
    if delete:
        if report_id is None:
            print("Ange vilket id som ska raderas.", file=sys.stderr)
            return 1
        report = store.get_self_report(report_id)
        if report is None:
            print(f"Ingen självrapport med id {report_id}.", file=sys.stderr)
            return 1
        store.delete_self_report(report_id)
        print(f"Raderade {report_id}: {report['day']} {report['category']}")
        return 0

    rows = store.list_self_reports(days=days)
    if not rows:
        print(f"Inga självrapporter de senaste {days} dagarna.")
        return 0
    print(f"{'id':>5}  {'dag':<12} {'kategori':<10} {'värde':>8}  anteckning")
    for r in rows:
        print(
            f"{r['id']:>5}  {r['day'] or '':<12} {r['category'] or '':<10} "
            f"{_fmt_report_value(r['value']):>8}  {r['note'] or ''}"
        )
    print("\nTa bort en rad: python -m src.main self-report --delete <id>")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="garmin-ai-pi")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync", help="Synka Intervals-data till SQLite")
    p_sync.add_argument(
        "--full",
        action="store_true",
        help="Hämta hela wellness-historiken, inte bara det rullande fönstret",
    )
    sub.add_parser("analyze-morning", help="Generera morgonrekommendation")
    sub.add_parser("analyze-evening", help="Generera kvällssammanfattning")
    sub.add_parser("analyze-coaching", help="Generera coachande rekommendationer")
    a_act = sub.add_parser("analyze-activity", help="Generera analys för en aktivitet")
    a_act.add_argument("activity_id")
    d_act = sub.add_parser(
        "dump-activity", help="Skriv ut lagrad rådata (raw_json) för en aktivitet"
    )
    d_act.add_argument("activity_id")
    d_act.add_argument(
        "--keys", action="store_true", help="Visa bara fältnamnen, inte värdena"
    )
    d_fit = sub.add_parser(
        "dump-fit",
        help="Ladda ner originalfilen för ett pass och visa vad den innehåller",
    )
    d_fit.add_argument("activity_id")
    imp = sub.add_parser(
        "import-strength",
        help="Importera övningar/vikter från styrkepassens originalfiler",
    )
    imp.add_argument(
        "activity_id",
        nargs="?",
        help="Ett enskilt pass. Utelämna för att gå igenom alla styrkepass.",
    )
    imp.add_argument(
        "--force",
        action="store_true",
        help="Importera om även pass som redan har FIT-data",
    )
    sr = sub.add_parser(
        "self-report",
        help="Lista eller radera självrapporterade uppgifter (vikt, sjukdom, ...)",
    )
    sr.add_argument(
        "--days", type=int, default=30, help="Hur långt bakåt som listas (default 30)"
    )
    sr.add_argument(
        "--delete", type=int, metavar="ID", help="Radera raden med det här id:t"
    )
    sub.add_parser(
        "backup", help="Skriv en daterad kopia av databasen och rensa gamla"
    )
    sub.add_parser("serve", help="Starta webbservern (uvicorn)")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    # Importera sent så att --help inte kräver .env.
    from config import load_settings
    from sync.store import Store

    settings = load_settings()
    store = Store(settings.db_path, settings.strength_corrections)
    store.init()

    if args.cmd == "sync":
        from scheduler import run_sync

        ok = run_sync(settings, full=args.full)
        if not ok:
            print("Sync misslyckades, se loggen ovan.", file=sys.stderr)
            return 1
        print("Sync klar.")
        return 0

    if args.cmd == "analyze-morning":
        from analysis.claude import ClaudeClient
        from analysis.pipeline import AnalysisPipeline

        AnalysisPipeline(store, ClaudeClient(settings), settings.athlete).morning_recommendation()
        print("Morgonrekommendation genererad.")
        return 0

    if args.cmd == "analyze-evening":
        from analysis.claude import ClaudeClient
        from analysis.pipeline import AnalysisPipeline

        AnalysisPipeline(store, ClaudeClient(settings), settings.athlete).evening_summary()
        print("Kvällssammanfattning genererad.")
        return 0

    if args.cmd == "analyze-coaching":
        from analysis.claude import ClaudeClient
        from analysis.pipeline import AnalysisPipeline

        AnalysisPipeline(store, ClaudeClient(settings), settings.athlete).coaching()
        print("Coaching-analys genererad.")
        return 0

    if args.cmd == "analyze-activity":
        from analysis.claude import ClaudeClient
        from analysis.pipeline import AnalysisPipeline

        pipeline = AnalysisPipeline(store, ClaudeClient(settings), settings.athlete)
        markdown = pipeline.analyze_activity(args.activity_id)
        print(markdown)
        return 0

    if args.cmd == "dump-activity":
        import json

        activity = store.get_activity(args.activity_id)
        if not activity:
            print(f"Aktivitet saknas: {args.activity_id}", file=sys.stderr)
            return 1
        try:
            raw = json.loads(activity.get("raw_json") or "{}")
        except ValueError:
            print("raw_json går inte att tolka som JSON.", file=sys.stderr)
            return 1
        print(f"Typ: {activity.get('type')} / sport: {activity.get('sport')}")
        print(f"Antal fält i rådatan: {len(raw)}")
        if args.keys:
            for key in sorted(raw):
                print(f"  {key}")
        else:
            print(json.dumps(raw, indent=2, ensure_ascii=False, default=str))
        return 0

    if args.cmd == "dump-fit":
        return _dump_fit(settings, args.activity_id)

    if args.cmd == "import-strength":
        return _import_strength(settings, store, args.activity_id, args.force)

    if args.cmd == "self-report":
        return _self_report(store, args.delete, args.delete is not None, args.days)

    if args.cmd == "backup":
        from scheduler import run_backup

        dest = run_backup(settings)
        if dest is None:
            print("Backup misslyckades, se loggen ovan.", file=sys.stderr)
            return 1
        print(f"Backup skriven: {dest}")
        return 0

    if args.cmd == "serve":
        import uvicorn

        if _open_to_the_network_without_token(settings):
            print(_OPEN_SERVER_REFUSAL.format(host=settings.web_host), file=sys.stderr)
            return 1

        # Före uvicorn.run: dess dictConfig nollställer handlers på de
        # loggers den känner till, men rör inte deras filter — så det här
        # överlever konfigurationen (verifierat mot en körande server).
        logging.getLogger("uvicorn.access").addFilter(_RedactQuerySecrets())
        uvicorn.run(
            # Fabrik, inte en färdig app: web.app bygger ingenting vid
            # import längre, så modulen går att importera utan .env.
            "web.app:create_app",
            factory=True,
            host=settings.web_host,
            port=settings.web_port,
            reload=False,
            log_level="info",
        )
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
