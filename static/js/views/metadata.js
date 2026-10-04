/* 视图：照片隐藏信息（EXIF）查看、单图编辑与批量修改。 */
window.Views = window.Views || {};
window.Views.metadata = (function () {
  const C = window.Common;

  let images = [];
  let metaCache = {};          // imageId -> metadata
  let selectedId = null;
  let selected = new Set();    // 批量多选
  let detailBusy = false;

  const ORIENTATIONS = [1, 2, 3, 4, 5, 6, 7, 8];
  const ORIENTATION_TEXT = {
    1: "正常（无旋转）", 2: "水平翻转", 3: "旋转 180°", 4: "垂直翻转",
    5: "顺时针 90° + 水平翻转", 6: "顺时针 90°",
    7: "逆时针 90° + 水平翻转", 8: "逆时针 90°",
  };

  // -------------------------------------------------------------- 主框架
  function el() {
    return document.querySelector('.view[data-view="metadata"]');
  }

  async function mount(root) {
    root.innerHTML = `
      <div class="split" style="grid-template-columns:1fr 380px">
        <div class="col">
          <div class="panel">
            <div class="panel-title">选择图像<span class="dim">点击查看隐藏信息；勾选复选框可批量修改</span></div>
            <div id="md-gallery"></div>
          </div>
          <div class="panel" id="md-batch-panel">
            <div class="panel-title">批量修改<span class="dim">只把勾选的字段应用到所选图片，逐张独立处理</span></div>
            <div id="md-batch-form"></div>
          </div>
          <div class="panel" id="md-batch-result" hidden></div>
        </div>
        <div class="col" id="md-detail"></div>
      </div>`;
    await reloadImages();
    renderBatchForm();
    if (window.Views.metadata._preselect) {
      const id = window.Views.metadata._preselect;
      window.Views.metadata._preselect = null;
      if (images.some((i) => i.id === id)) await openDetail(id);
    }
  }

  async function refresh() {
    if (!this.mounted) return;
    metaCache = {};
    await reloadImages();
    if (selectedId) await openDetail(selectedId, true);
  }

  async function reloadImages() {
    images = await C.fetchImages();
    renderGallery();
  }

  // -------------------------------------------------------------- 图库
  function renderGallery() {
    const box = el().querySelector("#md-gallery");
    if (!images.length) {
      box.innerHTML = `<div class="empty"><span class="big">🖼️</span>暂无图像，请先到「图像管理」上传</div>`;
      return;
    }
    box.innerHTML = `<div class="grid md-grid">` + images.map((im) => `
      <div class="card${im.id === selectedId ? " selected" : ""}" data-id="${C.esc(im.id)}">
        <label class="md-check" data-stop="1"><input type="checkbox" data-check="${C.esc(im.id)}" ${selected.has(im.id) ? "checked" : ""}></label>
        <img class="thumb" src="${C.esc(im.thumbnail_url)}" loading="lazy" draggable="false">
        <span class="card-badge">${im.width}×${im.height}</span>
        <div class="card-meta">
          <div class="card-name" title="${C.esc(im.filename)}">${C.esc(im.filename)}</div>
          <div class="card-dim">${C.esc(im.format)}</div>
        </div>
      </div>`).join("") + `</div>`;

    box.querySelectorAll(".card").forEach((card) => {
      card.addEventListener("click", async (e) => {
        if (e.target.closest("[data-stop]")) return;
        await openDetail(card.dataset.id);
      });
    });
    box.querySelectorAll("[data-check]").forEach((chk) => {
      chk.addEventListener("click", (e) => e.stopPropagation());
      chk.addEventListener("change", () => {
        if (chk.checked) selected.add(chk.dataset.check);
        else selected.delete(chk.dataset.check);
        renderBatchForm();
      });
    });
  }

  // -------------------------------------------------------------- 详情（只读展示 + 编辑）
  async function openDetail(id, keep) {
    selectedId = id;
    renderGallery();
    const box = el().querySelector("#md-detail");
    box.innerHTML = `<div class="panel"><div class="loading">读取隐藏信息中…</div></div>`;
    let meta;
    try {
      const data = await Api.get(`/api/images/${id}/metadata`);
      meta = data.metadata;
      metaCache[id] = meta;
    } catch (e) {
      box.innerHTML = `<div class="panel"><div class="panel-title">隐藏信息</div>
        <div class="md-alert error">读取失败：${C.esc(e.message)}</div></div>`;
      return;
    }
    const rec = images.find((i) => i.id === id);
    renderDetail(rec, meta);
  }

  function val(meta, key, dash) {
    const v = meta[key];
    return v === null || v === undefined || v === "" ? (dash === undefined ? `<span class="dim">未携带</span>` : dash) : v;
  }

  function renderDetail(rec, m) {
    const box = el().querySelector("#md-detail");
    const gps = m.gps;
    const exposure = m.exposure || {};
    const noExif = !m.has_exif;
    const missing = m.missing_labels || [];

    box.innerHTML = `
      <div class="panel">
        <div class="panel-title">隐藏信息<span class="dim">${C.esc(rec.filename)}</span></div>
        <img src="${C.esc(rec.file_url)}" class="md-preview" alt="">
        <div class="md-toolbar">
          <span class="badge ${m.writable ? "green" : "red"}">${m.writable ? "支持写回" : "不支持写回"}</span>
          <span class="badge ${noExif ? "amber" : "green"}">${noExif ? "无 EXIF 信息" : "含 EXIF 信息"}</span>
        </div>
        ${m.unsupported_reason ? `<div class="md-alert warn">${C.esc(m.unsupported_reason)}</div>` : ""}
        ${noExif ? `<div class="md-alert warn">该图片本身没有携带拍摄信息（时间、设备、GPS 等）。${m.writable ? "可以在下方直接补充并写入图片。" : ""}</div>` : ""}
        ${!noExif && missing.length ? `<div class="md-alert warn">缺少：${missing.map(C.esc).join("、")}</div>` : ""}

        <form id="md-form" autocomplete="off">
          <div class="md-section">拍摄</div>
          <div class="field" data-field="taken_at">
            <label>拍摄时间</label>
            <input type="datetime-local" id="md-taken" step="1" value="${C.esc(m.taken_at_input || "")}">
            <div class="md-field-err" hidden></div>
            <div class="hint">原值：${m.taken_at_raw ? C.esc(m.taken_at_raw) : "<span class='dim'>未携带</span>"}</div>
          </div>

          <div class="field" data-field="orientation">
            <label>方向（EXIF Orientation）</label>
            <select id="md-orient">
              <option value="">未携带</option>
              ${ORIENTATIONS.map((o) => `<option value="${o}" ${m.orientation === o ? "selected" : ""}>${o} — ${C.esc(ORIENTATION_TEXT[o])}</option>`).join("")}
            </select>
            <div class="md-field-err" hidden></div>
          </div>

          <div class="field" data-field="description">
            <label>图片说明</label>
            <textarea id="md-desc" rows="3" placeholder="未携带（可填写）">${C.esc(m.description || "")}</textarea>
            <div class="md-field-err" hidden></div>
          </div>

          <div class="md-section">设备（只读）</div>
          <div class="md-kv"><span>厂商</span><b>${val(m, "make")}</b></div>
          <div class="md-kv"><span>型号</span><b>${val(m, "model")}</b></div>
          <div class="md-kv"><span>镜头</span><b>${val(m, "lens")}</b></div>
          <div class="md-kv"><span>软件</span><b>${val(m, "software")}</b></div>
          <div class="md-kv"><span>作者</span><b>${val(m, "artist")}</b></div>
          <div class="md-kv"><span>版权</span><b>${val(m, "copyright")}</b></div>

          <div class="md-section">图像参数（只读）</div>
          <div class="md-kv"><span>分辨率</span><b>${m.width} × ${m.height} px</b></div>
          <div class="md-kv"><span>方向值</span><b>${m.orientation ? `${m.orientation} · ${C.esc(m.orientation_label || "")}` : val(m, "orientation")}</b></div>

          <div class="md-section">GPS 定位</div>
          <div class="md-row">
            <div class="field" data-field="gps_lat">
              <label>纬度（-90 ~ 90）</label>
              <input type="number" id="md-lat" step="0.000001" min="-90" max="90" placeholder="未携带"
                     value="${gps && gps.latitude !== undefined ? gps.latitude : ""}">
              <div class="md-field-err" hidden></div>
            </div>
            <div class="field" data-field="gps_lon">
              <label>经度（-180 ~ 180）</label>
              <input type="number" id="md-lon" step="0.000001" min="-180" max="180" placeholder="未携带"
                     value="${gps && gps.longitude !== undefined ? gps.longitude : ""}">
              <div class="md-field-err" hidden></div>
            </div>
          </div>
          <div class="md-kv"><span>原始坐标</span><b>${gps ? C.esc(gps.dms || "") : val(gps, "")}</b></div>
          <div class="md-kv"><span>海拔</span><b>${gps && gps.altitude !== null && gps.altitude !== undefined ? `${gps.altitude} m` : val(gps, "")}</b></div>

          <div class="md-section">拍摄参数（只读）</div>
          <div class="md-kv"><span>光圈</span><b>${val(exposure, "f_number_text")}</b></div>
          <div class="md-kv"><span>快门</span><b>${val(exposure, "exposure_time_text")}</b></div>
          <div class="md-kv"><span>ISO</span><b>${val(exposure, "iso")}</b></div>
          <div class="md-kv"><span>焦距</span><b>${val(exposure, "focal_length_text")}</b></div>
          <div class="md-kv"><span>白平衡</span><b>${val(exposure, "white_balance")}</b></div>

          <details class="md-raw">
            <summary>查看全部原始标签（${(m.raw_tags || []).length} 项）</summary>
            ${(m.raw_tags || []).length ? `
            <table class="table md-raw-table">
              <thead><tr><th>分组</th><th>标签</th><th>值</th></tr></thead>
              <tbody>${m.raw_tags.map((r) => `
                <tr><td class="dim">${C.esc(r.section)}</td><td>${C.esc(r.name)}</td>
                <td class="mono">${C.esc(r.value)}</td></tr>`).join("")}
              </tbody>
            </table>` : `<div class="dim" style="padding:8px 0">没有任何原始标签</div>`}
          </details>

          <div class="modal-actions" style="margin-top:14px">
            <button type="button" class="btn btn-sm" id="md-clear-gps">清空 GPS</button>
            <button type="button" class="btn btn-primary" id="md-save" ${m.writable ? "" : "disabled"}>
              ${m.writable ? "保存到图片本身" : "该格式不可写"}
            </button>
          </div>
          <div class="md-save-msg" id="md-save-msg" hidden></div>
        </form>
      </div>`;

    const form = box.querySelector("#md-form");
    box.querySelector("#md-clear-gps").onclick = () => {
      form.querySelector("#md-lat").value = "";
      form.querySelector("#md-lon").value = "";
      C.toast("将清除 GPS，点击保存后生效", "");
    };
    box.querySelector("#md-save").onclick = () => saveOne(rec, m, form);
  }

  function collectForm(form) {
    const updates = {
      taken_at: form.querySelector("#md-taken").value,
      orientation: form.querySelector("#md-orient").value,
      description: form.querySelector("#md-desc").value,
      gps_lat: form.querySelector("#md-lat").value,
      gps_lon: form.querySelector("#md-lon").value,
    };
    return updates;
  }

  function showFieldErrors(form, fields) {
    form.querySelectorAll("[data-field]").forEach((f) => {
      const errBox = f.querySelector(".md-field-err");
      const key = f.dataset.field;
      if (fields && fields[key]) {
        errBox.textContent = fields[key];
        errBox.hidden = false;
        f.classList.add("has-error");
      } else {
        errBox.hidden = true;
        f.classList.remove("has-error");
      }
    });
  }

  async function saveOne(rec, meta, form) {
    if (detailBusy) return;
    const btn = form.querySelector("#md-save");
    const msg = form.querySelector("#md-save-msg");
    const updates = collectForm(form);
    detailBusy = true;
    btn.disabled = true;
    btn.textContent = "保存中…";
    msg.hidden = true;
    showFieldErrors(form, null);
    try {
      const r = await Api.put(`/api/images/${rec.id}/metadata`, updates);
      metaCache[rec.id] = r.metadata;
      await C.refreshImages();
      images = await C.fetchImages();
      const fresh = images.find((i) => i.id === rec.id);
      renderDetail(fresh, r.metadata);
      renderGallery();
      C.toast("已写入图片文件本身", "success");
    } catch (e) {
      showFieldErrors(form, e.fields || null);
      msg.hidden = false;
      msg.className = "md-save-msg error";
      const fieldText = e.fields && Object.keys(e.fields).length
        ? "（" + Object.entries(e.fields).map(([k, v]) => `${fieldLabel(k)}：${v}`).join("；") + "）"
        : "";
      msg.textContent = "保存失败：" + e.message + fieldText;
      C.toast("保存失败：" + e.message, "error");
      btn.disabled = false;
      btn.textContent = "保存到图片本身";
    } finally {
      detailBusy = false;
    }
  }

  // -------------------------------------------------------------- 批量
  function renderBatchForm() {
    const root = el();
    if (!root) return;
    const box = root.querySelector("#md-batch-form");
    const count = selected.size;
    box.innerHTML = `
      <div class="md-toolbar" style="margin-bottom:10px">
        <span class="badge ${count ? "green" : ""}">已选 ${count} 张</span>
        <button class="btn btn-sm" id="md-select-all">全选</button>
        <button class="btn btn-sm" id="md-select-none">清空</button>
      </div>
      <div class="md-batch-fields">
        <label class="md-pick"><input type="checkbox" id="mb-use-taken"> 修改拍摄时间</label>
        <input type="datetime-local" id="mb-taken" step="1" disabled>

        <label class="md-pick"><input type="checkbox" id="mb-use-orient"> 修改方向</label>
        <select id="mb-orient" disabled>
          <option value="">未携带（清除）</option>
          ${ORIENTATIONS.map((o) => `<option value="${o}">${o} — ${C.esc(ORIENTATION_TEXT[o])}</option>`).join("")}
        </select>

        <label class="md-pick"><input type="checkbox" id="mb-use-desc"> 修改说明</label>
        <textarea id="mb-desc" rows="2" disabled placeholder="将写入该说明（留空并勾选 = 清除说明）"></textarea>

        <label class="md-pick"><input type="checkbox" id="mb-use-gps"> 修改 GPS</label>
        <div class="md-row">
          <input type="number" id="mb-lat" step="0.000001" placeholder="纬度" disabled>
          <input type="number" id="mb-lon" step="0.000001" placeholder="经度（两栏都留空 = 清除 GPS）" disabled>
        </div>
      </div>
      <div class="modal-actions">
        <button class="btn btn-primary" id="mb-apply" ${count ? "" : "disabled"}>应用到所选 ${count} 张</button>
      </div>
      <div class="hint" style="color:var(--text-faint);font-size:12px;margin-top:8px">
        逐张独立写回：某张格式不支持或字段不合法时，只会把它单独标红，其余图片照常成功。
      </div>`;

    box.querySelector("#mb-use-gps").addEventListener("change", () => {
      const on = box.querySelector("#mb-use-gps").checked;
      box.querySelector("#mb-lat").disabled = !on;
      box.querySelector("#mb-lon").disabled = !on;
    });
    box.querySelector("#mb-use-taken").addEventListener("change",
      (e) => { box.querySelector("#mb-taken").disabled = !e.target.checked; });
    box.querySelector("#mb-use-orient").addEventListener("change",
      (e) => { box.querySelector("#mb-orient").disabled = !e.target.checked; });
    box.querySelector("#mb-use-desc").addEventListener("change",
      (e) => { box.querySelector("#mb-desc").disabled = !e.target.checked; });

    box.querySelector("#md-select-all").onclick = () => {
      selected = new Set(images.map((i) => i.id));
      renderGallery(); renderBatchForm();
    };
    box.querySelector("#md-select-none").onclick = () => {
      selected.clear();
      renderGallery(); renderBatchForm();
    };
    box.querySelector("#mb-apply").onclick = runBatch;
  }

  async function runBatch() {
    const root = el();
    const box = root.querySelector("#md-batch-form");
    if (!selected.size) { C.toast("请先勾选图片", "error"); return; }

    const updates = {};
    const picks = [];
    if (box.querySelector("#mb-use-taken").checked) {
      updates.taken_at = box.querySelector("#mb-taken").value;
      if (!updates.taken_at) { C.toast("请填写拍摄时间", "error"); return; }
      picks.push("拍摄时间");
    }
    if (box.querySelector("#mb-use-orient").checked) {
      updates.orientation = box.querySelector("#mb-orient").value || null;
      picks.push("方向");
    }
    if (box.querySelector("#mb-use-desc").checked) {
      updates.description = box.querySelector("#mb-desc").value;
      picks.push("说明");
    }
    if (box.querySelector("#mb-use-gps").checked) {
      updates.gps_lat = box.querySelector("#mb-lat").value;
      updates.gps_lon = box.querySelector("#mb-lon").value;
      picks.push("GPS");
    }
    if (!picks.length) { C.toast("请至少勾选一个要修改的字段", "error"); return; }

    const btn = box.querySelector("#mb-apply");
    btn.disabled = true;
    btn.textContent = "逐张写回中…";
    let result;
    try {
      result = await Api.post("/api/images/metadata/batch",
        { image_ids: Array.from(selected), ...updates });
    } catch (e) {
      C.toast("批量请求失败：" + e.message, "error");
      btn.disabled = false;
      btn.textContent = `应用到所选 ${selected.size} 张`;
      return;
    }
    renderBatchResult(result, picks);
    await C.refreshImages();
    images = await C.fetchImages();
    metaCache = {};
    renderGallery();
    if (selectedId && result.succeeded.some((s) => s.image_id === selectedId)) {
      await openDetail(selectedId);
    }
    C.toast(`完成：${result.succeeded_count} 成功，${result.failed_count} 失败`,
      result.failed_count ? "error" : "success");
    btn.disabled = false;
    btn.textContent = `应用到所选 ${selected.size} 张`;
  }

  function renderBatchResult(result, picks) {
    const panel = el().querySelector("#md-batch-result");
    panel.hidden = false;
    const byId = Object.fromEntries(images.map((i) => [i.id, i]));
    const nameOf = (id, fallback) => (byId[id] && byId[id].filename) || fallback || id || "?";

    panel.innerHTML = `
      <div class="panel-title">批量结果<span class="dim">字段：${picks.map(C.esc).join("、")}</span></div>
      <div class="md-toolbar" style="margin-bottom:10px">
        <span class="badge green">成功 ${result.succeeded_count}</span>
        <span class="badge ${result.failed_count ? "red" : ""}">失败 ${result.failed_count}</span>
        <span class="spacer"></span>
        <button class="btn btn-sm" id="md-result-close">收起</button>
      </div>
      <div class="md-result-list">
        ${result.succeeded.map((s) => `
          <div class="md-result-row ok">
            <span class="md-ico">✔</span>
            <span class="md-rname" title="${C.esc(s.image_id)}">${C.esc(nameOf(s.image_id, s.filename))}</span>
            <span class="badge green">已写回</span>
          </div>`).join("")}
        ${result.failed.map((f) => `
          <div class="md-result-row fail" data-retry="${C.esc(f.image_id || "")}">
            <span class="md-ico">✘</span>
            <span class="md-rname">${C.esc(nameOf(f.image_id, f.filename))}</span>
            <span class="badge red">失败</span>
            <span class="md-rerr">${C.esc(f.error)}${Object.keys(f.fields || {}).length
              ? "（" + Object.entries(f.fields).map(([k, v]) => `${fieldLabel(k)}：${C.esc(v)}`).join("；") + "）"
              : ""}</span>
            ${f.image_id ? `<button class="btn btn-sm md-retry" data-id="${C.esc(f.image_id)}">单独打开修正</button>` : ""}
          </div>`).join("")}
      </div>`;
    panel.querySelector("#md-result-close").onclick = () => { panel.hidden = true; };
    panel.querySelectorAll(".md-retry").forEach((b) => {
      b.onclick = () => openDetail(b.dataset.id);
    });
  }

  function fieldLabel(k) {
    return { taken_at: "拍摄时间", orientation: "方向", description: "说明",
             gps_lat: "纬度", gps_lon: "经度" }[k] || k;
  }

  // 供其它视图跳转
  function openFor(id) {
    window.Views.metadata._preselect = id;
    window.App.show("metadata");
  }

  return {
    mount, refresh, openFor,
    _preselect: null,
  };
})();
