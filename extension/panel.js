// Discord Call Recorder — sānu panelis.
// Ieraksta taba audio ar tabCapture + MediaRecorder un vāc runātāju
// notikumus no content.js. Stop -> saglabā .webm, .speakers.json, .speakers.srt.

const els = {
  sensor: document.getElementById('sensor'),
  startBtn: document.getElementById('startBtn'),
  stopBtn: document.getElementById('stopBtn'),
  status: document.getElementById('status'),
  timer: document.getElementById('timer'),
  nowSpeaking: document.getElementById('nowSpeaking'),
  log: document.getElementById('log'),
};

let recorder = null;
let stream = null;
let micStream = null;
let audioCtx = null;
let chunks = [];
let events = []; // {name, userId, speaking, t_ms} relatīvi pret ieraksta sākumu
let t0 = 0;
let timerInterval = null;
let lastFiles = null; // {audio: Blob, json: Blob, srt: Blob, base: string}
const speakingNow = new Map(); // key -> name

// ---------- runātāju notikumi no content.js ----------

chrome.runtime.onMessage.addListener((msg) => {
  if (!msg || typeof msg !== 'object') return;
  if (msg.type === 'dvt-content-ready') {
    els.sensor.textContent = 'DOM sensors: aktīvs ✓';
    els.sensor.classList.add('ok');
    return;
  }
  if (msg.type !== 'dvt-speaking') return;

  els.sensor.textContent = 'DOM sensors: aktīvs ✓ (redz runātājus)';
  els.sensor.classList.add('ok');

  const key = msg.userId || msg.name;
  if (msg.speaking) speakingNow.set(key, msg.name);
  else speakingNow.delete(key);
  renderNowSpeaking();

  if (recorder && recorder.state === 'recording') {
    const t_ms = Math.max(0, msg.t - t0);
    events.push({ name: msg.name, userId: msg.userId || null, speaking: msg.speaking, t_ms });
    logLine(`${fmtTime(t_ms)} ${msg.speaking ? '▶' : '⏹'} ${msg.name}`, msg.speaking ? 'on' : 'off');
  }
});

function renderNowSpeaking() {
  if (speakingNow.size === 0) {
    els.nowSpeaking.textContent = '—';
    els.nowSpeaking.classList.add('muted');
    return;
  }
  els.nowSpeaking.classList.remove('muted');
  els.nowSpeaking.innerHTML = '';
  for (const name of speakingNow.values()) {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.textContent = name;
    els.nowSpeaking.appendChild(chip);
  }
}

function logLine(text, cls) {
  const div = document.createElement('div');
  div.textContent = text;
  if (cls) div.className = cls;
  els.log.prepend(div);
  while (els.log.childElementCount > 500) els.log.lastChild.remove();
}

// ---------- iestatījumi ----------

const SETTINGS_KEYS = ['setClaude', 'setLang', 'setReport', 'setAuto', 'setOutDir'];
const MODEL = 'large-v3-turbo'; // vienīgā opcija
const SCRIPT_PATH = '~/git/personal/discord-voice-transcriber/tools/transcribe.py';

async function loadSettings() {
  const stored = await chrome.storage.local.get('dvtSettings');
  const s = stored.dvtSettings || {};
  for (const id of SETTINGS_KEYS) {
    const el = document.getElementById(id);
    if (!(id in s)) continue;
    if (el.type === 'checkbox') el.checked = s[id];
    else el.value = s[id];
  }
}

function saveSettings() {
  const s = {};
  for (const id of SETTINGS_KEYS) {
    const el = document.getElementById(id);
    s[id] = el.type === 'checkbox' ? el.checked : el.value;
  }
  chrome.storage.local.set({ dvtSettings: s });
}

for (const id of SETTINGS_KEYS)
  document.getElementById(id).addEventListener('change', () => {
    saveSettings();
    if (lastFiles) showCommand(lastFiles.base);
  });
loadSettings();

function buildCommand(base) {
  const claude = document.getElementById('setClaude').value;
  const lang = document.getElementById('setLang').value;
  const report = document.getElementById('setReport').checked;
  let cmd = `python3 ${SCRIPT_PATH} ~/Downloads/${base}.webm --model ${MODEL}`;
  if (lang !== 'auto') cmd += ` --language ${lang}`;
  if (report) cmd += ` --report --claude ${claude}`;
  return cmd;
}

