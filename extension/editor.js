/**
 * Transcript editor page (?base=<recording>).
 * Loads lines and audio from the native host (SQLite-backed), plays the audio
 * fragment behind a clicked row, persists manual edits/deletes via the host.
 */
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

const THREAD_PALETTE = ['#e0b64c', '#5ec6d8', '#b07ce8', '#6fbf73', '#e8798a', '#8899e8', '#d89b5e', '#7ad0a8'];
function threadColor(threadId) {
  const i = recThreads.findIndex((th) => th.id === threadId);
  return i < 0 ? null : THREAD_PALETTE[i % THREAD_PALETTE.length];
}

/* Left sidebar for jumping between topics. Topics interleave, so one thread
 * may cover several separate segments of the call: threadSegs holds the row
 * id starting each segment, and repeated clicks on a topic cycle through
 * them. */
const threadSegs = new Map(); // thread_id -> [line id of each segment start]
const threadSegIdx = new Map(); // thread_id -> last visited segment index

function buildThreadNav() {
  const nav = document.getElementById('threadNav');
  const list = document.getElementById('threadNavList');
  list.innerHTML = '';
  threadSegs.clear();
  threadSegIdx.clear();
  if (!recThreads.length) {
    nav.hidden = true;
    return;
  }
  let prev = null;
  for (const u of utts) {
    if (u.thread_id != null && u.thread_id !== prev) {
      if (!threadSegs.has(u.thread_id)) threadSegs.set(u.thread_id, []);
      threadSegs.get(u.thread_id).push(u.id);
    }
    prev = u.thread_id;
  }
  for (const th of recThreads) {
    const segs = threadSegs.get(th.id) || [];
    const li = document.createElement('li');
    li.title = th.name;
    const dot = document.createElement('span');
    dot.className = 'nav-dot';
    dot.style.background = threadColor(th.id);
    const name = document.createElement('span');
    name.className = 'nav-name';
    name.textContent = th.name;
    li.append(dot, name);
    if (segs.length > 1) {
      const count = document.createElement('span');
      count.className = 'nav-count';
      count.textContent = segs.length;
      li.appendChild(count);
    }
    li.addEventListener('click', () => jumpToThread(th.id, li));
    list.appendChild(li);
  }
  nav.hidden = false;
}

function jumpToThread(threadId, li) {
  const segs = threadSegs.get(threadId) || [];
  if (!segs.length) return;
  const i = ((threadSegIdx.get(threadId) ?? -1) + 1) % segs.length;
  threadSegIdx.set(threadId, i);
  const row = document.querySelector(`.row[data-id="${segs[i]}"]`);
  if (!row) return;
  row.scrollIntoView({ behavior: 'smooth', block: 'start' });
  row.classList.add('jump-flash');
  setTimeout(() => row.classList.remove('jump-flash'), 1600);
  document.querySelectorAll('#threadNavList li').forEach((x) => x.classList.remove('active'));
  li.classList.add('active');
}

let utts = [];
let recThreads = [];
let recStartDt = null;
let audioChunks = [];
let stopAt = null;
let knownDurMs = 0;
let liveActive = false;
let livePollTimer = null;

/** While a live session is running (host reports live: true), re-fetch the
 * lines every few seconds so the transcript follows the ongoing call. */
function scheduleLivePoll() {
  clearTimeout(livePollTimer);
  if (!liveActive) return;
  livePollTimer = setTimeout(() => {
    // don't wipe an in-progress manual edit with a re-render
    if (document.querySelector('.row.editing')) return scheduleLivePoll();
    port.postMessage({ type: 'load', base, linesOnly: true });
  }, 4000);
}

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
    // Chunked load: metadata now, lines arrive in 'recording-lines' batches,
    // 'recording-end' closes (the host stays under Chrome's 1 MB message cap).
    if (msg.title) {
      els.title.textContent = msg.title;
      els.title.title = msg.base;
      document.title = msg.title;
    }
    recThreads = msg.threads || [];
    const hasReport = msg.report
      && (msg.report.summary || msg.report.decisions.length || msg.report.action_items.length);
    if (hasReport || recThreads.length) {
      renderReport(hasReport ? msg.report : null, recThreads);
      document.getElementById('reportBlock').hidden = false;
    }
    recStartDt = msg.start_dt;
    liveActive = !!msg.live;
    utts = [];
  } else if (msg.type === 'recording-lines') {
    utts = utts.concat(msg.lines);
  } else if (msg.type === 'recording-end') {
    knownDurMs = utts.reduce((m, u) => Math.max(m, u.end_dt_ms), 0);
    els.clock.textContent = `${fmt(0)} / ${knownDurMs ? fmt(knownDurMs) : '--:--:--'}`;
    const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 150;
    render();
    els.status.textContent = `${utts.length} ${t('edLines')}`
      + (recStartDt ? ` · ${recStartDt}` : '')
      + (liveActive ? ` · ${t('edLive')}` : '');
    if (liveActive && nearBottom) window.scrollTo(0, document.body.scrollHeight);
    scheduleLivePoll();
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
  } else if (msg.type === 'error') {
    els.status.textContent = t('edError') + msg.message;
  }
});
port.postMessage({ type: 'load', base });

