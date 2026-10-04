/* 视图：EXIF 元信息查看与编辑（单张 + 批量）。 */
window.Views = window.Views || {};
window.Views.exif = (function () {
  const C = window.Common;
  let selectedId = null;          // 当前查看详情的图像
  let checked = new Set();        // 批量选择
  let lastExif = null;            // 当前详情的 EXIF 数据（用于预填编辑表单）

  const ORIENT_OPTIONS = [
    [1, "1 · 正常"], [2, "2 · 水平翻转"], [3, "3 · 旋转 180°"], [4, "4 · 垂直翻转"],
    [5, "5 · 转置"], [6, "6 · 顺时针 90°"], [7, "7 · 转置并翻转"], [8, "8 · 逆时针 90°"],
  ];

  /* EXIF 时间 -> datetime-local 输入框格式 */
  function toInputDT(s) {
    if (!s) return "";
    const m = s.match(/^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/);
    return m ? `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6] || "00"}` : "";
  }

  // ------------------------------------------------------------------ 图库
  async function loadGallery(el) {
    const images = await C.fetchImages();
    const box = el.querySelector("#ex-gallery");
    if (!images.length) { box.innerHTML = C.galleryHTML(images); return; }
    box.innerHTML = `<div class="grid">` + images.map((im) => `
      <div class="card" data-id="${C.esc(im.id)}">
        <input type="checkbox" class="exif-check" data-id="${C.esc(im.id)}" title="勾选用于批量修改" ${checked.has(im.id) ? "checked" : ""}>
        <img class="thumb" src="${C.esc(im.thumbnail_url)}" loading="lazy" draggable="false">
        <span class="card-badge">${im.width}×${im.height}</span>
        <div class="card-meta">
          <div class="card-name" title="${C.esc(im.filename)}">${C.esc(im.filename)}</div>
          <div class="card-dim">${C.fmtBytes(im.size_bytes)} · ${C.fmtDate(im.created_at)}</div>
        </div>
      </div>`).join("") + `</div>`;

    box.querySelectorAll(".card").forEach((card) => {
      card.addEventListener("click", (e) => {
        if (e.target.classList.contains("exif-check")) return;
        selectedId = card.dataset.id;
        box.querySelectorAll(".card.selected").forEach((c) => c.classList.remove("selected"));
        card.classList.add("selected");
        loadDetail(el);
      });
    });
    box.querySelectorAll(".exif-check").forEach((cb) => {
      cb.addEventListener("click", (e) => e.stopPropagation());
      cb.addEventListener("change", () => {
        if (cb.checked) checked.add(cb.dataset.id); else checked.delete(cb.dataset.id);
        updateBatchCount(el);
      });
    });
    if (selectedId) {
      const card = box.querySelector(`.card[data-id="${selectedId}"]`);
      if (card) card.classList.add("selected");
    }
    updateBatchCount(el);
  }

  function updateBatchCount(el) {
    el.querySelector("#ex-batch-count").textContent = `已选 ${checked.size} 张`;
    el.querySelector("#ex-batch-apply").disabled = checked.size === 0;
  }

  // ------------------------------------------------------------------ 详情
  async function loadDetail(el) {
    const box = el.querySelector("#ex-detail");
    const editBox = el.querySelector("#ex-edit");
    if (!selectedId) {
      box.innerHTML = `<div class="panel"><div class="panel-title">元信息</div>
        <div class="empty"><span class="big">🏷️</span>点击左侧图像查看其携带的隐藏信息<br>（拍摄时间、设备、方向、GPS 等）</div></div>`;
      editBox.innerHTML = "";
      return;
    }
    box.innerHTML = `<div class="panel"><div class="panel-title">元信息</div><div class="loading">读取中…</div></div>`;
    let data;
    try {
      data = await Api.get(`/api/images/${selectedId}/exif`);
    } catch (e) {
      box.innerHTML = `<div class="panel"><div class="panel-title">元信息</div>
        <div class="empty">读取失败：${C.esc(e.message)}</div></div>`;
      editBox.innerHTML = "";
      return;
    }
    lastExif = data.exif;
    renderDetail(box, data.exif);
    renderEditForm(editBox, data.exif);
  }

  function kv(label, value) {
    return `<div><span class="dim">${label}</span> ${value == null ? '<span class="dim">—</span>' : `<strong>${C.esc(String(value))}</strong>`}</div>`;
  }

  function renderDetail(box, ex) {
    const img = ex.image || {};
    let body;
    if (!ex.has_exif) {
      body = `<div class="empty" style="padding:24px 12px">
          <span class="big">🚫</span>该图像不包含 EXIF 元信息<br>
          <span style="font-size:12px">拍摄时间、设备、方向、GPS 等均不可用</span>
        </div>
        <div class="keypoint-stats" style="line-height:1.9">
          ${kv("分辨率", `${img.width} × ${img.height}`)}
          ${kv("格式", ex.format)}
        </div>
        <div class="hint" style="color:var(--text-faint);font-size:12px;margin-top:10px">
          ${ex.can_edit ? "可通过下方表单向该图像写入新的元信息。" : "该格式不支持写入 EXIF（支持 JPEG/PNG/WebP/TIFF）。"}
        </div>`;
    } else {
      const cap = ex.capture || {}, dev = ex.device || {}, expo = ex.exposure || {}, gps = ex.gps;
      body = `
        <div class="exif-section">拍摄</div>
        <div class="keypoint-stats" style="line-height:1.9">
          ${kv("拍摄时间", cap.datetime_original || cap.datetime)}
          ${kv("数字化时间", cap.datetime_digitized)}
        </div>
        <div class="exif-section">设备</div>
        <div class="keypoint-stats" style="line-height:1.9">
          ${kv("相机品牌", dev.make)}${kv("相机型号", dev.model)}
          ${kv("镜头", dev.lens)}${kv("软件", dev.software)}
        </div>
        <div class="exif-section">曝光</div>
        <div class="keypoint-stats" style="line-height:1.9">
          ${kv("ISO", expo.iso)}${kv("快门", expo.exposure_time)}
          ${kv("光圈", expo.f_number)}${kv("焦距", expo.focal_length)}
          ${kv("闪光灯", expo.flash == null ? null : (expo.flash ? "开启" : "关闭"))}
        </div>
        <div class="exif-section">图像</div>
        <div class="keypoint-stats" style="line-height:1.9">
          ${kv("分辨率", `${img.width} × ${img.height}`)}
          ${kv("方向", img.orientation ? `${img.orientation} · ${img.orientation_text || "未知"}` : null)}
          ${kv("DPI", img.x_resolution ? `${img.x_resolution} × ${img.y_resolution}` : null)}
        </div>
        <div class="exif-section">GPS</div>
        <div class="keypoint-stats" style="line-height:1.9">
          ${gps ? kv("经纬度", `${gps.latitude ?? "?"}, ${gps.longitude ?? "?"}`) + kv("海拔", gps.altitude != null ? `${gps.altitude} m` : null)
                : '<div><span class="dim">无定位信息</span></div>'}
        </div>
        <div class="exif-section">说明</div>
        <div class="keypoint-stats" style="line-height:1.9">${kv("图像说明", ex.description)}</div>
        <div class="hint" style="color:var(--text-faint);font-size:11px;margin-top:8px">共 ${ex.tag_count} 个 EXIF 标签 · 格式 ${C.esc(ex.format)}</div>`;
    }
    box.innerHTML = `<div class="panel"><div class="panel-title">元信息
      <span class="dim">${ex.has_exif ? "" : "（无 EXIF）"}</span></div>${body}</div>`;
  }

  // ------------------------------------------------------------------ 单张编辑
  function renderEditForm(box, ex) {
    if (!ex.can_edit) {
      box.innerHTML = `<div class="panel"><div class="panel-title">编辑元信息</div>
        <div class="empty" style="padding:16px">该格式（${C.esc(ex.format || "未知")}）不支持写入 EXIF<br>支持 JPEG / PNG / WebP / TIFF</div></div>`;
      return;
    }
    const cap = ex.capture || {}, dev = ex.device || {}, img = ex.image || {};
    box.innerHTML = `<div class="panel">
      <div class="panel-title">编辑元信息<span class="dim">保存到图片本身；留空表示清除该字段</span></div>
      <div class="field"><label>拍摄时间</label>
        <input type="datetime-local" step="1" id="exf-dt" value="${toInputDT(cap.datetime_original || cap.datetime)}"></div>
      <div class="field"><label>方向</label>
        <select id="exf-orient">
          <option value="">未设置（清除）</option>
          ${ORIENT_OPTIONS.map(([v, t]) => `<option value="${v}" ${img.orientation === v ? "selected" : ""}>${t}</option>`).join("")}
        </select></div>
      <div class="field"><label>图像说明</label>
        <input type="text" id="exf-desc" value="${C.esc(ex.description || "")}" placeholder="照片备注说明"></div>
      <div class="field"><label>相机品牌</label>
        <input type="text" id="exf-make" value="${C.esc(dev.make || "")}"></div>
      <div class="field"><label>相机型号</label>
        <input type="text" id="exf-model" value="${C.esc(dev.model || "")}"></div>
      <div class="field"><label>软件</label>
        <input type="text" id="exf-software" value="${C.esc(dev.software || "")}"></div>
      <div class="toolbar" style="margin-bottom:0">
        <button class="btn btn-primary" id="exf-save">保存到图片</button>
      </div>
    </div>`;
    box.querySelector("#exf-save").onclick = async () => {
      const fields = {
        datetime_original: box.querySelector("#exf-dt").value,
        orientation: box.querySelector("#exf-orient").value,
        description: box.querySelector("#exf-desc").value,
        make: box.querySelector("#exf-make").value,
        model: box.querySelector("#exf-model").value,
        software: box.querySelector("#exf-software").value,
      };
      try {
        await Api.put(`/api/images/${selectedId}/exif`, { fields });
        C.toast("已保存到图片本身", "success");
        await C.refreshImages();
        loadDetail(document.querySelector('.view[data-view="exif"]'));
        loadGallery(document.querySelector('.view[data-view="exif"]'));
      } catch (e) {
        C.toast("保存失败：" + e.message, "error");
      }
    };
  }

  // ------------------------------------------------------------------ 批量
  function batchPanelHTML() {
    return `<div class="panel">
      <div class="panel-title">批量修改<span class="dim">勾选要修改的字段；留空表示清除该字段</span></div>
      <div class="batch-field">
        <input type="checkbox" id="exb-chk-dt"><label for="exb-chk-dt">拍摄时间</label>
        <input type="datetime-local" step="1" id="exb-dt">
      </div>
      <div class="batch-field">
        <input type="checkbox" id="exb-chk-orient"><label for="exb-chk-orient">方向</label>
        <select id="exb-orient">
          <option value="">未设置（清除）</option>
          ${ORIENT_OPTIONS.map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}
        </select>
      </div>
      <div class="batch-field">
        <input type="checkbox" id="exb-chk-desc"><label for="exb-chk-desc">图像说明</label>
        <input type="text" id="exb-desc" placeholder="留空 = 清除说明">
      </div>
      <div class="batch-field">
        <input type="checkbox" id="exb-chk-make"><label for="exb-chk-make">相机品牌</label>
        <input type="text" id="exb-make">
      </div>
      <div class="batch-field">
        <input type="checkbox" id="exb-chk-model"><label for="exb-chk-model">相机型号</label>
        <input type="text" id="exb-model">
      </div>
      <div class="batch-field">
        <input type="checkbox" id="exb-chk-software"><label for="exb-chk-software">软件</label>
        <input type="text" id="exb-software">
      </div>
      <div class="toolbar" style="margin-top:10px;margin-bottom:0">
        <span id="ex-batch-count" class="badge">已选 0 张</span>
        <span class="spacer"></span>
        <button class="btn btn-primary" id="ex-batch-apply" disabled>应用到所选图像</button>
      </div>
      <div id="ex-batch-results"></div>
    </div>`;
  }

  async function applyBatch(el) {
    const fields = {};
    const pick = (chk, input, key) => {
      if (el.querySelector(chk).checked) fields[key] = el.querySelector(input).value;
    };
    pick("#exb-chk-dt", "#exb-dt", "datetime_original");
    pick("#exb-chk-orient", "#exb-orient", "orientation");
    pick("#exb-chk-desc", "#exb-desc", "description");
    pick("#exb-chk-make", "#exb-make", "make");
    pick("#exb-chk-model", "#exb-model", "model");
    pick("#exb-chk-software", "#exb-software", "software");
    if (!Object.keys(fields).length) { C.toast("请至少勾选一个要修改的字段", "error"); return; }
    if (!checked.size) { C.toast("请先勾选图像", "error"); return; }

    const btn = el.querySelector("#ex-batch-apply");
    btn.disabled = true;
    try {
      const r = await Api.post("/api/images/exif/batch", { image_ids: Array.from(checked), fields });
      renderBatchResults(el, r);
      if (r.failed === 0) C.toast(`全部成功（${r.ok} 张）`, "success");
      else C.toast(`成功 ${r.ok} 张，失败 ${r.failed} 张（详见列表）`, "error");
      await C.refreshImages();
      loadGallery(el);
      if (selectedId) loadDetail(el);
    } catch (e) {
      C.toast("批量修改失败：" + e.message, "error");
    } finally {
      btn.disabled = checked.size === 0;
    }
  }

  function renderBatchResults(el, r) {
    const box = el.querySelector("#ex-batch-results");
    box.innerHTML = `<div style="margin-top:12px">
      <div class="exif-section">批量结果 · 成功 ${r.ok} · 失败 ${r.failed}</div>
      ${r.results.map((it) => `
        <div class="batch-result ${it.status === "ok" ? "" : "failed"}">
          <span class="badge ${it.status === "ok" ? "green" : "red"}">${it.status === "ok" ? "成功" : "失败"}</span>
          <span class="batch-name" title="${C.esc(it.filename)}">${C.esc(it.filename)}</span>
          ${it.reason ? `<span class="batch-reason">${C.esc(it.reason)}</span>` : ""}
        </div>`).join("")}
    </div>`;
  }

  // ------------------------------------------------------------------ 挂载
  return {
    mount(el) {
      el.innerHTML = `
        <div class="row">
          <div class="col" style="flex:1;min-width:0">
            <div class="panel">
              <div class="panel-title">选择图像<span class="dim">点击卡片查看元信息；勾选左上角复选框用于批量修改</span></div>
              <div id="ex-gallery" style="max-height:380px;overflow:auto"></div>
            </div>
            ${batchPanelHTML()}
          </div>
          <div class="col" style="width:360px;flex:none">
            <div id="ex-detail"></div>
            <div id="ex-edit"></div>
          </div>
        </div>`;
      el.querySelector("#ex-batch-apply").onclick = () => applyBatch(el);
      loadGallery(el);
      loadDetail(el);
    },
    refresh() {
      const el = document.querySelector('.view[data-view="exif"]');
      if (el && this.mounted) C.refreshImages().then(() => { loadGallery(el); loadDetail(el); });
    },
  };
})();
