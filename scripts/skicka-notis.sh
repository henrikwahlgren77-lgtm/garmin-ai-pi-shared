#!/usr/bin/env bash
#
# Skickar en ntfy-notis om att en systemd-tjänst har fallerat. Körs av
# garmin-ai-pi-notis@.service, som tjänsterna pekar ut med OnFailure=.
#
# Täcker det appen inte kan rapportera själv: att webbservern inte går
# att starta alls (då finns ingen process som kan skicka något), och att
# uppladdningen till molnet misslyckas — den körs som ett eget skript.
# Jobben INUTI appen (synk, analyser, backup) rapporterar via alerts.py.
#
# Användning: skicka-notis.sh <enhetsnamn>
set -euo pipefail

ROT="$(cd "$(dirname "$0")/.." && pwd)"
ENHET="${1:?ange enhetens namn}"

# Samma inläsning som i ladda-upp-backup.sh: en nyckel i taget, utan att
# köra .env som ett skalskript.
las_env() {
    [ -f "$ROT/.env" ] || return 0
    sed -n "s/^[[:space:]]*$1=//p" "$ROT/.env" | tail -1 | sed 's/[[:space:]]*$//'
}

URL="${NTFY_URL:-$(las_env NTFY_URL)}"
if [ -z "$URL" ]; then
    echo "NTFY_URL är inte satt i .env, ingen notis skickas." >&2
    exit 0
fi

case "$ENHET" in
    garmin-ai-pi.service)
        RUBRIK="Webbservern har stannat"
        TEXT="Den startar inte om av sig själv. Detaljer: journalctl -u $ENHET" ;;
    garmin-ai-pi-backup-upload.service)
        RUBRIK="Backupen i molnet är inte aktuell"
        TEXT="Uppladdningen misslyckades eller nattens backup saknas. Detaljer: journalctl -u $ENHET" ;;
    *)
        RUBRIK="$ENHET har fallerat"
        TEXT="Detaljer: journalctl -u $ENHET" ;;
esac

# JSON till serverns rot, inte rå text till ämnet: rubriken bär å och ä,
# som inte får stå i ett HTTP-huvud. Texterna ovan innehåller inga
# citattecken eller bakstreck, så de kan läggas in som de är.
SERVER="${URL%/*}/"
AMNE="${URL##*/}"
curl -fsS --max-time 15 -H "Content-Type: application/json" \
    -d "{\"topic\":\"$AMNE\",\"title\":\"$RUBRIK\",\"message\":\"$TEXT\",\"priority\":4,\"tags\":[\"warning\"]}" \
    "$SERVER" >/dev/null
echo "Notis skickad: $RUBRIK"
