"use strict";

let materialRuleSets = [], materialSetSignature = null, editingRuleSet = null, savingRuleSet = false;

function materialRuleSetBookIds() {
  const id = $("#search-rule-set").value;
  if (!id) return null;
  const group = materialRuleSets.find((item) => item.id === id);
  return group?.ready ? group.book_ids : [];
}
function receiveMaterialRuleSets(groups) {
  const signature = JSON.stringify(groups);
  if (signature === materialSetSignature) return;
  const selected = $("#search-rule-set").value;
  materialRuleSets = groups; materialSetSignature = signature;
  $("#search-rule-set").innerHTML = '<option value="">All books in the selected collection</option>' + groups.map((group) => `<option value="${group.id}">${escapeHTML(group.name)}${group.ready ? "" : " · needs attention"}</option>`).join("");
  if (selected) {
    if (!groups.some((group) => group.id === selected)) $("#search-rule-set").add(new Option("Selected set unavailable", selected));
    $("#search-rule-set").value = selected;
  }
  $("#rule-set-list").innerHTML = groups.length ? groups.map((group) => `<article class="rule-set-card"><div><h3>${escapeHTML(group.name)}</h3><span class="rule-set-status">${group.book_ids.length} ${group.book_ids.length === 1 ? "book" : "books"}${group.ready ? " · Ready to select" : " · Needs attention"}</span></div><ul>${group.books.map((book) => `<li>${escapeHTML(book.title)}${book.available ? "" : ' <span class="rule-error">· not ready in Rules</span>'}</li>`).join("") || "<li>Add indexed rulebooks to use this set.</li>"}</ul><div class="rule-set-card-actions"><button type="button" class="secondary-button" data-edit-rule-set="${group.id}" aria-label="Edit ${escapeHTML(group.name)}">Edit set</button><button type="button" class="text-button" data-search-rule-set="${group.id}" ${group.ready ? "" : "disabled"}>Search this set ↗</button></div></article>`).join("") : '<div class="rule-set-empty">Upload your rulebooks below, then create a set such as “Core Rules” or “My Homebrew”.</div>';
  if (selected) clearSearch(); else renderSearchControls();
}

function openRuleSetEditor(id = null) {
  editingRuleSet = id ? materialRuleSets.find((group) => group.id === id) : null;
  if (id && !editingRuleSet) return;
  $("#rule-set-dialog-title").textContent = editingRuleSet ? "Edit rulebook set" : "Create a rulebook set";
  $("#rule-set-form [name=name]").value = editingRuleSet?.name || "";
  $("#rule-set-error").hidden = true;
  const choices = books.filter((book) => book.collection === "rules" && book.status === "ready" && !book.needs_reindex);
  for (const book of editingRuleSet?.books || []) if (!choices.some((item) => item.id === book.id)) choices.push({...book, unavailable:true});
  $("#rule-set-books").innerHTML = choices.length ? choices.map((book) => `<label class="rule-book-choice"><input type="checkbox" name="book_ids" value="${book.id}" ${editingRuleSet?.book_ids.includes(book.id) ? "checked" : ""}><span>${escapeHTML(book.title)}${book.unavailable ? '<small class="rule-error">Not ready in Rules. Uncheck it or repair its index.</small>' : "<small>Indexed rulebook</small>"}</span></label>`).join("") : '<p class="muted">Upload at least one rulebook and wait for indexing to finish.</p>';
  $("#save-rule-set").disabled = !choices.length;
  $("#rule-set-dialog").showModal();
  $("#rule-set-form [name=name]").focus();
}
$("#new-rule-set").addEventListener("click", () => openRuleSetEditor());
$("#close-rule-set").addEventListener("click", () => $("#rule-set-dialog").close());
$("#rule-set-upload-link").addEventListener("click", () => $("#rule-set-dialog").close());
$("#rule-set-dialog").addEventListener("cancel", (event) => { if (savingRuleSet) event.preventDefault(); });
$("#rule-set-list").addEventListener("click", (event) => {
  const edit = event.target.closest("[data-edit-rule-set]");
  if (edit) openRuleSetEditor(edit.dataset.editRuleSet);
  const search = event.target.closest("[data-search-rule-set]");
  if (search) {
    $("#search-rule-set").value = search.dataset.searchRuleSet;
    chooseSearchRuleSet();
    $(".retrieval-section").scrollIntoView({block:"start"}); $("#search-query").focus({preventScroll:true});
  }
});
function chooseSearchRuleSet() {
  if ($("#search-rule-set").value) $("#search-collection").value = "rules";
  $("#search-collection").disabled = Boolean($("#search-rule-set").value);
  clearSearch();
}
$("#search-rule-set").addEventListener("change", chooseSearchRuleSet);
$("#rule-set-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (savingRuleSet) return;
  const payload = {name:$("#rule-set-form [name=name]").value, book_ids:[...event.target.querySelectorAll("[name=book_ids]:checked")].map((input) => input.value)};
  $("#rule-set-error").hidden = true;
  if (!payload.book_ids.length) { $("#rule-set-error").textContent = "Choose at least one indexed rulebook."; $("#rule-set-error").hidden = false; return; }
  const group = editingRuleSet;
  if (group) payload.expected_revision = group.revision;
  savingRuleSet = true; $("#save-rule-set").disabled = $("#close-rule-set").disabled = true;
  try {
    const saved = await api(`/api/rule-sets${group ? `/${group.id}` : ""}`, {method:group ? "PUT" : "POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    receiveMaterialRuleSets([...materialRuleSets.filter((item) => item.id !== saved.id), saved].sort((a,b) => a.name.localeCompare(b.name)));
    $("#rule-set-dialog").close();
    notice(`“${saved.name}” saved. Select it when starting a campaign, or change an existing campaign's Rulebook set.`);
  } catch (error) { $("#rule-set-error").textContent = error.message; $("#rule-set-error").hidden = false; }
  finally { savingRuleSet = false; $("#save-rule-set").disabled = $("#close-rule-set").disabled = false; }
});
// The archive request can finish before this deferred controller is ready.
refresh();
