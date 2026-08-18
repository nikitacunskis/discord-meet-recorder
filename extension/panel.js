// Discord Call Recorder — sānu panelis.
// Ieraksta taba audio (+ mikrofonu) un vāc runātāju notikumus no content.js.
// Stop -> native hosts saglabā mapē un pats transkribē; fallback: Downloads.

const els = {
  sensor: document.getElementById('sensor'),
  native: document.getElementById('native'),
  recBtn: document.getElementById('recBtn'),
  micBtn: document.getElementById('micBtn'),
  status: document.getElementById('status'),
  timer: document.getElementById('timer'),
  nowSpeaking: document.getElementById('nowSpeaking'),
  log: document.getElementById('log'),
  search: document.getElementById('recSearch'),
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
let participants = []; // visi dalībnieki no pēdējā pong
let micOn = true;

// Statusa rindas rādām TIKAI tad, ja kaut kas nav kārtībā.
function statusline(el, problemText) {
  if (problemText) {
    el.textContent = problemText;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

// ---------- runātāju notikumi no content.js ----------

chrome.runtime.onMessage.addListener((msg) => {
  if (!msg || typeof msg !== 'object') return;
  if (msg.type === 'dvt-content-ready') {
    statusline(els.sensor, null);
    return;
  }
  if (msg.type !== 'dvt-speaking') return;

  statusline(els.sensor, null);
  const key = msg.userId || msg.name;
  if (msg.speaking) speakingNow.set(key, msg.name);
  else speakingNow.delete(key);
  renderParticipants();

  if (recorder && recorder.state === 'recording') {
    const t_ms = Math.max(0, msg.t - t0);
    events.push({ name: msg.name, userId: msg.userId || null, speaking: msg.speaking, t_ms });
    logLine(`${fmtTime(t_ms)} ${msg.speaking ? '▶' : '⏹'} ${msg.name}`, msg.speaking ? 'on' : 'off');
  }
});

// Dalībnieku režģis: visi pelēki, runājošie zaļi.
function renderParticipants() {
  const speaking = new Set(speakingNow.values());
  const names = participants.length ? participants : [...speaking];
  if (names.length === 0) {
    els.nowSpeaking.textContent = '—';
    els.nowSpeaking.classList.add('muted');
    return;
  }
  els.nowSpeaking.classList.remove('muted');
  els.nowSpeaking.innerHTML = '';
  for (const name of names) {
    const chip = document.createElement('span');
    chip.className = 'chip' + (speaking.has(name) ? ' on' : '');
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

const SETTINGS_KEYS = ['setUiLang', 'setClaude', 'setLang', 'setMicDev', 'setReport', 'setAuto', 'setOutDir'];
const MODEL = 'large-v3-turbo'; // vienīgā opcija

async function loadSettings() {
  const stored = await chrome.storage.local.get(['dvtSettings', 'dvtMicOn']);
  const s = stored.dvtSettings || {};
  for (const id of SETTINGS_KEYS) {
    const el = document.getElementById(id);
    if (!(id in s)) continue;
    if (el.type === 'checkbox') el.checked = s[id];
    else el.value = s[id];
  }
  micOn = stored.dvtMicOn !== false;
  renderMicBtn();
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
    if (id === 'setUiLang') {
      I18N.setLang(document.getElementById(id).value);
      renderMicBtn();
    }
  });
loadSettings().then(() => I18N.init().then(renderMicBtn));

// Mapes selektors caur native hostu (macOS choose folder dialogs)
document.getElementById('dirBtn').innerHTML = DVT_ICONS.folder;
document.querySelector('.search-icon').innerHTML = DVT_ICONS.search;
document.getElementById('dirBtn').addEventListener('click', () => {
  nativePort && nativePort.postMessage({ type: 'pick-dir' });
});

// Mikrofona slēdzis: vai ierakstā iet arī tava balss
function renderMicBtn() {
  els.micBtn.innerHTML = DVT_ICONS.mic;
  els.micBtn.classList.toggle('off', !micOn);
  els.micBtn.title = micOn ? t('micTipOn') : t('micTipOff');
}
els.micBtn.addEventListener('click', () => {
  micOn = !micOn;
  chrome.storage.local.set({ dvtMicOn: micOn });
  renderMicBtn();
});

// ---------- mikrofona ierīce + līmeņa mērītājs ----------
// Chrome "default" ievade bieži NAV tas mikrofons, ko lieto Discord
// (Continuity/iPhone, virtuālās ierīces kā Krisp) — tad ierakstā ir šņākoņa.

async function populateMics() {
  const sel = document.getElementById('setMicDev');
  const stored = (await chrome.storage.local.get('dvtSettings')).dvtSettings || {};
  const want = stored.setMicDev || '';
  let devs = [];
  try { devs = await navigator.mediaDevices.enumerateDevices(); } catch (e) { return; }
  sel.innerHTML = '';
  const def = document.createElement('option');
  def.value = '';
  def.textContent = t('micDefault');
  sel.appendChild(def);
  for (const d of devs.filter((x) => x.kind === 'audioinput' && x.deviceId && x.deviceId !== 'default')) {
    const o = document.createElement('option');
    o.value = d.deviceId;
    o.textContent = d.label || 'mic ' + (sel.options.length);
    sel.appendChild(o);
  }
  if ([...sel.options].some((o) => o.value === want)) sel.value = want;
}
navigator.mediaDevices.addEventListener('devicechange', populateMics);
setTimeout(populateMics, 300); // pēc i18n init

function micConstraints() {
  const dev = document.getElementById('setMicDev').value;
  const c = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  if (dev) c.deviceId = { exact: dev };
  return c;
}

let meterRaf = null;
let meterCtx = null;
let silentSince = 0;
let silentWarned = false;

function startMeter(s) {
  stopMeter();
  document.getElementById('micMeter').hidden = false;
  meterCtx = new AudioContext();
  const an = meterCtx.createAnalyser();
  an.fftSize = 512;
  meterCtx.createMediaStreamSource(s).connect(an);
  const buf = new Float32Array(an.fftSize);
  silentSince = Date.now();
  silentWarned = false;
  const tick = () => {
    an.getFloatTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
    const rms = Math.sqrt(sum / buf.length);
    document.getElementById('micLevel').style.width = Math.min(100, rms * 300) + '%';
    if (rms > 0.004) {
      silentSince = Date.now();
    } else if (!silentWarned && Date.now() - silentSince > 6000 &&
               recorder && recorder.state === 'recording') {
      // 6s pilnīga klusuma ieraksta laikā = gandrīz droši nepareizā ierīce
      silentWarned = true;
      logLine(t('micSilent'));
      setStatus(t('micSilent'), true);
    }
    meterRaf = requestAnimationFrame(tick);
  };
  tick();
}

function stopMeter() {
  if (meterRaf) cancelAnimationFrame(meterRaf);
  meterRaf = null;
  if (meterCtx) meterCtx.close().catch(() => {});
  meterCtx = null;
  const m = document.getElementById('micMeter');
  if (m) m.hidden = true;
}

// Ierīces maiņa -> 4 s dzīvais tests ar mērītāju (ja šobrīd neierakstām)
let micTestStream = null;
document.getElementById('setMicDev').addEventListener('change', async () => {
  if (recorder && recorder.state === 'recording') return;
  try {
    if (micTestStream) micTestStream.getTracks().forEach((tr) => tr.stop());
    micTestStream = await navigator.mediaDevices.getUserMedia({ audio: micConstraints() });
    startMeter(micTestStream);
    setTimeout(() => {
      if (micTestStream) micTestStream.getTracks().forEach((tr) => tr.stop());
      micTestStream = null;
      if (!(recorder && recorder.state === 'recording')) stopMeter();
    }, 4000);
  } catch (e) {
    logLine(t('micWarn1') + ' (' + e.name + ')');
  }
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

const NATIVE_HOST = 'com.dvt.recorder';
let nativePort = null;

function connectNative() {
  try {
    nativePort = chrome.runtime.connectNative(NATIVE_HOST);
  } catch (e) {
    nativePort = null;
    statusline(els.native, t('nativeMissing'));
    return;
  }
  nativePort.onMessage.addListener(onNativeMsg);
  nativePort.onDisconnect.addListener(() => {
    nativePort = null;
    statusline(els.native, t('nativeMissing'));
  });
  nativePort.postMessage({ type: 'ping' });
  requestList();
}

let recPage = 0;
let recQuery = '';
function requestList() {
  if (nativePort)
    nativePort.postMessage({
      type: 'list', dir: collectSettings().outDir,
      page: recPage, pageSize: 5, q: recQuery,
    });
}

let searchTimer = null;
els.search.addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    recQuery = els.search.value.trim();
    recPage = 0;
    requestList();
  }, 300);
});

function onNativeMsg(msg) {
  if (msg.type === 'pong') {
    statusline(els.native, null); // viss kārtībā -> neko nerādām
  } else if (msg.type === 'saved') {
    logLine(t('savedPrefix') + msg.path);
    setStatus(t('savedFolder'));
    requestList();
  } else if (msg.type === 'log') {
    logLine(msg.line);
  } else if (msg.type === 'done') {
    setStatus(msg.code === 0 ? t('transDone') + msg.base : t('transFail'));
    requestList();
  } else if (msg.type === 'list') {
    renderRecList(msg);
  } else if (msg.type === 'title-set' || msg.type === 'recording-deleted') {
    requestList();
  } else if (msg.type === 'dir-picked') {
    if (msg.dir) {
      document.getElementById('setOutDir').value = msg.dir;
      saveSettings();
      requestList();
    }
  }
}

// ---------- ierakstu saraksts (kartītes + numurēta paginācija) ----------

function renderRecList(msg) {
  const el = document.getElementById('recList');
  recPage = msg.page;
  el.classList.remove('muted');
  el.innerHTML = '';
  if (!msg.items || msg.items.length === 0) {
    el.textContent = '—';
    el.classList.add('muted');
    return;
  }

  for (const it of msg.items) {
    const card = document.createElement('div');
    card.className = 'rec-card';

    const pill = document.createElement('span');
    pill.className = 'pill ' + (it.report ? 'ok' : it.transcript ? 'mid' : 'wait');
    pill.textContent = it.report ? 'report' : it.transcript ? 'transcript' : 'audio';

    const title = document.createElement('span');
    title.className = 'rec-title';
    title.textContent = it.title || it.base;
    title.title = t('openEditor');
    title.addEventListener('click', () =>
      chrome.tabs.create({ url: chrome.runtime.getURL('editor.html') + '?base=' + encodeURIComponent(it.base) }));

    const btn = document.createElement('button');
    btn.className = 'rec-edit';
    btn.innerHTML = DVT_ICONS.pencil;
    btn.title = t('renameTitle');
    btn.addEventListener('click', () => {
      const input = document.createElement('input');
      input.type = 'text';
      input.value = it.title || '';
      input.placeholder = t('titlePh');
      const commit = () => {
        nativePort && nativePort.postMessage({ type: 'set-title', base: it.base, title: input.value });
      };
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') input.blur();
        if (e.key === 'Escape') { input.removeEventListener('blur', commit); requestList(); }
      });
      input.addEventListener('blur', commit);
      title.textContent = '';
      title.appendChild(input);
      input.focus();
    });

    const del = document.createElement('button');
    del.className = 'rec-edit rec-del';
    del.innerHTML = DVT_ICONS.trash;
    del.title = t('deleteRec');
    del.addEventListener('click', () => {
      if (!del.classList.contains('danger')) {
        del.classList.add('danger');
        del.textContent = t('edConfirm');
        setTimeout(() => { del.classList.remove('danger'); del.innerHTML = DVT_ICONS.trash; }, 2500);
        return;
      }
      nativePort && nativePort.postMessage({ type: 'delete-recording', base: it.base });
    });

    card.append(pill, title, btn, del);
    el.appendChild(card);
  }

  if (msg.pages > 1) {
    const pager = document.createElement('div');
    pager.className = 'pager';
    for (let p = 0; p < msg.pages; p++) {
      const b = document.createElement('button');
      b.textContent = String(p + 1);
      if (p === msg.page) b.classList.add('current');
      b.addEventListener('click', () => { recPage = p; requestList(); });
      pager.appendChild(b);
    }
    el.appendChild(pager);
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

// Events bloks: paslēpts pēc noklusējuma, Rādīt/Slēpt poga
const logToggle = document.getElementById('logToggle');
logToggle.addEventListener('click', () => {
  els.log.hidden = !els.log.hidden;
  logToggle.dataset.i18n = els.log.hidden ? 'show' : 'hide';
  logToggle.textContent = t(logToggle.dataset.i18n);
});

connectNative();

// Statusu auto-atsvaidze (⏳ -> ✓), arī ja 'done' pienāk citam savienojumam.
setInterval(requestList, 10000);

// ---------- sensora ping ----------
// content.js ielādējas agrāk par paneli — pingojam; kļūdas rādām, OK klusējam.

async function pingSensor() {
  try {
    const tab = await findDiscordTab();
    if (!tab) {
      statusline(els.sensor, t('sensorNoTab'));
      participants = [];
      renderParticipants();
      return;
    }
    const resp = await chrome.tabs.sendMessage(tab.id, { type: 'dvt-ping' });
    if (resp && resp.type === 'dvt-pong') {
      statusline(els.sensor, null);
      participants = resp.participants || [];
      renderParticipants();
    }
  } catch (e) {
    statusline(els.sensor, t('sensorNotLoaded'));
    participants = [];
    renderParticipants();
  }
}
pingSensor();
setInterval(pingSensor, 3000);

// ---------- ieraksts ----------

els.recBtn.addEventListener('click', () => {
  if (recorder && recorder.state === 'recording') stopRecording();
  else startRecording();
});

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
    setStatus(t('shareHint'));
    const display = await navigator.mediaDevices.getDisplayMedia({
      video: true,
      audio: true,
      preferCurrentTab: false,
    });
    if (display.getAudioTracks().length === 0) {
      display.getTracks().forEach((t) => t.stop());
      throw new Error(t('shareNoAudio'));
    }
    return { stream: display, viaTabCapture: false };
  }
}

