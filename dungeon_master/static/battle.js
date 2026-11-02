"use strict";

let battleSelected = null, battleCampaign = null, battleBusy = false, battleRenderKey = null;
const battleHandlers = {
  select:(id) => { battleSelected = id; renderBattle(true); },
  move:(id,x,y) => changeBattle({move_tokens:[{id,x,y}]}, "Position saved."),
  feedback:(message) => battleFeedback(message),
};
const battleView = new BattleMapView(q("#battle-board"), battleHandlers);
const expandedBattleView = new BattleMapView(q("#expanded-battle-board"), battleHandlers);

function battleFeedback(message, error = false) {
  for (const id of ["#battle-feedback", "#expanded-battle-feedback", "#battle-setup-feedback"]) {
    q(id).textContent = message;
    q(id).classList.toggle("battle-error", error);
  }
}

function renderBattle(force = false) {
  if (!current) return;
  const battle = current.battle || {state:null, revision:0}, state = battle.state;
  const fresh = battleCampaign !== current.id;
  if (fresh) { battleCampaign = current.id; battleSelected = null; battleRenderKey = null; }
  if (battleSelected && !state?.tokens.some((token) => token.id === battleSelected)) battleSelected = null;
  const key = JSON.stringify([battle, battleSelected, battleBusy]);
  if (!force && key === battleRenderKey) return;
  const firstMap = !battleView.state && Boolean(state);
  battleRenderKey = key;
  q("#battle-empty").hidden = Boolean(state);
  q("#battle-content").hidden = !state;
  q("#battle-manage").hidden = !state;
  q("#battle-create-button").textContent = state ? "Replace encounter with this layout" : "Create encounter";
  q("#battle-quick-start").disabled = battleBusy;
  battleView.render(state, battleSelected, battleBusy);
  expandedBattleView.render(state, battleSelected, battleBusy);
  if (!state) return;
  q("#battle-title").textContent = state.title;
  q("#map-dialog-title").textContent = state.title;
  const active = state.tokens.find((token) => token.id === state.combat.active_token_id);
  q("#battle-combat").textContent = state.combat.active ? `Round ${state.combat.round}${active ? ` · ${active.name}'s turn` : ""}` : "Positioning · combat inactive";
  const token = state.tokens.find((item) => item.id === battleSelected);
  const info = token ? `<strong>${esc(token.name)}</strong><span class="selected-faction">${title(token.faction)}</span><span>Column ${token.x + 1}, row ${token.y + 1}</span>${token.hp_current !== null ? `<span>HP ${token.hp_current}${token.hp_max !== null ? ` / ${token.hp_max}` : ""}</span>` : ""}${token.conditions.length ? `<span>${esc(token.conditions.join(", "))}</span>` : ""}${token.movement_remaining !== null ? `<span>${token.movement_remaining} ft movement remaining</span>` : ""}${token.notes ? `<span>${esc(token.notes)}</span>` : ""}` : "Select a token for its full name and details. Drag to move, or click a square after selecting.";
  q("#battle-selection").innerHTML = info;
  q("#expanded-battle-selection").innerHTML = info;
  q("#battle-remove-token").disabled = !token || battleBusy;
  q("#battle-toggle-combat").textContent = state.combat.active ? "End combat" : "Start combat";
  for (const axis of ["x", "y"]) q(`#battle-token-form [name=${axis}]`).max = axis === "x" ? state.map.width : state.map.height;
  q("#battle-door-controls").innerHTML = state.map.doors.map((door) => `<button type="button" class="secondary-button" data-door-id="${door.id}">${door.open ? "Close" : "Open"} ${esc(door.id)}</button>`).join("");
  if (firstMap || fresh) battleView.fit();
}

