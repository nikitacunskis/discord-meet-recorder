// DVT redaktors: transkripta rindas no SQLite (caur native hostu), klikšķis
// uz rindas atskaņo fragmentu, ✎ ļauj labot runātāju/laikus/tekstu ar roku.

const base = new URLSearchParams(location.search).get('base');
const els = {
  title: document.getElementById('title'),
  status: document.getElementById('status'),
  list: document.getElementById('list'),
  audio: document.getElementById('audio'),
  playBtn: document.getElementById('playBtn'),
  seek: document.getElementById('seek'),
  clock: document.getElementById('clock'),
};

const PALETTE = ['#23a55a', '#e06c6c', '#5865f2', '#c98f2e', '#9b59b6', '#16a2b8', '#d4589e', '#7a8b2f'];
const speakerColors = new Map();
function colorFor(name) {
  const key = name.replace(/ \(\?\)$/, '').split(' → ')[0];
  if (!speakerColors.has(key)) speakerColors.set(key, PALETTE[speakerColors.size % PALETTE.length]);
  return speakerColors.get(key);
}

let utts = [];
let audioChunks = [];
let stopAt = null; // ms; fragmenta beigas, kur apturēt
let knownDurMs = 0; // rezerves ilgums, ja webm metadatos tā nav

// audio.duration MediaRecorder failiem mēdz būt Infinity — tad izmantojam
// rindu beigu laiku kā ilgumu
function durMs() {
  const d = els.audio.duration;
  return isFinite(d) && d > 0 ? d * 1000 : knownDurMs;
}

document.title = base || 'DVT redaktors';
I18N.init().then(() => { els.title.textContent = base || t('edNoBase'); });

const port = chrome.runtime.connectNative('com.dvt.recorder');
port.onDisconnect.addListener(() => {
  els.status.textContent = t('edHostMissing');
});
port.onMessage.addListener((msg) => {
  if (msg.type === 'recording') {
    utts = msg.lines;
    knownDurMs = utts.reduce((m, u) => Math.max(m, u.end_dt_ms), 0);
    render();
    els.status.textContent = `${utts.length} ${t('edLines')}` + (msg.start_dt ? ` · ${msg.start_dt}` : '');
  } else if (msg.type === 'audio-begin') {
    audioChunks = [];
    els.status.textContent += t('edAudioLoading');
  } else if (msg.type === 'audio-chunk') {
    audioChunks.push(Uint8Array.from(atob(msg.data), (c) => c.charCodeAt(0)));
  } else if (msg.type === 'audio-end') {
    els.audio.src = URL.createObjectURL(new Blob(audioChunks, { type: 'audio/webm' }));
    audioChunks = [];
    els.status.textContent = els.status.textContent.replace(t('edAudioLoading'), t('edAudioReady'));
  } else if (msg.type === 'audio-missing') {
    els.status.textContent += t('edAudioMissing');
  } else if (msg.type === 'updated' || msg.type === 'deleted') {
    // apstiprināts — nekas nav jādara, lokālais stāvoklis jau atjaunots
  } else if (msg.type === 'error') {
    els.status.textContent = t('edError') + msg.message;
  }
});
port.postMessage({ type: 'load', base });

// ---------- renderēšana ----------

