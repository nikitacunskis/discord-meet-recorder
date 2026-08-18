// Atver sānu paneli, uzklikšķinot uz paplašinājuma ikonas.
chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((e) => console.error('sidePanel setup failed', e));
