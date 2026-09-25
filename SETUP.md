# Installation på en Raspberry Pi

Guide för att sätta upp appen från noll. Räkna med en halvtimme, varav
det mesta är väntan på `pip install` och den första synken.

## Vad du behöver först

**Hårdvara och OS**
- Raspberry Pi (utvecklad och körd på en Pi 5, men inget i koden kräver
  just den modellen)
- Raspberry Pi OS Bookworm eller senare — den har Python 3.11, och
  projektet kräver 3.10+
- Nätverk. Diskbehovet är försumbart: databasen är en SQLite-fil på några MB

**Konton**
- Ett **Intervals.icu**-konto med data i. Intervals hämtar i sin tur från
  Garmin Connect, så kedjan är klocka → Garmin Connect → Intervals.icu →
  den här appen.
- En **Anthropic**-nyckel från https://console.anthropic.com/

> **Det här kostar pengar.** Varje analys och varje chattmeddelande är ett
> API-anrop till Claude. I normal drift blir det tre schemalagda analyser
> per dygn — morgon, träningsanalys och kväll — plus de passanalyser och
> "Generera om" du själv startar, och varje replik i chatten. Kolla aktuell
> prissättning innan du sätter igång, och sätt gärna en utgiftsgräns i
> Anthropic-konsolen.