function fmt(ms) {
  const s = Math.floor(ms / 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(Math.floor(s / 3600))}:${p(Math.floor((s % 3600) / 60))}:${p(s % 60)}`;
}

function render() {
  els.list.innerHTML = '';
  let prevEnd = -1;
  for (const u of utts) {
    const row = document.createElement('div');
    row.className = 'row';
    row.dataset.id = u.id;
    if (u.start_dt_ms < prevEnd) row.classList.add('interrupt'); // pārtraukums -> atkāpe
    prevEnd = Math.max(prevEnd, u.end_dt_ms);

    const speaker = document.createElement('span');
    speaker.className = 'speaker';
    speaker.textContent = u.speaker_name;
    speaker.style.color = colorFor(u.speaker_name);

    const time = document.createElement('span');
    time.className = 'time';
    time.textContent = `[${fmt(u.start_dt_ms)} - ${fmt(u.end_dt_ms)}]`;

    const text = document.createElement('span');
    text.className = 'text';
    text.textContent = u.speaker_line;

    const tools = document.createElement('span');
    tools.className = 'tools';
    const editBtn = document.createElement('button');
    editBtn.textContent = '✎';
    editBtn.title = t('edEdit');
    editBtn.addEventListener('click', (e) => { e.stopPropagation(); startEdit(row, u); });
    const delBtn = document.createElement('button');
    delBtn.textContent = '✕';
    delBtn.title = t('edDelete');
    delBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!delBtn.classList.contains('danger')) {
        delBtn.classList.add('danger');
        delBtn.textContent = t('edConfirm');
        setTimeout(() => { delBtn.classList.remove('danger'); delBtn.textContent = '✕'; }, 2500);
        return;
      }
      port.postMessage({ type: 'delete', id: u.id });
      utts = utts.filter((x) => x.id !== u.id);
      render();
    });
    tools.append(editBtn, delBtn);

    row.append(speaker, time, text, tools);
    row.addEventListener('click', () => playFragment(u));
    els.list.appendChild(row);
  }
}

// ---------- labošana ----------

function parseTime(str) {
  const parts = str.trim().split(':').map(Number);
  if (parts.some(isNaN)) return null;
  let s = 0;
  for (const p of parts) s = s * 60 + p;
  return Math.round(s * 1000);
}

function startEdit(row, u) {
  row.classList.add('editing');
  row.innerHTML = '';

  const speakerIn = Object.assign(document.createElement('input'), { value: u.speaker_name, className: 'speaker-in' });
  const startIn = Object.assign(document.createElement('input'), { value: fmt(u.start_dt_ms), className: 'time-in', title: 'HH:MM:SS' });
  const endIn = Object.assign(document.createElement('input'), { value: fmt(u.end_dt_ms), className: 'time-in', title: 'HH:MM:SS' });
  const textIn = document.createElement('textarea');
  textIn.value = u.speaker_line;

  const actions = document.createElement('div');
  actions.className = 'actions';
  const save = Object.assign(document.createElement('button'), { textContent: t('edSave'), className: 'save' });
  const cancel = Object.assign(document.createElement('button'), { textContent: t('edCancel'), className: 'cancel' });
  save.addEventListener('click', () => {
    const s = parseTime(startIn.value);
    const e = parseTime(endIn.value);
    if (s === null || e === null || e < s) { startIn.style.borderColor = '#c33'; return; }
    Object.assign(u, { speaker_name: speakerIn.value.trim() || u.speaker_name, start_dt_ms: s, end_dt_ms: e, speaker_line: textIn.value.trim() });
    port.postMessage({ type: 'update', id: u.id, speaker_name: u.speaker_name, start_dt_ms: s, end_dt_ms: e, speaker_line: u.speaker_line });
    utts.sort((a, b) => a.start_dt_ms - b.start_dt_ms || a.id - b.id);
    render();
  });
  cancel.addEventListener('click', () => render());
  actions.append(save, cancel);

  row.append(speakerIn, startIn, endIn, textIn, actions);
  textIn.focus();
}

// ---------- atskaņošana ----------

function playFragment(u) {
  if (!els.audio.src) return;
  stopAt = u.end_dt_ms;
  els.audio.currentTime = u.start_dt_ms / 1000;
  els.audio.play();
}

els.playBtn.addEventListener('click', () => {
  if (!els.audio.src) return;
  stopAt = null; // brīvā atskaņošana bez fragmenta robežas
  if (els.audio.paused) els.audio.play();
  else els.audio.pause();
});

els.audio.addEventListener('play', () => (els.playBtn.textContent = '❚❚'));
els.audio.addEventListener('pause', () => (els.playBtn.textContent = '▶'));

// standarta triks Infinity-duration webm failiem: aizsēkojam līdz beigām,
// lai pārlūks izrēķina īsto ilgumu, tad atgriežamies sākumā
els.audio.addEventListener('loadedmetadata', () => {
  if (els.audio.duration !== Infinity) return;
  const back = () => {
    els.audio.removeEventListener('seeked', back);
    els.audio.currentTime = 0;
  };
  els.audio.addEventListener('seeked', back);
  els.audio.currentTime = 1e7;
});

els.audio.addEventListener('timeupdate', () => {
  const ms = els.audio.currentTime * 1000;
  if (stopAt !== null && ms >= stopAt) {
    els.audio.pause();
    stopAt = null;
  }
  const dur = durMs();
  if (dur) els.seek.value = Math.round((ms / dur) * 1000);
  els.clock.textContent = `${fmt(ms)} / ${dur ? fmt(dur) : '--:--:--'}`;
  document.querySelectorAll('.row').forEach((row) => {
    const u = utts.find((x) => String(x.id) === row.dataset.id);
    row.classList.toggle('playing',
      !!u && !els.audio.paused && ms >= u.start_dt_ms && ms < u.end_dt_ms);
  });
});

els.seek.addEventListener('input', () => {
  const dur = durMs();
  if (!dur) return;
  stopAt = null;
  els.audio.currentTime = ((els.seek.value / 1000) * dur) / 1000;
});
