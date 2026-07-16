const state = { current: null, reviews: [], checklists: [], busy: false, categoryEdited: false };
const $ = (selector) => document.querySelector(selector);
const fileInput = $("#fileInput");
const uploadCard = $("#uploadCard");
const welcomeView = $("#welcomeView");
const messageList = $("#messageList");
const composer = $("#composer");
const messageInput = $("#messageInput");

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
}

function artifactUrl(path) {
  if (!path) return "#";
  return "/artifacts/" + path.replace(/^output[\\/]/, "").replaceAll("\\", "/");
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.hidden = false;
  window.setTimeout(() => { toast.hidden = true; }, 3500);
}

function formatTime(value) {
  try { return new Date(value).toLocaleTimeString([], {hour: "numeric", minute: "2-digit"}); }
  catch { return ""; }
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = response.headers.get("content-type")?.includes("application/json") ? await response.json() : null;
  if (!response.ok) throw new Error(data?.detail || "The request could not be completed.");
  return data;
}

function setBusy(busy, label = "Researching the provider website and preparing evidence...", showProgress = false) {
  state.busy = busy;
  fileInput.disabled = busy;
  messageInput.disabled = busy;
  $(".send-button").disabled = busy;
  if (busy) {
    welcomeView.hidden = true;
    messageList.hidden = false;
    const content = showProgress
      ? `<div class="progress-card">
          <div class="progress-heading"><span class="progress-title"><span class="loader progress-loader" aria-hidden="true"></span><span class="progress-label">${escapeHtml(label)}</span></span><strong class="progress-percent">0%</strong></div>
          <div class="progress-track" role="progressbar" aria-label="Document review progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><span></span></div>
          <div class="progress-stage" aria-live="polite">Starting the review</div>
        </div>`
      : `<span class="loader"></span><span>${escapeHtml(label)}</span>`;
    messageList.insertAdjacentHTML("beforeend", `<div class="message loading-message"><div class="avatar">E</div><div class="message-bubble loading-card ${showProgress ? "with-progress" : ""}">${content}</div></div>`);
    messageList.parentElement.scrollTop = messageList.parentElement.scrollHeight;
  } else {
    $(".loading-message")?.remove();
  }
}

function updateReviewProgress(value, stage) {
  const progress = Math.max(0, Math.min(100, Number(value) || 0));
  const bar = $(".progress-track");
  if (!bar) return;
  bar.setAttribute("aria-valuenow", String(progress));
  bar.querySelector("span").style.width = `${progress}%`;
  $(".progress-percent").textContent = `${progress}%`;
  $(".progress-stage").textContent = stage || "Reviewing the document";
}

