/**
 * Side panel controller.
 * Records tab audio (tabCapture, getDisplayMedia fallback) mixed with the
 * microphone through a gain node (MediaRecorder, audio/webm;codecs=opus).
 * The mic is always captured and always connected; mute is just gain 0, so
 * the mixed track never breaks and mute/unmute mid-recording is seamless.
 * The panel mic button and Discord's own mute/deafen switches (mirrored by
 * content.js as `dvt-mic`) both drive the same gain.
 * Chunks stream to the native host `com.dvt.recorder` continuously as the
 * recorder emits them; cumulative speaker-timeline snapshots (`events`) are
 * sent at silence boundaries (cut: true — safe places to split for live
 * transcription) and at least every 10 s. Falls back to plain Downloads when
 * the host is unavailable.
 */
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
let micGain = null;
let chunks = [];
let streaming = false;
let sessionBase = null;
let startPending = false;
let savePending = false;
let lastVoiceTs = 0;
let lastEventsSent = 0;
let flushChain = Promise.resolve();
let events = [];
let t0 = 0;
let timerInterval = null;
let lastFiles = null;
const speakingNow = new Map();
let participants = [];
let micOn = true;
let micSynced = false;

const MIC_GAIN = 1.8;

function statusline(el, problemText) {
  if (problemText) {
    el.textContent = problemText;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

chrome.runtime.onMessage.addListener((msg) => {
  if (!msg || typeof msg !== 'object') return;
  if (msg.type === 'dvt-content-ready') {
    statusline(els.sensor, null);
    return;
  }
  if (msg.type === 'dvt-mic') {
    setMicOn(!msg.muted, 'Discord');
    return;
  }
  if (msg.type !== 'dvt-speaking') return;

  statusline(els.sensor, null);
  lastVoiceTs = Date.now();
  const key = msg.userId || msg.name;
  if (msg.speaking) speakingNow.set(key, msg.name);
  else speakingNow.delete(key);
  renderParticipants();

  if (recorder && recorder.state === 'recording') {
    voiceSinceEvents = true;
    const t_ms = Math.max(0, msg.t - t0);
    events.push({ name: msg.name, userId: msg.userId || null, speaking: msg.speaking, t_ms });
    logLine(`${fmtTime(t_ms)} ${msg.speaking ? '▶' : '⏹'} ${msg.name}`, msg.speaking ? 'on' : 'off');
  }
});

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

const SETTINGS_KEYS = ['setUiLang', 'setAiProvider', 'setAiInstance', 'setLang', 'setMicDev', 'setFixConvo', 'setReport', 'setDecisions', 'setActions', 'setThreads', 'setAutoTitle', 'setAuto', 'setLive', 'setOutDir'];
const MODEL = 'large-v3-turbo';

/** Whisper multilingual model language set (code, English name). */
const WHISPER_LANGS = [
  ['af', 'Afrikaans'], ['sq', 'Albanian'], ['am', 'Amharic'], ['ar', 'Arabic'],
  ['hy', 'Armenian'], ['as', 'Assamese'], ['az', 'Azerbaijani'], ['ba', 'Bashkir'],
  ['eu', 'Basque'], ['be', 'Belarusian'], ['bn', 'Bengali'], ['bs', 'Bosnian'],
  ['br', 'Breton'], ['bg', 'Bulgarian'], ['my', 'Burmese'], ['yue', 'Cantonese'],
  ['ca', 'Catalan'], ['zh', 'Chinese'], ['hr', 'Croatian'], ['cs', 'Czech'],
  ['da', 'Danish'], ['nl', 'Dutch'], ['en', 'English'], ['et', 'Estonian'],
  ['fo', 'Faroese'], ['fi', 'Finnish'], ['fr', 'French'], ['gl', 'Galician'],
  ['ka', 'Georgian'], ['de', 'German'], ['el', 'Greek'], ['gu', 'Gujarati'],
  ['ht', 'Haitian Creole'], ['ha', 'Hausa'], ['haw', 'Hawaiian'], ['he', 'Hebrew'],
  ['hi', 'Hindi'], ['hu', 'Hungarian'], ['is', 'Icelandic'], ['id', 'Indonesian'],
  ['it', 'Italian'], ['ja', 'Japanese'], ['jw', 'Javanese'], ['kn', 'Kannada'],
  ['kk', 'Kazakh'], ['km', 'Khmer'], ['ko', 'Korean'], ['lo', 'Lao'],
  ['la', 'Latin'], ['lv', 'Latvian'], ['ln', 'Lingala'], ['lt', 'Lithuanian'],
  ['lb', 'Luxembourgish'], ['mk', 'Macedonian'], ['mg', 'Malagasy'], ['ms', 'Malay'],
  ['ml', 'Malayalam'], ['mt', 'Maltese'], ['mi', 'Maori'], ['mr', 'Marathi'],
  ['mn', 'Mongolian'], ['ne', 'Nepali'], ['no', 'Norwegian'], ['nn', 'Nynorsk'],
  ['oc', 'Occitan'], ['ps', 'Pashto'], ['fa', 'Persian'], ['pl', 'Polish'],
  ['pt', 'Portuguese'], ['pa', 'Punjabi'], ['ro', 'Romanian'], ['ru', 'Russian'],
  ['sa', 'Sanskrit'], ['sr', 'Serbian'], ['sn', 'Shona'], ['sd', 'Sindhi'],
  ['si', 'Sinhala'], ['sk', 'Slovak'], ['sl', 'Slovenian'], ['so', 'Somali'],
  ['es', 'Spanish'], ['su', 'Sundanese'], ['sw', 'Swahili'], ['sv', 'Swedish'],
  ['tl', 'Tagalog'], ['tg', 'Tajik'], ['ta', 'Tamil'], ['tt', 'Tatar'],
  ['te', 'Telugu'], ['th', 'Thai'], ['bo', 'Tibetan'], ['tr', 'Turkish'],
  ['tk', 'Turkmen'], ['uk', 'Ukrainian'], ['ur', 'Urdu'], ['uz', 'Uzbek'],
  ['vi', 'Vietnamese'], ['cy', 'Welsh'], ['yi', 'Yiddish'], ['yo', 'Yoruba'],
];

async function storedSetting(key) {
  const s = (await chrome.storage.local.get('dvtSettings')).dvtSettings || {};
  return s[key];
}

async function populateUiLangs() {
  const sel = document.getElementById('setUiLang');
  sel.innerHTML = '';
  for (const code of Object.keys(DVT_STRINGS)) {
    const o = document.createElement('option');
    o.value = code;
    o.textContent = DVT_STRINGS[code]._name || code;
    sel.appendChild(o);
  }
  sel.value = (await storedSetting('setUiLang')) || I18N.lang;
}

async function populateWhisperLangs() {
  const sel = document.getElementById('setLang');
  sel.innerHTML = '';
  const auto = document.createElement('option');
  auto.value = 'auto';
  auto.textContent = t('langAuto');
  sel.appendChild(auto);
  for (const [code, name] of WHISPER_LANGS) {
    const o = document.createElement('option');
    o.value = code;
    o.textContent = `${name} (${code})`;
    sel.appendChild(o);
  }
  sel.value = (await storedSetting('setLang')) || 'auto';
}

// AI providers come from the host factory (tools/ai_providers.py): only
// providers whose CLI is installed are listed. No provider on the machine
// is a normal setup, not an error — the whole AI section stays hidden and
// no AI step is ever requested.
let aiProviders = [];
const aiAvailable = () => aiProviders.length > 0;

function currentAiProvider() {
  const name = document.getElementById('setAiProvider').value;
  return aiProviders.find((p) => p.name === name) || aiProviders[0] || null;
}

function applyAiHelp() {
  const p = currentAiProvider();
  if (!p) return;
  const h = document.getElementById('aiHelp');
  h.dataset.i18n = 'aiHelp_' + p.name; // keeps the text live on language switch
  h.textContent = t('aiHelp_' + p.name);
}

async function populateAiProviders(providers) {
  aiProviders = providers || [];
  document.getElementById('aiSteps').hidden = !aiAvailable();
  if (!aiAvailable()) return;
  const sel = document.getElementById('setAiProvider');
  sel.innerHTML = '';
  for (const p of aiProviders) {
    const o = document.createElement('option');
    o.value = p.name;
    o.textContent = p.label || p.name;
    sel.appendChild(o);
  }
  const want = await storedSetting('setAiProvider');
  if (want && aiProviders.some((p) => p.name === want)) sel.value = want;
  // A single provider needs no picker row; everything still flows through it.
  document.getElementById('aiProviderRow').hidden = aiProviders.length < 2;
  await populateAiInstances();
}

async function populateAiInstances() {
  const p = currentAiProvider();
  const sel = document.getElementById('setAiInstance');
  sel.innerHTML = '';
  if (!p) return;
  for (const inst of p.instances) {
    const o = document.createElement('option');
    o.value = inst.name;
    o.textContent = inst.label || inst.name;
    sel.appendChild(o);
  }
  const want = (await storedSetting('setAiInstance')) ||
    (await storedSetting('setClaude')); // pre-factory settings key
  if (want && [...sel.options].some((o) => o.value === want)) sel.value = want;
  applyAiHelp();
}

document.getElementById('aiHelpBtn').addEventListener('click', () => {
  const h = document.getElementById('aiHelp');
  h.hidden = !h.hidden;
});

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

/* recordings.status pipeline states → i18n keys for the recording-list pill. */
const STATUS_LABELS = {
  recording: 'stRecording',
  transcribing: 'stTranscribing',
  ai_postprocess_autofix: 'stAiAutofix',
  ai_postprocess_threads: 'stAiThreads',
  ai_postprocess_report: 'stAiReport',
  ai_postprocess_title: 'stAiTitle',
  error: 'stError',
};

const AI_STEP_IDS = ['setFixConvo', 'setReport', 'setDecisions', 'setActions', 'setThreads', 'setAutoTitle'];
function syncPipelineLock() {
  const on = document.getElementById('setAuto').checked;
  document.getElementById('aiSteps').classList.toggle('off', !on);
  // Master off forces every AI step off — they cannot be on without
  // auto-transcription; disabled also keeps them out of the Tab order.
  for (const id of [...AI_STEP_IDS, 'setAiAll']) {
    const el = document.getElementById(id);
    el.disabled = !on;
    if (!on) el.checked = false;
  }
}
// The legend switch is derived state (all steps on), never stored itself.
function syncAiAll() {
  document.getElementById('setAiAll').checked =
    AI_STEP_IDS.every((id) => document.getElementById(id).checked);
}
document.getElementById('setAiAll').addEventListener('change', (e) => {
  for (const id of AI_STEP_IDS) document.getElementById(id).checked = e.target.checked;
  saveSettings();
});

for (const id of SETTINGS_KEYS)
  document.getElementById(id).addEventListener('change', () => {
    if (id === 'setAuto') syncPipelineLock(); // before save: it may force steps off
    saveSettings();
    if (AI_STEP_IDS.includes(id)) syncAiAll();
    if (id === 'setAiProvider') populateAiInstances();
    if (id === 'setUiLang') {
      I18N.setLang(document.getElementById(id).value);
      renderMicBtn();
      populateWhisperLangs();
    }
  });
loadSettings().then(() => I18N.init().then(() => {
  renderMicBtn();
  populateUiLangs();
  populateWhisperLangs();
  syncPipelineLock();
  syncAiAll();
}));

document.getElementById('dirBtn').innerHTML = DVT_ICONS.folder;
document.querySelector('.search-icon').innerHTML = DVT_ICONS.search;
document.getElementById('dirBtn').addEventListener('click', () => {
  nativePort && nativePort.postMessage({ type: 'pick-dir' });
});

function renderMicBtn() {
  els.micBtn.innerHTML = DVT_ICONS.mic;
  els.micBtn.classList.toggle('off', !micOn);
  els.micBtn.title = micOn ? t('micTipOn') : t('micTipOff');
}

/** Mute is gain, not track state: the mic node stays connected, so the mixed
 * track and the timeline never break, and unmuting mid-recording just works. */
function applyMicGain() {
  if (!micGain || !audioCtx) return;
  // short exponential ramp — no click on toggle
  micGain.gain.setTargetAtTime(micOn ? MIC_GAIN : 0, audioCtx.currentTime, 0.015);
}

function setMicOn(on, source) {
  if (on === micOn) return;
  micOn = on;
  chrome.storage.local.set({ dvtMicOn: micOn });
  renderMicBtn();
  applyMicGain();
  if (recorder && recorder.state === 'recording') {
    const suffix = source ? ` (${source})` : '';
    logLine(`${fmtTime(Date.now() - t0)} ${t(micOn ? 'micUnmutedLog' : 'micMutedLog')}${suffix}`,
            micOn ? 'on' : 'off');
  }
}

els.micBtn.addEventListener('click', () => setMicOn(!micOn));

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
setTimeout(populateMics, 300);

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
    if (rms > 0.004 || !micOn) {
      silentSince = Date.now();
    } else if (!silentWarned && Date.now() - silentSince > 6000 &&
               recorder && recorder.state === 'recording') {
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
    aiProvider: document.getElementById('setAiProvider').value || 'claude',
    aiInstance: document.getElementById('setAiInstance').value || '',
    uiLang: document.getElementById('setUiLang').value || I18N.lang,
    model: MODEL,
    language: document.getElementById('setLang').value,
    fixConvo: aiAvailable() && document.getElementById('setFixConvo').checked,
    report: aiAvailable() && document.getElementById('setReport').checked,
    decisions: aiAvailable() && document.getElementById('setDecisions').checked,
    actions: aiAvailable() && document.getElementById('setActions').checked,
    threads: aiAvailable() && document.getElementById('setThreads').checked,
    autoTitle: aiAvailable() && document.getElementById('setAutoTitle').checked,
    autoTranscribe: document.getElementById('setAuto').checked,
    live: document.getElementById('setLive').checked,
    outDir: document.getElementById('setOutDir').value.trim() || null,
  };
}

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
    if (streaming || savePending) {
      streaming = false;
      savePending = false;
      setStatus(t('hostError'), true);
    }
  });
  nativePort.postMessage({ type: 'ping' });
  nativePort.postMessage({ type: 'ai-providers' });
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
    statusline(els.native, null);
  } else if (msg.type === 'saved') {
    savePending = false;
    logLine(t('savedPrefix') + msg.path);
    setStatus(t('savedFolder'));
    requestList();
  } else if (msg.type === 'error') {
    if (msg.message) logLine(t('edError') + msg.message);
    setStatus(t('hostError'), true);
  } else if (msg.type === 'log') {
    logLine(msg.line);
  } else if (msg.type === 'live') {
    addLiveLines(msg.lines);
  } else if (msg.type === 'done') {
    setStatus(msg.code === 0 ? t('transDone') + msg.base : t('transFail'));
    requestList();
  } else if (msg.type === 'ai-providers') {
    populateAiProviders(msg.providers);
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

let renameInProgress = false;

function renderRecList(msg) {
  const el = document.getElementById('recList');
  if (renameInProgress) return; // don't destroy an in-progress rename input
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
    // An in-flight status (the recordings.status machine) wins over the
    // done-state pills; legacy rows have no status and fall through.
    const stKey = STATUS_LABELS[it.status];
    if (it.status !== 'done' && stKey) {
      pill.className = 'pill wait';
      pill.textContent = t(stKey);
    } else {
      pill.className = 'pill ' + (it.report ? 'ok' : it.transcript ? 'mid' : 'wait');
      pill.textContent = it.report ? 'report' : it.transcript ? 'transcript' : 'audio';
    }

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
      if (renameInProgress) return;
      renameInProgress = true;
      const input = document.createElement('input');
      input.type = 'text';
      input.value = it.title || '';
      input.placeholder = t('titlePh');
      input.addEventListener('click', (e) => e.stopPropagation());
      const commit = () => {
        renameInProgress = false;
        nativePort && nativePort.postMessage({ type: 'set-title', base: it.base, title: input.value });
      };
      const cancel = () => {
        renameInProgress = false;
        input.removeEventListener('blur', commit);
        title.textContent = it.title || it.base;
        requestList();
      };
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') input.blur();
        if (e.key === 'Escape') cancel();
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
    let prev = -1;
    for (const p of pagerItems(msg.page, msg.pages)) {
      if (prev >= 0 && p - prev > 1) {
        const gap = document.createElement('span');
        gap.className = 'gap';
        gap.textContent = '…';
        pager.appendChild(gap);
      }
      const b = document.createElement('button');
      b.textContent = String(p + 1);
      if (p === msg.page) b.classList.add('current');
      b.addEventListener('click', () => { recPage = p; requestList(); });
      pager.appendChild(b);
      prev = p;
    }
    el.appendChild(pager);
  }
}