async function startRecording() {
  try {
    const tab = await findDiscordTab();
    if (!tab) {
      return setStatus(t('noDiscordTab'), true);
    }

    const cap = await captureTabAudio(tab);
    stream = cap.stream;
    stream.getAudioTracks()[0].addEventListener('ended', () => {
      if (recorder && recorder.state === 'recording') stopRecording();
    });

    // Taba audio satur tikai PĀRĒJOS runātājus — tava balss tabā neskan,
    // tāpēc to ņemam no mikrofona un miksējam vienā celiņā.
    micStream = null;
    if (micOn) {
      try {
        micStream = await navigator.mediaDevices.getUserMedia({ audio: micConstraints() });
        startMeter(micStream);
        populateMics(); // pēc piekļuves parādās ierīču nosaukumi
      } catch (e) {
        logLine(t('micWarn1') + ' (' + e.name + ') — ' + t('micWarn2'));
        if (e.name === 'NotAllowedError') {
          // panelī atļaujas prompt nerādās — to paņem atsevišķā paplašinājuma lapā
          logLine(t('micPermOpening'));
          chrome.tabs.create({ url: chrome.runtime.getURL('mic.html') });
        }
      }
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
    els.recBtn.dataset.i18n = 'stop';
    els.recBtn.textContent = t('stop');
    els.recBtn.classList.add('recording');
    setStatus(t('recRunning'));
    timerInterval = setInterval(
      () => (els.timer.textContent = fmtTime(Date.now() - t0)),
      1000
    );
    logLine(`${fmtTime(0)} ${t('recStarted')} (${tab.title || 'Discord'})`);
  } catch (e) {
    console.error(e);
    if (/has not been invoked/i.test(e.message || '')) {
      setStatus(t('permHint'), true);
    } else {
      setStatus(t('startFailPrefix') + e.message, true);
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
    logLine(`${fmtTime(durMs)} ${t('sentToHost')} (${intervals.length})…`);
    sendToNative(lastFiles).catch((e) => {
      logLine(t('hostErrFallback') + e.message);
      downloadAll();
    });
  } else {
    downloadAll();
  }
  cleanup();

  function downloadAll() {
    download(audio, base + '.webm');
    download(json, base + '.speakers.json');
    download(srt, base + '.speakers.srt');
    logLine(`${fmtTime(durMs)} ${t('savedDl')} ${base}.webm (+speakers, ${intervals.length})`);
    setStatus(t('savedPrefix') + base);
  }
}

function cleanup() {
  if (timerInterval) clearInterval(timerInterval);
  timerInterval = null;
  if (stream) stream.getTracks().forEach((t) => t.stop());
  if (micStream) micStream.getTracks().forEach((t) => t.stop());
  if (audioCtx) audioCtx.close().catch(() => {});
  stopMeter();
  stream = null;
  micStream = null;
  audioCtx = null;
  recorder = null;
  document.body.classList.remove('rec');
  els.recBtn.dataset.i18n = 'start';
  els.recBtn.textContent = t('start');
  els.recBtn.classList.remove('recording');
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
  els.status.style.color = isErr ? 'var(--danger)' : '';
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