async function streamReviewUpload(form) {
  const response = await fetch("/api/reviews", {method: "POST", body: form});
  if (!response.ok) {
    const data = response.headers.get("content-type")?.includes("application/json") ? await response.json() : null;
    throw new Error(data?.detail || "The application could not be reviewed.");
  }
  if (!response.body || !response.headers.get("content-type")?.includes("application/x-ndjson")) {
    return await response.json();
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let reviewId = null;
  const consume = line => {
    if (!line.trim()) return;
    const event = JSON.parse(line);
    if (event.type === "progress") updateReviewProgress(event.progress, event.stage);
    if (event.type === "complete") {
      reviewId = event.review_id;
      updateReviewProgress(100, event.stage || "Review complete");
    }
    if (event.type === "error") throw new Error(event.message || "The application could not be reviewed.");
  };

  while (true) {
    const {value, done} = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    lines.forEach(consume);
    if (done) break;
  }
  if (buffer.trim()) consume(buffer);
  if (!reviewId) throw new Error("The review ended without a completed result.");
  return await api(`/api/reviews/${reviewId}`);
}

function renderMessages(review) {
  welcomeView.hidden = true;
  messageList.hidden = false;
  composer.hidden = false;
  messageList.innerHTML = review.messages.map(message => {
    const user = message.role === "user";
    return `<article class="message ${user ? "user" : "assistant"}">
      ${user ? "" : '<div class="avatar" aria-hidden="true">E</div>'}
      <div class="message-bubble">${escapeHtml(message.content)}<span class="message-time">${formatTime(message.at)}</span></div>
    </article>`;
  }).join("");
  messageList.parentElement.scrollTop = messageList.parentElement.scrollHeight;
}

function detail(label, value, wide = false, href = null) {
  const content = href ? `<a href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${escapeHtml(value || "Not extracted")}</a>` : `<strong>${escapeHtml(value || "Not extracted")}</strong>`;
  return `<div class="detail ${wide ? "wide" : ""}"><span>${escapeHtml(label)}</span>${content}</div>`;
}

function renderInspector(review) {
  $("#inspectorEmpty").hidden = true;
  $("#inspectorContent").hidden = false;
  const app = review.application;
  $("#phasePill").textContent = review.phase.replaceAll("_", " ");
  $("#requestDetails").innerHTML = [
    detail("Participant", app.participant_name), detail("Age", app.participant_age),
    detail("Category", app.category?.replaceAll("_", " ")), detail("Requested price", app.requested_price_text),
    detail("Provider", app.provider_name, true), detail("Request", app.requested_item, true),
    detail("Public website", app.website_url, true, app.website_url)
  ].join("");
  $("#findingCount").textContent = `${review.findings.length} items`;
  const evidenceByCriterion = new Map();
  review.evidence.filter(item => item.criterion_id).forEach(item => evidenceByCriterion.set(item.criterion_id, item));
  $("#findingList").innerHTML = review.findings.map(finding => {
    const evidence = evidenceByCriterion.get(finding.criterion_id);
    const statusClass = finding.status.toLowerCase().replaceAll(" ", "-");
    return `<article class="finding"><div class="finding-head"><h3>${escapeHtml(finding.label)}</h3><span class="status ${statusClass}">${escapeHtml(finding.status)}</span></div>
      <p>${escapeHtml(finding.note)}</p>
      ${evidence ? `<a class="evidence-link" href="${artifactUrl(evidence.stamped_path)}" target="_blank">View ${escapeHtml(evidence.id)} →</a>` : ""}</article>`;
  }).join("");
  const links = [];
  if (review.report_html) links.push(`<a href="${artifactUrl(review.report_html)}" target="_blank">Open report</a>`);
  if (review.report_pdf) links.push(`<a href="${artifactUrl(review.report_pdf)}" target="_blank">Open PDF</a>`);
  $("#reportLinks").innerHTML = links.join("");
}

function renderHistory() {
  const history = $("#reviewHistory");
  if (!state.reviews.length) { history.innerHTML = '<p class="muted small">No reviews yet.</p>'; return; }
  history.innerHTML = state.reviews.map(review => `<button class="history-item ${state.current?.id === review.id ? "active" : ""}" data-id="${review.id}">
    <strong>${escapeHtml(review.application.requested_item || review.application_filename)}</strong><span>${escapeHtml(review.application.provider_name || review.phase.replaceAll("_", " "))}</span></button>`).join("");
  history.querySelectorAll("button").forEach(button => button.addEventListener("click", () => loadReview(button.dataset.id)));
}

function render(review) {
  state.current = review;
  $("#reviewEyebrow").textContent = `REVIEW ${review.id.slice(0,8).toUpperCase()}`;
  $("#reviewTitle").textContent = review.application.requested_item || review.application_filename;
  $("#downloadButton").hidden = !review.package_zip;
  renderMessages(review);
  renderInspector(review);
  const existing = state.reviews.findIndex(item => item.id === review.id);
  if (existing >= 0) state.reviews[existing] = review; else state.reviews.unshift(review);
  renderHistory();
}

async function loadReview(id) {
  try { render(await api(`/api/reviews/${id}`)); }
  catch (error) { showToast(error.message); }
}

async function upload(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".pdf")) { showToast("Please choose a PDF application form."); return; }
  const form = new FormData();
  form.append("file", file);
  setBusy(true, `Reviewing ${file.name}`, true);
  try {
    const review = await streamReviewUpload(form);
    setBusy(false);
    render(review);
  } catch (error) {
    setBusy(false);
    showToast(error.message);
    if (!state.current) { welcomeView.hidden = false; messageList.hidden = true; }
  } finally { fileInput.value = ""; }
}