function showCommand(base) {
  document.getElementById('cmd').textContent = buildCommand(base);
  document.getElementById('cmdBlock').hidden = false;
}

document.getElementById('copyCmd').addEventListener('click', () => {
  if (!lastFiles) return;
  navigator.clipboard.writeText(buildCommand(lastFiles.base));
  setStatus('Komanda nokopēta — ielīmē terminālī.');
});

function collectSettings() {
  return {
    claude: document.getElementById('setClaude').value,
    model: MODEL,
    language: document.getElementById('setLang').value,
    report: document.getElementById('setReport').checked,
    autoTranscribe: document.getElementById('setAuto').checked,
    outDir: document.getElementById('setOutDir').value.trim() || null,
  };
}

// ---------- native messaging host ----------
// Ja hosts ir instalēts (native/install.sh), ieraksts automātiski nonāk
// projekta mapē un transcribe.py palaižas pats; citādi failus metam Downloads.

const NATIVE_HOST = 'com.dvt.recorder';
let nativePort = null;

function connectNative() {
  try {
    nativePort = chrome.runtime.connectNative(NATIVE_HOST);
  } catch (e) {
    nativePort = null;
    return;
  }
  nativePort.onMessage.addListener(onNativeMsg);
  nativePort.onDisconnect.addListener(() => {
    nativePort = null;
    els.native = els.native || document.getElementById('native');
    els.native.textContent =
      'Native host: nav instalēts — faili kritīs Downloads (sk. native/install.sh)';
    els.native.classList.remove('ok');
  });
  nativePort.postMessage({ type: 'ping' });
  nativePort.postMessage({ type: 'list', dir: collectSettings().outDir });
}

function onNativeMsg(msg) {
  const nativeEl = document.getElementById('native');
  if (msg.type === 'pong') {
    nativeEl.textContent = 'Native host: aktīvs ✓ (auto-saglabāšana un transkripcija)';
    nativeEl.classList.add('ok');
  } else if (msg.type === 'saved') {
    logLine('Saglabāts: ' + msg.path);
    setStatus('Saglabāts mapē, transkripcija rit…');
  } else if (msg.type === 'log') {
    logLine(msg.line);
  } else if (msg.type === 'done') {
    setStatus(msg.code === 0 ? 'Transkripcija pabeigta: ' + msg.base : 'Transkripcija neizdevās (skat. žurnālu)');
    nativePort && nativePort.postMessage({ type: 'list', dir: collectSettings().outDir });
  } else if (msg.type === 'list') {
    renderRecList(msg.items, msg.dir);
  }
}

function renderRecList(items, dir) {
  const el = document.getElementById('recList');
  if (!items || items.length === 0) {
    el.textContent = '— (' + dir + ')';
    return;
  }
  el.classList.remove('muted');
  el.innerHTML = '';
  el.title = dir;
  for (const it of items) {
    const div = document.createElement('div');
    div.textContent = `${it.base}  ${it.report ? '✓ report' : it.transcript ? '✓ transcript' : '⏳ tikai audio'}`;
    div.style.cursor = 'pointer';
    div.title = 'Atvērt redaktorā';
    div.addEventListener('click', () =>
      chrome.tabs.create({ url: chrome.runtime.getURL('editor.html') + '?base=' + encodeURIComponent(it.base) }));
    el.appendChild(div);
  }
}

function b64(u8) {
  let s = '';
  for (let i = 0; i < u8.length; i += 0x8000)
    s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
  return btoa(s);
}

async function sendToNative(files) {
  nativePort.postMessage({ type: 'begin', base: files.base, settings: collectSettings() });
  const buf = new Uint8Array(await files.audio.arrayBuffer());
  const CHUNK = 256 * 1024;
  for (let i = 0; i < buf.length; i += CHUNK)
    nativePort.postMessage({ type: 'chunk', data: b64(buf.subarray(i, i + CHUNK)) });
  nativePort.postMessage({
    type: 'finish',
    base: files.base,
    speakers: await files.json.text(),
    srt: await files.srt.text(),
  });
}

connectNative();

// ---------- sensora ping ----------
// content.js ielādējas agrāk par paneli, tāpēc "ready" ziņu panelis var nokavēt —
// aktīvi pingojam Discord tabu.

