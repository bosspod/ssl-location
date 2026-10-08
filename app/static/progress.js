(() => {
  const form = document.getElementById("location-search-form");
  if (!form) return;

  const panel = document.getElementById("search-progress");
  const current = document.getElementById("progress-current");
  const elapsed = document.getElementById("progress-elapsed");
  const events = document.getElementById("progress-events");
  const results = document.getElementById("results");
  const button = form.querySelector("button[type='submit']");
  const searching = document.getElementById("searching");
  const label = document.getElementById("search-label");
  const modelSelect = document.getElementById("ai-model");
  const modelStatus = document.getElementById("model-list-status");
  let elapsedTimer = null;

  const loadModels = async () => {
    if (!modelSelect || !modelStatus) return;
    try {
      const response = await fetch("/ui/models", { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const catalog = await response.json();
      modelSelect.replaceChildren();
      for (const model of catalog.models || []) {
        const option = document.createElement("option");
        option.value = model;
        option.textContent = model === catalog.defaultModel ? `${model} — ค่าเริ่มต้น` : model;
        option.selected = model === catalog.defaultModel;
        modelSelect.appendChild(option);
      }
      modelStatus.textContent = catalog.source === "openai"
        ? `พบ ${(catalog.models || []).length} โมเดลจาก OpenAI API • โมเดลต้องรองรับ Responses API และ Web Search`
        : "แสดงรายการแนะนำชั่วคราว เนื่องจากยังโหลดรายการจาก OpenAI API ไม่ได้";
    } catch (_error) {
      modelStatus.textContent = "โหลดรายการเพิ่มเติมไม่ได้ — ยังคงใช้ gpt-5.6-luna ได้ตามปกติ";
    }
  };

  loadModels();

  const compact = (value) => String(value ?? "").trim();
  const percent = (value) => `${Math.round(Number(value || 0) * 100)}%`;
  const summarize = (event) => {
    const detail = event.detail;
    if (!detail || typeof detail !== "object" || Array.isArray(detail)) {
      return Array.isArray(detail)
        ? `เตรียมคำค้น ${detail.length} รูปแบบ: ${detail.slice(0, 3).join(" • ")}${detail.length > 3 ? " …" : ""}`
        : compact(detail);
    }
    if (event.stage === "extracted") {
      const place = detail.place_name || detail.company_name || detail.site_name || detail.area_context || "ยังไม่พบชื่อชัดเจน";
      const address = [detail.house_number, detail.subdistrict, detail.district, detail.province, detail.postal_code].filter(Boolean).join(" ");
      return [`ชื่อ: ${place}`, address && `พื้นที่: ${address}`, detail.phone && `โทร: ${detail.phone}`].filter(Boolean).join(" • ");
    }
    if (event.stage === "map_search") {
      const providers = (detail.providers || []).filter((item) => ["success", "partial"].includes(item.status)).map((item) => item.provider);
      return `พบ ${detail.candidate_count || 0} ผลลัพธ์จาก ${providers.join(", ") || "Map Providers"}`;
    }
    if (event.stage === "entity_resolution") {
      const total = Number(detail.candidate_count_total || 0);
      const sent = Number(detail.candidate_count_sent_to_ai || 0);
      return detail.truncated
        ? `AI กำลังตรวจ ${sent} รายการที่คัดเลือกจากทั้งหมด ${total} รายการ (เกินขีดจำกัด)`
        : `AI กำลังตรวจ Candidate ครบทั้ง ${total} รายการ`;
    }
    if (event.stage === "resolution_result") {
      const top = (detail.top_candidates || [])[0];
      return top ? `อันดับหนึ่งตอนนี้: ${top.place_name || "ไม่ระบุชื่อ"} • ความมั่นใจ ${percent(top.confidence_score)}` : "ยังไม่มีสถานที่ที่มีหลักฐานเพียงพอ";
    }
    if (event.stage === "dbd_search") return detail.reason || `สถานะ DBD: ${detail.status}`;
    if (event.stage === "registry_result") {
      return detail.dbd?.reason || `DBD: ${detail.dbd?.status || "skipped"}`;
    }
    if (event.stage === "complete") {
      const seconds = Number(detail.processing_time_ms || 0) / 1000;
      return detail.top_candidate
        ? `เลือก Candidate ที่มั่นใจที่สุด: ${detail.top_candidate} • ${percent(detail.confidence)} • ${seconds.toFixed(1)} วินาที`
        : detail.map_preview
          ? `ปักหมุด ${detail.map_preview} สำหรับตรวจสอบ • ${seconds.toFixed(1)} วินาที`
          : `ไม่พบ Candidate ที่เกี่ยวข้อง • ${seconds.toFixed(1)} วินาที`;
    }
    return Object.entries(detail).filter(([, value]) => value !== null && value !== "" && value !== false).slice(0, 3).map(([key, value]) => `${key}: ${compact(value)}`).join(" • ");
  };

  const addEvent = (event) => {
    current.textContent = event.title || "กำลังประมวลผล…";
    const item = document.createElement("li");
    item.className = "flex gap-3 rounded-xl border border-sky-100 bg-white px-4 py-3";
    const check = document.createElement("span");
    check.className = "progress-check";
    check.textContent = "✓";
    check.setAttribute("aria-hidden", "true");
    const content = document.createElement("div");
    content.className = "min-w-0 flex-1";
    const title = document.createElement("p");
    title.className = "text-sm font-semibold text-slate-900";
    title.textContent = event.title || event.stage;
    const detail = document.createElement("p");
    detail.className = "progress-detail mt-1 text-xs text-slate-600";
    detail.textContent = summarize(event);
    content.append(title, detail);
    if (event.detail && typeof event.detail === "object") {
      const technical = document.createElement("details");
      technical.className = "mt-2 text-xs text-slate-500";
      const summary = document.createElement("summary");
      summary.className = "cursor-pointer select-none";
      summary.textContent = "ดูข้อมูลขั้นตอนนี้";
      const raw = document.createElement("pre");
      raw.className = "progress-raw mt-2 rounded-lg bg-slate-50 p-3";
      raw.textContent = JSON.stringify(event.detail, null, 2);
      technical.append(summary, raw);
      content.appendChild(technical);
    }
    item.append(check, content);
    events.appendChild(item);
    item.scrollIntoView({ behavior: "smooth", block: "nearest" });
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    panel.classList.remove("hidden");
    panel.classList.remove("search-complete");
    events.replaceChildren();
    results.replaceChildren();
    button.disabled = true;
    searching.hidden = false;
    label.hidden = true;
    const searchStarted = performance.now();
    if (elapsedTimer) window.clearInterval(elapsedTimer);
    const updateElapsed = () => {
      if (elapsed) elapsed.textContent = `• ${((performance.now() - searchStarted) / 1000).toFixed(1)} วินาที`;
    };
    updateElapsed();
    elapsedTimer = window.setInterval(updateElapsed, 100);

    try {
      const response = await fetch(form.action, { method: "POST", body: new FormData(form) });
      if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let receivedResult = false;
      const consume = (line) => {
        if (!line.trim()) return;
        const update = JSON.parse(line);
        if (update.stage === "error") throw new Error(update.title || "การค้นหาไม่สำเร็จ");
        if (update.stage === "result") {
          receivedResult = true;
          results.innerHTML = update.html;
          current.textContent = "ค้นหาเสร็จแล้ว — ตรวจสอบผลลัพธ์ด้านล่าง";
          panel.classList.add("search-complete");
          if (window.htmx) window.htmx.process(results);
          window.requestAnimationFrame(() => {
            results.scrollIntoView({ behavior: "smooth", block: "start" });
          });
        } else {
          addEvent(update);
        }
      };
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          consume(line);
        }
      }
      consume(buffer + decoder.decode());
      if (!receivedResult) throw new Error("การเชื่อมต่อสิ้นสุดก่อนส่งผลลัพธ์ กรุณาค้นหาอีกครั้ง");
    } catch (error) {
      addEvent({ title: "เชื่อมต่อการค้นหาไม่สำเร็จ", detail: error.message });
    } finally {
      if (elapsedTimer) window.clearInterval(elapsedTimer);
      elapsedTimer = null;
      button.disabled = false;
      searching.hidden = true;
      label.hidden = false;
    }
  });
})();
