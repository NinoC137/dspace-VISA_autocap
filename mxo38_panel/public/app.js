/* MXO38 远程面板 —— 前端逻辑（无框架） */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const S = {
  state: null, health: null, config: null,
  inflight: 0, runBusy: false,
  lastAcq: null,           // { count, at }
  runMode: null,           // "run" | "stop" | "single" | "single-armed"
  undoStack: [],           // [{ items:[{header,value}], li }]
  timer: null,
};

// ------------------------------------------------------------------ 数值格式
const PREFIX = [[1e9, "G"], [1e6, "M"], [1e3, "k"], [1, ""], [1e-3, "m"], [1e-6, "µ"], [1e-9, "n"], [1e-12, "p"]];

function pickPrefix(abs) {
  for (const p of PREFIX) if (abs >= p[0] * 0.9995) return p;
  return PREFIX[PREFIX.length - 1];
}

/** 显示用：0.05 s → "50 ms"，19230769 Sa/s → "19.23 MSa/s" */
function fmtSI(v, unit = "", sig = 4) {
  if (v === undefined || v === null || v === "") return "—";
  const x = Number(v);
  if (!Number.isFinite(x)) return String(v);
  if (x === 0) return `0 ${unit}`.trim();
  const p = pickPrefix(Math.abs(x));
  const m = Number((x / p[0]).toPrecision(sig));
  return `${m} ${p[1]}${unit}`.trim();
}

/** 输入框用：0.5 → "500m"，5e7 → "50M"（scope-ctl 认 f p n u m k M G 后缀） */
function toSI(v, sig = 6) {
  if (v === undefined || v === null || v === "") return "";
  const x = Number(v);
  if (!Number.isFinite(x)) return String(v);
  if (x === 0) return "0";
  const p = pickPrefix(Math.abs(x));
  const m = Number((x / p[0]).toPrecision(sig));
  return `${m}${p[1] === "µ" ? "u" : p[1]}`;
}

function plain(v, sig = 6) {
  const x = Number(v);
  return Number.isFinite(x) ? String(Number(x.toPrecision(sig))) : String(v ?? "");
}

function now() {
  return new Date().toTimeString().slice(0, 8);
}

// ------------------------------------------------------------------ 请求
async function api(path, body) {
  S.inflight += 1;
  document.body.classList.add("busy");
  try {
    const res = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
    const ct = res.headers.get("content-type") || "";
    if (ct.startsWith("image/")) return res.blob();
    return await res.json();
  } catch (e) {
    return { ok: false, error: `本机面板服务无响应：${e.message}` };
  } finally {
    S.inflight -= 1;
    if (S.inflight <= 0) { S.inflight = 0; document.body.classList.remove("busy"); }
  }
}

/** 通用 scope-ctl 调用：返回结果并写日志；写操作成功时压入撤销栈。 */
async function ctl(label, args, { confirm = false } = {}) {
  const r = await api("/api/ctl", { args, confirm, force: $("#force").checked });
  const li = logResult(label, args, r);
  const changes = r?.result?.changes;
  if (Array.isArray(changes)) {   // 部分成功也要能撤销：只收成功写入且有旧值的项
    const items = changes.filter((c) => "before" in c && c.ok !== false && c.before !== "(n/a)")
      .map((c) => ({ header: c.cmd.split(" ")[0], value: c.before })).reverse();
    if (items.length) { S.undoStack.push({ items, li }); $("#btn-undo").disabled = false; }
  }
  return r;
}

// ------------------------------------------------------------------ 日志
function logEntry(label, lines) {
  const li = document.createElement("li");
  const t = document.createElement("time");
  t.textContent = now();
  const box = document.createElement("div");
  const lab = document.createElement("span");
  lab.className = "label";
  lab.textContent = label;
  box.appendChild(lab);
  for (const [text, cls] of lines) {
    const s = document.createElement("span");
    s.className = "line" + (cls ? " " + cls : "");
    s.textContent = text;
    box.appendChild(s);
  }
  li.append(t, box);
  const log = $("#log");
  log.prepend(li);
  while (log.children.length > 200) log.lastChild.remove();
  return li;
}

function describeError(r) {
  const parts = [];
  if (r.error) parts.push(r.error);
  if (r.hint) parts.push(r.hint);
  return parts.join(" ") || "未知错误";
}

