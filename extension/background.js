/** Opens the side panel when the toolbar icon is clicked. */
chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((e) => console.error('sidePanel setup failed', e));

/** One-time notice page after installation (until acknowledged). */
chrome.runtime.onInstalled.addListener(async (details) => {
  if (details.reason !== 'install') return;
  const { dvtDisclaimerAck } = await chrome.storage.local.get('dvtDisclaimerAck');
  if (dvtDisclaimerAck) return;
  chrome.tabs.create({ url: chrome.runtime.getURL('disclaimer.html') });
});
