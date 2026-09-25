# garmin-ai-pi

AI-driven träningsanalys (Claude) av din data från **Intervals.icu**, körd på en
**Raspberry Pi 5**. Mobil-först webbsida med diagram ritade som inline-SVG —
inga externa anrop från webbläsaren, inget diagrambibliotek att ladda.

## Vad det gör

**Synkar** aktiviteter och daglig wellness (fitness/trötthet/form, sömn, HRV,
vilopuls, vikt, steg) från Intervals.icu till en lokal SQLite-databas, varje
timme.

**Läser styrkepassen ur klockans egen fil.** Intervals API har inga fält för
övningar, vikter eller reps — appen hämtar originalfilen (Garmin-FIT) och
parsar dess set-meddelanden. Se [Styrketräning](#styrketräning).

**Analyserar** med Claude i fyra former:

| Analys | När | Vad |
|---|---|---|
| Morgonanalys | Varje heltimme i fyra timmar från `MORNING_RECOMMENDATION_TIME`, tills ny sömndata kommit in | Nattens sömn, HRV och vilopuls mot din egen baslinje → rekommendation för dagens träning |
| Träningsanalys | `COACHING_TIME` (12:00) | Lägesbild och riktlinjer framåt: nästa pass, kommande vecka, periodisering |
| Kvällssammanfattning | `EVENING_SUMMARY_TIME` (23:59) | Dagens träning, steg, belastning och återhämtning |
| Passanalys | På begäran, per pass | Djupdykning i ett enskilt pass — puls, tempo, sektortider, eller övningar och volym för ett gympass |

**Chattar.** "Fråga coachen" på dashboarden har tillgång till din wellness-,
tränings- och styrkehistorik, och kan spara det du berättar: säg *"lite snorig
idag"* eller *"bänkpress 3x8 på 80 kg"* så hamnar det i databasen och vägs in i
kommande analyser. Se [Chatten](#chatten).

**Visar** allt på en sida byggd för telefon: konditionen mot tröttheten över 30
dagar med formen som ifyllt gap, styrkevolym per vecka i tolv veckor (varje
stapel länkar till sitt pass), dagens sömn/HRV/vilopuls/steg mot ditt eget
snitt, de tre analyserna, chatten och de tio senaste passen.

**Sköter sig själv.** Webbtjänsten kör sin egen schemaläggare — synk,
de tre dagliga analyserna och en nattlig backup av databasen. Ingen extern
timer behövs.

## Krav

- Raspberry Pi (utvecklad på en Pi 5, men inget i koden kräver just den) med Python 3.10+
- Ett Intervals.icu-konto med API-nyckel
- En Anthropic API-nyckel
- För automatisk styrkedata: en Garmin-klocka som registrerar set

> **Sätter du upp appen för första gången?** Följ [SETUP.md](SETUP.md) —
> en fullständig guide från tom Pi till körande tjänst, inklusive var
> nycklarna hämtas, hur atletprofilen fylls i och vad som kan gå fel.
> Avsnitten nedan är en kortare referens.

## Installation på Pi:n

```bash
# 1. Klona repot
cd ~
git clone https://github.com/henrikwahlgren77-lgtm/garmin-ai-pi-shared.git garmin-ai-pi
cd garmin-ai-pi

# På en standard Raspberry Pi OS-installation ligger hemkatalogen under /home/<användare>.
# Exemplen nedan utgår från användaren '<ditt-användarnamn>'. Ändra sökvägar om du använder en annan användare.

# 2. Skapa venv och installera
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]" -c requirements.lock

# 3. Konfigurera miljövariabler
cp .env.example .env
nano .env   # fyll i dina nycklar (se nedan)

# 4. Initiera databasen
python -m src.main sync   # första synken mot Intervals
```

## Konfiguration (`.env`)

Fullständig lista. Bara de tre översta är obligatoriska — resten har
fungerande standardvärden.

### Nycklar

| Variabel | Default | Var jag hittar den |
|---|---|---|
| `INTERVALS_API_KEY` | *krävs* | Intervals.icu → Settings → Developer → "API Key" |
| `INTERVALS_ATHLETE_ID` | *krävs* | Siffrorna i din profil-URL, t.ex. `intervals.icu/athletes/12345` → `i12345` |
| `ANTHROPIC_API_KEY` | *krävs* | https://console.anthropic.com/ |
| `ANTHROPIC_MODEL` | `claude-sonnet-5` | Byt bara om du vet varför |

### Webb

| Variabel | Default | Betyder |
|---|---|---|
| `WEB_ACCESS_TOKEN` | tom | Krävs när servern lyssnar på nätverket. Se [Säkerhet](#säkerhet) |
| `WEB_HOST` | `0.0.0.0` | Bindning för uvicorn |
| `WEB_PORT` | `8000` | Port |

### Data och schema

| Variabel | Default | Betyder |
|---|---|---|
| `DB_PATH` | `data/training.db` | Relativ sökväg tolkas från projektroten |
| `SYNC_INTERVAL_HOURS` | `1` | Timmar mellan Intervals-synkar |
| `MORNING_RECOMMENDATION_TIME` | `07:00` | Start på morgonfönstret. Analysen görs om varje heltimme i fyra timmar därefter, men bara när ny sömndata faktiskt kommit in |
| `COACHING_TIME` | `12:00` | När träningsanalysen genereras |
| `EVENING_SUMMARY_TIME` | `23:59` | När kvällssammanfattningen genereras |
| `BACKUP_TIME` | `03:30` | När den nattliga kopian tas |
| `BACKUP_KEEP` | `14` | Antal kopior som sparas |
| `BACKUP_DIR` | `<DB_PATH>/../backups` | Peka på annan disk för att skydda mot att SD-kortet dör |

Tiderna anges som `HH:MM` i Pi:ns lokala tid och valideras vid uppstart — en
felskriven tid ger ett felmeddelande som namnger variabeln, inte en krasch.

### Atletprofil

Läggs in som kontext i varje analys så Claude vet vem den skriver till. Alla
fält är frivilliga; det du lämnar tomt utelämnas ur prompten i stället för att
gissas.

| Variabel | Betyder |
|---|---|
| `ATHLETE_NAME` | Tilltalsnamn. Används i hälsningen ("God morgon Anna!") och i analyserna |
| `ATHLETE_SEX` | Fritext |
| `ATHLETE_BIRTH_DATE` | `YYYY-MM-DD`. Åldern räknas ut, så den blir inte fel nästa födelsedag |
| `ATHLETE_HEIGHT_CM` | Heltal |
| `ATHLETE_MEDICAL` | Fritext om sådant som bör påverka råd om puls och intensitet. **Skickas till Anthropic** — skriv bara det du är bekväm med att dela |
| `ATHLETE_ROUTINE` | Hur din vecka brukar se ut |
| `ATHLETE_GOAL` | Vad du tränar mot |

**Vikten fyller du inte i.** Den hämtas från Intervals eller från det du säger i
chatten, och den färskaste av de två vinner — så den följer med när den ändras.

## Kör

### Manuellt (testa innan systemd)

```bash
source .venv/bin/activate

# Starta webbservern
python -m src.main serve
# Öppna http://<pi-ip>:8000/?token=<WEB_ACCESS_TOKEN> första gången på varje enhet
```

### Som systemd-tjänst (rekommenderat)

```bash
# Installera enhetsfilerna
sudo cp systemd/garmin-ai-pi.service /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable --now garmin-ai-pi          # webbservern (synkar själv)

# Loggar
journalctl -u garmin-ai-pi -f
```

> Filerna förutsätter att repot ligger i `/home/<ditt-användarnamn>/garmin-ai-pi`, körs som användaren `<ditt-användarnamn>` och att venv:et finns i `.venv/` under repot. Ändra sökvägar/användare i filerna om du använder annat.

## Uppdatera efter `git pull`

```bash
cd ~/garmin-ai-pi
git pull
.venv/bin/pip install -e . -c requirements.lock   # när requirements.lock ändrats
sudo systemctl restart garmin-ai-pi
```

Omstarten är nödvändig: `ExecStart` kör utan `--reload`, så koden läses in en
gång vid start. CLI-kommandon plockar däremot upp ny kod direkt.

> Använd alltid `.venv/bin/pip` och `.venv/bin/python` (eller aktivera venv:et
> först med `source .venv/bin/activate`). Systemets `pip` vägrar installera på
> Raspberry Pi OS med felet `externally-managed-environment` (PEP 668), och
> systemets `python` hittar inte projektets moduler — `python -m src.main` ger
> då `ModuleNotFoundError: No module named 'config'`, eftersom det är
> editable-installationen i venv:et som lägger `src/` på `sys.path`.

## CLI-kommandon

Kör med venv:ets Python (`.venv/bin/python`) eller efter `source .venv/bin/activate`.
`-v` / `--verbose` framför kommandot ger DEBUG-loggning.

```bash
# Synk
python -m src.main sync                     # synka Intervals → SQLite
python -m src.main sync --full              # hela wellness-historiken (annars 14 dagar)

# Analyser (samma som schemat och knapparna i webben kör)
python -m src.main analyze-morning          # morgonrekommendation
python -m src.main analyze-coaching         # träningsanalys
python -m src.main analyze-evening          # kvällssammanfattning
python -m src.main analyze-activity <id>    # analys för ett enskilt pass

# Styrketräning
python -m src.main import-strength          # importera övningar/vikter för alla styrkepass
python -m src.main import-strength <id>     # bara ett pass
python -m src.main import-strength --force  # importera om även redan hämtade pass

# Självrapporter (vikt, sjukdom, alkohol, ...)
python -m src.main self-report              # lista de senaste 30 dagarna med id
python -m src.main self-report --days 90    # längre bakåt
python -m src.main self-report --delete <id>  # ta bort en felaktig rad

# Diagnostik
python -m src.main dump-activity <id>       # lagrad rådata (raw_json) för ett pass
python -m src.main dump-activity <id> --keys  # bara fältnamnen
python -m src.main dump-fit <id>            # vad originalfilen från klockan innehåller

python -m src.main backup                   # daterad kopia av databasen nu (tas annars nattetid)
python -m src.main serve                    # starta webbservern
```

## Chatten

"Fråga coachen" på dashboarden får med sig dagens wellness, de fem senaste
passen, 30 dagars wellness-historik och 14 dagars självrapporter och
styrketräning. Den kan alltså svara på trendfrågor ("sjunker min vilopuls?")
och på "vad lyfte jag förra gången?" utan att gissa.

Fyra verktyg gör att det du säger stannar kvar mellan samtal — och går att
ta tillbaka:

- **`log_self_report`** — vikt, sjukdom, alkohol, skada, humör eller fri
  anteckning. Anropas även när du nämner något i förbigående.
- **`log_strength_session`** — en övning med alla set. *"marklyft 5x120"* blir
  fem rader i `strength_sets`, kopplade till dagens gympass om det finns
  exakt ett.
- **`correct_self_report`** — *"nej förresten, det var 121,8"* eller *"det där
  var igår"*. Bara de fält du nämner ändras.
- **`delete_self_report`** — *"strunta i det där"*. Claude raderar aldrig
  något du inte bett om, och frågar hellre vilken rad du menar än gissar.

Går chatten inte att nå, eller pekar Claude på fel rad, finns samma sak på
kommandoraden: `python -m src.main self-report` listar med id, `--delete <id>`
tar bort.

Registrerade du passet på klockan importeras övningarna automatiskt ur
FIT-filen. Logga då inte samma övning en gång till i chatten — volymen
dubbelräknas.

## Backup

Databasen är den enda kopian av det appen själv skapat: alla genererade
analyser, det du rapporterat i chatten och dina loggade styrkeset. Intervals
har rådatan kvar, men inget av det andra går att återskapa.

Tjänsten tar därför en kopia varje natt (`BACKUP_TIME`, default 03:30) till
`data/backups/training-ÅÅÅÅ-MM-DD.db` och sparar de senaste `BACKUP_KEEP`
dagarna (default 14). Kopian tas med sqlite3:s backup-API, inte som en
filkopia — databasen körs i WAL-läge, och en `cp` kan då missa skrivningar
som ännu bara finns i `-wal`-filen.

**Kopian ligger som default på samma disk som databasen.** Det skyddar mot
tappade skrivningar och felaktiga raderingar, men inte mot att SD-kortet
slutar svara. Sätt `BACKUP_DIR` till en monterad disk eller nätverksplats för
att skydda mot det också.

### Kopia till molnet

`scripts/ladda-upp-backup.sh` kopierar backuperna till ett moln via
[rclone](https://rclone.org). Sätt `BACKUP_REMOTE` i `.env` och slå på
timern, så körs den 03:45 varje natt — kvart efter att nattens fil skrivits:

```bash
sudo apt install rclone
rclone config                 # en gång, interaktivt: välj din molntjänst
sudo systemctl enable --now garmin-ai-pi-backup-upload.timer
```

Skriptet använder `rclone copy` och inte `sync`. `sync` speglar även
raderingar, och då hade rotationen ovan tagit molnkopiorna med sig efter
`BACKUP_KEEP` dagar — precis det skyddsnät uppladdningen finns för. Molnet
växer alltså med ~2 MB per natt och gallras aldrig automatiskt.

Efter uppladdningen läses filstorleken tillbaka **från molnet** och jämförs
med den lokala. En avbruten uppladdning ger annars en kortare fil och en
utskrift som påstår att allt gick bra.

Rotationen rör bara filer som heter `training-*.db`. Vill du spara en kopia
permanent — inför en riskabel ändring, till exempel — döp om den:

```bash
cd data/backups
cp training-2026-09-03.db fore-ombyggnad-2026-09-03.db
```

Återställning är att kopiera tillbaka filen:

```bash
sudo systemctl stop garmin-ai-pi
cp data/backups/training-2026-08-30.db data/training.db
sudo systemctl start garmin-ai-pi
```

## Styrketräning

Intervals.icu:s API innehåller **inga** fält för övningar, vikter eller reps —
deras OpenAPI-spec har inga properties som heter `reps`, `weight`, `exercise`
eller `sets`. Datan kommer istället från två håll:

1. **Automatiskt från klockan.** Originalfilen (`GET /activity/{id}/file`)
   är en Garmin-FIT med set-meddelanden som innehåller övningskategori, vikt
   och reps. Synken hämtar och parsar den för varje nytt styrkepass.
2. **Manuellt via chatten.** Skriv t.ex. *"bänkpress 3x8 på 80 kg"* så sparas
   det via verktyget `log_strength_session`. Används för gympass som inte
   registrerats på klockan.

Setsen sparas i tabellen `strength_sets`, en rad per set, med kolumnen `source`
(`fit` eller `chat`). En om-import ersätter bara sina egna `fit`-rader och rör
aldrig det du loggat manuellt.

### Övningsnamnen

FIT-filen har två fält: `category` är grov (53 värden — allt tungt från golvet
är `deadlift`), medan `category_subtype` pekar ut vilken övning det faktiskt
var. Appen läser båda och översätter till svenska, så att `deadlift/0` blir
"marklyft" och `deadlift/1` blir "raka marklyft".

Det spelar roll för progressionen: utan subtypen hamnade 135 kg konventionellt
marklyft och 40 kg raka marklyft på samma rad, och serien såg ut att svänga
vilt mellan pass när det i själva verket var olika övningar.

En övning som saknas i översättningstabellen sparas med sitt FIT-namn och
loggas som `Oöversatt övningsvariant från FIT: ...` — sök i journalen efter den
raden om ett engelskt namn dyker upp på dashboarden, och lägg till det i
`_EXERCISE_VARIANT_NAMES_SV` i `src/sync/fit_strength.py`.

### Fylla på historiken

```bash
.venv/bin/python -m src.main import-strength
```

Efter en ändring i namnöversättningen behöver redan importerade pass läsas om.
**Ta en backup först** — kommandot skriver om alla `fit`-rader:

```bash
.venv/bin/python -m src.main backup
.venv/bin/python -m src.main import-strength --force
```

## Säkerhet

- `.env` är gitignorerad — lägg **aldrig** riktiga nycklar i kod eller git.
- Webbservern binder till `0.0.0.0:8000` och **startar inte utan
  `WEB_ACCESS_TOKEN`**. Det är inte bara dina hälsodata som ligger bakom den:
  `/chat` och `/analyze/*` kostar ett Anthropic-anrop var, så en öppen port
  vore också en öppen plånbok. Utan token går den bara att köra med
  `WEB_HOST=127.0.0.1`, alltså nåbar enbart från samma maskin. Exponera
  aldrig porten mot internet.
- **Första gången** på en enhet öppnar du sidan med `?token=...` i adressen.
  Enheten får då en kaka (`garmin_ai_token`, httpOnly, SameSite=Lax, ett år),
  och servern skickar dig vidare till samma adress utan token. Därefter
  räcker `http://<pi-ip>:8000`.
- Sidorna innehåller inget token: inte i länkar, formulär eller JavaScript.
  Kakan följer med av sig själv, och eftersom den är httpOnly kan inget
  skript på sidan läsa den.
- Byter du `WEB_ACCESS_TOKEN` slutar alla utdelade kakor gälla direkt.
  Jämförelsen sker mot värdet i `.env`, inte mot något sparat, så det finns
  ingen utloggning att göra.
- Bokmärket eller genvägen du öppnar första gången innehåller fortfarande
  tokenet, och trafiken går över vanlig http. Det är ett rimligt
  hemmanätverksskydd, inte en autentiseringslösning.
- Skrivande anrop (`POST /sync`, `POST /analyze/*`, `POST /chat`) avvisas när
  webbläsaren märker dem som `Sec-Fetch-Site: cross-site`, så en främmande
  sajt kan inte avfyra dem i din webbläsare.
- Analyser renderas som markdown i webbläsaren och saneras där. Ingen
  modellgenererad text når `innerHTML` osanerad — texten är byggd av data vi
  inte äger (passnamn från Intervals, anteckningar från chatten).
- För fjärråtkomst rekommenderas Tailscale eller en Caddy/TLS-proxy istället
  för att öppna portar.

## Utveckling

Utvecklas på Windows, körs på Pi:n. Byt `.venv/bin/` mot `.venv/Scripts/` på
Windows.

```bash
.venv/bin/pip install -e ".[dev]" -c requirements.lock
.venv/bin/python -m pytest              # 555 tester
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
```

`requirements.lock` låser hela beroendeträdet, så datorn och Pi:n kör samma
versioner. `pyproject.toml` säger bara vad koden tål. För att byta version på
ett beroende: installera den nya, kör `python tools/las-beroenden.py`, kör
testerna och checka in låsfilen. Pi:n följer med vid nästa
`pip install -e . -c requirements.lock`.

Testerna spärrar av nätverket: ett försök att nå intervals.icu eller
api.anthropic.com på riktigt fäller testet med ett meddelande om hur det ska
fejkas i stället. Se `tests/conftest.py`.

## Projektstruktur

```
src/
  config.py              # läser .env, validerar tider och tal
  main.py                # CLI
  scheduler.py           # APScheduler: synk, tre analyser, nattlig backup
  sync/
    intervals_client.py  # Intervals.icu REST-klient + synklogik
    fit_strength.py      # läser set (övning/vikt/reps) ur Garmin-FIT
    store.py             # SQLite-lager (schema, migrering, backup)
  analysis/
    claude.py            # Anthropic API-wrapper (tool use, avkortning)
    prompts.py           # systempromptar (svenska)
    pipeline.py          # orkestrering av analyser + chattverktygen
  web/
    app.py               # FastAPI: sidor, /api/*, /chat, /sync
    templates/           # Jinja2, mobil-först
    static/
      style.css
      markdown.js        # markdown-rendering med sanering (delad)
      chat.js            # chattlogik
      run-time.js        # "Kördes HH:MM" under analysrubriken
      vendor/            # marked (inlåst version, inga CDN-anrop)
tests/                   # 555 tester
scripts/                 # driftskript (backup till molnet)
systemd/                 # .service/.timer för Pi:n
tools/                   # låser beroendena (las-beroenden.py)
requirements.lock        # exakta versioner, genererad av tools/las-beroenden.py
data/                    # SQLite-databas och backuper (gitignorerad)
```

## Licens

MIT — se [LICENSE](LICENSE).
