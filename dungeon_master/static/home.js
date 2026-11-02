"use strict";

const portal = (selector) => document.querySelector(selector);
const safe = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const documentsPage = location.pathname === "/campaign-docs";
let saves = [], saveSignature = null, portalBusy = false, creatingSave = false;
let deleteTarget = null, deletingSave = false;
let portalVersion = 0;

portal("#home-view").hidden = documentsPage;
portal("#documents-view").hidden = !documentsPage;
portal(`[data-nav="${documentsPage ? "documents" : "home"}"]`).setAttribute("aria-current", "page");
document.title = `${documentsPage ? "Campaign Docs" : "Home"} · AI Dungeon Master`;

function playURL(id, tab = "") {
  const query = new URLSearchParams({campaign:id});
  if (tab) query.set("docs", tab);
  return `/campaigns?${query}`;
}
function characterLine(save) {
  const character = save.character || {};
  return [character.name || "Character not named", character.class_name, `Level ${character.level || 1}`].filter(Boolean).join(" · ");
}
function savedTime(stamp) {
  const date = new Date(stamp);
  return Number.isNaN(date.getTime()) ? "Saved locally" : date.toLocaleString(undefined, {month:"short",day:"numeric",year:"numeric",hour:"numeric",minute:"2-digit"});
}
function campaignEmblem() { return '<span class="save-emblem" aria-hidden="true"><img src="/static/mark.svg" alt=""></span>'; }
function newSaveButton() {
  return '<button type="button" class="save-new" data-new-campaign><span aria-hidden="true">＋</span><strong>Begin another story</strong><small>A new world. A fresh save file.</small></button>';
}
function deleteSaveButton(save) {
  return `<button type="button" class="campaign-delete-button" data-delete-campaign="${save.id}" aria-label="Delete ${safe(save.name)}">Delete</button>`;
}

function renderSaves() {
  const preferred = localStorage.getItem("dm-active-campaign");
  const recent = saves.find((save) => save.id === preferred) || saves[0];
  portal("#save-count").textContent = saves.length;
  portal("#documents-summary").textContent = `${saves.length} ${saves.length === 1 ? "campaign folio" : "campaign folios"} · Organized by campaign`;
  const continueLink = portal("#continue-campaign");
  continueLink.hidden = !recent;
  if (recent) { continueLink.href = playURL(recent.id); continueLink.setAttribute("aria-label", `Continue ${recent.name}`); }
  portal("#save-list").innerHTML = saves.map((save,index) => `<article class="save-card${save.id === recent?.id ? " recent-save" : ""}">
    <div class="save-topline"><span>Save file ${String(index + 1).padStart(2,"0")}</span><div class="save-options">${save.id === recent?.id ? '<span class="save-tag">Continue your story</span>' : ""}${deleteSaveButton(save)}</div></div>
    <div class="save-identity">${campaignEmblem()}<div><h3>${safe(save.name)}</h3><p>${safe(characterLine(save))}</p></div></div>
    <div class="save-detail"><span><small>Adventure</small>${safe(save.source_title)}</span><span><small>Last saved</small><time datetime="${safe(save.updated_at)}">${safe(savedTime(save.updated_at))}</time></span></div>
    <div class="save-actions"><a class="game-button game-button-outline" href="${playURL(save.id)}" aria-label="Load ${safe(save.name)}">Load campaign <span aria-hidden="true">→</span></a><a href="/campaign-docs#campaign-${save.id}" aria-label="Documents for ${safe(save.name)}">Campaign docs ↗</a></div>
  </article>`).join("") + newSaveButton();
}