function logResult(label, args, r) {
  const lines = [];
  if (!r || !r.ok) {
    if (!r || r.error || r.hint || !r.failed?.length) lines.push([describeError(r || {}), "err"]);
    if (r?.failed) for (const f of r.failed) lines.push([`${f.cmd}  ${(f.errors || []).join("; ")}`, "err"]);
    if (r?.result?.changes) for (const c of r.result.changes) if (!c.errors?.length && "before" in c) lines.push([`${c.cmd}   ${c.before} → ${c.after}`, "ok"]);
    return logEntry(label, lines);
  }
  const res = r.result || {};
  if (Array.isArray(res.changes)) {
    for (const c of res.changes) {
      if ("before" in c) lines.push([`${c.cmd}   ${c.before} → ${c.after}`, c.errors?.length ? "err" : "ok"]);
      else lines.push([`${c.cmd}`, c.errors?.length ? "err" : "ok"]);
      for (const e of c.errors || []) lines.push([`  ! ${e}`, "err"]);
    }
    if (!res.changes.length) lines.push(["没有可写的项", "warn"]);
  } else if (res.sent) {
    lines.push([`${res.sent}${"completed" in res ? (res.completed ? "  已完成" : "  未在等待时间内完成") : ""}`, res.completed === false ? "warn" : "ok"]);
    if (res.note) lines.push([res.note, "warn"]);
  } else if (Array.isArray(res.results)) {
    for (const x of res.results) {
      if ("value" in x) lines.push([`${x.cmd}  →  ${x.value}`, "ok"]);
      else if (x.skipped) lines.push([`${x.cmd}  ${x.skipped}`, "warn"]);
      else if (x.error) lines.push([`${x.cmd}  ${x.error}`, "err"]);
      else lines.push([`${x.cmd}`, x.errors?.length ? "err" : "ok"]);
      for (const e of x.errors || []) lines.push([`  ! ${e}`, "err"]);
    }
  } else if (Array.isArray(res.errors) && args[0] === "errors") {
    lines.push([res.errors.length ? res.errors.join("\n") : "错误队列为空", res.errors.length ? "warn" : "ok"]);
  } else if (Array.isArray(res.steps)) {
    for (const st of res.steps) lines.push([JSON.stringify(st), st.ok === false || st.open_error ? "err" : "ok"]);
    if (res.next) lines.push([res.next, "warn"]);
  } else if (res.path) {
    lines.push([`${res.path}${res.windows_path ? "   (" + res.windows_path + ")" : ""}${res.size ? "  " + res.size : ""}${res.keys ? "  " + res.keys + " 项" : ""}`, "ok"]);
  } else if ("applied" in res) {
    lines.push([`已写回 ${res.applied} 项，失败 ${res.failed?.length || 0} 项`, res.failed?.length ? "warn" : "ok"]);
    for (const f of res.failed || []) lines.push([`${f.cmd}  ${(f.errors || []).join("; ")}`, "err"]);
  } else if (res.after && res.before) {
    lines.push(["Autoset 已执行；仪器已重写通道/时基/触发，见刷新后的面板", "warn"]);
  } else {
    lines.push([JSON.stringify(res).slice(0, 300), ""]);
  }
  for (const e of r.pending_errors || []) lines.push([`残留错误队列: ${e}`, "warn"]);
  if (r.query_timeouts) lines.push([`${r.query_timeouts} 条查询超时（仪器不支持的项，链路正常）`, "warn"]);
  if (r.link_clears) lines.push([`链路 clear ${r.link_clears} 次`, "warn"]);
  return logEntry(label, lines);
}

// ------------------------------------------------------------------ 渲染
const CH_NAMES = ["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8"];
const MEAS_NAMES = { PDEL: "峰峰值", MEAN: "均值", RMS: "有效值", AMPL: "幅值", FREQ: "频率", PER: "周期", MAX: "最大值", MIN: "最小值",
  RTIM: "上升时间", FTIM: "下降时间", PDCY: "正占空比", NDCY: "负占空比", HIGH: "高电平", LOW: "低电平" };

function chUnit(n) {
  const u = S.state?.channels?.[`CH${n}`]?.probe?.unit;
  return u && u !== "?" ? u : "";
}

/** 让 select 显示仪器返回的短形式（NORM → NORMal，DCL → DCLimit） */
function selectShort(sel, short) {
  if (short === undefined || short === null) return;
  const s = String(short).toUpperCase();
  const opts = Array.from(sel.options);
  let hit = opts.find((o) => o.value.toUpperCase() === s) || opts.find((o) => o.value.toUpperCase().startsWith(s));
  if (hit) sel.value = hit.value;
}

