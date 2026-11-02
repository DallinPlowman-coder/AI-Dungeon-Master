"use strict";

let tableCampaignId = null, tableFeedKey = null, tableInventoryPage = 0, tableEntryPending = null;
let tableMediaPromise = null;
const abilityNames = {strength:"STR", dexterity:"DEX", constitution:"CON", intelligence:"INT", wisdom:"WIS", charisma:"CHA"};

function openCampaignDocs(tab = "character") {
  switchTab(tab);
  if (!q("#records-dialog").open) q("#records-dialog").showModal();
}
q("#open-docs").addEventListener("click", () => openCampaignDocs());
q("#close-docs").addEventListener("click", () => q("#records-dialog").close());
document.querySelectorAll("[data-open-doc]").forEach((button) => button.addEventListener("click", () => openCampaignDocs(button.dataset.openDoc)));

function tableHasDraft() { return Boolean(q("#table-entry").value.trim()); }
function tableLines(value) { return String(value || "").split("\n").map((line) => line.replace(/^\s*[-*]\s+/, "").trim()).filter(Boolean); }
function tableItems(items, empty) { return items.length ? items.map((item) => `<div class="rail-item">${esc(item)}</div>`).join("") : `<p class="rail-empty">${empty}</p>`; }

function tableCharacter() {
  const c = {...current.character, abilities:{...current.character.abilities}};
  if (!characterDirty) return c;
  const form = new FormData(q("#character-form"));
  for (const key of ["name", "class_name", "ancestry", "attacks", "inventory", "currency"]) if (form.has(key)) c[key] = String(form.get(key));
  for (const key of ["level", "hp_current", "hp_max", "armor_class", "speed", "proficiency_bonus"]) {
    const value = Number(form.get(key));
    if (Number.isFinite(value) && value >= 0) c[key] = value;
  }
  for (const ability of abilities) {
    const value = Number(form.get(`ability-${ability}`));
    if (Number.isInteger(value) && value >= 1 && value <= 30) c.abilities[ability] = value;
  }
  c.skill_proficiencies = [...q("#character-form").querySelectorAll("[data-skill]:checked")].map((input) => input.dataset.skill);
  c.skill_expertise = [...q("#character-form").querySelectorAll("[data-expertise]:checked")].map((input) => input.dataset.expertise);
  return c;
}

function renderTableCharacter() {
  if (!current) return;
  const c = tableCharacter();
  q("#rail-name").textContent = c.name || "Your character";
  q("#portrait-initials").textContent = c.name ? c.name.split(/\s+/).map((word) => word.match(/[\p{L}\p{N}]/u)?.[0]).filter(Boolean).slice(0, 2).join("").toUpperCase() : "✧";
  q("#rail-identity").textContent = [c.class_name, `Level ${c.level}`, c.ancestry, characterDirty ? "Unsaved draft" : ""].filter(Boolean).join(" · ");
  q("#rail-vitals").innerHTML = `<div class="vital-stat hp"><strong>${c.hp_current}<small> / ${c.hp_max}</small></strong><span>HP</span></div><div class="vital-stat"><strong>${c.armor_class}</strong><span>AC</span></div><div class="vital-stat"><strong>${c.speed}<small> ft</small></strong><span>Speed</span></div>`;
  q("#rail-conditions").hidden = !c.conditions.length;
  q("#rail-conditions").textContent = c.conditions.join(" · ");
  q("#rail-abilities").innerHTML = abilities.map((ability) => `<div class="rail-ability"><span><abbr title="${title(ability)}">${abilityNames[ability]}</abbr></span><strong aria-label="${title(ability)} score">${c.abilities[ability]}</strong><small aria-label="${title(ability)} modifier">${bonus(Math.floor((c.abilities[ability] - 10) / 2))}</small></div>`).join("");
  q("#rail-proficiency").textContent = `${bonus(c.proficiency_bonus)} bonus`;
  const trained = [...new Set([...c.skill_proficiencies, ...c.skill_expertise])];
  q("#rail-skills").innerHTML = trained.length ? trained.map((skill) => `<span class="rail-skill ${c.skill_expertise.includes(skill) ? "expert" : ""}" title="${c.skill_expertise.includes(skill) ? "Expertise" : "Proficient"}">${esc(skill)}${c.skill_expertise.includes(skill) ? " ✦" : ""}</span>`).join("") : '<p class="rail-empty">No skill training recorded.</p>';
  const attacks = tableLines(c.attacks);
  q("#rail-weapons").innerHTML = tableItems(attacks.slice(0, 3), "No weapons recorded.") + (attacks.length > 3 ? `<details><summary>${attacks.length - 3} more attacks</summary>${tableItems(attacks.slice(3), "")}</details>` : "");
  const inventory = tableLines(c.inventory), pages = Math.max(1, Math.ceil(inventory.length / 4));
  tableInventoryPage = Math.min(tableInventoryPage, pages - 1);
  q("#rail-inventory").innerHTML = tableItems(inventory.slice(tableInventoryPage * 4, tableInventoryPage * 4 + 4), "Your pack is waiting to be filled.");
  q("#rail-currency").textContent = c.currency || "Coin not recorded";
  q("#inventory-page").textContent = `${tableInventoryPage + 1} / ${pages}`;
  q("#inventory-previous").disabled = tableInventoryPage === 0;
  q("#inventory-next").disabled = tableInventoryPage >= pages - 1;
  const timed = [...current.events].reverse().find((event) => event.world_time);
  q("#rail-world-time").textContent = timed?.world_time || "Time not recorded";
}
q("#inventory-previous").addEventListener("click", () => { tableInventoryPage = Math.max(0, tableInventoryPage - 1); renderTableCharacter(); });
q("#inventory-next").addEventListener("click", () => { tableInventoryPage += 1; renderTableCharacter(); });

