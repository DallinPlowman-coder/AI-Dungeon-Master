"use strict";

let diceBusy = false, diceCreating = false, diceActive = null, diceResultIndex = new Map();
let freeDiceAttempt = null, diceRequestDraft = null;
const diceFormula = (modifier) => `1d20${modifier ? ` ${modifier > 0 ? "+" : "−"} ${Math.abs(modifier)}` : ""}`;

function renderDice() {
  if (!current) return;
  const rolls = current.rolls || [], pending = rolls.filter((roll) => roll.status === "pending");
  diceResultIndex = new Map(rolls.filter((roll) => roll.result).map((roll) => [roll.result.event_id, roll]));
  q("#roll-prompts").hidden = !pending.length;
  q(".story-stage").classList.toggle("has-roll-requests", Boolean(pending.length));
  // Preserve focused buttons while polling an unchanged request.
  const signature = JSON.stringify(pending.map((roll) => [roll.id, roll.label, roll.reason, roll.modifier]));
  if (q("#roll-prompts").dataset.signature !== signature) {
    q("#roll-prompts").dataset.signature = signature;
    q("#roll-prompts").innerHTML = pending.map((roll) => `<article class="roll-prompt"><div><span class="section-index">YOUR ROLL</span><h2>${esc(roll.label)}</h2>${roll.reason ? `<p>${esc(roll.reason)}</p>` : ""}<span class="roll-formula">${diceFormula(roll.modifier)}</span></div><button type="button" class="primary-button dice-roll-button" data-roll-id="${roll.id}" aria-label="Roll d20 for ${esc(roll.label)}">Roll d20 <span aria-hidden="true">◇</span></button></article>`).join("");
  }
  q("#quick-d20").disabled = diceBusy || diceCreating;
  q("#quick-d20").title = pending.length ? "Roll the next requested check" : "Make a free d20 roll";
  q("#quick-d20").setAttribute("aria-label", pending.length ? `Roll d20 for ${pending[0].label}` : "Roll a free d20");
  document.querySelectorAll(".dice-roll-button").forEach((button) => button.disabled = diceBusy || diceCreating);
}

function renderDiceEvent(event) {
  const roll = diceResultIndex.get(event.id);
  if (!roll) return "";
  return `<div class="saved-dice-result"><span class="saved-die-face">${roll.result.value}</span><span>${diceFormula(roll.modifier)}<strong>Total ${roll.result.total}</strong></span><button type="button" data-view-roll="${roll.id}">View roll ↗</button></div>`;
}

async function diceWrite(url, payload) {
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 15000);
  try { return await request(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload), signal:controller.signal}); }
  finally { clearTimeout(timeout); }
}

function prepareDiceDialog(roll) {
  q("#dice-title").textContent = roll.label;
  q("#dice-reason").textContent = roll.reason;
  q("#dice-error").hidden = true;
  q("#retry-dice").hidden = true;
  q("#dice-stage").classList.remove("natural-twenty");
  if (!q("#dice-dialog").open) q("#dice-dialog").showModal();
}

function showDiceResult(roll) {
  q("#dice-stage").classList.remove("rolling");
  q("#dice-stage").classList.toggle("natural-twenty", roll.result.value === 20);
  q("#dice-face").textContent = roll.result.value;
  const natural = roll.result.value === 20 ? "Natural 20" : roll.result.value === 1 ? "Natural 1" : `You rolled ${roll.result.value}`;
  q("#dice-result").textContent = natural + (roll.modifier ? ` · Total ${roll.result.total}` : "");
  q("#dice-calculation").textContent = roll.modifier ? `${roll.result.value} ${roll.modifier > 0 ? "+" : "−"} ${Math.abs(roll.modifier)} = ${roll.result.total}` : "1d20 · Saved in your campaign log";
  q("#retry-dice").hidden = true;
  q("#dice-error").hidden = true;
}

