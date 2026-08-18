// Mikrofona atļaujas lapa: paplašinājuma izcelsmei (chrome-extension://) ir
// sava atļauja, neatkarīga no discord.com. Sānu panelis prompt parādīt nevar,
// tāpēc atļauju paņemam šeit — pēc tam panelis mikrofonu dabū klusām.
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