async function sendMessage(message) {
  if (!state.current || !message.trim() || state.busy) return;
  const optimistic = structuredClone(state.current);
  optimistic.messages.push({role: "user", content: message.trim(), at: new Date().toISOString()});
  renderMessages(optimistic);
  messageInput.value = "";
  setBusy(true, "Updating the review and report package...");
  try {
    const review = await api(`/api/reviews/${state.current.id}/messages`, {method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({message: message.trim()})});
    setBusy(false);
    render(review);
  } catch (error) { setBusy(false); showToast(error.message); render(state.current); }
}

function resetView() {
  state.current = null;
  welcomeView.hidden = false;
  messageList.hidden = true;
  messageList.innerHTML = "";
  composer.hidden = true;
  $("#reviewEyebrow").textContent = "NEW REVIEW";
  $("#reviewTitle").textContent = "Website evidence review";
  $("#downloadButton").hidden = true;
  $("#inspectorEmpty").hidden = false;
  $("#inspectorContent").hidden = true;
  renderHistory();
}

function identifier(value = "") {
  return String(value).toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 60);
}

function criterionRow(values = {}) {
  const row = document.createElement("div");
  row.className = "criterion-row";
  row.innerHTML = `<div class="criterion-row-head"><strong>Checklist item</strong><button type="button" class="remove-criterion" aria-label="Remove this checklist item">Remove</button></div>
    <label><span>Item to verify</span><input class="criterion-label" required maxlength="160" value="${escapeHtml(values.label || "")}" placeholder="Example: Price is published"></label>
    <div class="criterion-options">
      <label><span>Review source</span><select class="criterion-scope"><option value="public_web">Public website</option><option value="internal">Internal review</option></select></label>
      <label><span>Special comparison</span><select class="criterion-rule"><option value="">None</option><option value="price_match">Match website price to application</option></select></label>
    </div>
    <label class="evidence-terms"><span>Evidence words</span><input value="${escapeHtml((values.evidence_terms || []).join(", "))}" placeholder="price, fee, annual"><small>Optional comma-separated words that help find relevant page sections.</small></label>`;
  row.querySelector(".criterion-scope").value = values.scope || "public_web";
  row.querySelector(".criterion-rule").value = values.rule || "";
  row.querySelector(".remove-criterion").addEventListener("click", () => {
    if ($("#criterionEditor").children.length <= 1) return showToast("A checklist needs at least one item.");
    row.remove();
  });
  $("#criterionEditor").appendChild(row);
}

function renderChecklistLibrary() {
  $("#checklistTotal").textContent = `${state.checklists.length} total`;
  const library = $("#checklistLibrary");
  if (!state.checklists.length) {
    library.innerHTML = '<p class="muted small">No checklists are available.</p>';
    return;
  }
  library.innerHTML = state.checklists.map(checklist => `<article class="checklist-card">
    <div class="checklist-card-head"><div><strong>${escapeHtml(checklist.display_name)}</strong><span>${escapeHtml(checklist.category)}</span></div><button type="button" data-remove-checklist="${escapeHtml(checklist.category)}">Remove</button></div>
    <details><summary>${checklist.criteria.length} checklist item${checklist.criteria.length === 1 ? "" : "s"}</summary>
      <ul>${checklist.criteria.map(item => `<li><span>${escapeHtml(item.label)}</span><small>${item.scope === "internal" ? "Internal review" : "Public website"}${item.rule === "price_match" ? " - price comparison" : ""}</small></li>`).join("")}</ul>
    </details>
  </article>`).join("");
  library.querySelectorAll("[data-remove-checklist]").forEach(button => button.addEventListener("click", async () => {
    const category = button.dataset.removeChecklist;
    const checklist = state.checklists.find(item => item.category === category);
    if (!window.confirm(`Remove the ${checklist?.display_name || category} checklist? Existing reports will remain, but new reviews cannot use it.`)) return;
    button.disabled = true;
    try {
      await api(`/api/checklists/${encodeURIComponent(category)}`, {method: "DELETE"});
      await loadChecklists();
      showToast("Checklist removed.");
    } catch (error) { showToast(error.message); button.disabled = false; }
  }));
}

