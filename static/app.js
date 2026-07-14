const state = {
  videos: [],
  categories: [],
  selectedId: null,
  selectedVideo: null,
  activeTab: "short",
};

const els = {
  form: document.querySelector("#video-form"),
  submit: document.querySelector("#video-form button[type=submit]"),
  status: document.querySelector("#status"),
  progress: document.querySelector("#progress"),
  grid: document.querySelector("#grid"),
  detail: document.querySelector("#detail"),
  detailPanel: document.querySelector("#detail .detail"),
  confirm: document.querySelector("#confirm"),
  settings: document.querySelector("#settings"),
  categoriesDialog: document.querySelector("#categories"),
  manageCategories: document.querySelector("#manage-categories"),
  generalChat: document.querySelector("#general-chat"),
  openGeneralChat: document.querySelector("#open-general-chat"),
  search: document.querySelector("#search"),
  categoryFilter: document.querySelector("#category-filter"),
};

// In-page confirmation dialog styled like the rest of the app (replaces the
// native window.confirm). Returns a Promise<boolean>.
function openConfirm({ title, bodyHtml, confirmLabel = "Conferma", danger = false }) {
  const dlg = els.confirm;
  dlg.querySelector(".confirm-title").textContent = title;
  dlg.querySelector(".confirm-body").innerHTML = bodyHtml;
  const ok = dlg.querySelector("[data-ok]");
  const cancel = dlg.querySelector("[data-cancel]");
  ok.textContent = confirmLabel;
  ok.classList.toggle("danger", danger);
  return new Promise((resolve) => {
    const finish = (result) => {
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onCancel);
      dlg.removeEventListener("cancel", onDismiss);
      dlg.removeEventListener("click", onBackdrop);
      if (dlg.open) dlg.close();
      resolve(result);
    };
    const onOk = () => finish(true);
    const onCancel = () => finish(false);
    const onDismiss = (event) => {
      event.preventDefault();
      finish(false);
    };
    const onBackdrop = (event) => {
      if (event.target === dlg) finish(false);
    };
    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onCancel);
    dlg.addEventListener("cancel", onDismiss);
    dlg.addEventListener("click", onBackdrop);
    dlg.showModal();
  });
}

// Backend processing is a single synchronous request, so we can't report real
// progress. Show an indeterminate bar and advance through the typical pipeline
// stages on a timer for feedback. The last stage stays put until the request ends.
const PROCESSING_STAGES = [
  "Download dell'audio…",
  "Conversione audio…",
  "Trascrizione con Whisper… (per video lunghi può richiedere alcuni minuti)",
  "Generazione di riassunto e categoria…",
  "Calcolo dell'embedding…",
  "Salvataggio nel database…",
];
let processingTimer = null;

function startProcessing() {
  let stage = 0;
  els.submit.disabled = true;
  els.progress.hidden = false;
  setStatus(PROCESSING_STAGES[0]);
  clearInterval(processingTimer);
  processingTimer = setInterval(() => {
    stage = Math.min(stage + 1, PROCESSING_STAGES.length - 1);
    setStatus(PROCESSING_STAGES[stage]);
  }, 6000);
}

function finishProcessing(message, isError = false) {
  clearInterval(processingTimer);
  processingTimer = null;
  els.progress.hidden = true;
  els.submit.disabled = false;
  setStatus(message, isError);
}

els.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const formData = new FormData(els.form);

  // Cost estimate (real Whisper price, from the source duration) before any
  // download/transcription. Ask for confirmation, since processing costs money.
  let estimate;
  try {
    setStatus("Calcolo della stima di costo…");
    const response = await fetch("/api/videos/estimate", { method: "POST", body: formData });
    estimate = await readJson(response);
  } catch (error) {
    setStatus(error.message, true);
    return;
  }
  const choices = await openSettings(estimate);
  if (!choices) {
    setStatus("Elaborazione annullata.");
    return;
  }
  formData.set("transcription_backend", choices.transcription_backend);

  startProcessing();
  try {
    const response = await fetch("/api/videos", { method: "POST", body: formData });
    const data = await readJson(response);
    state.selectedId = data.video.id;
    await loadVideos();
    await selectVideo(data.video.id);
    finishProcessing("Video elaborato e salvato nel database.");
  } catch (error) {
    finishProcessing(error.message, true);
  }
});

