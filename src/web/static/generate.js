/* "Generera om" och "Generera analys" — delat mellan dashboarden och
   aktivitetssidan.

   Knapparna postar ett vanligt formulär, och servern svarar först när
   Claude skrivit klart: 20-60 sekunder där ingenting på sidan ändrades.
   Ett andra tryck under tiden startade ett andra Claude-anrop, och gick
   man tillbaka och försökte igen blev det ett tredje.

   Nu låses knappen vid första trycket och säger vad som pågår. Formuläret
   skickas som förut — det här skriptet stoppar bara upprepningar. */

function lockGenerateButtons() {
  document.querySelectorAll('form.generate-form').forEach((form) => {
    const button = form.querySelector('button[type="submit"]');
    if (!button) return;
    const original = button.textContent;

    form.addEventListener('submit', (e) => {
      if (form.dataset.busy) {
        e.preventDefault();
        return;
      }
      form.dataset.busy = '1';
      // Att låsa knappen i submit-händelsen stoppar inte inskickningen:
      // den är redan igång när händelsen körs.
      button.disabled = true;
      button.textContent = 'Genererar…';
    });

    // Tillbaka-knappen kan visa sidan ur webbläsarens cache, i det skick
    // den lämnades — med knappen fortfarande låst. Lås upp den då.
    window.addEventListener('pageshow', (e) => {
      if (!e.persisted) return;
      delete form.dataset.busy;
      button.disabled = false;
      button.textContent = original;
    });
  });
}

/* Misslyckades det kommer man tillbaka med ?fel=<block> i adressen, och
   sidan visar ett felbesked i blocket (se _analysis_failed i web/app.py).
   Beskedet gäller det försöket, inte varje senare omladdning — så
   parametern tas bort ur adressen när sidan väl visat det. Ankaret står
   kvar. */
function forgetGenerateError() {
  const url = new URL(window.location.href);
  if (!url.searchParams.has('fel')) return;
  url.searchParams.delete('fel');
  window.history.replaceState(null, '', url);
}

lockGenerateButtons();
forgetGenerateError();