// Page indices (0-based) to show. Up to 6 pages: all of them.
// More: first, last, and a 3-wide window around the current page,
// e.g. 1 2 3 … 13 / 1 … 6 7 8 … 13 / 1 … 11 12 13.
function pagerItems(cur, pages) {
  if (pages <= 6) return Array.from({ length: pages }, (_, i) => i);
  const lo = Math.max(0, Math.min(cur - 1, pages - 3));
  const hi = Math.min(pages - 1, lo + 2);
  const set = new Set([0, pages - 1]);
  for (let p = lo; p <= hi; p++) set.add(p);
  return [...set].sort((a, b) => a - b);
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

function speakersPayload(durMs) {
  const intervals = buildIntervals(events, durMs);
  return {
    speakers: JSON.stringify(
      { started_at: new Date(t0).toISOString(), duration_ms: durMs, intervals }, null, 2),
    srt: toSrt(intervals),
  };
}

// Streaming: every recorder chunk goes to the host immediately. flushChain
// serializes the async blob→base64 conversions so chunk order is preserved.
let voiceSinceEvents = false;

function postChunk(blob) {
  flushChain = flushChain.then(async () => {
    const CHUNK = 256 * 1024;
    const buf = new Uint8Array(await blob.arrayBuffer());
    for (let i = 0; i < buf.length; i += CHUNK)
      nativePort && nativePort.postMessage({ type: 'chunk', data: b64(buf.subarray(i, i + CHUNK)) });
  });
  return flushChain;
}

/** Cumulative speaker snapshot. cut: true marks a silence boundary — a safe
 * place for the host to split the audio for live transcription. */
function postEvents(cut) {
  lastEventsSent = Date.now();
  voiceSinceEvents = false;
  const payload = speakersPayload(Date.now() - t0);
  flushChain = flushChain.then(() => {
    nativePort && nativePort.postMessage({ type: 'events', cut: !!cut, ...payload });
  });
  return flushChain;
}

function onStreamChunk(blob) {
  postChunk(blob);
  const now = Date.now();
  const silence = speakingNow.size === 0 && now - lastVoiceTs >= 1000;
  if (silence && voiceSinceEvents && now - lastEventsSent >= 2000) postEvents(true);
  else if (now - lastEventsSent >= 10000) postEvents(false);
}

// ---- live transcript (host-side incremental whisper during the call) ----
const liveToggle = document.getElementById('liveToggle');
const liveLog = document.getElementById('liveLog');
liveToggle.addEventListener('click', () => {
  liveLog.hidden = !liveLog.hidden;
  liveToggle.dataset.i18n = liveLog.hidden ? 'show' : 'hide';
  liveToggle.textContent = t(liveToggle.dataset.i18n);
});

function clearLive() {
  liveLog.innerHTML = '';
}

function addLiveLines(lines) {
  if (!Array.isArray(lines) || lines.length === 0) return;
  if (liveLog.hidden) {
    liveLog.hidden = false;
    liveToggle.dataset.i18n = 'hide';
    liveToggle.textContent = t('hide');
  }
  for (const l of lines) {
    const div = document.createElement('div');
    div.textContent = `[${fmtTime(l.start_ms)}] ${l.speaker}: ${l.text}`;
    liveLog.appendChild(div);
  }
  while (liveLog.childElementCount > 500) liveLog.firstChild.remove();
  liveLog.scrollTop = liveLog.scrollHeight;
}

const logToggle = document.getElementById('logToggle');
logToggle.addEventListener('click', () => {
  els.log.hidden = !els.log.hidden;
  logToggle.dataset.i18n = els.log.hidden ? 'show' : 'hide';
  logToggle.textContent = t(logToggle.dataset.i18n);
});

connectNative();

setInterval(requestList, 10000);

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
      // One-time initial sync: Discord's mute state is the source of truth
      // when the panel opens; afterwards only real changes (dvt-mic) apply,
      // so a manual panel toggle is not overridden every 3 s.
      if (!micSynced && typeof resp.micMuted === 'boolean') {
        micSynced = true;
        setMicOn(!resp.micMuted, 'Discord');
      }
    }
  } catch (e) {
    statusline(els.sensor, t('sensorNotLoaded'));
    participants = [];
    renderParticipants();
  }
}
pingSensor();
setInterval(pingSensor, 3000);

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
  if (startPending || (recorder && recorder.state !== 'inactive')) return;
  startPending = true;
  els.recBtn.disabled = true;
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

    // The mic is captured even when currently muted: mute is only gain 0 on
    // an always-connected node, so it can be turned on mid-recording.
    micStream = null;
    micGain = null;
    try {
      micStream = await navigator.mediaDevices.getUserMedia({ audio: micConstraints() });
      startMeter(micStream);
      populateMics();
    } catch (e) {
      logLine(t('micWarn1') + ' (' + e.name + ') — ' + t('micWarn2'));
      if (e.name === 'NotAllowedError') {
        logLine(t('micPermOpening'));
        chrome.tabs.create({ url: chrome.runtime.getURL('mic.html') });
      }
    }

    audioCtx = new AudioContext();
    const mixDest = audioCtx.createMediaStreamDestination();
    const tabSrc = audioCtx.createMediaStreamSource(new MediaStream(stream.getAudioTracks()));
    tabSrc.connect(mixDest);
    if (micStream) {
      micGain = audioCtx.createGain();
      micGain.gain.value = micOn ? MIC_GAIN : 0;
      audioCtx.createMediaStreamSource(micStream).connect(micGain);
      micGain.connect(mixDest);
    }

    if (cap.viaTabCapture) tabSrc.connect(audioCtx.destination);

    chunks = [];
    events = [];
    savePending = false;
    lastVoiceTs = 0;
    lastEventsSent = 0;
    voiceSinceEvents = false;
    flushChain = Promise.resolve();
    t0 = Date.now();
    sessionBase = 'discord-call-' + tsName(t0);
    streaming = !!nativePort;
    clearLive();
    if (streaming)
      nativePort.postMessage({ type: 'begin', base: sessionBase, settings: collectSettings() });
    recorder = new MediaRecorder(mixDest.stream, { mimeType: 'audio/webm;codecs=opus' });
    recorder.ondataavailable = (e) => {
      if (!e.data.size) return;
      if (streaming && nativePort) onStreamChunk(e.data);
      else chunks.push(e.data);
    };
    recorder.onstop = onRecorderStop;
    recorder.start(1000);

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
    streaming = false;
    cleanup();
  } finally {
    startPending = false;
    els.recBtn.disabled = false;
  }
}

