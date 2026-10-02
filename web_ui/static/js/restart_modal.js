/**
 * Restart Warning & Confirmation Modal Controller
 */
const RestartModal = {
  currentCallback: null,

  show({ filename, sheetName, statsText, onConfirm }) {
    const overlay = document.getElementById('restartModalOverlay');
    const titleEl = document.getElementById('restartModalTargetTitle');
    const statsEl = document.getElementById('restartModalStats');
    const confirmBtn = document.getElementById('btnConfirmRestartAction');

    if (!overlay) return;

    if (titleEl) {
      titleEl.textContent = `الملف: ${filename} | ورقة العمل: ${sheetName}`;
    }
    if (statsEl) {
      statsEl.innerHTML = statsText || 'الحالة: مكتملة بالكامل ✔';
    }

    this.currentCallback = onConfirm;

    if (confirmBtn) {
      confirmBtn.onclick = async () => {
        const origText = confirmBtn.innerHTML;
        confirmBtn.innerHTML = '⏳ جاري تصفير البيانات والبدء...';
        confirmBtn.disabled = true;
        try {
          if (this.currentCallback) {
            await this.currentCallback();
          }
        } finally {
          confirmBtn.innerHTML = origText;
          confirmBtn.disabled = false;
          this.close();
        }
      };
    }

    overlay.style.display = 'flex';
  },

  close() {
    const overlay = document.getElementById('restartModalOverlay');
    if (overlay) overlay.style.display = 'none';
    this.currentCallback = null;
  }
};