function setInput(inp, text) {
  if (document.activeElement === inp || inp.classList.contains("dirty")) return; // 正在编辑，不覆盖
  inp.value = text;
  inp.dataset.orig = text;
}

function renderHeader() {
  const st = S.state, h = S.health;
  const idn = st?.idn ? st.idn.split(",") : null;
  $("#idn").textContent = idn
    ? `${idn[0].replace("&", " & ")} ${idn[1]}，固件 ${idn[3]}，序列号 ${idn[2].split("/").pop()}${st.options ? "，选件 " + st.options : ""}`
    : `树莓派 ${S.config?.pi_host || ""}`;
  const dot = $("#link-dot");
  dot.className = "dot " + (st ? "ok" : h ? "warn" : "bad");
  const bench = $("#bench-text");
  if (h?.bench_running) {
    bench.textContent = `台架采集进行中：${h.busy.replace(/;$/, "")}`;
    $("#force-wrap").classList.remove("hidden");
  } else {
    bench.textContent = h ? `台架空闲，网页查看器${h.live === "active" ? "运行中（会占用仪器）" : "关"}，USB ${h.usb ? "已识别" : "未识别！"}` : "";
    if (!$("#force").checked) $("#force-wrap").classList.add("hidden");
  }
}

function renderAcq() {
  const a = S.state?.acquisition;
  if (!a) return;
  const count = Number(a.acquisitions_done);
  const el = $("#acq-count"), tr = $("#acq-trend");
  el.textContent = Number.isFinite(count) ? count.toLocaleString() : a.acquisitions_done;
  if (S.lastAcq && Number.isFinite(count)) {
    const growing = count !== S.lastAcq.count;
    tr.textContent = growing ? "采集计数在增长：运行中" : "采集计数未变：已停止或在等触发";
    if (growing && S.runMode !== "single-armed") S.runMode = "run";   // 仪器面板上被人按了 Run 也能看出来
    else if (!growing && S.runMode === "run") S.runMode = null;
  } else {
    tr.textContent = "采集计数";
  }
  if (Number.isFinite(count)) S.lastAcq = { count, at: Date.now() };
  lightKeys();
}

function lightKeys() {
  $("#btn-run").classList.toggle("lit", S.runMode === "run");
  $("#btn-stop").classList.toggle("lit", S.runMode === "stop");
  $("#btn-single").classList.toggle("lit", S.runMode === "single" || S.runMode === "single-armed");
}

function renderChannels() {
  const chans = S.state?.channels;
  if (!chans) return;
  const body = $("#ch-body");
  if (!body.children.length) buildChannelRows(body);
  for (let n = 1; n <= 8; n++) {
    const c = chans[`CH${n}`];
    const tr = body.children[n - 1];
    if (!c) continue;
    tr.classList.toggle("off", !c.on);
    $(".sw", tr).setAttribute("aria-pressed", c.on ? "true" : "false");
    const u = c.probe?.unit && c.probe.unit !== "?" ? c.probe.unit : "";
    const unitSel = $("select[data-flag=unit]", tr);
    if (u) unitSel.value = u;
    setInput($("input[data-flag=scale]", tr), toSI(c.scale_per_div));
    $(".u-scale", tr).textContent = u ? `${u}/div` : "/div";
    setInput($("input[data-flag=offset]", tr), toSI(c.offset));
    $(".u-offset", tr).textContent = u;
    setInput($("input[data-flag=position]", tr), plain(c.position_div));
    selectShort($("select[data-flag=coupling]", tr), c.coupling);
    setInput($("input[data-flag=bandwidth]", tr), c.bandwidth_hz === "?" ? "" : toSI(c.bandwidth_hz, 4));
    $("input[data-flag=invert]", tr).checked = c.invert === "1";
    const att = $("input[data-flag=atten]", tr);
    const mode = c.probe?.atten_mode;
    setInput(att, mode === "MAN" ? plain(c.probe.atten_manual) : "");
    att.placeholder = mode === "AUTO" ? "自动" : "";
    $(".hint-probe", tr).textContent = mode ? (mode === "MAN" ? "手动" : "自动") : "";
  }
}

