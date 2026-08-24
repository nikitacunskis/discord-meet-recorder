/**
 * Discord speaking sensor (content script).
 * Speaking signal: inline `box-shadow: var(--status-speaking)` (tile and sidebar
 * views); fallback: `usernameSpeaking` class. Polls every 250 ms — a body-wide
 * MutationObserver can crash the tab renderer in large calls.
 * Emits `dvt-speaking` runtime messages; answers `dvt-ping` with
 * {speakers, participants, voiceUsers, tiles, micMuted}.
 * Also mirrors Discord's own mic state (the Mute/Deafen `role="switch"`
 * buttons in the RTC controls) as `dvt-mic` messages so the panel can follow
 * it without a mic button of its own.
 */
(() => {
  const active = new Map();
  let lastMicMuted = null;

  function send(msg) {
    try {
      chrome.runtime.sendMessage(msg).catch(() => {});
    } catch (e) {
    }
  }

  const cls = (el) =>
    el && el.nodeType === 1 && typeof el.className === 'string'
      ? el.className.toLowerCase()
      : '';

  function userIdFrom(container) {
    if (!container) return null;
    const withBg = container.querySelectorAll('[style*="background-image"]');
    for (const el of withBg) {
      const m = (el.getAttribute('style') || '').match(/avatars\/(\d+)\//);
      if (m) return m[1];
    }
    return null;
  }

  function speakerInfo(el) {
    const tile = el.closest('[data-selenium-video-tile]');
    if (tile) {
      const userId = tile.getAttribute('data-selenium-video-tile') || null;
      let name = '';
      const ft = tile.querySelector('[class*="focusTarget"][aria-label]');
      if (ft) {
        const label = ft.getAttribute('aria-label') || '';
        const m = label.match(/^[^,]*,\s*(.+)$/);
        name = (m ? m[1] : label).trim();
      }
      return { name: name || userId || '', userId };
    }
    const vu = el.closest('[class*="voiceUser"]');
    if (vu) {
      const nameEl = vu.querySelector('[class*="username"]');
      return {
        name: nameEl ? (nameEl.textContent || '').trim() : '',
        userId: userIdFrom(vu),
      };
    }
    if (cls(el).includes('username')) {
      return { name: (el.textContent || '').trim(), userId: null };
    }
    return { name: '', userId: null };
  }

  const tilesVisible = () =>
    document.querySelector('[data-selenium-video-tile]') !== null;

  function currentSpeakers() {
    const now = new Map();
    const onlyTiles = tilesVisible();
    for (const el of document.querySelectorAll('[style*="status-speaking"]')) {
      if (onlyTiles && !el.closest('[data-selenium-video-tile]')) continue;
      const info = speakerInfo(el);
      if (info.name) now.set(info.userId || info.name, info);
    }
    if (!onlyTiles) {
      for (const el of document.querySelectorAll('[class*="usernameSpeaking"]')) {
        const info = speakerInfo(el);
        if (info.name && !now.has(info.userId || info.name))
          now.set(info.userId || info.name, info);
      }
    }
    return now;
  }

  /** Discord mic state from the RTC audio controls (locale-independent:
   * `role="switch"` + `aria-checked`, never the localized aria-label).
   * Mute and Deafen are both switches on the same `audioButtonWithMenu`
   * plate; either one checked means the user is not audible to the call.
   * Returns null when no controls are visible (not connected to voice). */
  function micMutedNow() {
    let found = false;
    for (const b of document.querySelectorAll('button[role="switch"][aria-checked]')) {
      if (!cls(b).includes('audiobuttonwithmenu')) continue;
      found = true;
      if (b.getAttribute('aria-checked') === 'true') return true;
    }
    if (!found) {
      if (document.querySelector('[class*="plateMuted"]')) return true;
      return null;
    }
    return false;
  }

  function sweep() {
    const muted = micMutedNow();
    if (muted !== null && muted !== lastMicMuted) {
      lastMicMuted = muted;
      send({ type: 'dvt-mic', muted, t: Date.now() });
    }
    const now = currentSpeakers();
    for (const key of [...active.keys()]) {
      if (!now.has(key)) {
        const v = active.get(key);
        active.delete(key);
        send({ type: 'dvt-speaking', speaking: false, ...v, t: Date.now() });
      }
    }
    for (const [key, info] of now) {
      if (!active.has(key)) {
        active.set(key, info);
        send({ type: 'dvt-speaking', speaking: true, ...info, t: Date.now() });
      }
    }
  }

  setInterval(sweep, 250);

  function myUserId() {
    const panel = document.querySelector('section[class*="panels"], [class*="panels_"]');
    if (!panel) return null;
    const el = panel.querySelector('[style*="avatars/"], img[src*="avatars/"]');
    const src = el ? (el.getAttribute('style') || '') + (el.getAttribute('src') || '') : '';
    const m = src.match(/avatars\/(\d+)\//);
    return m ? m[1] : null;
  }

  function currentParticipants() {
    const seen = new Map();
    for (const tile of document.querySelectorAll('[data-selenium-video-tile]')) {
      const ft = tile.querySelector('[class*="focusTarget"][aria-label]');
      let name = '';
      if (ft) {
        const label = ft.getAttribute('aria-label') || '';
        const m = label.match(/^[^,]*,\s*(.+)$/);
        name = (m ? m[1] : label).trim();
      }
      if (name) seen.set(tile.getAttribute('data-selenium-video-tile') || name, name);
    }
    if (seen.size === 0) {
      const me = myUserId();
      if (me) {
        for (const list of document.querySelectorAll('[class*="voiceUsers"]')) {
          if (!list.querySelector(`[style*="avatars/${me}/"]`)) continue;
          for (const vu of list.querySelectorAll('[class*="voiceUser"]')) {
            const nameEl = vu.querySelector('[class*="username"]');
            const name = nameEl ? (nameEl.textContent || '').trim() : '';
            if (name) seen.set(userIdFrom(vu) || name, name);
          }
          break;
        }
      }
    }
    return [...seen.values()];
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg && msg.type === 'dvt-ping') {
      sendResponse({
        type: 'dvt-pong',
        speakers: [...currentSpeakers().values()].map((s) => s.name),
        participants: currentParticipants(),
        voiceUsers: document.querySelectorAll('[class*="voiceUser"]').length,
        tiles: document.querySelectorAll('[data-selenium-video-tile]').length,
        micMuted: micMutedNow(),
      });
    }
    return false;
  });

  send({ type: 'dvt-content-ready', t: Date.now() });
})();
