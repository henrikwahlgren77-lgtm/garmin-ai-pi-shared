/* Delad markdown-rendering med sanering.

   Både dashboardens analysrutor och chattbubblorna körde tidigare
   `el.innerHTML = marked.parse(text)` rakt av. Den bundlade marked (v15)
   har inget sanitize-läge kvar — det togs bort i v8 — så all rå HTML i
   markdownen passerade igenom och kördes av webbläsaren.

   Texten kommer förvisso från Claude och inte från en angripare, men den
   är byggd av data vi inte äger: passnamn från Intervals.icu, och det du
   själv skriver i chatten. Ett passnamn som innehåller en <img>-tagg med
   onerror räckte för att köra kod på dashboarden (reproducerat i
   webbläsaren mot skarp data).

   Saneringen görs på det parsade DOM-trädet i stället för med en regex på
   markdownen, och i stället för att haka i marked:s renderer: den vägen
   är oberoende av vilken markdown-parser vi råkar använda, och kan inte
   missa något parsern hittat på i ett hörn av sitt eget format. */

/* Taggar som får finnas kvar. Allt marked kan producera från ren
   markdown, inget mer. Okända taggar packas upp (texten behålls, taggen
   försvinner) — utom de i DROP_ENTIRELY nedan, där själva innehållet är
   nyttolasten och inte något man vill se som text. */
const MD_ALLOWED_TAGS = new Set([
  "P", "BR", "HR", "SPAN", "DIV",
  "STRONG", "B", "EM", "I", "U", "S", "DEL", "INS", "SUP", "SUB", "MARK",
  "H1", "H2", "H3", "H4", "H5", "H6",
  "UL", "OL", "LI", "DL", "DT", "DD",
  "BLOCKQUOTE", "PRE", "CODE",
  "TABLE", "THEAD", "TBODY", "TFOOT", "TR", "TH", "TD",
  "A",
]);

const MD_DROP_ENTIRELY = new Set([
  "SCRIPT", "STYLE", "IFRAME", "OBJECT", "EMBED", "TEMPLATE", "NOSCRIPT",
  "IMG", "VIDEO", "AUDIO", "SOURCE", "SVG", "MATH", "LINK", "META", "BASE",
  "FORM", "INPUT", "BUTTON", "SELECT", "TEXTAREA",
]);

/* Attribut per tagg. Allt annat strippas — särskilt varenda on*-hanterare,
   som är den vanligaste vägen in. */
const MD_ALLOWED_ATTRS = {
  A: ["href", "title"],
  TH: ["colspan", "rowspan"],
  TD: ["colspan", "rowspan"],
};

/* Bara länkar man kan följa utan att något körs. javascript:, data: och
   vbscript: kan alla köra kod från ett href. */
