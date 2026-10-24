# Tredjepartsbibliotek

Filerna här laddades tidigare från `cdn.jsdelivr.net` vid varje sidvisning.
De ligger nu i repot av tre skäl:

1. **Appen fungerar utan internet.** Pi:n kan nå Intervals och Anthropic när
   den synkar, men dashboarden ska gå att öppna även när den inte kan nå
   ett CDN. Utan marked visas alla analyser och chattsvar som rå markdown.
2. **Versionerna är låsta.** `marked` hämtades tidigare från en opinnad
   URL (`npm/marked/marked.min.js`), alltså alltid senaste utgåvan — en
   bakåtinkompatibel release hade kunnat bryta sidan utan att något i
   repot ändrats.
3. **Inga externa anrop från webbläsaren.**

Båda är MIT-licensierade och deras licenshuvuden är kvar orörda i filerna.

| Fil | Bibliotek | Version | Laddas av | Källa |
|---|---|---|---|---|
| `marked.min.js` | marked | 15.0.7 | dashboarden, aktivitetssidan | https://cdn.jsdelivr.net/npm/marked@15.0.7/marked.min.js |

## Chart.js låg här tidigare

`chart.umd.min.js` (Chart.js 4.4.3, 205 kB) är borttagen. Belastnings-
diagrammet ritas som inline-SVG i `renderBand()` (`templates/index.html`)
och styrkevolymen på samma sätt i `renderStrength()`. Båda ritas i
containerns faktiska pixlar, vilket ett bibliotek med fast viewBox inte
gjorde läsbart på en telefon.

Filen laddades inte av någon mall efter det, men låg kvar i repot och
följde med varje klon. Behöver den någon gång tillbaka:

```bash
curl -sL -o src/web/static/vendor/chart.umd.min.js "https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"
```

## Uppdatera

```bash
curl -sL -o src/web/static/vendor/marked.min.js \
  "https://cdn.jsdelivr.net/npm/marked@<version>/marked.min.js"
```

Uppdatera tabellen ovan, och kontrollera sedan dashboarden i webbläsaren:
analysrutorna ska visa rubriker och punktlistor, inte rå markdown.
Kontrollera också att markdown-saneringen fortfarande håller — se
kommentarerna i `static/markdown.js` om varför den finns.
