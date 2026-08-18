// Discord runātāju sensors.
// Universālais signāls (pārbaudīts pret reāliem DOM paraugiem abos skatos, 2026-08-17):
// kad cilvēks runā, viņa elements (flīzes rāmis vai sānjoslas avatārs) iegūst
// inline stilu `box-shadow: ... var(--status-speaking) ...`. Klusumā šī CSS
// mainīgā lapā nav vispār. CSS mainīgā vārds ir semantisks un stabils —
// atšķirībā no klašu hash, kas mainās ar katru Discord build.
//
// Runātāja identifikācija:
//  - flīžu skats: tile elementam ir data-selenium-video-tile="<userId>" un
//    focusTarget ar aria-label="Call tile, <vārds>"
//  - sānjosla: voiceUser elements ar username div un avatāra URL (satur userId)
(() => {
  const active = new Map(); // key (userId vai vārds) -> {name, userId}

  function send(msg) {
    try {
      chrome.runtime.sendMessage(msg).catch(() => {});
    } catch (e) {
      // extension konteksts pārlādēts — ignorējam
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
    // flīžu (zvana) skats
    const tile = el.closest('[data-selenium-video-tile]');
    if (tile) {
      const userId = tile.getAttribute('data-selenium-video-tile') || null;
      let name = '';
      const ft = tile.querySelector('[class*="focusTarget"][aria-label]');
      if (ft) {
        const label = ft.getAttribute('aria-label') || '';
        const m = label.match(/^[^,]*,\s*(.+)$/); // "Call tile, vārds" -> "vārds"
        name = (m ? m[1] : label).trim();
      }
      return { name: name || userId || '', userId };
    }
    // sānjoslas saraksts
    const vu = el.closest('[class*="voiceUser"]');
    if (vu) {
      const nameEl = vu.querySelector('[class*="username"]');
      return {
        name: nameEl ? (nameEl.textContent || '').trim() : '',
        userId: userIdFrom(vu),
      };
    }
    // rezerve: pats elements ir username div (usernameSpeaking klases ceļš)
    if (cls(el).includes('username')) {
      return { name: (el.textContent || '').trim(), userId: null };
    }
    return { name: '', userId: null };
  }

  function currentSpeakers() {
    const now = new Map();
    // galvenais signāls: inline stils ar var(--status-speaking)
    for (const el of document.querySelectorAll('[style*="status-speaking"]')) {
      const info = speakerInfo(el);
      if (info.name) now.set(info.userId || info.name, info);
    }
    // rezerves signāls: sānjoslas usernameSpeaking__<hash> klase
    for (const el of document.querySelectorAll('[class*="usernameSpeaking"]')) {
      const info = speakerInfo(el);
      if (info.name && !now.has(info.userId || info.name))
        now.set(info.userId || info.name, info);
    }
    return now;
  }

  function sweep() {
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

  // Bez MutationObserver: Discord ģenerē tūkstošiem mutāciju sekundē, un
  // novērotājs uz visa body lielā zvanā spēj nogāzt taba renderi. Tā vietā —
  // viens lēts atribūta-selektora vaicājums 4× sekundē; ±250 ms precizitāte
  // runātāju intervāliem ir vairāk nekā pietiekama.
  setInterval(sweep, 250);

  // Visi balss kanāla dalībnieki (ne tikai runājošie) — paneļa režģim.
  function currentParticipants() {
    const seen = new Map();
    for (const vu of document.querySelectorAll('[class*="voiceUser"]')) {
      const nameEl = vu.querySelector('[class*="username"]');
      const name = nameEl && (nameEl.textContent || '').trim();
      if (name) seen.set(userIdFrom(vu) || name, name);
    }
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
    return [...seen.values()];
  }

  // Panelis pingo, lai zinātu, ka sensors dzīvs.
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg && msg.type === 'dvt-ping') {
      sendResponse({
        type: 'dvt-pong',
        speakers: [...currentSpeakers().values()].map((s) => s.name),
        participants: currentParticipants(),
        voiceUsers: document.querySelectorAll('[class*="voiceUser"]').length,
        tiles: document.querySelectorAll('[data-selenium-video-tile]').length,
      });
    }
    return false;
  });

  send({ type: 'dvt-content-ready', t: Date.now() });
})();