async function pingSensor() {
  try {
    const tab = await findDiscordTab();
    if (!tab) {
      els.sensor.textContent = 'DOM sensors: nav atvērta discord.com taba';
      els.sensor.classList.remove('ok');
      return;
    }
    const resp = await chrome.tabs.sendMessage(tab.id, { type: 'dvt-ping' });
    if (resp && resp.type === 'dvt-pong') {
      els.sensor.textContent = `DOM sensors: aktīvs ✓ (saraksts: ${resp.voiceUsers}, flīzes: ${resp.tiles}${
        resp.speakers.length ? ', runā: ' + resp.speakers.join(', ') : ''
      })`;
      els.sensor.classList.add('ok');
    }
  } catch (e) {
    els.sensor.textContent =
      'DOM sensors: nav ielādēts — pārlādē Discord tabu (F5) un uzklikšķini uz ikonas vēlreiz';
    els.sensor.classList.remove('ok');
  }
}
pingSensor();
setInterval(pingSensor, 3000);

// ---------- ieraksts ----------

els.startBtn.addEventListener('click', startRecording);
els.stopBtn.addEventListener('click', stopRecording);

async function findDiscordTab() {
  const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (activeTab && /^https:\/\/discord\.com\//.test(activeTab.url || '')) return activeTab;
  const tabs = await chrome.tabs.query({ url: 'https://discord.com/*' });
  return tabs[0] || null;
}

async function captureTabAudio(tab) {
  try {
    const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tab.id });
    const s = await navigator.mediaDevices.getUserMedia({
      audio: {
        mandatory: { chromeMediaSource: 'tab', chromeMediaSourceId: streamId },
      },
      video: false,
    });
    return { stream: s, viaTabCapture: true };
  } catch (e) {
    // Rezerves ceļš: Chrome koplietošanas dialogs. Izvēlies cilni "Chrome Tab" →
    // Discord tabu un ieslēdz "Also share tab audio".
    setStatus('Dialogā izvēlies Discord tabu un ieslēdz "Also share tab audio"…');
    const display = await navigator.mediaDevices.getDisplayMedia({
      video: true,
      audio: true,
      preferCurrentTab: false,
    });
    if (display.getAudioTracks().length === 0) {
      display.getTracks().forEach((t) => t.stop());
      throw new Error('Netika iedots taba audio — dialogā jāieslēdz "Also share tab audio".');
    }
    return { stream: display, viaTabCapture: false };
  }
}