async function loadChecklists() {
  state.checklists = await api("/api/checklists");
  renderChecklistLibrary();
}

function resetChecklistForm() {
  $("#checklistForm").reset();
  $("#criterionEditor").innerHTML = "";
  state.categoryEdited = false;
  criterionRow();
}

async function openChecklistSettings() {
  try {
    await loadChecklists();
    resetChecklistForm();
    $("#checklistDialog").showModal();
  } catch (error) { showToast(error.message); }
}

async function saveChecklist(event) {
  event.preventDefault();
  const criteria = [...document.querySelectorAll(".criterion-row")].map((row, index) => {
    const label = row.querySelector(".criterion-label").value.trim();
    return {
      id: identifier(label) || `criterion_${index + 1}`,
      label,
      scope: row.querySelector(".criterion-scope").value,
      evidence_terms: row.querySelector(".evidence-terms input").value.split(",").map(value => value.trim()).filter(Boolean),
      absence_status: "Needs Review",
      rule: row.querySelector(".criterion-rule").value || null
    };
  });
  const payload = {
    category: identifier($("#checklistCategory").value),
    display_name: $("#checklistName").value.trim(),
    aliases: $("#checklistAliases").value.split(",").map(value => value.trim()).filter(Boolean),
    criteria
  };
  const submit = event.submitter;
  submit.disabled = true;
  submit.textContent = "Saving...";
  try {
    await api("/api/checklists", {method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(payload)});
    await loadChecklists();
    resetChecklistForm();
    showToast("Checklist added and ready for new reviews.");
  } catch (error) { showToast(error.message); }
  finally { submit.disabled = false; submit.textContent = "Save checklist"; }
}

fileInput.addEventListener("change", event => upload(event.target.files[0]));
["dragenter", "dragover"].forEach(name => uploadCard.addEventListener(name, event => { event.preventDefault(); uploadCard.classList.add("dragover"); }));
["dragleave", "drop"].forEach(name => uploadCard.addEventListener(name, event => { event.preventDefault(); uploadCard.classList.remove("dragover"); }));
uploadCard.addEventListener("drop", event => upload(event.dataTransfer.files[0]));
$("#newReviewButton").addEventListener("click", resetView);
$("#downloadButton").addEventListener("click", () => { if (state.current) window.location.href = `/api/reviews/${state.current.id}/download`; });
$("#composer").addEventListener("submit", event => { event.preventDefault(); sendMessage(messageInput.value); });
messageInput.addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendMessage(messageInput.value); } });
messageInput.addEventListener("input", () => { messageInput.style.height = "auto"; messageInput.style.height = `${Math.min(messageInput.scrollHeight, 130)}px`; });
document.querySelectorAll("[data-prompt]").forEach(button => button.addEventListener("click", () => sendMessage(button.dataset.prompt)));
$("#checklistSettingsButton").addEventListener("click", openChecklistSettings);
$("#closeChecklistDialog").addEventListener("click", () => $("#checklistDialog").close());
$("#checklistDialog").addEventListener("click", event => { if (event.target === $("#checklistDialog")) $("#checklistDialog").close(); });
$("#addCriterionButton").addEventListener("click", () => criterionRow());
$("#checklistForm").addEventListener("submit", saveChecklist);
$("#checklistName").addEventListener("input", event => { if (!state.categoryEdited) $("#checklistCategory").value = identifier(event.target.value); });
$("#checklistCategory").addEventListener("input", event => { state.categoryEdited = Boolean(event.target.value); event.target.value = identifier(event.target.value); });

(async function init() {
  try { state.reviews = await api("/api/reviews"); renderHistory(); await loadChecklists(); }
  catch { renderHistory(); }
})();
