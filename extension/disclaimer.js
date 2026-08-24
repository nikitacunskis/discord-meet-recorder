/** One-time install notice: apply i18n, remember acknowledgement, close. */
I18N.init();
document.getElementById('ack').addEventListener('click', async () => {
  await chrome.storage.local.set({ dvtDisclaimerAck: true });
  window.close();
});
