/**
 * Microphone permission page. The side panel cannot render the getUserMedia
 * permission prompt, so the chrome-extension origin grant is acquired here.
 */
const result = document.getElementById('result');

document.getElementById('grant').addEventListener('click', async () => {
  try {
    const s = await navigator.mediaDevices.getUserMedia({ audio: true });
    s.getTracks().forEach((t) => t.stop());
    result.textContent = t('micPermOk');
    result.className = 'ok';
    setTimeout(() => window.close(), 2000);
  } catch (e) {
    result.textContent = t('micPermFail') + ' (' + e.name + ')';
    result.className = 'err';
  }
});

I18N.init();