const documentKinds = [
  {id:"character",title:"Character sheet",symbol:"◇",description:"Stats, equipment, and who you are."},
  {id:"log",title:"Campaign log",symbol:"≋",description:"Your choices and everything that happened."},
  {id:"relationships",title:"Relationships",symbol:"♧",description:"The people you know and what they know."},
  {id:"plan",title:"DM campaign plan",symbol:"⌖",description:"The adventure ahead, kept behind the screen."},
];
function renderDocuments() {
  const query = portal("#campaign-filter").value.trim().toLocaleLowerCase();
  const matches = saves.filter((save) => `${save.name} ${save.source_title} ${characterLine(save)}`.toLocaleLowerCase().includes(query));
  portal("#docs-count").textContent = `${matches.length} ${matches.length === 1 ? "campaign" : "campaigns"}`;
  portal("#campaign-document-list").innerHTML = matches.length ? matches.map((save) => `<section class="campaign-folio" id="campaign-${save.id}" aria-labelledby="folio-${save.id}">
    <div class="folio-heading">${campaignEmblem()}<div><span class="section-index">CAMPAIGN FOLIO</span><h2 id="folio-${save.id}">${safe(save.name)}</h2><p>${safe(characterLine(save))}<br>Adventure: ${safe(save.source_title)}</p></div><div class="folio-actions"><a href="${playURL(save.id)}" aria-label="Play ${safe(save.name)}">Return to game →</a>${deleteSaveButton(save)}</div></div>
    <div class="folio-docs">${documentKinds.map((kind) => `<article class="folio-document"><span class="doc-symbol" aria-hidden="true">${kind.symbol}</span><h3>${kind.title}</h3><p>${kind.description}</p><a href="${playURL(save.id, kind.id === "plan" ? "plan" : kind.id)}" class="doc-open" aria-label="Open ${kind.title} for ${safe(save.name)}">${kind.id === "plan" ? "Open DM workspace" : "View & edit"} ↗</a>${kind.id === "plan" ? '<span class="doc-private">DM only · Contains future story spoilers</span>' : `<div class="doc-downloads">${["pdf","md","json"].map((format) => `<a href="/api/campaigns/${save.id}/documents/${kind.id}.${format}" download aria-label="Download ${kind.title} for ${safe(save.name)} as ${format === "md" ? "Markdown" : format.toUpperCase()}">${format === "md" ? "Markdown" : format.toUpperCase()}</a>`).join("")}</div>`}</article>`).join("")}</div>
  </section>`).join("") : `<div class="portal-empty"><h2>${saves.length ? "No matching campaigns." : "Your first story starts here."}</h2><p>${saves.length ? "Try another campaign or character name." : 'Create a campaign to begin its character sheet, log, and relationships. <a href="/">Choose a save file →</a>'}</p></div>`;
}
portal("#campaign-filter").addEventListener("input", renderDocuments);

async function portalRequest(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(Array.isArray(data.detail) ? data.detail.map((item) => item.msg).join(" ") : data.detail || "The app could not complete this request.");
    error.status = response.status;
    throw error;
  }
  return data;
}
function portalError(message) {
  portal("#portal-notice").textContent = message;
  portal("#portal-notice").hidden = false;
  portal("#portal-notice").classList.add("error");
}
async function refreshPortal() {
  if (portalBusy) return;
  portalBusy = true;
  const version = portalVersion;
  try {
    const data = await portalRequest("/api/campaigns");
    if (version !== portalVersion) return;
    const signature = JSON.stringify(data.campaigns);
    if (signature !== saveSignature) {
      saves = data.campaigns; saveSignature = signature;
      renderSaves(); renderDocuments();
    }
    portal("#portal-notice").hidden = true;
  } catch (error) {
    portalError(`Your saves could not be loaded. ${error.message}`);
    if (saveSignature === null) {
      portal("#save-list").innerHTML = '<p class="portal-empty">Reconnect to the local app to see your saved campaigns.</p>';
      portal("#campaign-document-list").innerHTML = '<p class="portal-empty">Campaign documents will appear when the app reconnects.</p>';
    }
  } finally { portalBusy = false; }
}
async function refreshMaterialsSummary() {
  try {
    const {books} = await portalRequest("/api/books");
    const rules = books.filter((book) => book.collection === "rules").length;
    const adventures = books.filter((book) => book.collection === "campaign").length;
    portal("#materials-summary").textContent = `${rules} ${rules === 1 ? "rulebook" : "rulebooks"} · ${adventures} campaign ${adventures === 1 ? "book" : "books"}`;
  } catch { portal("#materials-summary").textContent = "Open your materials library ↗"; }
}

async function openNewSave() {
  portal("#new-save-dialog").showModal();
  portal("#new-save-form [name=name]").focus();
  portal("#new-save-error").hidden = true;
  portal("#create-save").disabled = true;
  portal("#new-save-help").textContent = "Finding your adventure books…";
  try {
    const {books} = await portalRequest("/api/books?collection=campaign");
    const ready = books.filter((book) => book.status === "ready" && !book.needs_reindex);
    const selected = portal("#new-save-book").value;
    portal("#new-save-book").innerHTML = '<option value="">Choose an adventure…</option>' + ready.map((book) => `<option value="${book.id}">${safe(book.title)}</option>`).join("");
    if (ready.some((book) => book.id === selected)) portal("#new-save-book").value = selected;
    else if (ready.length === 1) portal("#new-save-book").value = ready[0].id;
    portal("#create-save").disabled = !ready.length || creatingSave;
    portal("#new-save-help").textContent = ready.length ? "Your AI will prepare an adventure plan while you set up your character." : "Upload a campaign book and let it finish indexing first. Rulebooks stay in the separate Rules collection.";
  } catch (error) { portal("#new-save-help").textContent = error.message; }
}
document.addEventListener("click", (event) => {
  if (event.target.closest("[data-new-campaign]")) openNewSave();
  const button = event.target.closest("[data-delete-campaign]");
  if (button) openDeleteSave(button.dataset.deleteCampaign);
});

