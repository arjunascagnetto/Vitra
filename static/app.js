const state = {
  videos: [],
  categories: [],
  selectedId: null,
  selectedVideo: null,
  activeTab: "short",
};

const els = {
  form: document.querySelector("#video-form"),
  status: document.querySelector("#status"),
  grid: document.querySelector("#grid"),
  detail: document.querySelector("#detail"),
  detailPanel: document.querySelector("#detail .detail"),
  search: document.querySelector("#search"),
  categoryFilter: document.querySelector("#category-filter"),
};

els.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setStatus("Elaborazione in corso. Per video lunghi può richiedere alcuni minuti.");
  const formData = new FormData(els.form);
  try {
    const response = await fetch("/api/videos", { method: "POST", body: formData });
    const data = await readJson(response);
    state.selectedId = data.video.id;
    await loadVideos();
    await selectVideo(data.video.id);
    setStatus("Video elaborato e salvato nel database.");
  } catch (error) {
    setStatus(error.message, true);
  }
});

els.search.addEventListener("input", debounce(loadVideos, 250));
els.categoryFilter.addEventListener("change", loadVideos);

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
      const poster = document.createElement("button");
      poster.type = "button";
      poster.className = `poster${video.id === state.selectedId ? " selected" : ""}`;
      poster.title = video.title;
      poster.addEventListener("click", () => selectVideo(video.id));
      const thumb = video.thumbnail
        ? `<img class="thumb" src="${escapeHtml(video.thumbnail)}" alt="${escapeHtml(video.title)}">`
        : '<div class="thumb"></div>';
      poster.innerHTML = `
        ${thumb}
        <span class="poster-title">${escapeHtml(video.title)}</span>
      `;
      posterGrid.append(poster);
    }
    els.grid.append(section);
  }
}

async function selectVideo(id) {
  state.selectedId = id;
  const response = await fetch(`/api/videos/${id}`);
  state.selectedVideo = await readJson(response);
  state.activeTab = "short";
  renderGrid();
  renderDetail();
  if (!els.detail.open) els.detail.showModal();
}

function renderDetail() {
  const video = state.selectedVideo;
  if (!video) return;
  const body = renderActiveTab(video);
  els.detailPanel.innerHTML = `
    <div class="detail-head">
      <div>
        <span class="pill">${escapeHtml(video.category)}</span>
        <h2>${escapeHtml(video.title)}</h2>
        <p class="meta">${escapeHtml(video.uploader || "")} ${video.webpage_url ? `· <a href="${escapeHtml(video.webpage_url)}" target="_blank" rel="noreferrer">Apri video</a>` : ""}</p>
      </div>
      <button class="icon-button" type="button" data-close aria-label="Chiudi">×</button>
    </div>
    <div class="exports">
      <a class="button-link" href="/api/videos/${video.id}/export/summary.txt">Riassunto TXT</a>
      <a class="button-link" href="/api/videos/${video.id}/export/summary.pdf">Riassunto PDF</a>
      <a class="button-link" href="/api/videos/${video.id}/export/transcript.txt">Trascrizione TXT</a>
      <a class="button-link" href="/api/videos/${video.id}/export/transcript.pdf">Trascrizione PDF</a>
      <a class="button-link" href="/api/videos/${video.id}/export/transcript.json">JSON timestamp</a>
      <a class="button-link" href="/api/videos/${video.id}/audio">Audio MP3</a>
    </div>
    <audio class="audio-player" controls preload="metadata" src="/api/videos/${video.id}/audio"></audio>
    <div class="tabs">
      <button class="tab ${state.activeTab === "short" ? "active" : ""}" data-tab="short">Breve</button>
      <button class="tab ${state.activeTab === "long" ? "active" : ""}" data-tab="long">Lungo</button>
      <button class="tab ${state.activeTab === "points" ? "active" : ""}" data-tab="points">Punti</button>
      <button class="tab ${state.activeTab === "transcript" ? "active" : ""}" data-tab="transcript">Trascrizione</button>
    </div>
    <div class="text-block ${state.activeTab !== "transcript" ? "markdown" : ""}">${body}</div>
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
}

function renderActiveTab(video) {
  if (state.activeTab === "short") return renderMarkdown(video.summary_short || video.summary || "");
  if (state.activeTab === "long") return renderMarkdown(video.summary_long || video.summary || "");
  if (state.activeTab === "points") return renderKeyPoints(video.key_points || []);
  return escapeHtml(video.transcript || "");
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
    const category = video.category || "Generale";
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