function costTableHtml(costs, models) {
  if (!costs || costs.total_usd == null) {
    return `<p class="confirm-note">Durata non disponibile: impossibile stimare il costo.</p>`;
  }
  models = models || {};
  const row = (label, model, value) => `
    <tr>
      <td>${escapeHtml(label)}${model ? ` <span class="cost-model">${escapeHtml(model)}</span>` : ""}</td>
      <td class="cost-value">${formatCost(value)}</td>
    </tr>`;
  return `
    <table class="cost-table">
      <tbody>
        ${row("Trascrizione", models.transcription, costs.transcription_usd)}
        ${row("Riassunti e categoria", models.summary, costs.summary_usd)}
        ${costs.translation_usd ? row("Traduzione in italiano", models.summary, costs.translation_usd) : ""}
        ${row("Embedding", models.embedding, costs.embedding_usd)}
      </tbody>
      <tfoot>${row("Totale stimato", "", costs.total_usd)}</tfoot>
    </table>`;
}

// Recompute the displayed costs when transcription runs locally (free).
function adjustedCosts(costs, transcriptionLocal) {
  if (!costs || costs.total_usd == null) return costs;
  const transcription = transcriptionLocal ? 0 : costs.transcription_usd;
  const total =
    (transcription || 0) +
    (costs.summary_usd || 0) +
    (costs.translation_usd || 0) +
    (costs.embedding_usd || 0);
  return {
    transcription_usd: transcription,
    summary_usd: costs.summary_usd,
    translation_usd: costs.translation_usd,
    embedding_usd: costs.embedding_usd,
    total_usd: Math.round(total * 10000) / 10000,
  };
}

// Settings dialog opened on "Processa": choose model backend per phase and see the
// cost update live. Summary/embedding local backends are not wired yet (disabled).
function openSettings(estimate) {
  const dlg = els.settings;
  const body = dlg.querySelector(".settings-body");
  const models = estimate.models || {};
  const baseCosts = estimate.costs || {};
  const minutes = estimate.duration ? Math.round(estimate.duration / 60) : "?";

  body.innerHTML = `
    <p class="confirm-meta">${escapeHtml(estimate.title || "Video")} · durata ~${minutes} min</p>
    <div class="settings-rows">
      <label class="settings-row">
        <span>Trascrizione</span>
        <select data-phase="transcription">
          <option value="local">Locale · large-v3 (gratis)</option>
          <option value="openai">Cloud · whisper-1</option>
        </select>
      </label>
      <label class="settings-row">
        <span>Riassunti</span>
        <select data-phase="summary">
          <option value="openai">Cloud · ${escapeHtml(models.summary || "gpt")}</option>
          <option value="local" disabled>Locale (presto)</option>
        </select>
      </label>
      <label class="settings-row">
        <span>Embedding</span>
        <select data-phase="embedding">
          <option value="openai">Cloud · ${escapeHtml(models.embedding || "")}</option>
          <option value="local" disabled>Locale (presto)</option>
        </select>
      </label>
    </div>
    <div class="settings-cost"></div>
    <p class="confirm-note">La trascrizione locale gira su questa macchina ed è gratuita. Backend locale per riassunti/embedding in arrivo.</p>
  `;

  const transcriptionSelect = body.querySelector('[data-phase="transcription"]');
  transcriptionSelect.value = "local";
  const costBox = body.querySelector(".settings-cost");
  const renderCost = () => {
    const local = transcriptionSelect.value === "local";
    costBox.innerHTML = costTableHtml(adjustedCosts(baseCosts, local), {
      transcription: local ? "large-v3" : "whisper-1",
      summary: models.summary,
      embedding: models.embedding,
    });
  };
  transcriptionSelect.addEventListener("change", renderCost);
  renderCost();

  const ok = dlg.querySelector("[data-ok]");
  const cancel = dlg.querySelector("[data-cancel]");
  return new Promise((resolve) => {
    const finish = (result) => {
      ok.removeEventListener("click", onOk);
      cancel.removeEventListener("click", onCancel);
      dlg.removeEventListener("cancel", onDismiss);
      dlg.removeEventListener("click", onBackdrop);
      if (dlg.open) dlg.close();
      resolve(result);
    };
    const onOk = () => finish({ transcription_backend: transcriptionSelect.value });
    const onCancel = () => finish(null);
    const onDismiss = (event) => {
      event.preventDefault();
      finish(null);
    };
    const onBackdrop = (event) => {
      if (event.target === dlg) finish(null);
    };
    ok.addEventListener("click", onOk);
    cancel.addEventListener("click", onCancel);
    dlg.addEventListener("cancel", onDismiss);
    dlg.addEventListener("click", onBackdrop);
    dlg.showModal();
  });
}