function openDeleteSave(id) {
  deleteTarget = saves.find((save) => save.id === id);
  if (!deleteTarget || deletingSave) return;
  portal("#delete-save-name").textContent = `“${deleteTarget.name}”`;
  portal("#delete-save-error").hidden = true;
  portal("#delete-save-dialog").showModal();
  portal("#cancel-delete-save").focus();
}
portal("#cancel-delete-save").addEventListener("click", () => portal("#delete-save-dialog").close());
portal("#delete-save-dialog").addEventListener("cancel", (event) => { if (deletingSave) event.preventDefault(); });
portal("#delete-save-dialog").addEventListener("close", () => { deleteTarget = null; });

async function clearDeletedSaveFromBrowser(id) {
  if (localStorage.getItem("dm-active-campaign") === id) localStorage.removeItem("dm-active-campaign");
  localStorage.removeItem(`dm-map-open:${id}`);
  await new Promise((resolve, reject) => {
    const request = indexedDB.open("dm-table-artwork", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("images");
    request.onerror = () => reject(new Error("Browser artwork could not be cleared."));
    request.onblocked = () => reject(new Error("Browser artwork storage is busy."));
    request.onsuccess = () => {
      const db = request.result, transaction = db.transaction("images", "readwrite"), images = transaction.objectStore("images");
      images.delete(`${id}:portrait`); images.delete(`${id}:map`);
      transaction.oncomplete = () => { db.close(); resolve(); };
      transaction.onerror = transaction.onabort = () => { db.close(); reject(new Error("Browser artwork could not be cleared.")); };
    };
  });
}
portal("#delete-save-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!deleteTarget || deletingSave) return;
  const target = deleteTarget;
  deletingSave = true;
  portal("#confirm-delete-save").disabled = portal("#cancel-delete-save").disabled = true;
  portal("#confirm-delete-save").textContent = "Deleting…";
  portal("#delete-save-error").hidden = true;
  try {
    try { await portalRequest(`/api/campaigns/${target.id}`, {method:"DELETE"}); }
    catch (error) { if (error.status !== 404) throw error; }
    portalVersion++;
    let cleanupNotice = "";
    try { await clearDeletedSaveFromBrowser(target.id); }
    catch { cleanupNotice = " Some artwork cached in this browser could not be cleared."; }
    saves = saves.filter((save) => save.id !== target.id);
    saveSignature = JSON.stringify(saves);
    renderSaves(); renderDocuments();
    portal("#delete-save-dialog").close();
    portal("#portal-notice").textContent = `“${target.name}” was deleted.${cleanupNotice}`;
    portal("#portal-notice").classList.remove("error");
    portal("#portal-notice").hidden = false;
    portal(documentsPage ? "#campaign-filter" : ".hero-actions [data-new-campaign]").focus({preventScroll:true});
  } catch (error) {
    portal("#delete-save-error").textContent = `Campaign could not be deleted. ${error.message}`;
    portal("#delete-save-error").hidden = false;
  } finally {
    deletingSave = false;
    portal("#confirm-delete-save").disabled = portal("#cancel-delete-save").disabled = false;
    portal("#confirm-delete-save").textContent = "Delete campaign";
  }
});
portal("#close-new-save").addEventListener("click", () => portal("#new-save-dialog").close());
portal("#new-save-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (creatingSave) return;
  creatingSave = true; portal("#create-save").disabled = true; portal("#new-save-error").hidden = true;
  try {
    const values = Object.fromEntries(new FormData(event.target));
    const save = await portalRequest("/api/campaigns", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(values)});
    location.assign(playURL(save.id,"character"));
  } catch (error) { portal("#new-save-error").textContent = error.message; portal("#new-save-error").hidden = false; }
  finally { creatingSave = false; portal("#create-save").disabled = !portal("#new-save-book").value; }
});
window.addEventListener("pageshow", refreshPortal);
async function portalPoll() {
  if (!document.hidden && !portal("#new-save-dialog").open && !portal("#delete-save-dialog").open) await refreshPortal();
  setTimeout(portalPoll, 7000);
}
(async () => {
  await Promise.allSettled([refreshPortal(), refreshMaterialsSummary()]);
  if (documentsPage && /^#campaign-[a-f0-9-]+$/i.test(location.hash)) document.getElementById(location.hash.slice(1))?.scrollIntoView({block:"start"});
  setTimeout(portalPoll, 7000);
})();