function buildChannelRows(body) {
  for (let n = 1; n <= 8; n++) {
    const tr = document.createElement("tr");
    tr.style.setProperty("--c", `var(--c${n})`);
    tr.innerHTML = `
      <td><button class="sw" type="button" aria-pressed="false" aria-label="通道 ${n} 开关" data-ch="${n}"><span></span></button></td>
      <td><span class="chip">C${n}</span></td>
      <td><select data-cmd="chan" data-ch="${n}" data-flag="unit"><option>V</option><option>A</option></select></td>
      <td><input data-cmd="chan" data-ch="${n}" data-flag="scale" size="5"><span class="u u-scale"></span></td>
      <td><input data-cmd="chan" data-ch="${n}" data-flag="offset" size="5"><span class="u u-offset"></span></td>
      <td><input data-cmd="chan" data-ch="${n}" data-flag="position" size="4"><span class="u">div</span></td>
      <td><select data-cmd="chan" data-ch="${n}" data-flag="coupling">
            <option value="DC">DC 50 Ω</option><option value="DCLimit">DC 1 MΩ</option><option value="AC">AC</option></select></td>
      <td><input data-cmd="chan" data-ch="${n}" data-flag="bandwidth" size="5" list="bw-list"><span class="u">Hz</span></td>
      <td><input type="checkbox" data-cmd="chan" data-ch="${n}" data-flag="invert"></td>
      <td><input data-cmd="chan" data-ch="${n}" data-flag="atten" size="4"><span class="hint hint-probe"></span></td>`;
    body.appendChild(tr);
  }
}

function renderTimebase() {
  const t = S.state?.timebase;
  if (!t) return;
  setInput($("input[data-cmd=tb][data-flag=scale]"), toSI(t.scale_s_per_div));
  setInput($("input[data-cmd=tb][data-flag=position]"), toSI(t.position_s));
  setInput($("input[data-cmd=tb][data-flag=reference]"), plain(t.reference_pct, 4));
  selectShort($("select[data-cmd=tb][data-flag=roll]"), t.roll);
  $("#tb-range").textContent = fmtSI(t.range_s, "s");
  $("#tb-div").textContent = t.divisions;
}

function renderAcquisition() {
  const a = S.state?.acquisition;
  if (!a) return;
  selectShort($("select[data-flag=type]"), a.type);
  setInput($("input[data-flag=count]"), plain(a.average_count));
  selectShort($("select[data-flag=srate-mode]"), a.sample_rate_mode);
  setInput($("input[data-flag=srate]"), toSI(a.sample_rate_hz));
  selectShort($("select[data-flag=points-mode]"), a.record_length_mode);
  setInput($("input[data-flag=points]"), plain(a.record_length, 9));
  selectShort($("select[data-flag=interp]"), a.interpolation);
  $("#acq-res").textContent = fmtSI(a.resolution_s, "s");
  $("#acq-max").textContent = fmtSI(a.record_length_max, "pts");
}

function renderTrigger() {
  const t = S.state?.trigger;
  if (!t) return;
  selectShort($("select[data-cmd=trig][data-flag=mode]"), t.mode);
  selectShort($("#trig-source"), t.source);
  selectShort($("select[data-cmd=trig][data-flag=type]"), t.type);
  selectShort($("select[data-cmd=trig][data-flag=slope]"), t.edge_slope);
  selectShort($("select[data-cmd=trig][data-flag=holdoff-mode]"), t.holdoff_mode);
  $("#trig-coup").textContent = t.coupling ?? "—";
  updateLevelField();
}

function updateLevelField() {
  const t = S.state?.trigger;
  const src = $("#trig-source").value;
  const inp = $("#trig-level");
  const m = /^C([1-8])$/.exec(src);
  if (!m) { inp.value = ""; inp.disabled = true; $("#trig-level-unit").textContent = "外部/线路源"; return; }
  inp.disabled = false;
  const lev = t?.levels?.[src];
  setInput(inp, lev === undefined || lev === "?" ? "" : toSI(lev));
  $("#trig-level-unit").textContent = chUnit(m[1]);
}

function measUnit(type, src) {
  const t = String(type).toUpperCase();
  if (t.startsWith("FREQ")) return "Hz";
  if (["PER", "RTIM", "FTIM"].some((k) => t.startsWith(k))) return "s";
  if (t.endsWith("DCY")) return "%";
  const m = /^C([1-8])$/.exec(src || "");
  return m ? chUnit(m[1]) : "";
}