function renderTable() {
  if (!current) return;
  if (tableCampaignId !== current.id) {
    tableCampaignId = current.id;
    tableFeedKey = null;
    tableInventoryPage = 0;
    tableEntryPending = null;
    q("#table-entry-form").reset();
    q("#battle-map").open = localStorage.getItem(`dm-map-open:${current.id}`) !== "false";
    showTableImage("map", null);
    showTableImage("portrait", null);
    loadTableImages(current.id);
  }
  renderTableCharacter();
  const key = current.events.map((event) => event.id).join(",");
  if (key === tableFeedKey) return;
  const feed = q("#story-feed"), follow = tableFeedKey === null || feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
  const scroll = feed.scrollTop;
  tableFeedKey = key;
  feed.innerHTML = current.events.length ? current.events.map((event) => `<article class="story-entry"><span class="story-entry-mark" aria-hidden="true">✧</span><div><div class="story-entry-meta"><span>Session ${event.session}</span><span aria-hidden="true">·</span><span>${esc(event.kind)}</span>${event.world_time ? `<span aria-hidden="true">·</span><span>${esc(event.world_time)}</span>` : ""}</div><h2>${esc(event.title)}</h2><p>${esc(event.summary)}</p>${event.details ? `<details><summary>Read the full scene</summary><p>${esc(event.details)}</p></details>` : ""}</div></article>`).join("") : '<div class="story-empty"><span aria-hidden="true">✧</span><h2>Every story begins somewhere.</h2><p>Your recorded scenes and choices will appear here. Write the first entry below, or open your campaign docs.</p></div>';
  feed.scrollTop = follow ? feed.scrollHeight : scroll;
}
q("#battle-map").addEventListener("toggle", () => {
  if (current) localStorage.setItem(`dm-map-open:${current.id}`, String(q("#battle-map").open));
});

q("#table-entry-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!current) return;
  const input = q("#table-entry"), text = input.value.trim(), button = event.submitter || q(".composer-send");
  if (!text) return;
  const campaignId = current.id;
  if (!tableEntryPending || tableEntryPending.text !== text || tableEntryPending.campaignId !== campaignId) tableEntryPending = {key:crypto.randomUUID(), text, campaignId};
  const pending = tableEntryPending;
  button.disabled = true;
  try {
    const latest = current.events.at(-1);
    const heading = text.split(/\n|[.!?](?:\s|$)/)[0].slice(0, 80);
    await write(`/api/campaigns/${campaignId}/turns`, {idempotency_key:pending.key, event:{title:heading || "Journal entry", summary:text,
      kind:"note", session:latest?.session || 1, world_time:[...current.events].reverse().find((item) => item.world_time)?.world_time || "", player_visible:true}});
    if (current.id === campaignId) {
      if (input.value.trim() === text) input.value = "";
      tableEntryPending = null;
      await loadCampaign(campaignId);
      q("#story-feed").scrollTop = q("#story-feed").scrollHeight;
      announce("Entry saved to your campaign log.");
    }
  } catch (error) { announce(error.message, true); }
  finally { button.disabled = false; }
});
q("#table-entry").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !q(".composer-send").disabled) {
    event.preventDefault(); q("#table-entry-form").requestSubmit(q(".composer-send"));
  }
});