function stopRecording() {
  if (recorder && recorder.state === 'recording') recorder.stop();
}

function onRecorderStop() {
  const durMs = Date.now() - t0;
  for (const [key, name] of speakingNow) {
    events.push({ name, userId: key === name ? null : key, speaking: false, t_ms: durMs });
  }

  const base = sessionBase || 'discord-call-' + tsName(t0);
  const intervals = buildIntervals(events, durMs);
  const payload = speakersPayload(durMs);

  if (streaming && nativePort) {
    streaming = false;
    savePending = true;
    logLine(`${fmtTime(durMs)} ${t('sentToHost')} (${intervals.length})…`);
    flushChain
      .then(() => {
        nativePort && nativePort.postMessage({ type: 'finish', base, ...payload });
      })
      .catch((e) => {
        savePending = false;
        logLine(t('hostErrFallback') + e.message);
        setStatus(t('hostError'), true);
      });
    cleanup();
    return;
  }

  const audio = new Blob(chunks, { type: 'audio/webm' });
  const json = new Blob([payload.speakers], { type: 'application/json' });
  const srt = new Blob([payload.srt], { type: 'text/plain' });

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
  micGain = null;
  recorder = null;
  document.body.classList.remove('rec');
  els.recBtn.dataset.i18n = 'start';
  els.recBtn.textContent = t('start');
  els.recBtn.classList.remove('recording');
  els.timer.textContent = '';
}

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
      if (e.t_ms - s.t_ms >= 150)
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
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}
