// AI Clip Farm — vanilla SPA. Polls the API and renders jobs + clips.
const API = "/api";
const STAGES = ["ingest", "transcribe", "analyze"];

// ---- Tabs ----
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    document.getElementById(`${tab.dataset.tab}-form`).classList.add("active");
  });
});

// ---- YouTube submit ----
document.getElementById("youtube-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = document.getElementById("yt-url").value.trim();
  const title = document.getElementById("yt-title").value.trim();
  await fetch(`${API}/videos`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source_type: "youtube", source_ref: url, title: title || null }),
  });
  e.target.reset();
  refresh();
});

// ---- Upload ----
const fileInput = document.getElementById("file-input");
const fileLabel = document.getElementById("file-label");
const filedrop = document.getElementById("filedrop");
fileInput.addEventListener("change", () => {
  fileLabel.textContent = fileInput.files[0]?.name || "Drop an MP4 here or click to browse";
});
["dragover", "dragenter"].forEach((ev) =>
  filedrop.addEventListener(ev, (e) => { e.preventDefault(); filedrop.classList.add("drag"); })
);
["dragleave", "drop"].forEach((ev) =>
  filedrop.addEventListener(ev, (e) => { e.preventDefault(); filedrop.classList.remove("drag"); })
);
filedrop.addEventListener("drop", (e) => {
  fileInput.files = e.dataTransfer.files;
  fileLabel.textContent = fileInput.files[0]?.name || "";
});
document.getElementById("upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!fileInput.files[0]) return;
  const fd = new FormData();
  fd.append("file", fileInput.files[0]);
  await fetch(`${API}/videos/upload`, { method: "POST", body: fd });
  e.target.reset();
  fileLabel.textContent = "Drop an MP4 here or click to browse";
  refresh();
});

// ---- Render ----
async function refresh() {
  const videos = await (await fetch(`${API}/videos`)).json();
  const list = document.getElementById("video-list");
  if (!videos.length) {
    list.innerHTML = `<div class="empty">No jobs yet — paste a YouTube URL above to begin.</div>`;
    return;
  }
  const details = await Promise.all(
    videos.map((v) => fetch(`${API}/videos/${v.id}`).then((r) => r.json()))
  );
  list.innerHTML = details.map(renderVideo).join("");
}

function renderVideo(v) {
  const stageMap = Object.fromEntries((v.jobs || []).map((j) => [j.stage, j]));
  const stages = STAGES.map((s) => {
    const st = stageMap[s]?.status || "pending";
    return `<span class="stage ${st}">${s}${
      stageMap[s]?.progress > 0 && st === "running"
        ? ` ${Math.round(stageMap[s].progress * 100)}%`
        : ""
    }</span>`;
  }).join("");

  const done = (v.clips || []).filter((c) => c.status === "completed").length;
  const clips = (v.clips || [])
    .map(
      (c) => `
    <div class="clip-card" onclick='openClip(${JSON.stringify(c.id)})'>
      <div class="clip-thumb" style="${
        c.thumbnail_path ? `background-image:url(${API}/clips/${c.id}/thumbnail)` : ""
      }">
        <span class="clip-score">${Math.round(c.score)}</span>
        ${c.status !== "completed" ? `<span>${c.status}</span>` : ""}
      </div>
      <div class="clip-info">
        <div class="t">${escapeHtml(c.gen_title || c.reason || "Clip")}</div>
        <div class="d">${(c.end_seconds - c.start_seconds).toFixed(0)}s · #${c.rank}</div>
      </div>
    </div>`
    )
    .join("");

  return `
  <div class="video-item">
    <div class="video-head">
      <div>
        <div class="video-title">${escapeHtml(v.title || "Untitled")}</div>
        <div class="video-meta">${v.source_type} · ${
    v.clips?.length || 0
  } clips (${done} rendered)</div>
      </div>
      <span class="badge ${v.status}">${v.status}</span>
    </div>
    <div class="stages">${stages}</div>
    ${clips ? `<div class="clip-grid">${clips}</div>` : ""}
  </div>`;
}

// ---- Clip modal ----
window.openClip = async function (clipId) {
  const c = await (await fetch(`${API}/clips/${clipId}`)).json();
  const tags = (c.gen_hashtags || []).map((h) => `<span class="tag">#${escapeHtml(h)}</span>`).join("");
  document.getElementById("modal-body").innerHTML = `
    ${
      c.status === "completed"
        ? `<video controls src="${API}/clips/${c.id}/download"></video>`
        : `<div class="empty">Clip status: ${c.status}</div>`
    }
    <div class="meta-row"><div class="label">Title</div><div class="val">${escapeHtml(c.gen_title)}</div></div>
    <div class="meta-row"><div class="label">Hook</div><div class="val">${escapeHtml(c.gen_hook)}</div></div>
    <div class="meta-row"><div class="label">Description</div><div class="val">${escapeHtml(c.gen_description)}</div></div>
    <div class="meta-row"><div class="label">Why selected (score ${Math.round(c.score)})</div><div class="val">${escapeHtml(c.reason)}</div></div>
    <div class="meta-row"><div class="label">Hashtags</div><div class="tags">${tags}</div></div>
    <div class="meta-row" style="clear:both">
      ${c.status === "completed" ? `<a class="btn primary" href="${API}/clips/${c.id}/download">Download MP4</a>` : ""}
      <button class="btn ghost" onclick='rerender(${JSON.stringify(c.id)})'>Re-render</button>
    </div>`;
  document.getElementById("modal").classList.remove("hidden");
};
window.closeModal = () => document.getElementById("modal").classList.add("hidden");
window.rerender = async (id) => {
  await fetch(`${API}/clips/${id}/rerender`, { method: "POST" });
  closeModal();
  refresh();
};

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (m) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m])
  );
}

refresh();
setInterval(refresh, 4000); // live progress polling