function renderMeasurements(values) {
  const meas = values || S.state?.measurements;
  if (!meas) return;
  const body = $("#meas-body");
  body.innerHTML = "";
  let any = false;
  for (const [slot, m] of Object.entries(meas)) {
    if (m.enabled === false) continue;
    any = true;
    const tr = document.createElement("tr");
    const n = /^C([1-8])$/.exec(m.source || "")?.[1];
    const v = Number(m.value);
    const shown = Number.isFinite(v) ? fmtSI(v, measUnit(m.type, m.source), 5) : (m.value ?? "—");
    tr.innerHTML = `<td>${slot}</td><td>${MEAS_NAMES[String(m.type).toUpperCase()] || m.type || "—"}</td>
      <td>${n ? `<span class="src" style="background:var(--c${n})"></span>` : ""}${m.source || ""}</td><td class="val"></td>`;
    $(".val", tr).textContent = shown;
    body.appendChild(tr);
  }
  if (!any) body.innerHTML = `<tr><td class="muted">没有启用的测量槽</td></tr>`;
}

function renderAll() {
  renderHeader();
  renderAcq();
  renderChannels();
  renderTimebase();
  renderAcquisition();
  renderTrigger();
  renderMeasurements();
}

// ------------------------------------------------------------------ 刷新
let refreshing = false;
async function refresh({ force = false, quiet = false } = {}) {
  if (refreshing) return;
  refreshing = true;
  try {
    const r = await api(`/api/state${force ? "?force=1" : ""}`);
    S.health = r.health || null;
    if (r.state?.ok) {
      S.state = r.state.result;
      $("#link-text").textContent = `链路正常，${r.state.seconds} s`;
      renderAll();
    } else {
      const msg = r.error || r.state?.error || describeError(r);
      $("#link-text").textContent = msg;
      renderHeader();
      $("#link-dot").className = "dot " + (S.health?.bench_running ? "warn" : "bad");
      if (!quiet) logEntry("刷新", [[msg, S.health?.bench_running ? "warn" : "err"]]);
    }
  } finally {
    refreshing = false;
  }
}

function scheduleRefresh(delay = 250) {
  clearTimeout(scheduleRefresh.t);
  scheduleRefresh.t = setTimeout(() => refresh({ quiet: true }), delay);
}

function setAuto(seconds) {
  clearInterval(S.timer);
  S.timer = null;
  if (seconds > 0) {
    S.timer = setInterval(async () => {
      if (S.inflight > 0 || document.hidden) return;
      await refresh({ quiet: true });
      if ($("#auto-shot").checked && S.state) await takeScreenshot(true);
    }, seconds * 1000);
  }
}

// ------------------------------------------------------------------ 字段提交
function fieldLabel(el) {
  const cmd = el.dataset.cmd, flag = el.dataset.flag;
  const names = { scale: "每格", offset: "偏置", position: "位置", coupling: "耦合", bandwidth: "带宽", invert: "反相", unit: "单位", atten: "探头衰减",
    reference: "参考点", roll: "滚动", type: "类型", count: "平均次数", "srate-mode": "采样率模式", srate: "采样率", "points-mode": "记录长度模式",
    points: "记录长度", interp: "插值", mode: "模式", source: "触发源", slope: "边沿", level: "电平", "holdoff-mode": "释抑" };
  const sect = { chan: `C${el.dataset.ch}`, tb: "时基", acq: "采集", trig: "触发" }[cmd];
  return `${sect} ${names[flag] || flag}`;
}

function argsFor(el, value) {
  const cmd = el.dataset.cmd, flag = el.dataset.flag;
  const args = cmd === "chan" ? ["chan", el.dataset.ch] : [cmd];
  args.push(`--${flag}`, value);
  return args;
}

async function commitField(el, value) {
  if (el.disabled) return;
  el.disabled = true;
  try {
    const r = await ctl(fieldLabel(el), argsFor(el, value));
    if (r.ok) {
      el.classList.remove("dirty");
      if (el.tagName === "INPUT" && el.type !== "checkbox") el.dataset.orig = el.value;
      if (el.dataset.cmd === "trig" && el.dataset.flag === "source") updateLevelField();
    }
  } finally {
    el.disabled = false;
    scheduleRefresh();
  }
}