function formatCost(value) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "n/d";
  if (num > 0 && num < 0.01) return "<$0.01";
  return `$${num.toFixed(2)}`;
}

els.search.addEventListener("input", debounce(loadVideos, 250));
els.categoryFilter.addEventListener("change", loadVideos);
els.manageCategories.addEventListener("click", openCategoriesManager);
els.openGeneralChat.addEventListener("click", openGeneralChat);
wireDialogClose(els.categoriesDialog);
wireDialogClose(els.generalChat);

// Categories selected as scope for the general chat (empty = all videos).
const gchatScope = new Set();

async function openGeneralChat() {
  const dlg = els.generalChat;
  const messagesEl = dlg.querySelector("[data-gchat-messages]");
  const form = dlg.querySelector("[data-gchat-form]");
  const input = dlg.querySelector("[data-gchat-input]");
  const catsEl = dlg.querySelector("[data-gchat-categories]");
  const resetBtn = dlg.querySelector("[data-gchat-reset]");

  const appendMessage = (role, content) => {
    const bubble = document.createElement("div");
    bubble.className = `chat-bubble ${role}`;
    if (role === "user") {
      bubble.textContent = content;
    } else {
      bubble.classList.add("markdown");
      bubble.innerHTML = renderMarkdown(content);
    }
    messagesEl.append(bubble);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return bubble;
  };
  const renderMessages = (messages) => {
    messagesEl.innerHTML = messages.length
      ? ""
      : '<div class="chat-empty">Chiedi qualcosa sui video dell\'archivio.</div>';
    messages.forEach((m) => appendMessage(m.role, m.content));
  };

  // Category scope toggles (multi-select; none = all).
  try {
    const cats = (await readJson(await fetch("/api/categories"))).categories;
    catsEl.innerHTML =
      '<span class="gchat-cat-label">Parla con:</span>' +
      (cats
        .map(
          (c) =>
            `<button type="button" class="cat-toggle${
              gchatScope.has(c.name) ? " active" : ""
            }" data-cat="${escapeHtml(c.name)}">${escapeHtml(c.name)}</button>`
        )
        .join("") || '<span class="gchat-cat-label">nessuna categoria</span>');
    catsEl.querySelectorAll(".cat-toggle").forEach((button) => {
      button.addEventListener("click", () => {
        const name = button.dataset.cat;
        if (gchatScope.has(name)) gchatScope.delete(name);
        else gchatScope.add(name);
        button.classList.toggle("active");
      });
    });
  } catch (error) {
    catsEl.innerHTML = `<span class="gchat-cat-label">${escapeHtml(error.message)}</span>`;
  }

  messagesEl.innerHTML = '<div class="chat-empty">Caricamento…</div>';
  try {
    renderMessages((await readJson(await fetch("/api/chat"))).messages);
  } catch (error) {
    messagesEl.innerHTML = `<div class="chat-empty">${escapeHtml(error.message)}</div>`;
  }

  form.onsubmit = async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    const empty = messagesEl.querySelector(".chat-empty");
    if (empty) empty.remove();
    appendMessage("user", text);
    input.value = "";
    input.disabled = true;
    const pending = appendMessage("assistant", "…");
    try {
      const body = new FormData();
      body.set("message", text);
      gchatScope.forEach((name) => body.append("categories", name));
      const data = await readJson(await fetch("/api/chat", { method: "POST", body }));
      pending.classList.add("markdown");
      pending.innerHTML = renderMarkdown(data.reply.content);
    } catch (error) {
      pending.textContent = `Errore: ${error.message}`;
      pending.classList.add("error");
    } finally {
      input.disabled = false;
      input.focus();
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
  };

  resetBtn.onclick = async () => {
    const ok = await openConfirm({
      title: "Reset chat",
      bodyHtml: "<p>Cancellare tutta la cronologia della chat sull'archivio?</p>",
      confirmLabel: "Reset",
      danger: true,
    });
    if (!ok) return;
    try {
      renderMessages((await readJson(await fetch("/api/chat", { method: "DELETE" }))).messages);
    } catch (error) {
      setStatus(error.message, true);
    }
  };

  if (!dlg.open) dlg.showModal();
}

function wireDialogClose(dlg) {
  const close = () => {
    if (dlg.open) dlg.close();
  };
  dlg.querySelector("[data-cancel]").addEventListener("click", close);
  dlg.addEventListener("cancel", close);
  dlg.addEventListener("click", (event) => {
    if (event.target === dlg) close();
  });
}

