# Discord Voice Transcriber — situācijas izpēte

Datums: 2026-08-17. Mērķis: bezmaksas, open-source Chrome paplašinājums, kas transkribē Discord balss kanālu ar runātāju atribūciju ("kas un ko teica"), bez bota (darba serveris, botu pielikt nevar).

## Secinājums

**Tehniski realizējams.** Neviens gatavs projekts nedara tieši šo (tab audio + runātāja noteikšana no DOM), bet visi trīs būvbloki eksistē un ir pārbaudīti:

1. **Runātāja noteikšana no DOM — apstiprināta ar reāliem HTML paraugiem** (2026-08-17, `is_voice.html` vs `no_voice.html` no darba servera):
   - **Vienīgā klašu atšķirība starp "kāds runā" un "klusums": lietotājvārda `<div>` iegūst klasi `usernameSpeaking__07f91`.** Elementa `textContent` = redzamais vārds ("Kristaps Strods"). Tas ir viss, kas vajadzīgs.
   - Struktūra: `div.voiceUser__07f91 > div.content__07f91 > … > div.username__07f91.usernameSpeaking__07f91`
   - Sekundārais signāls (rezerve): avatāra inline `style` maina `box-shadow: … var(--status-speaking) …` — CSS mainīgā vārds ir semantisks un stabilāks par hash klasēm.
   - Unikāla identifikācija: avatāra `background-image` URL satur Discord user ID (`cdn.discordapp.com/avatars/<userId>/…`) — der, ja diviem sakrīt vārdi.
   - Papildus: `div.focusTarget__54e4b[aria-label]` dod vārdu + stāvokli (piem. "Aleksandr, Muted").
   - Sānjoslas saraksts (paraugā 16 `voiceUser` elementi) atjaunojas arī bez atvērta pilnekrāna zvana skata.
   - Hash sufiksi (`__07f91` u.c.) mainās ar katru Discord build → selektori tikai substring formā: `[class*="usernameSpeaking"]`, `[class*="voiceUser"]`.
   - Mehānisms: content script ar `MutationObserver` (attributeFilter: `class`,`style`, subtree) → notikumu žurnāls `{userId, vārds, sākums, beigas}`.

2. **Audio tveršana**: `chrome.tabCapture` API — tver visu taba audio (jauktu, visi runātāji kopā).
   - ⚠️ Strādā **tikai ar Discord web Chrome tabā**, ne ar desktop aplikāciju.
   - ⚠️ tabCapture apklusina tabu — audio jāatskaņo atpakaļ caur AudioContext (standarta paņēmiens).

3. **Transkripcija (bezmaksas, open-source)**:
   - **Whisper pārlūkā** caur transformers.js — 100 % lokāli, nekas nekur netiek sūtīts. Modeļi: tiny ~40 MB, base ~80 MB, small ~250 MB (pirmajā reizē lejupielādējas, tad kešā). WebGPU paātrina; uz CPU small var atpalikt no reāllaika.
   - **Lokāls WhisperLive serveris** (Collabora, open-source) — labāka kvalitāte/latentums, extension sūta audio uz localhost.
   - Latviešu valodai vajadzēs vismaz `small` (multilingual); `tiny/base` LV kvalitāte vāja.

## Gatavie open-source projekti, ko var pārņemt par bāzi

