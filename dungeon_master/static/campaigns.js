"use strict";
const q = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const abilities = ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"];
const skills = ["Acrobatics", "Animal Handling", "Arcana", "Athletics", "Deception", "History", "Insight", "Intimidation", "Investigation", "Medicine", "Nature", "Perception", "Performance", "Persuasion", "Religion", "Sleight of Hand", "Stealth", "Survival"];
const textFields = ["attacks", "inventory", "currency", "features", "languages", "spells", "appearance", "backstory", "personality", "ideals", "bonds", "flaws", "goals", "notes"];
let current = null, dmContext = null, npcMemories = [], characterDirty = false, npcDirty = false, dmDirty = false;
let selectedTab = "character", polling = false, contextBlobURL = null, eventKey = null;
let characterFormRevision = 0;
const title = (text) => text.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
const bonus = (value) => `${value >= 0 ? "+" : ""}${value}`;
function announce(message, error = false) {
  q("#campaign-notice").textContent = message;
  q("#campaign-notice").classList.toggle("error", error);
  q("#campaign-notice").hidden = false;
}
async function request(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(Array.isArray(data.detail) ? data.detail.map((item) => `${item.loc?.at(-1) || "Record"}: ${item.msg}`).join(" ") : data.detail || "The record could not be saved.");
  return data;
}
const write = (url, data, method = "POST") => request(url, {method, headers:{"Content-Type":"application/json"}, body:JSON.stringify(data)});
const endpoint = (suffix = "") => `/api/campaigns/${current.id}${suffix}`;
function field(name, value, options = {}) {
  const {type = "text", min, max, placeholder = "", label = title(name)} = options;
  return `<label>${esc(label)}<input name="${name}" type="${type}" value="${esc(value)}" ${type === "text" ? 'maxlength="160"' : ""} ${min !== undefined ? `min="${min}"` : ""} ${max !== undefined ? `max="${max}"` : ""} placeholder="${esc(placeholder)}"></label>`;
}
function downloads() {
  document.querySelectorAll(".download-links").forEach((element) => {
    const kind = element.dataset.document, dm = kind === "plan";
    element.innerHTML = !current || (dm && !dmContext?.plan) ? "" : ["pdf", "md", "json"].map((format) =>
      `<a href="${endpoint(`/documents/${kind}.${format}`)}${dm ? "?audience=dm" : ""}" download>${format === "pdf" ? "Download PDF" : format === "md" ? "Markdown" : "JSON"}</a>`).join("");
  });
}
function renderCharacter() {
  characterFormRevision = current.character_revision;
  const c = current.character, d = current.derived;
  q("#character-fields").innerHTML = `
    <div class="record-group"><h3>Identity</h3><div class="form-grid three-columns">
      ${field("name", c.name, {label:"Character name"})}${field("player_name", c.player_name)}${field("pronouns", c.pronouns)}
      ${field("class_name", c.class_name, {label:"Class"})}${field("subclass", c.subclass)}${field("level", c.level, {type:"number", min:1, max:20})}
      ${field("ancestry", c.ancestry)}${field("background", c.background)}${field("alignment", c.alignment)}
    </div></div>
    <div class="record-group"><h3>Ability scores</h3><div class="ability-grid">${abilities.map((ability) => `<label>${ability}<input name="ability-${ability}" type="number" min="1" max="30" required value="${c.abilities[ability]}"><small data-modifier="${ability}">${bonus(d.modifiers[ability])} modifier</small></label>`).join("")}</div></div>
    <div class="record-group"><h3>Combat &amp; resources</h3><div class="form-grid three-columns">
      ${field("hp_current", c.hp_current, {label:"Current HP", type:"number", min:0, max:10000})}${field("hp_max", c.hp_max, {label:"Maximum HP", type:"number", min:1, max:10000})}${field("temp_hp", c.temp_hp, {label:"Temporary HP", type:"number", min:0, max:10000})}
      ${field("armor_class", c.armor_class, {type:"number", min:0, max:100})}${field("speed", c.speed, {label:"Speed (feet)", type:"number", min:0, max:1000})}${field("initiative_bonus", c.initiative_bonus ?? "", {label:"Initiative override", type:"number", min:-30, max:50, placeholder:`Automatic: ${bonus(d.initiative)}`})}
      ${field("proficiency_bonus", c.proficiency_bonus, {type:"number", min:0, max:12})}${field("experience", c.experience, {type:"number", min:0})}${field("hit_dice", c.hit_dice)}
      ${field("death_save_successes", c.death_save_successes, {type:"number", min:0, max:3})}${field("death_save_failures", c.death_save_failures, {type:"number", min:0, max:3})}${field("conditions", c.conditions.join(", "), {label:"Conditions (comma-separated)"})}
    </div><div class="check-row"><label><input type="checkbox" name="inspiration" ${c.inspiration ? "checked" : ""}> Inspiration</label></div><p class="form-help muted">Passive perception: ${d.passive_perception}. Proficiency and initiative can be adjusted for your rules.</p></div>
    <details class="record-group"><summary>Saving throws &amp; skill training</summary><h3>Saving throw proficiencies</h3><div class="check-row">${abilities.map((ability) => `<label><input type="checkbox" data-save="${ability}" ${c.saving_throw_proficiencies.includes(ability) ? "checked" : ""}> ${title(ability)} (${bonus(d.saving_throws[ability])})</label>`).join("")}</div><h3>Skills</h3><div class="skill-grid">${skills.map((skill) => `<div class="skill-choice"><span>${skill} (${bonus(d.skills[skill])})</span><div><label><input type="checkbox" data-skill="${skill}" ${c.skill_proficiencies.includes(skill) ? "checked" : ""}> Trained</label><label><input type="checkbox" data-expertise="${skill}" ${c.skill_expertise.includes(skill) ? "checked" : ""}> Expert</label></div></div>`).join("")}</div></details>
    <details class="record-group"><summary>Spellcasting, equipment &amp; character details</summary><div class="form-grid"><label>Spellcasting ability<select name="spellcasting_ability"><option value="">None</option>${abilities.map((ability) => `<option value="${ability}" ${c.spellcasting_ability === ability ? "selected" : ""}>${title(ability)}</option>`).join("")}</select></label><label>Spell slots: level, total, used<textarea name="spell_slots" rows="3" placeholder="1, 4, 2">${c.spell_slots.map((slot) => `${slot.level}, ${slot.total}, ${slot.used}`).join("\n")}</textarea></label></div><div class="character-text-grid">${textFields.map((key) => `<label>${title(key)}<textarea name="${key}" rows="3" maxlength="12000">${esc(c[key])}</textarea></label>`).join("")}</div></details>`;
}
function renderLog() {
  q("#event-list").innerHTML = current.events.length ? [...current.events].reverse().map((event) => `<article class="event-card"><span class="event-meta">SESSION ${event.session} · ${esc(event.kind.toUpperCase())} · ${esc(event.world_time || new Date(event.created_at).toLocaleString())}</span><h3>${esc(event.title)}</h3><p>${esc(event.summary)}</p>${event.details ? `<details><summary>Full scene / details</summary><p>${esc(event.details)}</p></details>` : ""}</article>`).join("")
    : '<div class="record-panel"><p>The story has not been recorded yet. Each saved event will appear here in order.</p></div>';
}
function renderNpcs() {
  q("#npc-list").innerHTML = current.npcs.length ? current.npcs.map((npc) => `<article class="npc-card"><h3>${esc(npc.name)}</h3><p class="npc-role">${esc(npc.role || "Role not recorded")}</p><p>${esc(npc.public_notes || "No public notes yet.")}</p><p>${npc.disposition ? `${esc(title(npc.disposition))} · Trust ${npc.trust}<br>${esc(npc.feelings)}` : '<span class="muted">Their private feelings have not been revealed.</span>'}</p>${npc.knowledge.length ? `<ul>${npc.knowledge.map((fact) => `<li>${esc(fact.fact)}<br><small>Learned from: ${esc(fact.learned_from)}</small></li>`).join("")}</ul>` : ""}</article>`).join("")
    : '<div class="record-panel"><p>No NPCs have been recorded yet. Add relationship memory below as you meet people.</p></div>';
}
function updatePlanBadge() {
  const labels = {queued:"Plan queued", reading:"Reading the adventure", synthesizing:"Planning the campaign", ready:"Campaign plan ready", failed:"Planning needs attention", needs_setup:"Local planner needs setup"};
  q("#campaign-plan-badge").textContent = labels[current.plan_status] || current.plan_status;
}
async function loadCampaign(id, reset = false) {
  const state = await request(`/api/campaigns/${id}`);
  if (reset) { characterDirty = npcDirty = dmDirty = false; dmContext = null; npcMemories = []; q("#npc-editor").open = false; q("#dm-workspace").hidden = true; q("#dm-gate").hidden = false; }
  current = state;
  localStorage.setItem("dm-active-campaign", id);
  q("#campaign-workspace").hidden = false;
  q("#campaign-empty").hidden = true;
  q("#campaign-source").textContent = `Source adventure: ${current.source_title}`;
  updatePlanBadge();
  if (!characterDirty) renderCharacter();
  renderLog(); renderNpcs(); downloads();
  if (dmContext && !dmDirty) await loadDm();
}
async function refreshCampaignList() {
  const data = await request("/api/campaigns");
  const selected = current?.id || localStorage.getItem("dm-active-campaign");
  q("#active-campaign").innerHTML = data.campaigns.length ? data.campaigns.map((campaign) => `<option value="${campaign.id}">${esc(campaign.name)}</option>`).join("") : '<option value="">Create your first campaign</option>';
  if (data.campaigns.some((campaign) => campaign.id === selected)) q("#active-campaign").value = selected;
  return data.campaigns;
}
async function refreshBooks() {
  const data = await request("/api/books?collection=campaign");
  const ready = data.books.filter((book) => book.status === "ready" && !book.needs_reindex);
  q("#campaign-book").innerHTML = '<option value="">Choose an indexed adventure</option>' + ready.map((book) => `<option value="${book.id}">${esc(book.title)}</option>`).join("");
  q("#create-campaign-form button[type=submit]").disabled = !ready.length;
  if (!ready.length) q("#planner-status").textContent = "Upload and index an adventure in the Campaigns library first.";
  else {
    const status = await request("/api/campaigns/planner");
    q("#planner-status").textContent = `${status.model} · ${status.message}${status.ready ? ". Planning runs locally and can take a few minutes." : " You can create records now and retry planning after setup."}`;
  }
}
async function showCreate() { q("#create-panel").hidden = false; await refreshBooks(); q("#create-campaign-form input[name=name]").focus(); }
q("#new-campaign").addEventListener("click", () => showCreate().catch((error) => announce(error.message, true)));
q("#cancel-create").addEventListener("click", () => q("#create-panel").hidden = true);
q("#create-campaign-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    const form = new FormData(event.target), campaign = await write("/api/campaigns", Object.fromEntries(form));
    q("#create-panel").hidden = true; event.target.reset();
    await refreshCampaignList(); q("#active-campaign").value = campaign.id;
    await loadCampaign(campaign.id, true); switchTab("character");
    announce("Campaign created. Your local AI is preparing the full adventure plan while you set up your character.");
  } catch (error) { announce(error.message, true); } finally { button.disabled = false; }
});
q("#active-campaign").addEventListener("change", async (event) => {
  if ((characterDirty || npcDirty || dmDirty) && !confirm("Switch campaigns and discard unsaved form changes?")) { event.target.value = current.id; return; }
  try { await loadCampaign(event.target.value, true); } catch (error) { announce(error.message, true); }
});
function switchTab(tab) {
  selectedTab = tab;
  document.querySelectorAll("[data-tab]").forEach((button) => { const active = button.dataset.tab === tab; button.setAttribute("aria-selected", String(active)); button.tabIndex = active ? 0 : -1; q(`#panel-${button.dataset.tab}`).hidden = !active; });
}
document.querySelectorAll("[data-tab]").forEach((button) => {
  button.addEventListener("click", () => switchTab(button.dataset.tab));
  button.addEventListener("keydown", (event) => {
    const tabs = [...document.querySelectorAll("[data-tab]")], index = tabs.indexOf(button);
    let target = event.key === "ArrowRight" ? (index + 1) % tabs.length : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : null;
    if (target !== null) { event.preventDefault(); switchTab(tabs[target].dataset.tab); tabs[target].focus(); }
  });
});
q("#character-form").addEventListener("input", (event) => {
  characterDirty = true; q("#character-saved").textContent = "Unsaved changes";
  if (event.target.name.startsWith("ability-")) { const ability = event.target.name.slice(8); q(`[data-modifier="${ability}"]`).textContent = `${bonus(Math.floor((Number(event.target.value) - 10) / 2))} modifier`; }
});
q("#reload-character").addEventListener("click", async () => {
  if (characterDirty && !confirm("Discard unsaved changes and reload the saved character sheet?")) return;
  try { characterDirty = false; await loadCampaign(current.id); q("#character-saved").textContent = "Latest saved character loaded."; }
  catch (error) { announce(error.message, true); }
});
q("#character-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    const form = new FormData(event.target), sheet = {...current.character};
    for (const key of ["name", "player_name", "pronouns", "class_name", "subclass", "ancestry", "background", "alignment", "hit_dice", ...textFields]) sheet[key] = String(form.get(key));
    for (const key of ["level", "hp_current", "hp_max", "temp_hp", "armor_class", "speed", "proficiency_bonus", "experience", "death_save_successes", "death_save_failures"]) sheet[key] = Number(form.get(key));
    sheet.initiative_bonus = form.get("initiative_bonus") === "" ? null : Number(form.get("initiative_bonus"));
    sheet.abilities = Object.fromEntries(abilities.map((ability) => [ability, Number(form.get(`ability-${ability}`))]));
    sheet.inspiration = form.has("inspiration"); sheet.conditions = String(form.get("conditions")).split(",").map((item) => item.trim()).filter(Boolean);
    sheet.saving_throw_proficiencies = [...event.target.querySelectorAll("[data-save]:checked")].map((input) => input.dataset.save);
    sheet.skill_proficiencies = [...event.target.querySelectorAll("[data-skill]:checked")].map((input) => input.dataset.skill);
    sheet.skill_expertise = [...event.target.querySelectorAll("[data-expertise]:checked")].map((input) => input.dataset.expertise);
    sheet.spellcasting_ability = form.get("spellcasting_ability") || null;
    sheet.spell_slots = String(form.get("spell_slots")).split("\n").filter((line) => line.trim()).map((line) => {
      const parts = line.split(",").map((part) => Number(part.trim()));
      if (parts.length !== 3 || parts.some((part) => !Number.isInteger(part))) throw new Error("Spell slots use one line per level: level, total, used.");
      return {level:parts[0], total:parts[1], used:parts[2]};
    });
    current = await write(endpoint(`/character?expected_revision=${characterFormRevision}`), sheet, "PUT"); characterDirty = false; renderCharacter();
    q("#character-saved").textContent = "Character saved. Downloads now include these changes."; announce("Character sheet saved.");
  } catch (error) { announce(error.message, true); } finally { button.disabled = false; }
});
q("#event-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    const values = Object.fromEntries(new FormData(event.target)); values.session = Number(values.session); values.player_visible = true;
    eventKey ||= crypto.randomUUID();
    await write(endpoint("/turns"), {event:values, idempotency_key:eventKey}); eventKey = null;
    const session = values.session; event.target.reset(); event.target.elements.session.value = session;
    await loadCampaign(current.id); announce("Event saved in the campaign log.");
  } catch (error) { announce(error.message, true); } finally { button.disabled = false; }
});
function knowledgeRow(fact = {}) {
  const events = [...current.events];
  const options = events.map((event) => `<option value="${event.id}" ${fact.evidence_event_id === event.id ? "selected" : ""}>${esc(event.title)}</option>`).join("");
  const missing = fact.evidence_event_id && !events.some((event) => event.id === fact.evidence_event_id);
  return `<div class="knowledge-entry"><label>Known fact<textarea data-fact required maxlength="2000" rows="2">${esc(fact.fact)}</textarea></label><label>How did this NPC learn it?<input data-learned required maxlength="2000" value="${esc(fact.learned_from)}" placeholder="The player told them, they witnessed a scene…"></label><label>Evidence event<select data-evidence><option value="">Manual note / no event link</option>${options}${missing ? `<option value="${fact.evidence_event_id}" selected>Saved DM event</option>` : ""}</select></label><div class="check-row"><label><input type="checkbox" data-fact-visible ${fact.player_visible ? "checked" : ""}> Player knows this information is known</label></div><button class="secondary-button" data-remove-fact type="button">Remove fact</button></div>`;
}
function fillNpc(id = "") {
  const npc = npcMemories.find((item) => item.id === id), form = q("#npc-form"); form.reset(); npcDirty = false;
  for (const key of ["id", "name", "role", "disposition", "trust", "public_notes", "feelings", "private_notes"]) if (npc) form.elements[key].value = npc[key];
  form.elements.player_visible.checked = npc?.player_visible ?? true;
  form.elements.relationship_revealed.checked = npc?.relationship_revealed ?? false;
  q("#knowledge-fields").innerHTML = (npc?.knowledge || []).map(knowledgeRow).join("");
}
async function loadNpcMemories() {
  const data = await request(endpoint("/npc-memories")); npcMemories = data.npcs;
  q("#edit-npc").innerHTML = '<option value="">New NPC</option>' + npcMemories.map((npc) => `<option value="${npc.id}">${esc(npc.name)}</option>`).join("");
  fillNpc();
}
q("#npc-editor").addEventListener("toggle", () => { if (q("#npc-editor").open && !npcDirty) loadNpcMemories().catch((error) => announce(error.message, true)); });
q("#edit-npc").addEventListener("change", (event) => fillNpc(event.target.value));
q("#add-knowledge").addEventListener("click", () => { q("#knowledge-fields").insertAdjacentHTML("beforeend", knowledgeRow()); npcDirty = true; });
q("#knowledge-fields").addEventListener("click", (event) => { if (event.target.matches("[data-remove-fact]")) { event.target.closest(".knowledge-entry").remove(); npcDirty = true; } });
q("#npc-form").addEventListener("input", () => npcDirty = true);
q("#npc-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    const form = new FormData(event.target), npc = Object.fromEntries(form);
    npc.id ||= null; npc.trust = Number(npc.trust); npc.player_visible = form.has("player_visible"); npc.relationship_revealed = form.has("relationship_revealed");
    npc.knowledge = [...q("#knowledge-fields").querySelectorAll(".knowledge-entry")].map((row) => ({fact:row.querySelector("[data-fact]").value, learned_from:row.querySelector("[data-learned]").value, evidence_event_id:row.querySelector("[data-evidence]").value || null, player_visible:row.querySelector("[data-fact-visible]").checked}));
    await write(endpoint("/npcs"), npc, "PUT"); npcDirty = false; await loadCampaign(current.id); await loadNpcMemories(); announce("NPC knowledge and relationship memory saved.");
  } catch (error) { announce(error.message, true); } finally { button.disabled = false; }
});
function renderPlan() {
  const plan = dmContext.plan;
  q("#plan-status").innerHTML = `<strong>${esc(title(dmContext.plan_status))}</strong><div class="plan-progress-detail">${esc(dmContext.plan_detail)}</div><progress max="100" value="${dmContext.plan_progress}" aria-label="Campaign planning progress"></progress>${dmContext.plan_error ? `<p class="plan-error">${esc(dmContext.plan_error)}</p>` : ""}`;
  q("#retry-plan").disabled = ["queued", "reading", "synthesizing"].includes(dmContext.plan_status);
  q("#plan-content").innerHTML = plan ? `<div class="record-panel"><p class="privacy-note">AI-generated roadmap. Verify established facts against the source book; branches and endings are possibilities.</p><span class="section-index">COMPLETE BOOK REVIEW · VERSION ${plan.version}</span><p class="plan-overview">${esc(plan.overview)}</p><p class="muted form-help">${plan.coverage.passages_read} / ${plan.coverage.total_passages} passages read · ${esc(plan.model)} · ${esc(plan.source_book.title)}</p></div>` + plan.phases.map((phase, index) => `<article class="phase-card"><div class="phase-heading"><h3>${index + 1}. ${esc(phase.title)}</h3><span class="small-tag">${esc(phase.status.toUpperCase())}</span></div><p>${esc(phase.summary)}</p><h4>Goal &amp; entry conditions</h4><p>${esc(phase.goal)}</p><h4>Possible scenes</h4><ul>${phase.scenes.map((scene) => `<li>${esc(scene)}</li>`).join("")}</ul><h4>Branches &amp; consequences</h4><ul>${phase.branches.map((branch) => `<li>${esc(branch)}</li>`).join("")}</ul><h4>Completion condition</h4><p>${esc(phase.completion_condition)}</p><p class="phase-sources">Sources: ${phase.source_ids.map((source) => `[${esc(source)}] ${esc(plan.sources[source])}`).join(", ") || "Proposed connective material; verify against the source."}</p><div class="phase-actions">${["active", "completed", "skipped", "planned"].filter((status) => status !== phase.status).map((status) => `<button class="secondary-button" data-phase="${phase.id}" data-status="${status}">${status === "active" ? "Make current" : status === "completed" ? "Mark completed" : status === "skipped" ? "Skip" : "Reset to planned"}</button>`).join("")}</div></article>`).join("") + ["endings", "npc_agendas", "unresolved_questions"].map((key) => `<div class="record-panel"><h3>${title(key)}</h3><ul>${plan[key].map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div>`).join("") : "";
  if (!dmDirty) q("#dm-character-notes").value = dmContext.character.dm_notes;
  q("#dm-context-preview").textContent = JSON.stringify(dmContext, null, 2);
  if (contextBlobURL) URL.revokeObjectURL(contextBlobURL);
  contextBlobURL = URL.createObjectURL(new Blob([JSON.stringify(dmContext, null, 2)], {type:"application/json"}));
  q("#dm-context-download").href = contextBlobURL; q("#dm-context-download").download = "dm-campaign-context.json";
  downloads();
}
async function loadDm() { dmContext = await request(endpoint("/dm-context")); renderPlan(); }
q("#reveal-dm").addEventListener("click", async () => {
  try { await loadDm(); q("#dm-gate").hidden = true; q("#dm-workspace").hidden = false; }
  catch (error) { announce(error.message, true); }
});
q("#retry-plan").addEventListener("click", async () => {
  if (dmContext?.plan && !confirm("Create a new outline from the full book? The previous version remains in history; the new plan starts with fresh milestone statuses.")) return;
  try { await write(endpoint("/plan"), {}); await loadCampaign(current.id); }
  catch (error) { announce(error.message, true); }
});
q("#plan-content").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-phase]"); if (!button) return;
  button.disabled = true;
  try {
    await write(endpoint("/turns"), {event:{title:"Campaign milestone updated", summary:`${button.dataset.phase} marked ${button.dataset.status}.`, kind:"note", player_visible:false}, phase_updates:[{phase_id:button.dataset.phase, status:button.dataset.status}]});
    await loadCampaign(current.id); announce("Campaign milestone saved to DM memory.");
  } catch (error) { announce(error.message, true); button.disabled = false; }
});
q("#dm-character-notes").addEventListener("input", () => dmDirty = true);
q("#dm-character-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try { const latest = await request(endpoint()); await write(endpoint(`/character?expected_revision=${latest.character_revision}`), {...latest.character, dm_notes:q("#dm-character-notes").value}, "PUT"); dmDirty = false; await loadCampaign(current.id); announce("Private character notes saved; player downloads exclude them."); }
  catch (error) { announce(error.message, true); } finally { button.disabled = false; }
});
async function poll() {
  if (polling) return; polling = true;
  try {
    if (current) {
      const list = await request("/api/campaigns"), next = list.campaigns.find((item) => item.id === current.id);
      if (next && next.updated_at !== current.updated_at) await loadCampaign(current.id);
    }
  } catch (error) { announce(`Campaign refresh failed. ${error.message}`, true); }
  finally { polling = false; setTimeout(poll, ["queued", "reading", "synthesizing"].includes(current?.plan_status) ? 2000 : 7000); }
}
(async () => {
  try { const campaigns = await refreshCampaignList(); if (campaigns.length) await loadCampaign(q("#active-campaign").value, true); else await showCreate(); }
  catch (error) { announce(error.message, true); }
  poll();
})();