async function openCategoriesManager() {
  const dlg = els.categoriesDialog;
  const body = dlg.querySelector(".categories-body");

  const render = async () => {
    let categories = [];
    try {
      categories = (await readJson(await fetch("/api/categories"))).categories;
    } catch (error) {
      body.innerHTML = `<div class="chat-empty">${escapeHtml(error.message)}</div>`;
      return;
    }
    const items = categories
      .map(
        (c) => `
        <li>
          <span>${escapeHtml(c.name)} <span class="cost-model">${c.count}</span></span>
          <button type="button" class="poster-delete cat-del" data-del="${escapeHtml(c.name)}" aria-label="Elimina" title="Elimina">${TRASH_ICON}</button>
        </li>`
      )
      .join("");
    body.innerHTML = `
      <div class="category-create">
        <input data-new-cat type="text" autocomplete="off" placeholder="Nuova categoria…" />
        <button type="button" data-create-cat>Crea</button>
      </div>
      <ul class="category-list">${items || '<li class="chat-empty">Nessuna categoria.</li>'}</ul>`;

    const input = body.querySelector("[data-new-cat]");
    const create = async () => {
      const name = input.value.trim();
      if (!name) return;
      try {
        const fd = new FormData();
        fd.set("name", name);
        await readJson(await fetch("/api/categories", { method: "POST", body: fd }));
        await render();
      } catch (error) {
        setStatus(error.message, true);
      }
    };
    body.querySelector("[data-create-cat]").addEventListener("click", create);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        create();
      }
    });
    body.querySelectorAll("[data-del]").forEach((button) => {
      button.addEventListener("click", async () => {
        const name = button.dataset.del;
        const ok = await openConfirm({
          title: "Elimina categoria",
          bodyHtml: `<p>Eliminare la categoria <strong>${escapeHtml(name)}</strong>?</p>
            <p class="confirm-note">I video associati resteranno senza categoria.</p>`,
          confirmLabel: "Elimina",
          danger: true,
        });
        if (!ok) return;
        try {
          await readJson(await fetch(`/api/categories/${encodeURIComponent(name)}`, { method: "DELETE" }));
          await render();
          await loadVideos();
        } catch (error) {
          setStatus(error.message, true);
        }
      });
    });
  };

  await render();
  if (!dlg.open) dlg.showModal();
}

async function loadVideos() {
  const params = new URLSearchParams();
  if (els.search.value.trim()) params.set("q", els.search.value.trim());
  if (els.categoryFilter.value) params.set("category", els.categoryFilter.value);
  const response = await fetch(`/api/videos?${params}`);
  const data = await readJson(response);
  state.videos = data.videos;
  state.categories = data.categories;
  renderCategories();
  renderGrid();
}

function renderCategories() {
  const current = els.categoryFilter.value;
  els.categoryFilter.innerHTML = '<option value="">Tutte le categorie</option>';
  for (const category of state.categories) {
    const option = document.createElement("option");
    option.value = category.category;
    option.textContent = `${category.category} (${category.count})`;
    els.categoryFilter.append(option);
  }
  els.categoryFilter.value = current;
}

function renderGrid() {
  els.grid.innerHTML = "";
  if (!state.videos.length) {
    els.grid.innerHTML = '<div class="empty-state"><h2>Nessun video</h2><p>Importa un link o modifica la ricerca.</p></div>';
    return;
  }

  for (const [category, videos] of groupedByCategory(state.videos)) {
    const section = document.createElement("section");
    section.className = "category-section";
    section.innerHTML = `
      <div class="category-header">
        <h2>${escapeHtml(category)}</h2>
        <span>${videos.length}</span>
      </div>
      <div class="poster-grid"></div>
    `;
    const posterGrid = section.querySelector(".poster-grid");
    for (const video of videos) {
      const poster = document.createElement("div");
      poster.className = `poster${video.id === state.selectedId ? " selected" : ""}`;
      poster.title = video.title;
      poster.setAttribute("role", "button");
      poster.tabIndex = 0;
      poster.addEventListener("click", () => selectVideo(video.id));
      poster.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectVideo(video.id);
        }
      });
      const thumb = video.thumbnail
        ? `<img class="thumb" src="${escapeHtml(video.thumbnail)}" alt="${escapeHtml(video.title)}">`
        : '<div class="thumb"></div>';
      poster.innerHTML = `
        ${thumb}
        <span class="poster-title">${escapeHtml(video.title)}</span>
        <button type="button" class="poster-delete" aria-label="Elimina" title="Elimina">${TRASH_ICON}</button>
      `;
      poster.querySelector(".poster-delete").addEventListener("click", (event) => {
        event.stopPropagation();
        deleteVideo(video.id, video.title);
      });
      posterGrid.append(poster);
    }
    els.grid.append(section);
  }
}