async function pressD20(roll, campaignId = current.id) {
  if (diceBusy || diceCreating) return;
  diceBusy = true;
  diceActive = {roll, campaignId};
  prepareDiceDialog(roll);
  q("#dice-face").textContent = "?";
  q("#dice-result").textContent = "Rolling…";
  q("#dice-calculation").textContent = diceFormula(roll.modifier);
  q("#dice-stage").classList.add("rolling");
  renderDice();
  try {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const [data] = await Promise.all([diceWrite(`/api/campaigns/${campaignId}/rolls/${roll.id}/roll`, {}),
      reduced ? Promise.resolve() : new Promise((resolve) => setTimeout(resolve, 1000))]);
    diceActive.roll = data.roll;
    showDiceResult(data.roll);
    if (current?.id === campaignId) {
      try { await loadCampaign(campaignId); }
      catch { announce("Your roll was saved. Refresh the campaign to see its log entry.", true); }
    }
  } catch (error) {
    q("#dice-stage").classList.remove("rolling");
    q("#dice-face").textContent = "—";
    q("#dice-result").textContent = "The result could not be retrieved.";
    q("#dice-error").textContent = "Retry this same roll to retrieve its saved result. It will not roll a second time if already saved.";
    q("#dice-error").hidden = false;
    q("#retry-dice").hidden = false;
  } finally { diceBusy = false; renderDice(); }
}

q("#roll-prompts").addEventListener("click", (event) => {
  const button = event.target.closest("[data-roll-id]");
  const roll = current?.rolls?.find((item) => item.id === button?.dataset.rollId);
  if (roll) pressD20(roll);
});
q("#quick-d20").addEventListener("click", async () => {
  if (!current || diceBusy || diceCreating) return;
  const pending = current.rolls?.find((roll) => roll.status === "pending");
  if (pending) return pressD20(pending);
  const campaignId = current.id;
  if (freeDiceAttempt?.campaignId !== campaignId) freeDiceAttempt = {campaignId, key:crypto.randomUUID()};
  diceCreating = true; renderDice();
  let created;
  try {
    created = await diceWrite(`/api/campaigns/${campaignId}/rolls`, {label:"Free d20 roll", reason:"A player-initiated d20 roll.", idempotency_key:freeDiceAttempt.key});
    freeDiceAttempt = null;
  } catch (error) { announce("The roll could not be requested. Try again when the local app is available.", true); }
  finally { diceCreating = false; renderDice(); }
  if (created && current?.id === campaignId) await pressD20(created.roll, campaignId);
});
q("#retry-dice").addEventListener("click", () => { if (diceActive) pressD20(diceActive.roll, diceActive.campaignId); });
q("#close-dice").addEventListener("click", () => q("#dice-dialog").close());
q("#done-dice").addEventListener("click", () => q("#dice-dialog").close());
q("#story-feed").addEventListener("click", (event) => {
  const button = event.target.closest("[data-view-roll]");
  const roll = current?.rolls?.find((item) => item.id === button?.dataset.viewRoll);
  if (!roll?.result || diceBusy || diceCreating) return;
  prepareDiceDialog(roll); showDiceResult(roll);
});

q("#event-form").closest("details").insertAdjacentHTML("beforebegin", `<details class="record-panel"><summary>Request a player d20 roll</summary><form id="request-roll-form" class="record-form"><p class="form-help muted">The player sees this request at the table and presses the button to roll. Use only details they should know.</p><div class="form-grid"><label>Roll label<input name="label" required maxlength="120" placeholder="Dexterity check"></label><label>Modifier<input name="modifier" type="number" min="-30" max="30" value="0" required></label></div><label>Why is a roll needed?<textarea name="reason" maxlength="1200" rows="2" placeholder="Keep your balance while crossing the narrow bridge."></textarea></label><button type="submit" class="primary-button">Request roll</button></form></details>`);
q("#request-roll-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target), button = event.submitter;
  const payload = {label:String(form.get("label")).trim(), reason:String(form.get("reason")).trim(), modifier:Number(form.get("modifier"))};
  const campaignId = current.id;
  const signature = JSON.stringify([current.id, payload]);
  if (diceRequestDraft?.signature !== signature) diceRequestDraft = {signature, key:crypto.randomUUID()};
  button.disabled = true;
  try {
    await diceWrite(`/api/campaigns/${campaignId}/rolls`, {...payload, idempotency_key:diceRequestDraft.key});
    diceRequestDraft = null; event.target.reset();
    if (current?.id === campaignId) {
      await loadCampaign(campaignId);
      q("#records-dialog").close();
      announce("Roll requested. Press its d20 button when you are ready.");
    }
  } catch (error) { announce(error.message, true); }
  finally { button.disabled = false; }
});
if (current) { tableFeedKey = null; renderTable(); }
