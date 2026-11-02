"use strict";
const $ = (selector) => document.querySelector(selector);
const activeStatuses = new Set(["queued", "extracting", "chunking", "embedding"]);
let books = [], snapshot = "", uploadBusy = false, searchBusy = false, lastContext = "", refreshBusy = false;
let previewBook = null, previewOffset = 0, previewTotal = 0, maxUploadMB = 100;
const escapeHTML = (value) => String(value).replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const bytes = (size) => size < 1024 * 1024 ? `${Math.max(1, Math.round(size / 1024))} KB` : `${(size / 1024 / 1024).toFixed(1)} MB`;
function notice(message, error = false) { $("#notice").textContent = message; $("#notice").classList.toggle("error", error); $("#notice").hidden = false; }
function errorMessage(data) { return Array.isArray(data.detail) ? data.detail.map((item) => item.msg).join(" ") : data.detail || "Something went wrong. Please try again."; }
async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(errorMessage(data));
  return data;
}
function readyBooks() { return books.filter((book) => book.status === "ready" && !book.needs_reindex); }
function renderBooks() {
  const ready = readyBooks(), processing = books.filter((book) => activeStatuses.has(book.status));
  $("#book-count").textContent = $("#nav-count").textContent = books.length;
  $("#library-summary").textContent = books.length ? `${ready.length} ready${processing.length ? ` · ${processing.length} processing` : ""}` : "Ready for your first book";
  $("#search-button").disabled = !ready.length || searchBusy;
  if (!lastContext && !searchBusy) $("#search-status").textContent = ready.length ? "Search by meaning or exact terms. Results are source excerpts, not an AI-generated answer." : "Your books will be searchable once indexing is complete.";
  const selected = $("#search-book").value;
  $("#search-book").innerHTML = '<option value="">All ready books</option>' + ready.map((book) => `<option value="${book.id}">${escapeHTML(book.title)}</option>`).join("");
  if (ready.some((book) => book.id === selected)) $("#search-book").value = selected;
  if (!books.length) {
    $("#library-list").innerHTML = '<div class="empty-state"><span class="empty-book" aria-hidden="true">▤</span><h3>The shelves are waiting.</h3><p>Upload your first book to start building your world.</p></div>';
    return;
  }
  $("#library-list").innerHTML = books.map((book) => {
    const active = activeStatuses.has(book.status), failed = book.status === "failed";
    const state = book.needs_reindex ? "Reindex needed" : book.status === "ready" ? "Ready for retrieval" : failed ? "Needs attention" : ({queued:"In the queue",extracting:"Extracting text",chunking:"Preparing passages",embedding:"Building knowledge"}[book.status]);
    const details = book.status === "ready" ? `${book.chunk_count.toLocaleString()} passages · ${book.page_count} ${book.format === "pdf" ? "pages" : book.format === "epub" ? "sections" : "document"}` : book.stage_detail;
    return `<article class="book-row"><div class="book-cover" aria-hidden="true">${book.format.toUpperCase()}</div><div><h3 class="book-title">${escapeHTML(book.title)}</h3><p class="book-meta">${book.format.toUpperCase()} <span>·</span> ${bytes(book.size_bytes)}</p></div><div class="book-progress"><div class="book-state ${active ? "active" : failed ? "failed" : ""}"><span class="dot"></span>${state}</div><div class="stage-detail">${escapeHTML(details)}</div>${active ? `<progress max="100" value="${book.progress}" aria-label="Processing ${escapeHTML(book.title)}"></progress>` : ""}</div><div class="book-actions">${book.status === "ready" ? `<button class="secondary-button" data-action="preview" data-id="${book.id}">View passages</button>` : ""}${!active ? `<button class="secondary-button" data-action="reindex" data-id="${book.id}">${failed ? "Retry" : "Reindex"}</button><button class="secondary-button danger" data-action="remove" data-id="${book.id}" aria-label="Remove ${escapeHTML(book.title)}">Remove</button>` : ""}</div>${book.error ? `<p class="book-error">${escapeHTML(book.error)}</p>` : ""}${book.warnings.length ? `<div class="book-warnings">${book.warnings.map(escapeHTML).join("<br>")}</div>` : ""}${book.needs_reindex ? '<p class="book-warnings">The embedding settings changed. Reindex this book before searching it.</p>' : ""}</article>`;
  }).join("");
}
async function refresh() {
  if (refreshBusy) return;
  refreshBusy = true;
  try {
    const data = await api("/api/books"), next = JSON.stringify(data.books);
    books = data.books;
    if (next !== snapshot) { snapshot = next; renderBooks(); }
  } catch (error) { notice(`Cannot reach the library. ${error.message}`, true); }
  finally { refreshBusy = false; }
}
async function poll() { await refresh(); setTimeout(poll, books.some((book) => activeStatuses.has(book.status)) ? 1400 : 6000); }
function uploadOne(file) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest(), form = new FormData();
    form.append("file", file); xhr.open("POST", "/api/books");
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      const percent = Math.round(event.loaded / event.total * 100);
      $("#upload-meter").value = percent; $("#upload-percent").textContent = `${percent}%`;
    };
    xhr.onload = () => {
      let data; try { data = JSON.parse(xhr.responseText); } catch { reject(new Error("The server returned an unreadable response.")); return; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data); else reject(new Error(errorMessage(data)));
    };
    xhr.onerror = () => reject(new Error("The upload could not reach the server."));
    xhr.onabort = () => reject(new Error("Upload cancelled."));
    xhr.send(form);
  });
}
async function uploadFiles(files) {
  if (uploadBusy || !files.length) return;
  uploadBusy = true; $("#dropzone").classList.add("busy"); $("#book-input").disabled = true; $("#sample-button").disabled = true;
  $("#upload-progress").hidden = false;
  let added = 0, duplicates = 0, errors = [];
  try {
    for (const file of files) {
      $("#upload-message").textContent = `Uploading ${file.name}`; $("#upload-percent").textContent = "0%"; $("#upload-meter").value = 0;
      try {
        if (!/\.(pdf|epub|txt|md)$/i.test(file.name)) throw new Error("Choose a PDF, EPUB, TXT, or Markdown file.");
        if (file.size > maxUploadMB * 1024 * 1024) throw new Error(`Maximum book size is ${maxUploadMB} MB.`);
        const data = await uploadOne(file); data.duplicate ? duplicates++ : added++;
      } catch (error) { errors.push(`${file.name}: ${error.message}`); }
      await refresh();
    }
    const messages = [];
    if (added) messages.push(`${added} ${added === 1 ? "book added" : "books added"}. Indexing will continue in the background. The first book downloads the local model; this can take a few minutes.`);
    if (duplicates) messages.push(`${duplicates} ${duplicates === 1 ? "file was" : "files were"} already in the library.`);
    messages.push(...errors); notice(messages.join(" "), errors.length > 0);
  } finally {
    uploadBusy = false; $("#dropzone").classList.remove("busy"); $("#book-input").disabled = false; $("#sample-button").disabled = false; $("#book-input").value = ""; $("#upload-progress").hidden = true;
  }
}
$("#book-input").addEventListener("change", (event) => uploadFiles(Array.from(event.target.files)));
for (const eventName of ["dragover", "dragenter"]) $("#dropzone").addEventListener(eventName, (event) => { event.preventDefault(); $("#dropzone").classList.add("dragging"); });
for (const eventName of ["dragleave", "drop"]) $("#dropzone").addEventListener(eventName, (event) => { event.preventDefault(); $("#dropzone").classList.remove("dragging"); });
$("#dropzone").addEventListener("drop", (event) => uploadFiles(Array.from(event.dataTransfer.files)));
// Keep dropped files outside the target from navigating away from the app.
window.addEventListener("dragover", (event) => event.preventDefault()); window.addEventListener("drop", (event) => event.preventDefault());
$("#sample-button").addEventListener("click", async () => {
  try { const response = await fetch("/static/sample-book.txt"); if (!response.ok) throw new Error("Cannot load the sample book."); await uploadFiles([new File([await response.blob()], "The Hollow Lantern.txt", {type:"text/plain"})]); }
  catch (error) { notice(error.message, true); }
});
$("#library-list").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]"); if (!button) return;
  const book = books.find((book) => book.id === button.dataset.id); if (!book) return;
  if (button.dataset.action === "preview") { previewBook = book; previewOffset = 0; $("#passage-title").textContent = book.title; $("#passage-dialog").showModal(); await loadPassages(); return; }
  if (button.dataset.action === "remove" && !window.confirm(`Remove “${book.title}” and all of its indexed passages?`)) return;
  button.disabled = true;
  try {
    await api(`/api/books/${book.id}${button.dataset.action === "reindex" ? "/reindex" : ""}`, {method:button.dataset.action === "reindex" ? "POST" : "DELETE"});
    lastContext = ""; $("#search-results").replaceChildren(); $("#context-actions").hidden = true;
    await refresh();
  } catch (error) { notice(error.message, true); button.disabled = false; }
});
async function loadPassages() {
  $("#passage-list").textContent = "Loading passages…"; $("#previous-passages").disabled = $("#next-passages").disabled = true;
  try {
    const data = await api(`/api/books/${previewBook.id}/chunks?offset=${previewOffset}&limit=3`); previewTotal = data.total;
    $("#passage-list").innerHTML = data.chunks.map((chunk) => `<article class="result-card"><div class="result-heading"><span class="citation">${escapeHTML(chunk.source_label)}</span><span class="source-tag">Passage ${chunk.ordinal + 1} · ${chunk.token_count} tokens</span></div><p class="result-text">${escapeHTML(chunk.text)}</p></article>`).join("");
    $("#passage-range").textContent = `${previewOffset + 1}–${Math.min(previewOffset + 3, previewTotal)} of ${previewTotal}`;
    $("#previous-passages").disabled = previewOffset === 0; $("#next-passages").disabled = previewOffset + 3 >= previewTotal;
  } catch (error) { $("#passage-list").textContent = error.message; }
}
$("#close-passages").addEventListener("click", () => $("#passage-dialog").close());
$("#previous-passages").addEventListener("click", () => { previewOffset = Math.max(0, previewOffset - 3); loadPassages(); });
$("#next-passages").addEventListener("click", () => { previewOffset += 3; loadPassages(); });
$("#search-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (searchBusy || !readyBooks().length) return;
  const query = $("#search-query").value.trim(); if (query.length < 2) return;
  searchBusy = true; $("#search-button").disabled = true; $("#search-button").textContent = "Searching…"; $("#search-status").textContent = "Looking through your indexed books…";
  $("#search-results").replaceChildren(); $("#context-actions").hidden = true; lastContext = "";
  try {
    const data = await api("/api/retrieve", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({query,book_id:$("#search-book").value || null,limit:5})});
    lastContext = data.context; $("#search-status").textContent = data.matches.length ? `${data.matches.length} closest passages · Check the source to confirm relevance.` : "No passages available. Add a book or wait for indexing to finish.";
    $("#search-results").innerHTML = data.matches.map((match) => `<article class="result-card"><div class="result-heading"><span class="citation">[${match.citation}] ${escapeHTML(match.title)}</span><span class="source-tag">${escapeHTML(match.source_label)}</span></div><p class="result-text">${escapeHTML(match.text)}</p></article>`).join("");
    $("#context-actions").hidden = !data.matches.length;
  } catch (error) { $("#search-status").textContent = error.message; }
  finally { searchBusy = false; $("#search-button").disabled = !readyBooks().length; $("#search-button").textContent = "Search archive ↗"; }
});
$("#copy-context").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(lastContext); $("#copy-context").textContent = "Copied!"; setTimeout(() => $("#copy-context").textContent = "Copy source context", 1800); }
  catch { notice("Clipboard access is unavailable in this browser. You can select and copy the passages directly.", true); }
});
(async () => {
  try { const health = await api("/api/health"); maxUploadMB = health.max_upload_mb; $("#upload-types").textContent = `PDF, EPUB, TXT, MD · up to ${maxUploadMB} MB each`; }
  catch (error) { notice(error.message, true); }
  renderBooks(); poll();
})();