const TRASH_ICON =
  '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path></svg>';

async function selectVideo(id) {
  state.selectedId = id;
  const response = await fetch(`/api/videos/${id}`);
  state.selectedVideo = await readJson(response);
  state.activeTab = "short";
  renderGrid();
  renderDetail();
  if (!els.detail.open) els.detail.showModal();
}

async function deleteVideo(id, title) {
  const proceed = await openConfirm({
    title: "Elimina contenuto",
    bodyHtml: `
      <p>Eliminare definitivamente <strong>${escapeHtml(title)}</strong>?</p>
      <p class="confirm-note">L'operazione non è reversibile e rimuove anche l'audio salvato.</p>
    `,
    confirmLabel: "Elimina",
    danger: true,
  });
  if (!proceed) return;
  try {
    const response = await fetch(`/api/videos/${id}`, { method: "DELETE" });
    await readJson(response);
    if (state.selectedId === id) {
      state.selectedId = null;
      state.selectedVideo = null;
      if (els.detail.open) els.detail.close();
    }
    await loadVideos();
    setStatus("Contenuto eliminato.");
  } catch (error) {
    setStatus(error.message, true);
  }
}

function renderDetail() {
  const video = state.selectedVideo;
  if (!video) return;
  const body = renderActiveTab(video);
  els.detailPanel.innerHTML = `
    <div class="detail-head">
      <div>
        <div class="category-pill-wrap">
          <button class="pill pill-button" type="button" data-category-edit title="Cambia categoria">${escapeHtml(video.category || "Senza categoria")} ▾</button>
          <div class="category-menu" data-category-menu hidden></div>
        </div>
        <h2>${escapeHtml(video.title)}</h2>
        <p class="meta">${escapeHtml(video.uploader || "")} ${video.webpage_url ? `· <a href="${escapeHtml(video.webpage_url)}" target="_blank" rel="noreferrer">Apri video</a>` : ""}${video.estimated_cost_usd != null ? ` · Costo stimato: ${formatCost(video.estimated_cost_usd)}` : ""}</p>
      </div>
      <button class="icon-button" type="button" data-close aria-label="Chiudi">×</button>
    </div>
    <div class="exports">
      <a class="button-link" href="/api/videos/${video.id}/export/summary.txt">Riassunto TXT</a>
      <a class="button-link" href="/api/videos/${video.id}/export/summary.pdf">Riassunto PDF</a>
      <a class="button-link" href="/api/videos/${video.id}/export/transcript.txt">Trascrizione TXT</a>
      <a class="button-link" href="/api/videos/${video.id}/export/transcript.pdf">Trascrizione PDF</a>
      <a class="button-link" href="/api/videos/${video.id}/export/transcript.json">JSON timestamp</a>
      ${
        Array.isArray(video.translation) && video.translation.length
          ? `<a class="button-link" href="/api/videos/${video.id}/export/translation.txt">Traduzione TXT</a>
             <a class="button-link" href="/api/videos/${video.id}/export/translation.pdf">Traduzione PDF</a>`
          : ""
      }
      <a class="button-link" href="/api/videos/${video.id}/audio">Audio MP3</a>
    </div>
    <audio class="audio-player" controls preload="metadata" src="/api/videos/${video.id}/audio"></audio>
    <div class="tabs">${renderTabButtons(video)}</div>
    ${
      state.activeTab === "chat"
        ? `<div class="chat">
             <div class="chat-messages" data-chat-messages></div>
             <form class="chat-form" data-chat-form>
               <input class="chat-input" data-chat-input type="text" autocomplete="off"
                 placeholder="Fai una domanda su questo video…" />
               <button type="submit">Invia</button>
             </form>
             <div class="chat-actions">
               <button type="button" class="secondary" data-chat-compact>Compatta</button>
               <button type="button" class="secondary" data-chat-reset>Reset</button>
             </div>
           </div>`
        : `<div class="text-block ${state.activeTab !== "transcript" ? "markdown" : ""}">${body}</div>`
    }
  `;
  els.detailPanel.querySelector("[data-close]").addEventListener("click", () => els.detail.close());
  els.detailPanel.querySelectorAll("[data-seek]").forEach((button) => {
    button.addEventListener("click", () => seekAudio(Number(button.dataset.seek || 0)));
  });
  els.detailPanel.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => {
      state.activeTab = button.dataset.tab;
      renderDetail();
    });
  });
  if (state.activeTab === "chat") setupChat(video);

  const pill = els.detailPanel.querySelector("[data-category-edit]");
  const menu = els.detailPanel.querySelector("[data-category-menu]");
  if (pill && menu) {
    pill.addEventListener("click", async () => {
      if (!menu.hidden) {
        menu.hidden = true;
        return;
      }
      menu.hidden = false;
      await populateCategoryMenu(video, menu);
    });
  }
}