const MD_SAFE_URL = /^(https?:|mailto:|#|\/)/i;

/* Escapar text för att kunna läggas in i HTML — även i ETT ATTRIBUT.

   textContent -> innerHTML escapar &, < och >, men INTE " eller '. Det
   räcker för text mellan taggar, och funktionen användes länge bara så.
   Sedan började index.html bygga attribut med den (href och aria-label
   på styrkediagrammets stapellänkar), och där är ett oescapat citattecken
   precis vägen ut ur attributet och in i en ny on*-hanterare.

   Ingenting som passerar hit idag kan innehålla ett citattecken —
   etiketterna byggs av ett datum och ett tal — men den här filen finns
   för att appen redan blivit biten två gånger av HTML-injektion, båda
   gångerna via data vi inte äger (passnamn från Intervals, anteckningar
   från chatten). En escape-funktion som inte håller i attributläge är en
   fälla som väntar på nästa fält någon stoppar in där. */
function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function sanitizeMarkdownDom(root) {
  // Gå igenom bakifrån: vi ändrar trädet under tiden, och en bakåtgående
  // ordning gör att en nod vi packar upp inte flyttar noder vi redan
  // hunnit förbi.
  const elements = Array.prototype.slice.call(root.querySelectorAll("*")).reverse();
  for (const el of elements) {
    const tag = el.tagName.toUpperCase();

    if (MD_DROP_ENTIRELY.has(tag)) {
      el.remove();
      continue;
    }

    if (!MD_ALLOWED_TAGS.has(tag)) {
      // Packa upp: behåll barnen (texten), kasta taggen.
      el.replaceWith(...el.childNodes);
      continue;
    }

    const allowed = MD_ALLOWED_ATTRS[tag] || [];
    for (const attr of Array.prototype.slice.call(el.attributes)) {
      if (!allowed.includes(attr.name.toLowerCase())) {
        el.removeAttribute(attr.name);
        continue;
      }
      if (attr.name.toLowerCase() === "href" && !MD_SAFE_URL.test(attr.value.trim())) {
        el.removeAttribute(attr.name);
      }
    }
  }
  return root;
}

/* Renderar markdown in i ett element. Enda vägen till innerHTML för
   modellgenererad text i appen — anropa den här i stället för
   marked.parse() direkt.

   Parsningen sker i ett INERT dokument via DOMParser, inte genom att
   sätta innerHTML på måltaggen och sanera efteråt. Skillnaden är inte
   akademisk: sätter man innerHTML på ett element som redan sitter i
   sidan börjar webbläsaren hämta resurser omedelbart, så ett
   <img src=x onerror=...> hinner köra sin hanterare INNAN saneringen
   ens har börjat. Första versionen av den här funktionen gjorde precis
   det, och nyttolasten gick igenom — upptäckt genom att provköra den i
   webbläsaren, inte genom att läsa koden.

   DOMParser bygger ett dokument utan browsing context: där hämtas inga
   bilder och körs inga skript. Först efter saneringen flyttas noderna
   in i sidan. */
function renderMarkdownInto(el, text, options) {
  const source = text == null ? "" : String(text);
  if (typeof marked === "undefined") {
    // Utan marked (t.ex. om vendorfilen inte laddats) blir det åtminstone
    // läsbar text med radbrytningar kvar, aldrig tolkad HTML.
    el.innerHTML = escapeHtml(source).replace(/\n/g, "<br>");
    return el;
  }

  const parsed = new DOMParser().parseFromString(
    "<body>" + marked.parse(source, options) + "</body>",
    "text/html",
  );
  sanitizeMarkdownDom(parsed.body);

  el.replaceChildren(...document.adoptNode(parsed.body).childNodes);
  return el;
}

/* Analyserna inleds med "# Torsdag 27 augusti 2026", som marked renderar
   som <h1>. Sidan har redan en h1 — hälsningen på dashboarden, passnamnet
   på aktivitetssidan — så det blev två, och analysens egna h2:or hamnade
   på samma nivå som sidans avsnittsrubriker ("Nyckeltal", "Passdetaljer").
   Analysens titel är ett avsnitt PÅ sidan, inte sidans titel.

   Görs efter parsning i stället för med en regex på markdownen, så det
   inte råkar träffa ett "#" mitt i en textrad.

   Låg tidigare inline i index.html medan aktivitetssidan fick samma
   nedtrappning server-side via markdown-bibliotekets toc-tillägg
   (baselevel=2). Två implementationer av samma regel; den här är den
   enda kvar.

   TVÅ steg, inte ett: analysen står alltid i ett kort som har en egen h2
   ("Morgonanalys", "AI-analys"). Ett steg gav analysens datumrubrik h2
   också — en rubrik på samma nivå som kortet den står i. Nu h3 för
   titeln och h4 för avsnitten. */
function demoteHeadings(container) {
  for (const level of [4, 3, 2, 1]) {
    container.querySelectorAll("h" + level).forEach((old) => {
      const next = document.createElement("h" + Math.min(level + 2, 6));
      next.innerHTML = old.innerHTML;
      old.replaceWith(next);
    });
  }
}

/* Renderar en analys ur ett element som håller markdownen som TEXT.

   Aktivitetssidan renderas server-side och lägger analysens markdown i
   elementet som textinnehåll (Jinja escapar det), i stället för som
   färdig HTML. Skillnaden är hela poängen: markdownen är byggd av data vi
   inte äger — passnamn från Intervals, notes från chattloggade set — och
   servern hade ingen sanering alls. Ett passnamn med <img onerror=...>
   som Claude ekade i analysen kördes alltså av webbläsaren, precis den
   lucka som stängdes på dashboarden men aldrig här.

   Nu går båda sidorna genom renderMarkdownInto ovan. */
function renderAnalysisElement(el) {
  if (!el) return;
  const source = el.textContent;
  if (!source || !source.trim()) return;
  renderMarkdownInto(el, source);
  demoteHeadings(el);
}
