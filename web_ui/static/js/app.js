/**
 * Main Application Controller for Smart Zain Checker
 */
const App = {
  isRunning: false,
  isPaused: false,
  pollTimer: null,

  async init() {
    LiveFeed.init();
    QueueUI.refresh();

    // Load initial workbooks
    try {
      const info = await API.getSessionInfo();
      const wbSel = document.getElementById('selectWorkbook');
      if (wbSel && info.workbooks) {
        wbSel.innerHTML = '';
        info.workbooks.forEach(w => {
          const opt = document.createElement('option');
          opt.value = w;
          opt.textContent = w;
          wbSel.appendChild(opt);
        });
      }
      this.updateSheetsList(info.default_sheets || ['Sheet1']);
    } catch (e) {
      console.warn('Initial session info failed:', e);
    }

    // Setup file browse button and input
    const btnBrowse = document.getElementById('btnBrowseFile');
    const fileInput = document.getElementById('fileInputUpload');
    if (btnBrowse && fileInput) {
      btnBrowse.addEventListener('click', () => fileInput.click());
      fileInput.addEventListener('change', async (e) => {
        const file = e.target.files?.[0];
        if (file) await this.handleFileUpload(file);
      });
    }

    // Setup workbook dropdown change listener to dynamically refresh sheet list
    const wbDropdown = document.getElementById('selectWorkbook');
    if (wbDropdown) {
      wbDropdown.addEventListener('change', async () => {
        const selectedWb = wbDropdown.value;
        if (!selectedWb) return;
        try {
          const res = await API.getWorkbookSheets(selectedWb);
          if (res.status === 'ok') {
            this.updateSheetsList(res.sheets || ['Sheet1']);
          }
        } catch (err) {
          console.warn('Failed to load sheets for workbook:', err);
        }
      });
    }

    // Start polling
    this.poll();
    this.pollTimer = setInterval(() => this.poll(), 1000);

    // Setup live clock
    setInterval(() => {
      const el = document.getElementById('clockBox');
      if (el) el.textContent = new Date().toTimeString().split(' ')[0];
    }, 1000);
  },

  async handleFileUpload(file) {
    const btnBrowse = document.getElementById('btnBrowseFile');
    const origHtml = btnBrowse ? btnBrowse.innerHTML : '📂 استعراض...';
    if (btnBrowse) btnBrowse.innerHTML = '⏳ جاري الرفع...';

    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const base64Data = String(reader.result).split(',')[1];
        const res = await API.uploadWorkbook(file.name, base64Data);
        if (res.status === 'ok') {
          const wbSel = document.getElementById('selectWorkbook');
          if (wbSel) {
            let opt = Array.from(wbSel.options).find(o => o.value === res.filename);
            if (!opt) {
              opt = document.createElement('option');
              opt.value = res.filename;
              opt.textContent = res.filename;
              wbSel.insertBefore(opt, wbSel.firstChild);
            }
            wbSel.value = res.filename;
          }
          this.updateSheetsList(res.sheets || ['Sheet1']);
          alert(`✅ تم استيراد الملف بنجاح: ${res.filename} (${(res.sheets || []).length} ورقة عمل)`);
        } else {
          alert('تعذر استيراد الملف: ' + res.message);
        }
      } catch (err) {
        alert('خطأ أثناء رفع الملف: ' + err.message);
      } finally {
        if (btnBrowse) btnBrowse.innerHTML = origHtml;
        const fileInput = document.getElementById('fileInputUpload');
        if (fileInput) fileInput.value = '';
      }
    };
    reader.readAsDataURL(file);
  },

  updateSheetsList(sheets) {
    const sSel = document.getElementById('selectSheet');
    if (!sSel) return;
    sSel.innerHTML = '';
    sheets.forEach((s, idx) => {
      const opt = document.createElement('option');
      opt.value = idx;
      opt.textContent = `${idx + 1}. ${s}`;
      sSel.appendChild(opt);
    });
  },

  async poll() {
    try {
      const data = await API.getLiveStatus();
      this.isRunning = data.running;
      this.isPaused = data.paused;

      LiveFeed.updateKPIs(data.kpis);
      LiveFeed.updateWorkerPills(data.workers);
      LiveFeed.renderTable(data.table_rows);
      const notice = document.getElementById('sessionNotice');
      notice.textContent = data.verification_notice || '';
      notice.style.display = data.verification_notice ? 'block' : 'none';
      this.renderVerifications(data.verifications || []);
      document.getElementById('sessionWorkbook').textContent = data.workbook ? `ملف جلسة الفحص: ${data.workbook}` : '';
      const telegramBadge = document.getElementById('tgStatusBadge');
      telegramBadge.textContent = data.telegram_enabled ? 'مفعّل' : 'غير مفعّل';
      telegramBadge.className = data.telegram_enabled ? 'badge badge-match' : 'badge';

      // Keep Queue table and tab badge in real-time sync
      this._pollCount = (this._pollCount || 0) + 1;
      if (LiveFeed.activeTab === 'queue' || this._pollCount % 2 === 0) {
        QueueUI.refresh();
      }

      this.updateControlButtons();
    } catch (e) {
      // Server may be briefly busy or restarting
    }
  },

  updateControlButtons() {
    const btn = document.getElementById('btnActionRun');
    const btnCancel = document.getElementById('btnActionCancel');

    if (!btn) return;

    if (this.isRunning) {
      btnCancel.style.display = 'inline-flex';
      if (this.isPaused) {
        btn.innerHTML = '▶️ استئناف الفحص';
        btn.className = 'btn btn-primary';
        btn.onclick = () => API.resumeSession();
      } else {
        btn.innerHTML = '⏸️ إيقاف مؤقت';
        btn.className = 'btn btn-secondary';
        btn.onclick = () => API.pauseSession();
      }
    } else {
      btnCancel.style.display = 'none';
      btn.innerHTML = '⚙️ فحص وتعيين الأعمدة';
      btn.className = 'btn btn-primary';
      btn.onclick = () => ColumnMapper.openModal();
    }
  },

  renderVerifications(items) {
    const container = document.getElementById('serviceVerifications');
    const signature = JSON.stringify(items.map(item => [item.id, item.submitted]));
    if (this.verificationSignature === signature) return;
    this.verificationSignature = signature;
    container.replaceChildren();
    container.style.display = items.length ? 'block' : 'none';
    items.forEach(item => {
      const panel = document.createElement('div');
      panel.style.cssText = 'padding:16px;margin-bottom:12px;border:1px solid var(--border-light);border-radius:var(--radius-sm);';
      const title = document.createElement('h3');
      title.textContent = `تحقق زين — ${item.route === 'proxy' ? 'جلسة البروكسي' : 'الجلسة المباشرة'}`;
      const image = document.createElement('img');
      image.src = `/api/service-verification-image?id=${encodeURIComponent(item.id)}`;
      image.alt = 'التحدي الحالي في جلسة العامل';
      image.style.cssText = 'display:block;max-width:100%;margin:12px 0;background:white;';
      const input = document.createElement('input');
      input.type = 'text';
      input.maxLength = 64;
      input.autocomplete = 'off';
      input.setAttribute('aria-label', `رمز تحقق ${item.route === 'proxy' ? 'البروكسي' : 'المباشر'}`);
      input.className = 'form-select';
      input.disabled = item.submitted;
      const button = document.createElement('button');
      button.className = 'btn btn-primary';
      button.textContent = item.submitted ? 'أُرسل الرمز؛ انتظر النتيجة' : 'إرسال رمز التحقق';
      button.disabled = item.submitted;
      button.style.marginTop = '8px';
      const status = document.createElement('p');
      status.setAttribute('role', 'status');
      button.onclick = async () => {
        if (!input.value.trim()) { status.textContent = 'أدخل الرمز الظاهر في الصورة'; return; }
        button.disabled = true;
        try {
          const response = await API.post('/api/service-verification-submit', {id:item.id,answer:input.value});
          status.textContent = response.message;
          input.value = '';
          input.disabled = true;
          button.textContent = 'أُرسل الرمز؛ انتظر النتيجة';
        } catch (error) {
          status.textContent = error.message;
          button.disabled = false;
        }
      };
      panel.append(title, image, input, button, status);
      container.append(panel);
    });
  },

  async handleCancel() {
    if (!confirm('هل أنت متأكد من رغبتك في إيقاف وإلغاء جلسة الفحص الحالية؟')) return;
    try {
      await API.cancelSession();
      this.poll();
    } catch (e) {
      alert('تعذر إلغاء الجلسة: ' + e.message);
    }
  },

  async testTelegram() {
    const el = document.getElementById('tgStatusBadge');
    if (el) el.textContent = '⏳ جاري الإرسال...';
    try {
      await API.testTelegramPing();
      if (el) el.textContent = 'متصل ✔';
      alert('✅ تم إرسال رسالة تجريبية بنجاح إلى حساب تلقرام الخاص بك!');
    } catch (e) {
      if (el) el.textContent = 'خطأ اتصال ✖';
      alert('تعذر إرسال الإشعار: ' + e.message);
    }
  }
};

window.addEventListener('DOMContentLoaded', () => App.init());