/* One card per thread plus one for the whole record, side by side. Each card
 * has Summary / Decisions / Action items tabs; empty sections are disabled
 * and the first non-empty one opens by default. */
function renderReport(rep, threads = []) {
  const body = document.getElementById('reportBody');
  body.innerHTML = '';
  for (const th of threads) {
    body.appendChild(reportCard(th.name, th, threadColor(th.id)));
  }
  if (rep) body.appendChild(reportCard(t('edRecord'), rep, null));
}

function reportCard(name, rep, color) {
  const card = document.createElement('div');
  card.className = 'rep-card';
  const h = document.createElement('h3');
  h.className = 'rep-card-name';
  if (color) {
    const dot = document.createElement('span');
    dot.className = 'nav-dot';
    dot.style.background = color;
    h.appendChild(dot);
  }
  const nameEl = document.createElement('span');
  nameEl.className = 'rep-card-title';
  nameEl.textContent = name;
  h.appendChild(nameEl);
  h.title = name;

  const tabs = document.createElement('div');
  tabs.className = 'rep-tabs';
  const content = document.createElement('div');
  content.className = 'rep-content';
  const show = (val) => {
    content.innerHTML = '';
    if (Array.isArray(val)) {
      const ul = document.createElement('ul');
      for (const item of val) {
        const li = document.createElement('li');
        li.textContent = item;
        ul.appendChild(li);
      }
      content.appendChild(ul);
    } else {
      const p = document.createElement('p');
      p.textContent = val;
      content.appendChild(p);
    }
  };

  const sections = [
    ['edSummary', rep.summary],
    ['edDecisions', rep.decisions],
    ['edActions', rep.action_items],
  ];
  const btns = [];
  for (const [key, val] of sections) {
    const b = document.createElement('button');
    b.textContent = t(key);
    b.disabled = Array.isArray(val) ? !val.length : !val;
    b.addEventListener('click', () => {
      btns.forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      show(val);
    });
    btns.push(b);
    tabs.appendChild(b);
  }
  const first = btns.findIndex((b) => !b.disabled);
  if (first >= 0) {
    btns[first].classList.add('active');
    show(sections[first][1]);
  }

  card.append(h, tabs, content);
  return card;
}

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
    if (u.start_dt_ms < prevEnd) row.classList.add('interrupt');
    prevEnd = Math.max(prevEnd, u.end_dt_ms);

    const tc = u.thread_id != null ? threadColor(u.thread_id) : null;
    if (tc) {
      row.classList.add('threaded');
      row.style.borderLeftColor = tc;
      row.title = (recThreads.find((th) => th.id === u.thread_id) || {}).name || '';
    }

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
    editBtn.innerHTML = DVT_ICONS.pencil;
    editBtn.title = t('edEdit');
    editBtn.addEventListener('click', (e) => { e.stopPropagation(); startEdit(row, u); });
    const delBtn = document.createElement('button');
    delBtn.innerHTML = DVT_ICONS.x;
    delBtn.title = t('edDelete');
    delBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!delBtn.classList.contains('danger')) {
        delBtn.classList.add('danger');
        delBtn.textContent = t('edConfirm');
        setTimeout(() => { delBtn.classList.remove('danger'); delBtn.innerHTML = DVT_ICONS.x; }, 2500);
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
  buildThreadNav();
}

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

function playFragment(u) {
  if (!els.audio.src) return;
  stopAt = u.end_dt_ms;
  els.audio.currentTime = u.start_dt_ms / 1000;
  els.audio.play();
}

els.playBtn.addEventListener('click', () => {
  if (!els.audio.src) return;
  stopAt = null;
  if (els.audio.paused) els.audio.play();
  else els.audio.pause();
});

els.audio.addEventListener('play', () => (els.playBtn.innerHTML = DVT_ICONS.pause));
els.playBtn.innerHTML = DVT_ICONS.play;
els.audio.addEventListener('pause', () => (els.playBtn.innerHTML = DVT_ICONS.play));

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