async function populateCategoryMenu(video, menu) {
  menu.innerHTML = '<div class="chat-empty">Caricamento…</div>';
  let categories = [];
  try {
    categories = (await readJson(await fetch("/api/categories"))).categories;
  } catch (error) {
    menu.innerHTML = `<div class="chat-empty">${escapeHtml(error.message)}</div>`;
    return;
  }
  const options = categories
    .map(
      (c) =>
        `<button type="button" class="category-option${
          c.name === video.category ? " current" : ""
        }" data-cat="${escapeHtml(c.name)}">${escapeHtml(c.name)}</button>`
    )
    .join("");
  menu.innerHTML = `
    <div class="category-options">${options || '<div class="chat-empty">Nessuna categoria.</div>'}</div>
    <div class="category-create">
      <input data-new-cat type="text" autocomplete="off" placeholder="Crea nuova…" />
      <button type="button" data-create-cat title="Crea e assegna">＋</button>
    </div>`;
  menu.querySelectorAll(".category-option").forEach((button) => {
    button.addEventListener("click", () => assignCategory(video.id, button.dataset.cat));
  });
  const input = menu.querySelector("[data-new-cat]");
  const create = () => {
    const name = input.value.trim();
    if (name) assignCategory(video.id, name);
  };
  menu.querySelector("[data-create-cat]").addEventListener("click", create);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      create();
    }
  });
  input.focus();
}

async function assignCategory(videoId, name) {
  try {
    const body = new FormData();
    body.set("category", name);
    const data = await readJson(
      await fetch(`/api/videos/${videoId}/category`, { method: "PUT", body })
    );
    if (state.selectedVideo && state.selectedVideo.id === videoId) {
      state.selectedVideo.category = data.category;
    }
    await loadVideos();
    renderDetail();
    setStatus(`Categoria aggiornata: ${data.category}.`);
  } catch (error) {
    setStatus(error.message, true);
  }
}

function renderTabButtons(video) {
  const hasTranslation = Array.isArray(video.translation) && video.translation.length > 0;
  const tabs = [
    ["short", "Breve"],
    ["long", "Lungo"],
    ["points", "Punti"],
    ["transcript", "Trascrizione"],
    ...(hasTranslation ? [["translation", "Traduzione"]] : []),
    ["chat", "Chat"],
  ];
  return tabs
    .map(
      ([id, label]) =>
        `<button class="tab ${state.activeTab === id ? "active" : ""}" data-tab="${id}">${label}</button>`
    )
    .join("");
}

function renderActiveTab(video) {
  if (state.activeTab === "short") return renderMarkdown(video.summary_short || video.summary || "");
  if (state.activeTab === "long") return renderMarkdown(video.summary_long || video.summary || "");
  if (state.activeTab === "points") return renderKeyPoints(video.key_points || []);
  if (state.activeTab === "translation") return renderTranslation(video);
  if (state.activeTab === "chat") return "";
  return escapeHtml(video.transcript || "");
}

