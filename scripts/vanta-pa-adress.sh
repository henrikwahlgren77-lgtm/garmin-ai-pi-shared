#!/usr/bin/env bash
#
# Väntar tills WEB_HOST finns på ett nätverkskort. Körs av
# garmin-ai-pi.service som ExecStartPre.
#
# Med WEB_HOST satt till Tailscale-adressen kan webbservern inte starta
# förrän tailscaled har kopplat upp och fått sin adress. Vid en omstart
# av Pi:n startar tjänsten ofta före det: uvicorn får "Cannot assign
# requested address", systemd försöker igen var femte sekund, och efter
# fem försök (StartLimitBurst) ger det upp. Webbservern blir då stående
# tills någon startar den för hand, trots att Tailscale var uppe en halv
# minut senare.
#
# En adress som alltid finns (0.0.0.0, ::, 127.x, localhost) eller ett
# värdnamn släpps igenom direkt. Bara en IP-adress väntas in.
set -u

ADRESS="${WEB_HOST:-0.0.0.0}"
GRANS="${VANTA_SEKUNDER:-80}"

case "$ADRESS" in
    0.0.0.0|::|127.*|localhost|::1) exit 0 ;;
esac
# Bara IPv4- och IPv6-literaler; ett värdnamn löser uvicorn själv.
if ! printf '%s' "$ADRESS" | grep -qE '^[0-9.]+$|^[0-9a-fA-F:]+$'; then
    exit 0
fi

for ((i = 0; i < GRANS; i++)); do
    if ip -o addr show | grep -qF " $ADRESS/"; then
        [ "$i" -gt 0 ] && echo "$ADRESS fanns efter $i sekunder."
        exit 0
    fi
    sleep 1
done

echo "WEB_HOST=$ADRESS finns inte på något nätverkskort efter $GRANS sekunder." \
     "Är Tailscale uppe? Kontrollera: ip -o addr, systemctl status tailscaled" >&2
exit 1
