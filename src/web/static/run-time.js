/* Klockslaget under en analysrubrik — delat mellan dashboarden och
   aktivitetssidan.

   Analysens rubrik ("# Söndag 30 augusti 2026") säger vilken DAG den
   gäller, aldrig när den skrevs. Skillnaden är inte akademisk: genererar
   man om kvällssammanfattningen strax efter midnatt får den rubriken för
   den nya dagen och rapporterar varken steg eller träning, eftersom
   dygnet är sexton minuter gammalt. Det ser ut som att datan saknas.
   Klockslaget under rubriken gör tidpunkten synlig i stället för något
   man får gissa.

   Låg tidigare bara inline i index.html. Aktivitetssidan skrev ut sin
   egen variant server-side i stället, med både annan ordning (klockslag
   FÖRE rubriken) och annat format ("Kördes 2026-08-27 20:37" mot
   dashboardens "Kördes 27 aug 20:37"). Två sidor, två svar på samma
   fråga. Nu en. */

const MONTHS_SHORT = ['jan', 'feb', 'mar', 'apr', 'maj', 'jun',
                      'jul', 'aug', 'sep', 'okt', 'nov', 'dec'];

/* contextDay är dagen analysen GÄLLER (YYYY-MM-DD) — ref_id från API:et,
   eller passets datum på aktivitetssidan. Kördes analysen samma dag som
   den handlar om räcker klockslaget, för rubriken direkt ovanför säger
   redan vilken dag det är; datumet skrivs bara ut när de två skiljer sig.

   Jämförde tidigare mot IDAG i stället för mot analysens egen dag. Det
   gav rätt svar så länge man tittade på dagens analys, men fel så fort
   man inte gjorde det: kvällssammanfattningen för i lördags kördes
   23:59 samma kväll den handlar om, och fick ändå "Kördes 29 aug 23:59"
   under en rubrik som redan stod på "Lördag 29 augusti 2026". */
function runTimeLabel(iso, contextDay) {
  if (!iso) return '';
  // Tidsstämpeln skrivs som lokal tid utan zon (datetime.now().isoformat()),
  // och tolkas därför som lokal tid av Date — samma tidszon som Pi:n.
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  const clock = String(d.getHours()).padStart(2, '0') + ':' +
                String(d.getMinutes()).padStart(2, '0');
  // Lokalt datum ur tidsstämpeln, som YYYY-MM-DD. Inte toISOString() —
  // den går via UTC och skulle kunna landa på fel dygn.
  const runDay = d.getFullYear() + '-' +
                 String(d.getMonth() + 1).padStart(2, '0') + '-' +
                 String(d.getDate()).padStart(2, '0');
  // Utan känd kontextdag faller vi tillbaka på idag, så en anropare som
  // inte skickar med den beter sig som förut.
  const referens = contextDay || new Date().toLocaleDateString('sv-SE');
  if (runDay === referens) return 'Kördes ' + clock;
  return 'Kördes ' + d.getDate() + ' ' + MONTHS_SHORT[d.getMonth()] + ' ' + clock;
}

/* Lägger klockslaget direkt efter analysens egen rubrik, så flödet blir
   dag och datum -> när den kördes -> knappen. */
function insertRunTime(container, iso, contextDay) {
  const label = runTimeLabel(iso, contextDay);
  if (!label) return;
  const note = document.createElement('p');
  note.className = 'run-time';
  note.textContent = label;   // textContent, aldrig innerHTML
  const heading = container.querySelector('h1, h2, h3, h4, h5, h6');
  if (heading) heading.insertAdjacentElement('afterend', note);
  else container.prepend(note);
}
