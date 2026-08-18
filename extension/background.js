/** Opens the side panel when the toolbar icon is clicked. */
chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((e) => console.error('sidePanel setup failed', e));