**Styrketräning** kräver dessutom en Garmin-klocka som registrerar set —
appen läser övningar, vikter och reps ur klockans originalfil. Se
[avsnittet längst ner](#styrketräning).

## 1. Installera

```bash
sudo apt update
sudo apt install -y git python3-venv

git clone https://github.com/henrikwahlgren77-lgtm/garmin-ai-pi-shared.git ~/garmin-ai-pi
cd ~/garmin-ai-pi

python3 -m venv .venv
.venv/bin/pip install -e . -c requirements.lock
```

> Använd alltid `.venv/bin/pip` och `.venv/bin/python`, eller aktivera
> venv:et först med `source .venv/bin/activate`. Systemets `pip` vägrar
> installera på Raspberry Pi OS med felet
> `externally-managed-environment` (PEP 668), och systemets `python`
> hittar inte projektets moduler.

## 2. Hämta dina nycklar

**Intervals.icu**
1. Logga in och gå till **Settings**
2. Scrolla till **Developer** och kopiera din **API Key**
3. Ditt athlete-id är siffrorna i profil-URL:en, med `i` framför:
   `intervals.icu/athletes/12345` → `i12345`

**Anthropic**
1. https://console.anthropic.com/ → **API Keys** → skapa en nyckel
2. Kopiera den direkt; den visas bara en gång

## 3. Konfigurera

```bash
cp .env.example .env
nano .env
```

Fyll i nycklarna. Fyll också i **atletprofilen** — den läggs in som
kontext i varje analys så att Claude vet vem den skriver till:

```
ATHLETE_NAME=Anna
ATHLETE_SEX=kvinna
ATHLETE_BIRTH_DATE=1980-05-14
ATHLETE_HEIGHT_CM=172
ATHLETE_MEDICAL=
ATHLETE_ROUTINE=styrketräning 2-3 gånger/vecka, löpning 1-2 gånger/vecka
ATHLETE_GOAL=bygga uthållighet inför ett halvmaraton
```

Alla profilfält är frivilliga. Det du lämnar tomt utelämnas ur prompten
i stället för att gissas — lämnar du allt tomt får Claude uttryckligen
instruktionen att inte anta något om ålder, kön eller hälsa.

`ATHLETE_NAME` används både i webbens hälsning ("God morgon Anna!") och i
analyserna, så de tilltalar dig vid namn.

`ATHLETE_MEDICAL` är fritext för sådant som bör påverka råd om intensitet
och puls, till exempel en hjärtåkomma eller en gammal skada. Fältet
skickas till Anthropic som en del av prompten — skriv bara sådant du är
bekväm med att dela. Lämna det tomt om inget är relevant.

Vikten ska du **inte** fylla i: den hämtas från Intervals eller från det
du säger i chatten, och följer alltså med när den ändras.

`.env` är gitignorerad och stannar på din maskin. Resten av inställningarna
— tider, backup, portar — har fungerande standardvärden och är beskrivna
både i filen och i [README:ns konfigurationstabell](README.md#konfiguration-env).

## 4. Första synken

```bash
.venv/bin/python -m src.main sync
```

Hämtar upp till ett års aktiviteter och wellness-data, och laddar ner
originalfilen för varje styrkepass den hittar. Första gången tar det en
stund — räkna med några minuter om du har ett års historik. Går det fel
här är det nästan alltid fel API-nyckel eller athlete-id.

Testa sedan att generera en analys:

```bash
.venv/bin/python -m src.main analyze-morning
```

## 5. Starta webben

```bash
.venv/bin/python -m src.main serve
```

Servern startar inte förrän `WEB_ACCESS_TOKEN` är satt i `.env`: utan
token vore sidan öppen för alla på nätverket, och `/chat` kostar pengar per
anrop. Ett slumpat värde får du med

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

Öppna sedan `http://<pi-ip>:8000/?token=<värdet>` en gång på varje enhet.
Enheten får en kaka, tokenet försvinner ur adressen, och därefter räcker
`http://<pi-ip>:8000`. Se [Säkerhet i README](README.md#säkerhet).

## 6. Kör som tjänst

```bash
# Anpassa sökvägar och användarnamn i filerna först — de utgår från
# /home/<ditt-användarnamn>/garmin-ai-pi
nano systemd/garmin-ai-pi.service

sudo cp systemd/garmin-ai-pi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now garmin-ai-pi

journalctl -u garmin-ai-pi -f
```

Webbservern kör då sin egen schemaläggare:

| Jobb | Tid | Styrs av |
|---|---|---|
| Intervals-synk | Varje timme | `SYNC_INTERVAL_HOURS` |
| Morgonanalys | Varje heltimme i fyra timmar från 07:00, tills ny sömndata kommit in | `MORNING_RECOMMENDATION_TIME` |
| Träningsanalys | 12:00 | `COACHING_TIME` |
| Kvällssammanfattning | 23:59 | `EVENING_SUMMARY_TIME` |
| Backup av databasen | 03:30 | `BACKUP_TIME`, `BACKUP_KEEP`, `BACKUP_DIR` |

De tre analysjobben körs också en gång vid uppstart, med sina egna spärrar
(för tidigt på dygnet / redan genererad idag / ingen ny sömndata), så en
omstart strax efter en schemalagd tid inte gör att analysen uteblir.
Backupen likaså: saknas kopian från den senaste schemalagda tiden, till
exempel för att Pi:n var avstängd 03:30, tas den vid start.

## 7. Chatta med coachen

Längst ner på dashboarden finns "Fråga coachen". Den ser din wellness-,
tränings- och styrkehistorik, och sparar det du berättar så att det finns
kvar till morgondagens analys:

- *"lite snorig idag"* → sparas som sjukdom
- *"tog två öl igår"* → sparas som alkohol, daterad till igår
- *"vägde mig, 91,2"* → blir din nya kända vikt i alla analyser
- *"bänkpress 3x8 på 80 kg"* → tre set i `strength_sets`, kopplade till
  dagens gympass

Registrerade du passet på klockan behöver du inte logga övningarna — de
importeras automatiskt. Loggar du dem ändå dubbelräknas volymen.

Du kan också fråga: *"hur har min vilopuls sett ut den senaste veckan?"*
eller *"vad lyfte jag i marklyft förra gången?"* — svaret bygger på
databasen, inte på gissningar.

**Blev något fel?** Säg det bara: *"nej förresten, det var 121,8"* eller
*"strunta i det där om alkoholen"*. Claude rättar respektive tar bort
raden. Vill du se allt som ligger sparat, eller rensa något själv:

```bash
.venv/bin/python -m src.main self-report
.venv/bin/python -m src.main self-report --delete 4
```

Listningen visar id:t som `--delete` vill ha.

## Styrketräning

Intervals.icu:s API innehåller **inga** fält för övningar, vikter eller
reps. Appen hämtar dem i stället ur klockans originalfil (`GET
/activity/{id}/file`), där en Garmin-FIT har set-meddelanden med
övningskategori, vikt och antal repetitioner.

Nya styrkepass importeras automatiskt vid synk. För att fylla på
historiken bakåt:

```bash
.venv/bin/python -m src.main import-strength
```

Osäker på om just din klocka och synkkedja levererar det? Kör diagnosen
mot ett styrkepass:

```bash
.venv/bin/python -m src.main dump-fit <activity_id>
```

Den skriver ut vad filen innehåller och säger rakt ut om set-data finns.
Gör den inte det — eller kommer dina pass in via Strava, där endpointen
inte stöds — går det att logga övningar via chatten i stället.

Passen där filen saknar set-data noteras i databasen, så de inte laddas
ner på nytt vid varje synk. Det är alltså normalt att `import-strength`
rapporterar pass som "redan importerade" trots att de inte har några set.

## Uppdatera

```bash
cd ~/garmin-ai-pi
git pull
.venv/bin/pip install -e . -c requirements.lock   # när requirements.lock ändrats
sudo systemctl restart garmin-ai-pi
```

Omstarten behövs: tjänsten kör utan `--reload`, så ny kod läses in först
vid start. CLI-kommandon plockar upp den direkt.

## Backup och återställning

Databasen är den enda kopian av allt appen själv skapat — analyserna, det
du rapporterat i chatten och dina styrkeset. Tjänsten tar en kopia varje
natt till `data/backups/`. Ta en manuellt inför något riskabelt:

```bash
.venv/bin/python -m src.main backup
```

Återställning är att kopiera tillbaka filen:

```bash
sudo systemctl stop garmin-ai-pi
cp data/backups/training-2026-08-30.db data/training.db
sudo systemctl start garmin-ai-pi
```

Rotationen tar bort allt utom de `BACKUP_KEEP` senaste, men rör bara filer
som heter `training-*.db` — döp om en kopia du vill spara permanent.

Kopiorna hamnar som default på **samma kort som databasen**, så ett trasigt
SD-kort tar både originalet och backupen. Vill du ha dem någon annanstans
laddar `scripts/ladda-upp-backup.sh` upp dem till ett moln varje natt:

```bash
sudo apt install rclone
rclone config                 # en gång, interaktivt: välj din molntjänst
echo 'BACKUP_REMOTE=jottacloud:garmin-ai-pi/backups' >> .env
sudo systemctl enable --now garmin-ai-pi-backup-upload.timer
```

Fler detaljer i [README](README.md#backup).

## Om något inte fungerar

| Symptom | Trolig orsak |
|---|---|
| `externally-managed-environment` | Systemets pip i stället för `.venv/bin/pip` |
| `ModuleNotFoundError: No module named 'config'` | Systemets python i stället för `.venv/bin/python` |
| `Miljövariabeln X saknas` vid start | `.env` är inte ifylld, eller tjänsten hittar den inte — kolla `EnvironmentFile` i service-filen |
| `Servern startar inte: WEB_ACCESS_TOKEN är tom` | Sätt ett token i `.env` enligt steg 5, eller `WEB_HOST=127.0.0.1` om sidan bara ska nås från Pi:n själv |
| `401 Unauthorized` vid synk | Fel `INTERVALS_API_KEY` eller `INTERVALS_ATHLETE_ID` |
| Tom dashboard | Ingen synk körd än |
| `Ogiltig token` i webbläsaren | Enheten saknar kakan, eller tokenet i `.env` har bytts. Öppna sidan en gång med `?token=...` |
| Analyser genereras inte | Kolla `journalctl -u garmin-ai-pi` — oftast Anthropic-nyckel eller kredit |
| Analysen slutar mitt i en mening med "(avkortat)" | Svaret nådde takets gräns. Loggraden `output-tokens ... varav N tänkande` visar var budgeten tog vägen |
| `409 En synk pågår redan` | Du tryckte "Synka Intervals" medan timsynken körde. Vänta och försök igen |
| Ändringen syns inte i webben | Tjänsten inte omstartad efter `git pull` |
| Inga övningar på styrkepass | Kör `dump-fit` enligt ovan |
| Engelskt övningsnamn på dashboarden | En FIT-variant som saknar svensk översättning. Sök i loggen efter `Oöversatt övningsvariant` |
| Styrkevolymen ser dubbelt så hög ut | Samma pass både importerat från klockan och loggat i chatten |

Loggen är första stället att titta:

```bash
journalctl -u garmin-ai-pi -n 100 --no-pager
```

Vill du se vad ett enskilt pass faktiskt innehåller:

```bash
.venv/bin/python -m src.main dump-activity <activity_id> --keys
```

## Utveckling

Projektet utvecklas på Windows och körs på Pi:n. Byt `.venv/bin/` mot
`.venv/Scripts/` på Windows.

```bash
.venv/bin/pip install -e ".[dev]" -c requirements.lock
.venv/bin/python -m pytest              # 555 tester
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy src
```

Testerna spärrar av nätverket: försöker koden nå intervals.icu eller
api.anthropic.com på riktigt fälls testet med ett meddelande om hur
anropet ska fejkas i stället. Se `tests/conftest.py`.