// Artwork is a presentation preference stored in this browser, separately from campaign records.
function tableMedia() {
  tableMediaPromise ||= new Promise((resolve, reject) => {
    const request = indexedDB.open("dm-table-artwork", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("images");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new Error("This browser could not open local artwork storage."));
  });
  return tableMediaPromise;
}
async function readTableImage(campaignId, kind) {
  const db = await tableMedia();
  return new Promise((resolve, reject) => {
    const request = db.transaction("images").objectStore("images").get(`${campaignId}:${kind}`);
    request.onsuccess = () => resolve(request.result || null);
    request.onerror = () => reject(new Error("The saved image could not be loaded."));
  });
}
async function saveTableImage(campaignId, kind, image) {
  const db = await tableMedia();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction("images", "readwrite"), store = transaction.objectStore("images");
    if (image) store.put(image, `${campaignId}:${kind}`); else store.delete(`${campaignId}:${kind}`);
    transaction.oncomplete = resolve;
    transaction.onerror = () => reject(new Error("The image could not be saved. This browser's local storage may be full."));
    transaction.onabort = () => reject(new Error("The image could not be saved in this browser."));
  });
}
function showTableImage(kind, data) {
  if (kind === "map") {
    const img = q("#battle-map-image");
    img.hidden = !data;
    if (data) img.src = data.url; else img.removeAttribute("src");
    q("#map-empty").hidden = Boolean(data);
    q("#map-image-tools").hidden = !data;
    q("#map-upload-label").textContent = data ? "Change map" : "Add a battle map";
    q("#map-storage").textContent = data ? "Custom map · saved in this browser" : "PNG, JPG or WebP · up to 8 MB";
  } else {
    const img = q("#character-portrait");
    img.hidden = !data;
    if (data) img.src = data.url; else img.removeAttribute("src");
    q("#portrait-initials").hidden = Boolean(data);
    q("#remove-portrait").hidden = !data;
  }
}
async function loadTableImages(campaignId) {
  try {
    const images = await Promise.all([readTableImage(campaignId, "map"), readTableImage(campaignId, "portrait")]);
    if (current?.id !== campaignId) return;
    showTableImage("map", images[0]); showTableImage("portrait", images[1]);
  } catch (error) { announce(error.message, true); }
}
for (const kind of ["map", "portrait"]) {
  q(`#${kind}-file`).addEventListener("change", async (event) => {
    const file = event.target.files[0];
    if (!file || !current) return;
    const campaignId = current.id;
    try {
      if (!["image/png", "image/jpeg", "image/webp"].includes(file.type) || file.size > 8 * 1024 * 1024) throw new Error("Choose a PNG, JPG, or WebP image up to 8 MB.");
      const url = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error("The image could not be read.")); reader.readAsDataURL(file); });
      const preview = new Image(); preview.src = url; await preview.decode();
      await saveTableImage(campaignId, kind, {url, name:file.name});
      if (current.id === campaignId) showTableImage(kind, {url});
      announce(`${kind === "map" ? "Battle map" : "Character portrait"} saved in this browser.`);
    } catch (error) { announce(error.message || "This image could not be opened. Choose a PNG, JPG, or WebP.", true); }
    finally { event.target.value = ""; }
  });
  q(`#remove-${kind}`).addEventListener("click", async () => {
    const campaignId = current.id;
    try { await saveTableImage(campaignId, kind, null); if (current.id === campaignId) showTableImage(kind, null); }
    catch (error) { announce(error.message, true); }
  });
}
q("#expand-map").addEventListener("click", () => { q("#expanded-map-image").src = q("#battle-map-image").src; q("#map-dialog").showModal(); });
q("#close-map").addEventListener("click", () => q("#map-dialog").close());
if (current) renderTable();
