#!/usr/bin/env node
/**
 * MXO38 远程面板 —— Mac 侧上位机。
 *
 * 结构：浏览器 ⇄ 本机 127.0.0.1:8038（本文件） ⇄ ssh 复用连接 ⇄ 树莓派 scope-ctl --json ⇄ MXO38
 *
 * - 零第三方依赖，只用 Node 内置模块。
 * - 所有树莓派调用排成一条队列串行执行：VISA 会话独占，两条 scope-ctl 并发会撞出 0xBFFF0011。
 * - 写操作（改参数、RUN/STOP/SINGle、恢复快照…）先在树莓派上 pgrep 台架采集进程
 *   （pi_agent.py / scope_capture_usb.py），发现正在采集就拒绝，避免打断批测。
 * - 本进程只碰示波器，不涉及 ControlDesk / Enable1。危险 SCPI（*RST、SYST:PRES、MMEM:DEL…）
 *   scope-ctl 需要 --i-know 才放行，这里永远不传。
 * - 每次写操作的 before → after 追加到 logs/panel-YYYYMMDD.jsonl，便于复现实验现场。
 *
 * 环境变量：SCOPE_PI_HOST（默认 root@172.16.102.132）、PANEL_PORT（默认 8038）、
 *          PANEL_BIND（默认 127.0.0.1）、SCOPE_PI_CTL（默认 scope-ctl）。
 * 参数：--no-open 启动后不自动打开浏览器。
 */
import http from "node:http";
import { spawn, execFile } from "node:child_process";
import { readFile, appendFile, mkdir } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const VERSION = "0.1.0";
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.join(__dirname, "public");
const LOG_DIR = path.join(__dirname, "logs");

const PI_HOST = process.env.SCOPE_PI_HOST || "root@172.16.102.132";
const PI_CTL = process.env.SCOPE_PI_CTL || "scope-ctl";
const PORT = Number(process.env.PANEL_PORT || 8038);
const BIND = process.env.PANEL_BIND || "127.0.0.1";
const OPEN_BROWSER = !process.argv.includes("--no-open");

// 树莓派侧路径
const SNAP_DIR = "/root/scope_data/panel";          // 面板快照（树莓派本地盘，不依赖 SMB）
const BASELINE = "/root/scope_data/ctl_pretest.json"; // 2026-09-20 调试前基线
const SHOT_TMP = "/tmp/mxo38_panel";                 // 预览截图临时目录（取回后即删）
const SHARE_DIR = "/mnt/scope_results";              // work-pc 共享盘 F:\ZYS_HYBASE\automation\scope_results
const BUSY_PATTERN = String.raw`pi_agen[t]\.py|scope_capture_us[b]\.py`; // 方括号技巧：不匹配 pgrep/bash 自身

// Unix 套接字路径上限 104 字节，macOS 的 os.tmpdir() 太长，固定用 /tmp
const CONTROL_PATH = "/tmp/mxo38-panel-ssh-%C";
const SSH_ARGS = [
  "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR",
  "-o", "ControlMaster=auto", "-o", `ControlPath=${CONTROL_PATH}`, "-o", "ControlPersist=900",
  "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
  PI_HOST, "bash", "-s",
];

// --------------------------------------------------------------------------- 树莓派调用
/** 把一段 bash 脚本经 stdin 送到树莓派执行（无需为远端做任何引号转义）。 */
function runRemote(script, { timeout = 60000, binary = false } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn("ssh", SSH_ARGS, {
      env: { ...process.env, LC_ALL: "C.UTF-8", LANG: "C.UTF-8" },
      stdio: ["pipe", "pipe", "pipe"],
    });
    const out = [], err = [];
    let killed = false;
    const timer = setTimeout(() => { killed = true; child.kill("SIGTERM"); }, timeout);
    child.stdout.on("data", (d) => out.push(d));
    child.stderr.on("data", (d) => err.push(d));
    child.on("error", (e) => { clearTimeout(timer); reject(e); });
    child.on("close", (code) => {
      clearTimeout(timer);
      const stdout = Buffer.concat(out);
      const stderr = Buffer.concat(err).toString("utf8");
      if (killed) return reject(new Error(`等待树莓派超过 ${Math.round(timeout / 1000)} s，已中止（远端命令可能仍在运行）`));
      resolve({ code, stdout: binary ? stdout : stdout.toString("utf8"), stderr });
    });
    child.stdin.on("error", () => {});
    child.stdin.end(script.endsWith("\n") ? script : script + "\n");
  });
}