| Projekts | Kas tajā ir | Kā trūkst |
|---|---|---|
| [collabora/WhisperLive](https://github.com/collabora/WhisperLive) + [Audio-Transcription-Chrome](https://github.com/collabora/WhisperLive/tree/main/Audio-Transcription-Chrome) | tabCapture → lokāls Whisper serveris, gandrīz reāllaiks, MIT | Nav runātāju atribūcijas |
| [ainoya/chrome-extension-web-transcriptor-ai](https://github.com/ainoya/chrome-extension-web-transcriptor-ai) | tabCapture + transformers.js, viss pārlūkā, privātums | Nav runātāju, nav Discord specifikas |
| [AIex7/Local-Whisper-Captions](https://github.com/AIex7/Local-Whisper-Captions-Chrome-Extension-) | tabCapture + in-browser Whisper + subtitru overlay | Nav runātāju |
| [LaurinBrechter/tab-transcribe](https://github.com/LaurinBrechter/tab-transcribe) | Vienkāršs tabCapture + Whisper (20 s intervāli) | Ne reāllaiks, nav runātāju |

**Unikālā daļa, kas jāuzraksta pašiem** (~neviens to nedara): content script ar speaking-timeline + korelācija "audio segments ↔ kurš tobrīd dega zaļš". Tā kā cilvēki runā pārsvarā pēc kārtas, intervālu pārklāšanās atribūcija būs uzticama.

## Piedāvātā arhitektūra (MV3)

```
content.js      → MutationObserver uz [class*="speaking"] → {vārds, ts, on/off} events
side panel      → Start/Stop, tabCapture → AudioWorklet → VAD (klusuma robeža ~700ms)
                → segmenti → Whisper (transformers.js lokāli VAI localhost WhisperLive)
                → segmenta [t0,t1] × speaking-timeline pārklāšanās → "12:03 Jānis: ..."
                → eksports .md/.txt
```

## Riski

- Discord klases hash mainās ar build — substring selektori + brīdinājums UI, ja 0 dalībnieku atrasts.
- Jāsēž **Discord web** (ne desktop app) — darba plūsmas maiņa lietotājam.
- In-browser Whisper small uz vāja CPU var atpalikt; drošais ceļš — WhisperLive lokāli (docker/pip).
- Ja divi runā vienlaikus, jauktais audio segments tiks piešķirts dominējošajam runātājam (pieņemts kā OK).

## Flīžu (zvana) skats — otrs runāšanas signāls (2026-08-17)

Lielajā zvana skatā `speaking` klases NAV. Tur signāls ir inline stils:
- klusums: `<div class="border__2f4f7" style="">`
- runā: `<div class="border__2f4f7" style="box-shadow: 0 0 0 0px var(--status-speaking), inset 0 0 0 2px var(--status-speaking), …">`
- flīzes saknes elementam ir `data-selenium-video-tile="<userId>"` — stabils, netulkojams userId
- vārds: `focusTarget` ar `aria-label="Call tile, <vārds>"`

Universālais selektors abiem skatiem: `[style*="status-speaking"]` — CSS mainīgā vārds ir semantisks un nemainās ar build (atšķirībā no klašu hash). Sensors izmanto to kā primāro signālu + `usernameSpeaking` klasi kā rezervi.

## Scope maiņa (2026-08-17): tikai ieraksts, bez live STT

Lietotāja lēmums: paplašinājums **neveic transkripciju**. Tas tikai (1) ieraksta taba audio un (2) pieraksta runātāju laika joslu no DOM. Transkripciju pēc sarunas veic manuāli ChatGPT/Claude.

Ko paplašinājums saglabā par katru zvanu:
1. `call-<datums>.webm` — viss taba audio (visi runātāji vienā celiņā; tabCapture → MediaRecorder, opus).
2. `call-<datums>.speakers.json` — `[{name, userId, start_ms, end_ms}]` no `usernameSpeaking` DOM notikumiem, ar to pašu pulksteni kā audio.
3. (ērtībai) `call-<datums>.speakers.srt` — tā pati laika josla subtitru formātā, ko var iedot LLM kopā ar audio.

Atribūcija pēc fakta: LLM/Whisper transkribē audio ar laika zīmogiem → segmentus sakrusto ar speakers laika joslu → "kurš ko teica". Tā kā runā pārsvarā pa vienam, sakritība ir viennozīmīga.

Piezīme: atsevišķas audio straumes katram lietotājam Chrome paplašinājumam nav tieši pieejamas (to dod tikai bots). Teorētisks ceļš caur WebRTC `RTCPeerConnection` hook eksistē, bet track↔lietotājs kartēšana ir nestabila — nav vajadzīgs, DOM laika josla pietiek.

## Nākamais solis (kad apstiprināsi)

MVP zem `~/git/personal/discord-voice-transcriber/` (~1–2 dienas darba): fork/aizguvums no ainoya vai WhisperLive extension + pašu content script un atribūcijas loģika.
