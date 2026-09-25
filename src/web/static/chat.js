/* Chattlogiken för dashboardens chatt (index.html).

   Den låg här för att delas med en fristående chattsida, som hade en
   nästan identisk kopia. Den sidan är borttagen; uppdelningen håller
   logiken utanför templaten och får stå kvar.

   Sidan äger markupen och anropar sedan initChat(). Inloggningen är
   kakan, som fetch skickar av sig själv. */

function initChat() {
  const msgsEl = document.getElementById("chat-messages");
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  // Sidor utan chatt anropar aldrig hit, men var defensiv ändå.
  if (!msgsEl || !form || !input) {
    return;
  }

  // Hette `history` i båda de tidigare kopiorna, deklarerad med let på
  // skriptets toppnivå — vilket skuggade window.history i hela filen.
  const conversation = [];

  // Tråden rullar inte i en egen ruta längre (se .chat-thread i
  // style.css), så sidan rullas till det nya i stället. Ett svar som är
  // högre än skärmen visas från början — annars hamnar man i slutet av
  // svaret och får rulla upp för att läsa det. Allt annat: se till att
  // inmatningsraden, och därmed det senaste meddelandet ovanför den,
  // syns.
  function reveal(el) {
    if (el && el.getBoundingClientRect().height > window.innerHeight * 0.7) {
      el.scrollIntoView({ block: "start" });
    } else {
      form.scrollIntoView({ block: "nearest" });
    }
  }

  function addMessage(role, text) {
    const div = document.createElement("div");
    div.className = "chat-bubble " + (role === "user" ? "user" : "bot");
    if (role === "user") {
      // Egen text ska aldrig tolkas som markdown eller HTML.
      div.textContent = text;
    } else {
      // Claude svarar i Markdown. Tidigare kördes bara en regex för
      // **fetstil** och radbrytningar, så punktlistor och rubriker
      // visades som rå text ("- ", "#") mitt i bubblan.
      //
      // renderMarkdownInto (static/markdown.js) parsar med marked och
      // sanerar resultatet. Den anropades tidigare som marked.parse()
      // rakt in i innerHTML, och marked v15 har inget sanitize-läge —
      // rå HTML i svaret kördes alltså av webbläsaren. Funktionen
      // hanterar också fallet att marked saknas.
      // breaks: true bevarar enkla radbrytningar, som den gamla
      // \n -> <br>-ersättningen gjorde.
      renderMarkdownInto(div, text, { breaks: true });
    }
    msgsEl.appendChild(div);
    reveal(div);
    return div;
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = input.value.trim();
    if (!msg) {
      return;
    }
    addMessage("user", msg);
    // Historiken som skickas är samtalet FÖRE det här meddelandet.
    // Servern lägger på det själv (messages = history + [user_msg] i
    // /chat), så när det pushades hit före anropet skickades det två
    // gånger i rad till Claude — varje tur, hela samtalet igenom.
    // Uppmätt på det som nådde Anthropic: [user:"hej", user:"hej"].
    const historyBefore = conversation.slice();
    conversation.push({ role: "user", content: msg });
    input.value = "";
    input.disabled = true;

    const typing = document.createElement("div");
    typing.className = "chat-bubble bot chat-typing";
    typing.textContent = "Skriver…";
    msgsEl.appendChild(typing);
    reveal(typing);

    try {
      const resp = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: msg, history: historyBefore }),
      });
      typing.remove();
      if (!resp.ok) {
        // Servern förklarar själv i "detail" — "Tomt meddelande", att
        // meddelandet är för långt, eller att chatten misslyckades och var
        // felet finns. Här stod "⚠️ Något gick fel (HTTP 413)", och en
        // statuskod säger ingenting för den som skrev meddelandet.
        let detail = "";
        try {
          detail = (await resp.json()).detail;
        } catch (e) {
          /* svaret var inte JSON */
        }
        addMessage("bot", typeof detail === "string" && detail
          ? detail
          : "Coachen svarade inte. Försök igen om en stund.");
        return;
      }
      const data = await resp.json();
      addMessage("bot", data.reply);
      conversation.push({ role: "assistant", content: data.reply });
    } catch (err) {
      typing.remove();
      console.error("Chatten:", err);
      addMessage("bot", "Kunde inte nå servern. Kontrollera anslutningen " +
        "och försök igen.");
    } finally {
      input.disabled = false;
      // preventScroll: fokus får inte rulla bort början av ett långt svar,
      // som reveal() nyss rullade fram.
      input.focus({ preventScroll: true });
    }
  });
}