async function changeBattle(edits, success, method = "PATCH", starter = false) {
  if (!current || battleBusy) return false;
  const campaignId = current.id, expected_revision = current.battle?.revision || 0;
  battleBusy = true; renderBattle(true);
  try {
    const response = await fetch(`/api/campaigns/${campaignId}/battle${starter ? "/starter" : ""}`, {
      method, headers:{"Content-Type":"application/json"}, body:JSON.stringify({expected_revision, ...edits}),
    });
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 409 && current?.id === campaignId) await loadCampaign(campaignId);
      throw new Error(Array.isArray(data.detail) ? data.detail.map((item) => item.msg).join(" ") : data.detail || "The map could not be saved.");
    }
    if (current?.id === campaignId) {
      current.battle = data;
      renderBattle(true);
      battleFeedback(success);
    }
    return true;
  } catch (error) { battleFeedback(error.message || "The map could not be saved. Try again when the app is available.", true); return false; }
  finally { battleBusy = false; renderBattle(true); }
}

function openBattleSetup() {
  renderBattle(true);
  const state = current?.battle?.state;
  if (state) {
    const blocked = new Set([...state.tokens.map((token) => `${token.x},${token.y}`), ...state.map.terrain.filter((tile) => tile.kind === "obstacle").map((tile) => `${tile.x},${tile.y}`)]);
    let free = null;
    for (let y = 0; y < state.map.height && !free; y++) for (let x = 0; x < state.map.width; x++) if (!blocked.has(`${x},${y}`)) { free = {x,y}; break; }
    if (free) { q("#battle-token-form [name=x]").value = free.x + 1; q("#battle-token-form [name=y]").value = free.y + 1; }
  }
  if (!q("#battle-setup-dialog").open) q("#battle-setup-dialog").showModal();
}
document.querySelectorAll("[data-battle-setup]").forEach((button) => button.addEventListener("click", openBattleSetup));
q("#close-battle-setup").addEventListener("click", () => q("#battle-setup-dialog").close());
q("#battle-quick-start").addEventListener("click", async () => { if (await changeBattle({preset:"room", width:10, height:10}, "Starter encounter ready. Select a token to move it.", "POST", true)) { battleView.fit(); } });
q("#battle-create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.target));
  if (current.battle?.state && !confirm("Replace this encounter with the selected starter layout?")) return;
  const done = await changeBattle({preset:values.preset, width:Number(values.width), height:Number(values.height)}, "Encounter created.", "POST", true);
  if (done) { q("#battle-setup-dialog").close(); battleView.fit(); }
});
q("#battle-token-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.target));
  const done = await changeBattle({spawn_tokens:[{id:crypto.randomUUID(), name:values.name, faction:values.faction, x:Number(values.x) - 1, y:Number(values.y) - 1}]}, "Token added.");
  if (done) { q("#battle-setup-dialog").close(); }
});
q("#battle-remove-token").addEventListener("click", () => { if (battleSelected) changeBattle({remove_tokens:[battleSelected]}, "Token removed."); });
q("#battle-toggle-combat").addEventListener("click", () => {
  const state = current.battle?.state;
  if (state) changeBattle({combat:{...state.combat, active:!state.combat.active}}, "Combat status saved.");
});
q("#battle-door-controls").addEventListener("click", (event) => {
  const button = event.target.closest("[data-door-id]");
  const door = current.battle?.state?.map.doors.find((item) => item.id === button?.dataset.doorId);
  if (door) changeBattle({door_updates:[{id:door.id, open:!door.open}]}, "Door updated.");
});
q("#battle-fit").addEventListener("click", () => battleView.fit());
q("#battle-zoom-in").addEventListener("click", () => battleView.zoom(1));
q("#battle-zoom-out").addEventListener("click", () => battleView.zoom(-1));
q("#expand-map").addEventListener("click", () => { q("#map-dialog").showModal(); expandedBattleView.fit(); });
q("#close-map").addEventListener("click", () => q("#map-dialog").close());
q("#battle-map").addEventListener("toggle", () => { if (q("#battle-map").open && current?.battle?.state) battleView.fit(); });
new ResizeObserver(() => { if (current?.battle?.state && !battleBusy && !battleView.drag) battleView.fit(); }).observe(q("#battle-board"));
if (current) renderBattle();
