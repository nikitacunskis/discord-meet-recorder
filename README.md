# Discord Call Recorder + Speaker Timeline

Chrome paplašinājums (MV3), kas Discord **web** zvana laikā:

1. ieraksta visu taba audio → `discord-call-<datums>.webm` (opus),
2. no DOM nolasa, **kurš kurā brīdī runā** (zaļais indikators = `usernameSpeaking` klase) → `*.speakers.json` un `*.speakers.srt`.

Transkripciju NEveic — audio + laika joslu pēc sarunas manuāli iedod Claude/ChatGPT, kas transkribē un atribuē "kurš ko teica". Viss lokāli, nekas nekur netiek sūtīts.

## Instalācija

1. Chrome → `chrome://extensions`
2. Ieslēdz **Developer mode** (augšējais labais stūris)
3. **Load unpacked** → izvēlies mapi `extension/`

## Lietošana

1. Atver **discord.com** Chrome tabā (desktop app neder — paplašinājums to nedzird!) un pieslēdzies balss kanālam.
2. Esot uz Discord taba, nospied paplašinājuma ikonu → atveras sānu panelis. Panelim jārāda "DOM sensors: aktīvs ✓".
3. **● Sākt ierakstu**. Skaņu turpināsi dzirdēt kā parasti. Paneli neaizver, kamēr rit ieraksts.
4. **■ Beigt un saglabāt** → Downloads mapē nokrīt 3 faili (Chrome var vienreiz pajautāt atļauju vairākiem failiem).

## Pilnā automatizācija (native host)

Ar reģistrētu native hostu (`native/install.sh <extension-id>`) viss notiek pats:
ieraksts pēc Stop nonāk `recordings/` mapē (vai iestatījumos norādītajā), hosts pats
palaiž `tools/transcribe.py` ar paneļa iestatījumiem (Whisper modelis, valoda,
Claude instance: claude / claude-personal / claude-rgp), un panelī redzams dzīvs
progresa žurnāls + ierakstu saraksts ar statusiem (⏳ audio → ✓ transcript → ✓ report).

Ja hosts nav pieejams, panelis automātiski krīt atpakaļ uz failu lejupielādi
Downloads mapē + gatavu termināļa komandu ar Copy pogu.

## Pēc sarunas — transkripcija (lokāli, viena komanda)

ChatGPT/Claude čats augšupielādētu audio pats netranskribē, tāpēc STT notiek lokāli ar whisper.cpp:

```bash
# vienreizēja uzstādīšana
brew install ffmpeg whisper-cpp

# transkripcija + runātāju atribūcija (modelis lejupielādējas pats pirmajā reizē)
python3 tools/transcribe.py ~/Downloads/discord-call-<datums>.webm --model small
```

Rezultāts: `discord-call-<datums>.transcript.md` ar rindām `[HH:MM:SS] vārds: teksts`.
To iemet ChatGPT/Claude kopsavilkumam un action items (`.prompt.txt` vairs nav obligāts — tas domāts gadījumam, ja audio apstrādā pats čats).

Kvalitāte pret ātrumu: `--model small` (noklusēts, ~470 MB) → `medium` (~1.5 GB) → `large-v3` (~3 GB, labākais jauktai LV/EN/RU runai). Ja zvans pārsvarā vienā valodā, pievieno `--language ru` / `lv` / `en` — mazāk halucināciju.

## Zināmie ierobežojumi

- Audio ir viens sajaukts celiņš; ja divi runā vienlaikus, teksts pielips dominējošajam runātājam.
- Discord klašu hash (`usernameSpeaking__07f91`) mainās ar build — selektori meklē pēc apakšvirknes, tāpēc parasti pārdzīvo atjauninājumus. Ja panelis pārstāj rādīt runātājus, paziņo — jāatjauno selektori.
- Ieraksts stāv atmiņā līdz Stop: ~15 MB/stundā, dažu stundu zvans nav problēma.
- Ierakstās viss taba audio — arī Discord pīkstieni un cits tabā skanošais.

## Struktūra

```
extension/
├── manifest.json    MV3, atļaujas: tabCapture, sidePanel, tabs
├── background.js    ikona → sānu panelis
├── content.js       MutationObserver uz [class*="usernameSpeaking"] → runātāju notikumi
├── panel.js         tabCapture + MediaRecorder + intervālu būve + eksports
├── panel.html/css   UI
RESEARCH.md          izpētes piezīmes (DOM pierādījumi, alternatīvas)
PROMPT.md            gatavs prompts transkripcijai ar atribūciju
is_voice.html /      reāli DOM paraugi, pret kuriem verificēti selektori
no_voice.html
```
