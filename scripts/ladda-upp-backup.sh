#!/usr/bin/env bash
#
# Kopierar de nattliga databasbackuperna till ett rclone-fjärrmål
# (Jottacloud i drift). Körs av garmin-ai-pi-backup-upload.timer strax
# efter att run_backup skrivit nattens fil.
#
# Varför utanför appen: den ska inte behöva känna till ett moln eller
# bära dess inloggning, och ett uppladdningsfel ska inte kunna påverka
# analyserna. rclone sköter dessutom omförsök och autentisering bättre
# än något vi skulle skriva själva.
#
# Konfiguras via .env (samma fil som appen):
#   BACKUP_DIR      katalogen run_backup skriver till (default data/backups)
#   BACKUP_REMOTE   rclone-mål, t.ex. jottacloud:garmin-ai-pi/backups
set -euo pipefail

ROT="$(cd "$(dirname "$0")/.." && pwd)"

# Läs ut EN nyckel ur .env. Filen källas medvetet INTE med `.`: den är
# inte ett skalskript, och att köra den betyder att ett värde med
# mellanslag, komma eller $ blir kommandon. Den riktiga .env har redan
# en sådan rad (PROGRESSION_HIDDEN_EXERCISES), som gav "command not
# found" mitt i uppstarten.
las_env() {
    [ -f "$ROT/.env" ] || return 0
    sed -n "s/^[[:space:]]*$1=//p" "$ROT/.env" | tail -1 | sed 's/[[:space:]]*$//'
}

: "${BACKUP_DIR:=$(las_env BACKUP_DIR)}"
: "${BACKUP_REMOTE:=$(las_env BACKUP_REMOTE)}"

KALLA="${BACKUP_DIR:-$ROT/data/backups}"
MAL="${BACKUP_REMOTE:?BACKUP_REMOTE är inte satt i .env (t.ex. jottacloud:garmin-ai-pi/backups)}"

command -v rclone >/dev/null || { echo "rclone saknas: sudo apt install rclone" >&2; exit 1; }
[ -d "$KALLA" ] || { echo "Ingen backupkatalog: $KALLA" >&2; exit 1; }

# copy, inte sync. sync speglar ÄVEN raderingar, och då tar den lokala
# rotationen (BACKUP_KEEP, 14 dagar) med sig molnkopiorna — precis det
# skyddsnät den här uppladdningen finns för. En backup som kan raderas
# av samma rutin som den skyddar mot är ingen backup.
rclone copy "$KALLA" "$MAL" --include "training-*.db" --checksum

# Kontrollera mot FJÄRREN, inte mot rclones utgångskod. En uppladdning
# som avbryts halvvägs kan lämna en kortare fil, och ett skript som bara
# säger "klart" har då ljugit om precis det som betyder något.
NYAST="$(ls -1 "$KALLA"/training-*.db | tail -1)"
NAMN="$(basename "$NYAST")"
LOKAL_STORLEK="$(stat -c %s "$NYAST")"
FJARR_STORLEK="$(rclone lsjson "$MAL/$NAMN" 2>/dev/null | sed -n 's/.*"Size":\([0-9]*\).*/\1/p' | head -1)"

if [ "${FJARR_STORLEK:-0}" != "$LOKAL_STORLEK" ]; then
    echo "Uppladdningen stämmer inte: $NAMN är $LOKAL_STORLEK byte lokalt," \
         "men ${FJARR_STORLEK:-saknas} hos $MAL." >&2
    exit 1
fi

# Är nyaste filen färsk? Utan den frågan laddas gårdagens kopia upp
# troget varje natt med texten "Uppladdat och verifierat" — och en
# backup som slutat skrivas syns först den dag den behövs.
#
# Gränsen går vid I FÖRRGÅR och inte vid idag, med flit: en enstaka
# missad natt (Pi:n avstängd, strömavbrott, en omstart som råkade ligga
# över 03:30) är inget haveri, och ett larm som ljuder för den slutar
# man läsa. Två uteblivna nätter i rad är däremot ingen slump.
GRANS="training-$(date -d '1 day ago' +%F).db"
if [ "$NAMN" \< "$GRANS" ]; then
    echo "VARNING: nyaste backupen är $NAMN — äldre än gårdagens." \
         "Uppladdningen gjordes ändå, men den nattliga backupen ser ut" \
         "att ha slutat skrivas. Kontrollera BACKUP_TIME i .env och" \
         "journalctl -u garmin-ai-pi | grep -i backup." >&2
    exit 2
fi

echo "Uppladdat och verifierat: $NAMN ($LOKAL_STORLEK byte) -> $MAL"