async function startRecording() {
  try {
    const tab = await findDiscordTab();
    if (!tab) {
      return setStatus('Neatradu atvērtu discord.com tabu.', true);
    }

    const cap = await captureTabAudio(tab);
    stream = cap.stream;
    stream.getAudioTracks()[0].addEventListener('ended', () => {
      if (recorder && recorder.state === 'recording') stopRecording();
    });

    // Taba audio satur tikai PĀRĒJOS runātājus — tava balss tabā neskan,
    // tāpēc to ņemam no mikrofona un miksējam vienā celiņā.
    try {
      micStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (e) {
      micStream = null;
      logLine('⚠ Mikrofons nav pieejams (' + e.name + ') — tava balss ierakstā nebūs!');
    }

    audioCtx = new AudioContext();
    const mixDest = audioCtx.createMediaStreamDestination();
    const tabSrc = audioCtx.createMediaStreamSource(new MediaStream(stream.getAudioTracks()));
    tabSrc.connect(mixDest);
    if (micStream) audioCtx.createMediaStreamSource(micStream).connect(mixDest);

    // tabCapture apklusina tabu — laižam TIKAI taba skaņu atpakaļ skaļruņos
    // (mikrofonu ne, citādi dzirdētu pats sevi ar aizturi).
    if (cap.viaTabCapture) tabSrc.connect(audioCtx.destination);

    chunks = [];
    events = [];
    t0 = Date.now();
    recorder = new MediaRecorder(mixDest.stream, { mimeType: 'audio/webm;codecs=opus' });
    recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    recorder.onstop = onRecorderStop;
    recorder.start(1000);

    // ja kāds jau runā ieraksta sākumā, atzīmējam no 0
    for (const [key, name] of speakingNow) {
      events.push({ name, userId: key === name ? null : key, speaking: true, t_ms: 0 });
    }

    document.body.classList.add('rec');
    els.startBtn.disabled = true;
    els.stopBtn.disabled = false;
    setStatus('● Ieraksts rit');
    timerInterval = setInterval(
      () => (els.timer.textContent = fmtTime(Date.now() - t0)),
      1000
    );
    logLine(`${fmtTime(0)} — ieraksts sākts (${tab.title || 'Discord'})`);
  } catch (e) {
    console.error(e);
    if (/has not been invoked/i.test(e.message || '')) {
      setStatus(
        'Chrome vēl nav devis atļauju šim tabam: aizver paneli, uzklikšķini uz paplašinājuma ikonas, esot uz Discord taba, un spied Sākt vēlreiz.',
        true
      );
    } else {
      setStatus('Neizdevās sākt: ' + e.message, true);
    }
    cleanup();
  }
}

function stopRecording() {
  if (recorder && recorder.state === 'recording') recorder.stop();
}

function onRecorderStop() {
  const durMs = Date.now() - t0;
  // aizveram vaļējos intervālus ieraksta beigās
  for (const [key, name] of speakingNow) {
    events.push({ name, userId: key === name ? null : key, speaking: false, t_ms: durMs });
  }

  const base = 'discord-call-' + tsName(t0);
  const intervals = buildIntervals(events, durMs);

  const audio = new Blob(chunks, { type: 'audio/webm' });
  const json = new Blob(
    [JSON.stringify({ started_at: new Date(t0).toISOString(), duration_ms: durMs, intervals }, null, 2)],
    { type: 'application/json' }
  );
  const srt = new Blob([toSrt(intervals)], { type: 'text/plain' });

  lastFiles = { audio, json, srt, base };

  if (nativePort) {
    logLine(`${fmtTime(durMs)} — sūtu native hostam (${intervals.length} intervāli)…`);
    sendToNative(lastFiles).catch((e) => {
      logLine('Native host kļūda, krītu uz Downloads: ' + e.message);
      downloadAll();
    });
  } else {
    downloadAll();
    showCommand(base);
  }
  cleanup();

  function downloadAll() {
    download(audio, base + '.webm');
    download(json, base + '.speakers.json');
    download(srt, base + '.speakers.srt');
    logLine(`${fmtTime(durMs)} — saglabāts Downloads: ${base}.webm + speakers (${intervals.length} intervāli)`);
    setStatus('Saglabāts: ' + base);
  }
}

function cleanup() {
  if (timerInterval) clearInterval(timerInterval);
  timerInterval = null;
  if (stream) stream.getTracks().forEach((t) => t.stop());
  if (micStream) micStream.getTracks().forEach((t) => t.stop());
  if (audioCtx) audioCtx.close().catch(() => {});
  stream = null;
  micStream = null;
  audioCtx = null;
  recorder = null;
  document.body.classList.remove('rec');
  els.startBtn.disabled = false;
  els.stopBtn.disabled = true;
  els.timer.textContent = '';
}

// ---------- eksports ----------

// No on/off notikumiem saliek [{name, userId, start_ms, end_ms}]
function buildIntervals(evts, durMs) {
  const open = new Map();
  const out = [];
  const sorted = [...evts].sort((a, b) => a.t_ms - b.t_ms);
  for (const e of sorted) {
    const key = e.userId || e.name;
    if (e.speaking) {
      if (!open.has(key)) open.set(key, e);
    } else if (open.has(key)) {
      const s = open.get(key);
      open.delete(key);
      if (e.t_ms - s.t_ms >= 150) // izmetam <150ms sprakšķus
        out.push({ name: s.name, userId: s.userId, start_ms: s.t_ms, end_ms: e.t_ms });
    }
  }
  for (const s of open.values())
    out.push({ name: s.name, userId: s.userId, start_ms: s.t_ms, end_ms: durMs });
  return out.sort((a, b) => a.start_ms - b.start_ms);
}

function toSrt(intervals) {
  return intervals
    .map(
      (iv, i) =>
        `${i + 1}\n${srtTime(iv.start_ms)} --> ${srtTime(iv.end_ms)}\n${iv.name}\n`
    )
    .join('\n');
}

function download(blob, filename) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 30000);
}


// ---------- helpers ----------

function setStatus(text, isErr) {
  els.status.textContent = text;
  els.status.style.color = isErr ? '#f23f43' : '';
}

function fmtTime(ms) {
  const s = Math.floor(ms / 1000);
  const h = String(Math.floor(s / 3600)).padStart(2, '0');
  const m = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
  const sec = String(s % 60).padStart(2, '0');
  return `${h}:${m}:${sec}`;
}

function srtTime(ms) {
  return fmtTime(ms) + ',' + String(Math.floor(ms % 1000)).padStart(3, '0');
}

function tsName(t) {
  const d = new Date(t);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
}