/** POSIX 单引号包裹，供远端 bash 使用。 */
function shq(value) {
  const text = String(value);
  if (/^[A-Za-z0-9_.:/=+,-]+$/.test(text)) return text;
  return `'${text.replace(/'/g, `'\\''`)}'`;
}

function lastLine(text) {
  return String(text || "").trim().split(/\r?\n/).filter((l) => l && !/setlocale/.test(l)).pop() || "";
}

/** 从 scope-ctl 输出里切出 JSON 对象；没有 JSON 时把 stderr 变成可读错误。 */
function parseCtl({ code, stdout, stderr }) {
  const start = stdout.indexOf("{");
  if (start < 0) {
    if (code === 255) throw new Error(`连不上树莓派 ${PI_HOST}：${lastLine(stderr) || "ssh 失败"}`);
    throw new Error(`scope-ctl 没有返回 JSON（exit ${code}）：${lastLine(stderr) || lastLine(stdout) || "无输出"}`);
  }
  return JSON.parse(stdout.slice(start));
}

/** 串行队列：同一时刻只有一条命令在树莓派上打开 VISA。 */
let chain = Promise.resolve();
let queueDepth = 0;
let current = null;
function enqueue(label, fn) {
  queueDepth += 1;
  const run = async () => {
    current = { label, started: Date.now() };
    try { return await fn(); } finally { current = null; queueDepth -= 1; }
  };
  const p = chain.then(run, run);
  chain = p.catch(() => {});
  return p;
}

const HEALTH_SCRIPT = `
busy=$(pgrep -fa '${BUSY_PATTERN}' | tr '\\n' ';')
echo "@@BUSY@@$busy"
echo "@@LIVE@@$(systemctl is-active scope-live 2>/dev/null)"
echo "@@USB@@$(lsusb 2>/dev/null | grep -c 0aad)"
echo "@@PITIME@@$(date +%s)"
`;

function parseHealth(text) {
  const h = { busy: "", live: "", usb: 0, pi_time: 0 };
  for (const line of text.split(/\r?\n/)) {
    const m = /^@@(\w+)@@(.*)$/.exec(line);
    if (!m) continue;
    if (m[1] === "BUSY") h.busy = m[2].trim();
    else if (m[1] === "LIVE") h.live = m[2].trim();
    else if (m[1] === "USB") h.usb = Number(m[2]) || 0;
    else if (m[1] === "PITIME") h.pi_time = Number(m[2]) || 0;
  }
  h.bench_running = h.busy.length > 0;
  return h;
}

/** 健康探测 + 全量状态，一次往返。台架采集进行中时不碰 VISA（state 为 null）。 */
async function readState({ force = false } = {}) {
  const script = `${HEALTH_SCRIPT}
echo "@@STATE@@"
if [ -z "$busy" ] || [ "${force ? 1 : 0}" = 1 ]; then ${PI_CTL} --json state --all </dev/null; fi
`;
  const r = await runRemote(script, { timeout: 45000 });
  if (r.code === 255 && !r.stdout.includes("@@")) throw new Error(`连不上树莓派 ${PI_HOST}：${lastLine(r.stderr) || "ssh 失败"}`);
  const [head, tail = ""] = r.stdout.split("@@STATE@@");
  const health = parseHealth(head);
  let state = null, error = null;
  if (health.bench_running && !force) {
    error = `台架采集进行中（${health.busy}），本轮未读取仪器`;
  } else {
    try { state = parseCtl({ code: r.code, stdout: tail, stderr: r.stderr }); }
    catch (e) { error = e.message; }
  }
  return { health, state, error, queue: queueDepth };
}

/** 执行一条 scope-ctl；写操作先探测台架进程。 */
async function runCtl(args, { timeout = 60000, write = false, force = false } = {}) {
  const line = [PI_CTL, "--json", ...args.map(shq)].join(" ") + " </dev/null";
  if (!write) return parseCtl(await runRemote(line, { timeout }));
  const script = `${HEALTH_SCRIPT}
if [ -n "$busy" ] && [ "${force ? 1 : 0}" != 1 ]; then echo "@@REFUSED@@"; exit 3; fi
echo "@@STATE@@"
${line}
`;
  const r = await runRemote(script, { timeout });
  const [head, tail = ""] = r.stdout.split("@@STATE@@");
  const health = parseHealth(head);
  if (r.stdout.includes("@@REFUSED@@")) {
    return { ok: false, refused: true, health,
      error: `台架采集进行中（${health.busy}），已拒绝写入。等批测结束再操作，或勾选"强制"。` };
  }
  const out = parseCtl({ code: r.code, stdout: tail, stderr: r.stderr });
  out.health = health;
  return out;
}

// --------------------------------------------------------------------------- 命令校验
const SUBCOMMANDS = new Set(["info", "state", "chan", "tb", "acq", "run", "stop", "single", "trig", "display",
  "view", "meas", "screenshot", "settings", "scpi", "errors", "recover", "autoscale"]);
const SNAP_RE = /^\/root\/scope_data\/(panel\/[A-Za-z0-9_.-]+|ctl_pretest)\.json$/;

function classify(args) {
  const [sub, ...rest] = args;
  if (!SUBCOMMANDS.has(sub)) throw new Error(`不支持的子命令 ${sub}`);
  if (args.some((a) => /^--i-know$/.test(a))) throw new Error("--i-know 不允许从面板传入");
  if (args.some((a) => /[\r\n\0]/.test(a) || a.length > 300)) throw new Error("参数含非法字符或过长");
  let write = false, timeout = 60000;
  switch (sub) {
    case "run": case "stop": write = true; break;
    case "single": {
      write = true;
      const i = rest.indexOf("--wait");
      const wait = i >= 0 ? Number(rest[i + 1]) : 20;
      if (!(wait >= 0 && wait <= 600)) throw new Error("--wait 需在 0–600 s");
      timeout = 30000 + wait * 1000;
      break;
    }
    case "autoscale": write = true; timeout = 90000; break;
    case "recover": write = true; timeout = 90000; break;
    case "view": write = rest[0] !== "list"; break;
    case "settings": {
      write = ["save", "recall", "restore"].includes(rest[0]);
      if (rest[0] === "restore" && !SNAP_RE.test(rest[1] || "")) throw new Error("只允许恢复面板快照或基线文件");
      if (rest[0] === "snapshot" && !SNAP_RE.test(rest[1] || "")) throw new Error("快照只能写到 /root/scope_data/panel/");
      if ((rest[0] === "save" || rest[0] === "recall") && !/^[A-Za-z0-9_.-]+$/.test(rest[1] || "")) throw new Error("设置名只能含字母数字 _ . -");
      timeout = 180000;
      break;
    }
    case "scpi": write = rest.includes("--write"); timeout = 90000; break;
    case "meas": write = rest[0] !== "read" && rest.length > 1; break;
    case "chan": write = rest.slice(1).some((a) => a.startsWith("--") || /^(on|off)$/i.test(a)); break;
    case "tb": case "acq": case "trig": case "display": write = rest.some((a) => a.startsWith("--")); break;
    case "screenshot": timeout = 90000; break;
    default: break;
  }
  return { write, timeout };
}

// --------------------------------------------------------------------------- 日志
async function logWrite(entry) {
  try {
    await mkdir(LOG_DIR, { recursive: true });
    const d = new Date();
    const name = `panel-${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}.jsonl`;
    await appendFile(path.join(LOG_DIR, name), JSON.stringify({ ts: d.toISOString(), ...entry }) + "\n");
  } catch (e) {
    console.error("写日志失败:", e.message);
  }
}

function stamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

// --------------------------------------------------------------------------- HTTP
const MIME = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".json": "application/json; charset=utf-8" };

function sendJson(res, status, body) {
  const text = JSON.stringify(body);
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  res.end(text);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => { chunks.push(c); if (chunks.length > 2000) reject(new Error("请求体过大")); });
    req.on("end", () => {
      const text = Buffer.concat(chunks).toString("utf8");
      if (!text) return resolve({});
      try { resolve(JSON.parse(text)); } catch { reject(new Error("请求体不是 JSON")); }
    });
    req.on("error", reject);
  });
}

async function serveStatic(res, urlPath) {
  const rel = urlPath === "/" ? "/index.html" : urlPath;
  const file = path.normalize(path.join(PUBLIC_DIR, rel));
  if (!file.startsWith(PUBLIC_DIR)) { res.writeHead(403); return res.end(); }
  try {
    const data = await readFile(file);
    res.writeHead(200, { "Content-Type": MIME[path.extname(file)] || "application/octet-stream", "Cache-Control": "no-cache" });
    res.end(data);
  } catch {
    res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
    res.end("not found");
  }
}

const routes = {
  "GET /api/config": async () => ({
    ok: true, version: VERSION, pi_host: PI_HOST, port: PORT, snap_dir: SNAP_DIR, baseline: BASELINE,
    share_dir: SHARE_DIR, windows_share: "F:\\ZYS_HYBASE\\automation\\scope_results",
  }),

  "GET /api/state": async (url) => enqueue("state", () => readState({ force: url.searchParams.get("force") === "1" })),

  "POST /api/ctl": async (url, body) => {
    const args = Array.isArray(body.args) ? body.args.map(String) : null;
    if (!args || !args.length) throw new Error("缺少 args");
    const { write, timeout } = classify(args);
    if (args[0] === "autoscale" && body.confirm !== true) throw new Error("Autoset 会重写通道/时基/触发，需要确认");
    const result = await enqueue(args.slice(0, 2).join(" "), () => runCtl(args, { timeout, write, force: body.force === true }));
    if (write) await logWrite({ args, ok: result.ok, refused: !!result.refused, changes: result.result?.changes,
      sent: result.result?.sent, failed: result.failed, error: result.error, pending_errors: result.pending_errors });
    return result;
  },

  /** 撤销：把上一组写操作的 before 值原样写回（走 scope-ctl scpi --write，危险命令仍被其护栏挡住）。 */
  "POST /api/undo": async (url, body) => {
    const items = Array.isArray(body.items) ? body.items : [];
    const cmds = [];
    for (const it of items) {
      const header = String(it.header || ""), value = String(it.value ?? "");
      if (!/^[A-Za-z0-9:]+$/.test(header)) throw new Error(`非法 SCPI 头 ${header}`);
      if (!/^[-+A-Za-z0-9.:_ ()\/"]+$/.test(value) || value.startsWith("(")) continue; // 跳过 (n/a) 之类
      cmds.push(`${header} ${value}`);
    }
    if (!cmds.length) throw new Error("没有可撤销的项");
    const args = ["scpi", ...cmds, "--write"];
    const result = await enqueue("undo", () => runCtl(args, { timeout: 60000, write: true, force: body.force === true }));
    await logWrite({ args, undo: true, ok: result.ok, refused: !!result.refused, results: result.result?.results, error: result.error });
    return result;
  },

  /** 预览截图：写到树莓派 /tmp，取回后删除。返回 PNG 二进制。 */
  "GET /api/screenshot": async (url, body, res) => {
    const png = await enqueue("screenshot", async () => {
      const shot = parseCtl(await runRemote(`mkdir -p ${SHOT_TMP}; ${PI_CTL} --json screenshot -o ${SHOT_TMP} -f preview </dev/null`, { timeout: 90000 }));
      if (!shot.ok || !shot.result?.path) throw new Error(shot.error || "截图失败");
      const p = shot.result.path;
      if (!p.startsWith(SHOT_TMP + "/")) throw new Error(`截图路径异常 ${p}`);
      const r = await runRemote(`cat ${shq(p)} && rm -f ${shq(p)}`, { timeout: 60000, binary: true });
      if (r.code !== 0 || !r.stdout.length) throw new Error(`取回截图失败：${lastLine(r.stderr)}`);
      return r.stdout;
    });
    res.writeHead(200, { "Content-Type": "image/png", "Cache-Control": "no-store", "Content-Length": png.length });
    res.end(png);
    return null;
  },

  /** 截图保存到 work-pc 共享盘（不取回）。 */
  "POST /api/screenshot/save": async (url, body) => {
    const prefix = /^[A-Za-z0-9_-]{1,40}$/.test(body.prefix || "") ? body.prefix : "panel";
    return enqueue("screenshot", () => runCtl(["screenshot", "-f", prefix], { timeout: 90000 }));
  },

  "GET /api/snapshots": async () => {
    const r = await runRemote(`mkdir -p ${SNAP_DIR}; stat -c '%n|%s|%Y' ${SNAP_DIR}/*.json ${BASELINE} 2>/dev/null | sort -t'|' -k3 -rn`, { timeout: 20000 });
    const entries = r.stdout.split(/\r?\n/).filter(Boolean).map((line) => {
      const [p, size, mtime] = line.split("|");
      return { path: p, name: path.basename(p), bytes: Number(size), mtime: Number(mtime) * 1000, baseline: p === BASELINE };
    });
    return { ok: true, entries };
  },

  "POST /api/snapshot": async (url, body) => {
    const tag = /^[A-Za-z0-9_-]{1,40}$/.test(body.tag || "") ? `_${body.tag}` : "";
    const file = `${SNAP_DIR}/snap_${stamp()}${tag}.json`;
    const result = await enqueue("snapshot", () => runCtl(["settings", "snapshot", file], { timeout: 60000 }));
    result.path = file;
    return result;
  },

  "POST /api/restore": async (url, body) => {
    const file = String(body.path || "");
    if (!SNAP_RE.test(file)) throw new Error("只允许恢复面板快照或基线文件");
    const result = await enqueue("restore", () => runCtl(["settings", "restore", file], { timeout: 180000, write: true, force: body.force === true }));
    await logWrite({ args: ["settings", "restore", file], ok: result.ok, applied: result.result?.applied, failed: result.result?.failed, error: result.error });
    return result;
  },

  "GET /api/queue": async () => ({ ok: true, depth: queueDepth, current }),
};

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  const key = `${req.method} ${url.pathname}`;
  const handler = routes[key];
  if (!handler) {
    if (req.method === "GET" && !url.pathname.startsWith("/api/")) return serveStatic(res, url.pathname);
    return sendJson(res, 404, { ok: false, error: `no route ${key}` });
  }
  try {
    const body = req.method === "POST" ? await readBody(req) : {};
    const out = await handler(url, body, res);
    if (out !== null) sendJson(res, 200, out);
  } catch (e) {
    if (!res.headersSent) sendJson(res, 200, { ok: false, error: e.message });
    else res.end();
  }
});

server.listen(PORT, BIND, () => {
  const addr = `http://${BIND}:${PORT}/`;
  console.log(`MXO38 远程面板 v${VERSION}  ${addr}  → ${PI_HOST}`);
  console.log(`快照目录 ${SNAP_DIR}  日志 ${LOG_DIR}`);
  if (OPEN_BROWSER && process.platform === "darwin") execFile("open", [addr], () => {});
});

// 退出时顺手关掉 ssh 复用主连接（否则它会按 ControlPersist 再挂 15 min，无害但不整洁）
for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    server.close();
    execFile("ssh", ["-O", "exit", "-o", `ControlPath=${CONTROL_PATH}`, PI_HOST], () => process.exit(0));
    setTimeout(() => process.exit(0), 1500).unref();
  });
}