function renderTranslation(video) {
  const original = video.transcript_json || [];
  const translated = video.translation || [];
  if (!translated.length) return "<p>Nessuna traduzione disponibile per questo video.</p>";
  const rows = translated
    .map((seg, i) => {
      const seconds = Number(seg.start || 0);
      const orig = (original[i] && original[i].text) || "";
      return `
        <tr>
          <td><button class="time-button" type="button" data-seek="${seconds}">${formatTimestamp(seconds)}</button></td>
          <td>${escapeHtml(orig)}</td>
          <td>${escapeHtml(seg.text || "")}</td>
        </tr>`;
    })
    .join("");
  return `
    <table class="points-table">
      <thead>
        <tr><th>Minuto</th><th>Originale</th><th>Italiano</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

async function setupChat(video) {
  const messagesEl = els.detailPanel.querySelector("[data-chat-messages]");
  const form = els.detailPanel.querySelector("[data-chat-form]");
  const input = els.detailPanel.querySelector("[data-chat-input]");
  const compactBtn = els.detailPanel.querySelector("[data-chat-compact]");
  const resetBtn = els.detailPanel.querySelector("[data-chat-reset]");
  if (!messagesEl || !form || !input) return;

  const appendMessage = (role, content) => {
    const bubble = document.createElement("div");
    bubble.className = `chat-bubble ${role}`;
    if (role === "user") {
      bubble.textContent = content;
    } else {
      bubble.classList.add("markdown");
      bubble.innerHTML = renderMarkdown(content);
    }
    messagesEl.append(bubble);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return bubble;
  };

  const renderMessages = (messages) => {
    messagesEl.innerHTML = "";
    if (!messages.length) {
      messagesEl.innerHTML = '<div class="chat-empty">Nessun messaggio. Fai una domanda sul video.</div>';
    } else {
      messages.forEach((m) => appendMessage(m.role, m.content));
    }
  };

  messagesEl.innerHTML = '<div class="chat-empty">Caricamento…</div>';
  try {
    renderMessages((await readJson(await fetch(`/api/videos/${video.id}/chat`))).messages);
  } catch (error) {
    messagesEl.innerHTML = `<div class="chat-empty">${escapeHtml(error.message)}</div>`;
  }

  compactBtn.addEventListener("click", async () => {
    compactBtn.disabled = true;
    resetBtn.disabled = true;
    try {
      const data = await readJson(
        await fetch(`/api/videos/${video.id}/chat/compact`, { method: "POST" })
      );
      renderMessages(data.messages);
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      compactBtn.disabled = false;
      resetBtn.disabled = false;
    }
  });

  resetBtn.addEventListener("click", async () => {
    const ok = await openConfirm({
      title: "Reset chat",
      bodyHtml: "<p>Cancellare tutta la cronologia di questa chat?</p>",
      confirmLabel: "Reset",
      danger: true,
    });
    if (!ok) return;
    try {
      const data = await readJson(
        await fetch(`/api/videos/${video.id}/chat`, { method: "DELETE" })
      );
      renderMessages(data.messages);
    } catch (error) {
      setStatus(error.message, true);
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    const empty = messagesEl.querySelector(".chat-empty");
    if (empty) empty.remove();
    appendMessage("user", text);
    input.value = "";
    input.disabled = true;
    const pending = appendMessage("assistant", "…");
    try {
      const body = new FormData();
      body.set("message", text);
      const data = await readJson(await fetch(`/api/videos/${video.id}/chat`, { method: "POST", body }));
      pending.classList.add("markdown");
      pending.innerHTML = renderMarkdown(data.reply.content);
    } catch (error) {
      pending.textContent = `Errore: ${error.message}`;
      pending.classList.add("error");
    } finally {
      input.disabled = false;
      input.focus();
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
  });
}

function renderKeyPoints(points) {
  if (!points.length) return "<p>Nessun punto temporale disponibile per questo video.</p>";
  const rows = points
    .map((point) => {
      const seconds = Number(point.time_seconds || 0);
      return `
        <tr>
          <td><button class="time-button" type="button" data-seek="${seconds}">${formatTimestamp(seconds)}</button></td>
          <td><strong>${escapeHtml(point.title || "")}</strong></td>
          <td>${escapeHtml(point.detail || "")}</td>
        </tr>
      `;
    })
    .join("");
  return `
    <table class="points-table">
      <thead>
        <tr>
          <th>Minuto</th>
          <th>Punto</th>
          <th>Dettaglio</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function seekAudio(seconds) {
  const player = els.detailPanel.querySelector(".audio-player");
  if (!player) return;
  player.currentTime = Math.max(0, seconds);
  player.play().catch(() => {});
}

els.detail.addEventListener("click", (event) => {
  if (event.target === els.detail) els.detail.close();
});

function groupedByCategory(videos) {
  const groups = new Map();
  for (const video of videos) {
    const category = video.category || "Senza categoria";
    if (!groups.has(category)) groups.set(category, []);
    groups.get(category).push(video);
  }
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b, "it"));
}