function wireFields() {
  document.addEventListener("focusin", (e) => {
    const el = e.target;
    if (el.matches("input[data-cmd]:not([type=checkbox])") && el.dataset.orig === undefined) el.dataset.orig = el.value;
  });
  document.addEventListener("input", (e) => {
    const el = e.target;
    if (el.matches("input[data-cmd]:not([type=checkbox])")) el.classList.toggle("dirty", el.value !== el.dataset.orig);
  });
  document.addEventListener("keydown", (e) => {
    const el = e.target;
    if (!el.matches("input[data-cmd]:not([type=checkbox])")) return;
    if (e.key === "Enter") {
      e.preventDefault();
      const v = el.value.trim();
      if (!v || v === el.dataset.orig) { el.classList.remove("dirty"); return; }
      commitField(el, v);
    } else if (e.key === "Escape") {
      el.value = el.dataset.orig ?? "";
      el.classList.remove("dirty");
      el.blur();
    }
  });
  document.addEventListener("change", (e) => {
    const el = e.target;
    if (el.matches("select[data-cmd]")) commitField(el, el.value);
    else if (el.matches("input[type=checkbox][data-cmd]")) commitField(el, el.checked ? "on" : "off");
  });
  $("#ch-body").addEventListener("click", async (e) => {
    const sw = e.target.closest(".sw");
    if (!sw) return;
    const n = sw.dataset.ch;
    const on = sw.getAttribute("aria-pressed") === "true";
    sw.disabled = true;
    try {
      const r = await ctl(`C${n} ${on ? "关闭" : "打开"}`, ["chan", n, on ? "off" : "on"]);
      if (r.ok) sw.setAttribute("aria-pressed", on ? "false" : "true");
    } finally { sw.disabled = false; scheduleRefresh(); }
  });
  $("#trig-source").addEventListener("change", updateLevelField);
  $$("[data-trig-act]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try { await ctl(b.dataset.trigAct === "force" ? "强制触发" : "找电平", ["trig", `--${b.dataset.trigAct}`]); }
    finally { b.disabled = false; scheduleRefresh(); }
  }));
}

// ------------------------------------------------------------------ 运行控制
async function runAction(action) {
  if (S.runBusy) return;
  S.runBusy = true;
  $$(".key").forEach((k) => (k.disabled = true));
  try {
    if (action === "single") {
      const wait = Math.max(0, Math.min(600, Number($("#single-wait").value) || 0));
      S.runMode = "single-armed"; lightKeys();
      const r = await ctl("单次采集", ["single", "--wait", String(wait)]);
      S.runMode = r.ok ? (r.result?.completed ? "single" : "single-armed") : null;
    } else {
      const r = await ctl(action === "run" ? "连续采集" : "停止采集", [action]);
      S.runMode = r.ok ? action : null;
    }
  } finally {
    S.runBusy = false;
    $$(".key").forEach((k) => (k.disabled = false));
    lightKeys();
    scheduleRefresh(400);
  }
}

// ------------------------------------------------------------------ 截图 / 快照 / 其他
async function takeScreenshot(quiet = false) {
  const btn = $("#btn-shot");
  btn.disabled = true;
  try {
    const t0 = performance.now();
    const r = await api(`/api/screenshot?t=${Date.now()}`);
    if (r instanceof Blob) {
      const img = $("#shot");
      if (img.dataset.url) URL.revokeObjectURL(img.dataset.url);
      const url = URL.createObjectURL(r);
      img.src = url; img.dataset.url = url;
      img.parentElement.classList.add("has-shot");
      $("#shot-info").textContent = `${now()}，${(r.size / 1024).toFixed(0)} KB，${((performance.now() - t0) / 1000).toFixed(1)} s`;
    } else if (!quiet || !r.ok) {
      logEntry("截图", [[describeError(r), "err"]]);
    }
  } finally { btn.disabled = false; }
}

async function loadSnapshots() {
  const r = await api("/api/snapshots");
  const sel = $("#snap-list");
  sel.innerHTML = `<option value="">恢复快照…</option>`;
  if (!r.ok) return;
  for (const e of r.entries) {
    const o = document.createElement("option");
    o.value = e.path;
    const d = new Date(e.mtime);
    o.textContent = `${e.baseline ? "基线 " : ""}${e.name}  ${d.toLocaleDateString()} ${d.toTimeString().slice(0, 5)}`;
    sel.appendChild(o);
  }
}

async function saveSnapshot() {
  const b = $("#btn-snapshot");
  b.disabled = true;
  try {
    const r = await api("/api/snapshot", {});
    logResult("保存快照", ["settings", "snapshot"], r);
    if (r.ok) await loadSnapshots();
  } finally { b.disabled = false; }
}