function renderMarkdown(value) {
  const lines = String(value ?? "").split("\n");
  const html = [];
  let listOpen = false;
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) {
      if (listOpen) {
        html.push("</ul>");
        listOpen = false;
      }
      continue;
    }
    const bullet = trimmed.match(/^[-*]\s+(.+)$/);
    if (bullet) {
      if (!listOpen) {
        html.push("<ul>");
        listOpen = true;
      }
      html.push(`<li>${inlineMarkdown(bullet[1])}</li>`);
      continue;
    }
    if (listOpen) {
      html.push("</ul>");
      listOpen = false;
    }
    const heading = trimmed.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      const level = Math.min(heading[1].length + 2, 4);
      html.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
    } else {
      html.push(`<p>${inlineMarkdown(trimmed)}</p>`);
    }
  }
  if (listOpen) html.push("</ul>");
  return html.join("");
}

function inlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.+?)\*/g, "<em>$1</em>");
}

async function readJson(response) {
  const text = await response.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: text || `Errore HTTP ${response.status}` };
  }
  if (!response.ok) throw new Error(data.detail || "Errore inatteso");
  return data;
}

function setStatus(message, isError = false) {
  els.status.textContent = message;
  els.status.className = `status${isError ? " error" : ""}`;
}

function debounce(fn, delay) {
  let timer;
  return () => {
    clearTimeout(timer);
    timer = setTimeout(fn, delay);
  };
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

// Minimal, XSS-safe Markdown → HTML for chat messages. Input is escaped first,
// then a small subset is applied: fenced/inline code, headings, bullet/numbered
// lists, bold, italic, links, and paragraph/line breaks.
function renderMarkdown(text) {
  let html = escapeHtml(text ?? "");

  // Pull code spans out first so their contents aren't touched by other rules.
  const codeBlocks = [];
  html = html.replace(/```[^\n]*\n([\s\S]*?)```/g, (_, code) => {
    codeBlocks.push(code.replace(/\n$/, ""));
    return ` CB${codeBlocks.length - 1} `;
  });
  const inlineCodes = [];
  html = html.replace(/`([^`\n]+)`/g, (_, code) => {
    inlineCodes.push(code);
    return ` IC${inlineCodes.length - 1} `;
  });

  // Block structure: headings, lists, and paragraphs.
  const out = [];
  let listType = null;
  let para = [];
  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${para.join("<br>")}</p>`);
      para = [];
    }
  };
  const closeList = () => {
    if (listType) {
      out.push(`</${listType}>`);
      listType = null;
    }
  };
  for (const line of html.split("\n")) {
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+\.\s+(.*)$/);
    if (heading) {
      flushPara();
      closeList();
      const level = heading[1].length;
      out.push(`<h${level}>${heading[2]}</h${level}>`);
    } else if (bullet) {
      flushPara();
      if (listType !== "ul") {
        closeList();
        out.push("<ul>");
        listType = "ul";
      }
      out.push(`<li>${bullet[1]}</li>`);
    } else if (numbered) {
      flushPara();
      if (listType !== "ol") {
        closeList();
        out.push("<ol>");
        listType = "ol";
      }
      out.push(`<li>${numbered[1]}</li>`);
    } else if (line.trim() === "") {
      flushPara();
      closeList();
    } else {
      closeList();
      para.push(line);
    }
  }
  flushPara();
  closeList();
  html = out.join("");

  // Inline spans (safe: block tags contain no * or ] to confuse these).
  html = html
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*\n]+)\*/g, "<em>$1</em>")
    .replace(
      // Absolute http(s) links or root-relative paths (e.g. /api/reports/<hash>.pdf).
      // Restricting the scheme keeps this XSS-safe (no javascript: URLs).
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+|\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
    );

  // Restore code spans.
  html = html.replace(/ IC(\d+) /g, (_, i) => `<code>${inlineCodes[+i]}</code>`);
  html = html.replace(
    / CB(\d+) /g,
    (_, i) => `<pre><code>${codeBlocks[+i]}</code></pre>`
  );
  return html;
}

function formatDuration(seconds) {
  if (!seconds) return "Durata n/d";
  const mins = Math.floor(seconds / 60);
  const secs = String(seconds % 60).padStart(2, "0");
  return `${mins}:${secs}`;
}

function formatDate(value) {
  return new Date(value).toLocaleDateString("it-IT");
}

function formatTimestamp(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hrs = Math.floor(total / 3600);
  const mins = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hrs) return `${String(hrs).padStart(2, "0")}:${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

loadVideos().catch((error) => setStatus(error.message, true));