async function restoreSnapshot() {
  const p = $("#snap-list").value;
  if (!p) return logEntry("恢复快照", [["先在下拉框里选一个快照", "warn"]]);
  if (!confirm(`把仪器参数恢复到\n${p}\n？这会写回上百项设置。`)) return;
  const b = $("#btn-restore");
  b.disabled = true;
  try {
    const r = await api("/api/restore", { path: p, force: $("#force").checked });
    logResult("恢复快照", ["settings", "restore", p], r);
    if (r.ok) clearUndo();
  } finally { b.disabled = false; scheduleRefresh(); }
}

function clearUndo() {
  S.undoStack.length = 0;
  $("#btn-undo").disabled = true;
}

async function undoLast() {
  const top = S.undoStack.pop();
  $("#btn-undo").disabled = S.undoStack.length === 0;
  if (!top) return;
  const r = await api("/api/undo", { items: top.items, force: $("#force").checked });
  logResult("撤销", ["scpi", "--write"], r);
  if (r.ok) top.li.classList.add("undone");
  else S.undoStack.push(top), ($("#btn-undo").disabled = false);
  scheduleRefresh();
}

async function sendScpi() {
  const inp = $("#scpi-cmd");
  const cmd = inp.value.trim();
  if (!cmd) return;
  const isQuery = cmd.endsWith("?") || cmd.includes("? ");
  if (!isQuery && !$("#scpi-write").checked) return logEntry("SCPI", [["写命令需要先勾选“允许写命令”", "warn"]]);
  const args = ["scpi", cmd, ...(isQuery ? [] : ["--write"])];
  const r = await ctl("SCPI", args);
  if (r.ok && !isQuery) scheduleRefresh();
}

function wireTools() {
  $("#btn-refresh").addEventListener("click", () => refresh({ force: $("#force").checked }));
  $("#auto").addEventListener("change", (e) => setAuto(Number(e.target.value)));
  $("#btn-shot").addEventListener("click", () => takeScreenshot());
  $("#btn-shot-save").addEventListener("click", async () => {
    const r = await api("/api/screenshot/save", { prefix: "panel" });
    logResult("截图存共享盘", ["screenshot"], r);
  });
  $("#btn-snapshot").addEventListener("click", saveSnapshot);
  $("#btn-restore").addEventListener("click", restoreSnapshot);
  $("#btn-undo").addEventListener("click", undoLast);
  $("#view-preset").addEventListener("change", async (e) => {
    const v = e.target.value;
    if (!v) return;
    e.target.value = "";
    await ctl(`界面预设 ${v}`, ["view", v]);
  });
  $("#btn-errors").addEventListener("click", () => ctl("错误队列", ["errors"]));
  $("#btn-autoset").addEventListener("click", async () => {
    if (!confirm("Autoset 会让仪器自动重写通道量程、时基和触发。继续？")) return;
    await ctl("Autoset", ["autoscale"], { confirm: true });
    clearUndo();
    S.runMode = null;
    scheduleRefresh(1000);
  });
  $("#btn-recover").addEventListener("click", async () => {
    if (!confirm("链路救援：先 VISA clear，不行再让树莓派重新枚举 USB。台架采集进行中时不要用。继续？")) return;
    await ctl("链路救援", ["recover"]);
    scheduleRefresh(1000);
  });
  $("#btn-meas").addEventListener("click", async () => {
    const r = await api("/api/ctl", { args: ["meas", "read"] });
    if (r.ok) renderMeasurements(r.result); else logResult("读测量值", ["meas", "read"], r);
  });
  $("#btn-scpi").addEventListener("click", sendScpi);
  $("#scpi-cmd").addEventListener("keydown", (e) => { if (e.key === "Enter") sendScpi(); });
  $$(".key").forEach((k) => k.addEventListener("click", () => runAction(k.dataset.run)));
  $("#force").addEventListener("change", (e) => {
    if (e.target.checked) logEntry("强制模式", [["已勾选：即使台架采集进行中也会写入仪器，可能打断批测", "warn"]]);
  });
  window.addEventListener("beforeunload", () => clearInterval(S.timer));
}

// ------------------------------------------------------------------ 启动
(async function main() {
  wireFields();
  wireTools();
  S.config = await api("/api/config");
  renderHeader();
  await refresh();
  setAuto(Number($("#auto").value));
  loadSnapshots();
})();
