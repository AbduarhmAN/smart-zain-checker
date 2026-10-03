/**
 * Main Application Controller for Smart Zain Checker
 */
const App = {
  isRunning: false,
  isPaused: false,
  pollTimer: null,

  async init() {
    Dashboard.init();
    LiveFeed.init();
    QueueUI.refresh();

    // Load initial workbooks
    try {
      const info = await API.getSessionInfo();
      const wbSel = document.getElementById('selectWorkbook');
      if (wbSel && info.workbooks) {
        wbSel.innerHTML = '';
        const placeholder = document.createElement('option');
        placeholder.value = '';placeholder.textContent = 'اختر ملف العملاء';
        wbSel.appendChild(placeholder);
        info.workbooks.forEach(w => {
          const opt = document.createElement('option');
          opt.value = w;
          opt.textContent = w;
          wbSel.appendChild(opt);
        });
      }
      this.updateSheetsList(info.default_sheets || ['Sheet1']);
      // Auto-preview initial workbook if it is a results/errors workbook
      if (wbSel && wbSel.value) {
        const sSel = document.getElementById('selectSheet');
        const errIdx = (info.default_sheets || []).findIndex(s => s.includes('أخطاء') || s.includes('ملاحظات'));
        if (errIdx > 0 && sSel) sSel.value = errIdx;
        this.checkAndPreviewResultsSheet(wbSel.value, sSel ? parseInt(sSel.value || '0', 10) : 0);
      }
    } catch (e) {
      console.warn('Initial session info failed:', e);
    }

    // Setup file browse button and input
    const btnBrowse = document.getElementById('btnBrowseFile');
    const fileInput = document.getElementById('fileInputUpload');
    if (btnBrowse && fileInput) {
      btnBrowse.addEventListener('click', () => fileInput.click());
      fileInput.addEventListener('change', async (e) => {
        const files = Array.from(e.target.files || []);
        if (files.length) await this.handleFilesUpload(files);
      });
    }

    // Setup workbook dropdown change listener to dynamically refresh sheet list and auto-preview
    const wbDropdown = document.getElementById('selectWorkbook');
    const sheetDropdown = document.getElementById('selectSheet');

    const onWorkbookOrSheetChange = async () => {
      const selectedWb = wbDropdown ? wbDropdown.value : '';
      const selectedSheet = sheetDropdown ? parseInt(sheetDropdown.value || '0', 10) : 0;
      if (!selectedWb) {this.detectedResultsInfo=null;this.previewRequest=(this.previewRequest||0)+1;this.updateControlButtons();await this.poll();return;}

      const sheetLabel=sheetDropdown?.selectedOptions?.[0]?.textContent || '';
      if (!this.isRunning && (/^نتائج/.test(selectedWb) || /أخطاء|نتائج/.test(sheetLabel))) {
        await this.checkAndPreviewResultsSheet(selectedWb, selectedSheet);
      }
      this.updateControlButtons();
    };

    if (wbDropdown) {
      wbDropdown.addEventListener('change', async () => {
        const selectedWb = wbDropdown.value;
        this.detectedResultsInfo=null;
        this.previewRequest=(this.previewRequest||0)+1;
        if (!selectedWb) {await onWorkbookOrSheetChange();return;}
        try {
          const res = await API.getWorkbookSheets(selectedWb);
          if (wbDropdown.value !== selectedWb) return;
          if (res.status === 'ok') {
            this.updateSheetsList(res.sheets || ['Sheet1']);
            const errIdx = (res.sheets || []).findIndex(s => s.includes('أخطاء') || s.includes('ملاحظات'));
            if (errIdx > 0 && sheetDropdown) {
              sheetDropdown.value = errIdx;
            }
          }
        } catch (err) {
          console.warn('Failed to load sheets for workbook:', err);
        }
        await onWorkbookOrSheetChange();
      });
    }

    if (sheetDropdown) {
      sheetDropdown.addEventListener('change', onWorkbookOrSheetChange);
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

  async handleFilesUpload(files) {
    if(this.uploadBatch || this.uploading)return;
    this.uploadBatch=true;this.uploadReviewFiles=this.uploadReviewFiles || [];
    this.updateControlButtons();
    try {
      for(const file of files){
        const uploaded=await this.handleFileUpload(file);
        if(uploaded)this.uploadReviewFiles.push(uploaded);
      }
      await this.reviewNextUploadedFile();
    } finally {this.uploadBatch=false;this.updateControlButtons();}
  },

  async reviewNextUploadedFile(finishedFilename) {
    if(finishedFilename)this.uploadReviewFiles=(this.uploadReviewFiles || []).filter(file=>file.filename!==finishedFilename);
    const next=(this.uploadReviewFiles || [])[0];
    if(!next)return false;
    document.getElementById('selectWorkbook').value=next.filename;
    this.updateSheetsList(next.sheets || ['Sheet1']);
    this.detectedResultsInfo=null;
    await ColumnMapper.openModal();return true;
  },

  async handleFileUpload(file) {
    if (this.uploading) return;
    this.uploading = true;
    const btnBrowse = document.getElementById('btnBrowseFile');
    const origHtml = btnBrowse ? btnBrowse.innerHTML : '📂 استعراض...';
    if (btnBrowse) btnBrowse.innerHTML = '⏳ جاري الرفع...';
    if (btnBrowse) btnBrowse.disabled = true;

    const reader = new FileReader();
    return new Promise(resolve=>{
    let uploaded=null;
    reader.onload = async () => {
      try {
        const base64Data = String(reader.result).split(',')[1];
        const res = await API.uploadWorkbook(file.name, base64Data);
        if (res.status === 'ok') {
          uploaded=res;
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
          Dashboard.toast(`✅ تم استيراد الملف بنجاح: ${res.filename} (${(res.sheets || []).length} ورقة عمل)`);
        } else {
          Dashboard.toast('تعذر استيراد الملف: ' + res.message);
        }
      } catch (err) {
        Dashboard.toast('خطأ أثناء رفع الملف: ' + err.message);
      } finally {
        this.uploading = false;
        if (btnBrowse) btnBrowse.innerHTML = origHtml;
        if (btnBrowse) btnBrowse.disabled = this.isRunning;
        this.updateControlButtons();
        const fileInput = document.getElementById('fileInputUpload');
        if (fileInput) fileInput.value = '';
        resolve(uploaded);
      }
    };
    reader.onerror = () => {this.uploading=false;if(btnBrowse){btnBrowse.innerHTML=origHtml;btnBrowse.disabled=this.isRunning;}Dashboard.toast('تعذر قراءة الملف من الجهاز','error');resolve(null);};
    reader.onabort=reader.onerror;
    try {reader.readAsDataURL(file);} catch {reader.onerror();}
    });
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
    if (this.polling) return;
    this.polling = true;
    try {
      const data = await API.getLiveStatus();
      this.isRunning = data.running;
      this.isPaused = data.paused;
      this.pendingVerifications = (data.verifications || []).length;
      let viewData = data;
      if (!data.running && this.detectedResultsInfo) {
        const rows=this.detectedResultsInfo.rows || [];
        const count=status=>rows.filter(row=>row.status===status).length;
        viewData={...data,_preview:true,workbook:document.getElementById('selectWorkbook').value,
          kpis:{total:this.detectedResultsInfo.total_records || rows.length,completed:rows.length,
            remaining:0,matches:count('match'),mismatches:count('mismatch'),
            verified:count('match')+count('mismatch'),errors:count('error'),
            needs_review:count('needs_review'),not_found:count('not_found'),deferred:0,
            mismatch_total:rows.filter(row=>row.status==='mismatch').reduce((sum,row)=>sum+Math.abs(Number(row.diff_sar)||0),0)}};
      }
      Dashboard.update(viewData);

      LiveFeed.updateKPIs(viewData.kpis);
      if (viewData._preview) document.getElementById('kpiRemaining').textContent='—';
      LiveFeed.updateWorkerPills(data.workers);
      if (data.running || data.round_pending) LiveFeed.renderTable([]);
      else if (!this.detectedResultsInfo) LiveFeed.renderTable(data.table_rows);
      const notice = document.getElementById('sessionNotice');
      const note=data.verification_notice || (viewData._preview ? 'معاينة نتائج محفوظة؛ لن تبدأ طلبات جديدة إلا عند تشغيل الفحص.' : '');
      notice.textContent = note;
      notice.style.display = note ? 'block' : 'none';
      this.renderVerifications(data.verifications || []);

      // Keep Queue table and tab badge in real-time sync
      this._pollCount = (this._pollCount || 0) + 1;
      if (LiveFeed.activeTab === 'queue' || this._pollCount % 2 === 0) {
        QueueUI.refresh();
      }
      if (this._pollCount % 15 === 0) Dashboard.refreshTelegram();

      this.updateControlButtons();
    } catch (e) {
      Dashboard.offline();
    } finally { this.polling = false; }
  },

  updateControlButtons() {
    const btn = document.getElementById('btnActionRun');
    const btnCancel = document.getElementById('btnActionCancel');

    if (!btn) return;
    const another=document.getElementById('btnCheckAnotherFile');
    const waitingFiles=(QueueUI.lastJobs || []).filter(job=>job.status==='pending').length;
    if(another){another.style.display=!this.isRunning && waitingFiles ? 'inline-flex':'none';another.disabled=!Dashboard.connected || !!this.actionBusy || !document.getElementById('selectWorkbook').value;}
    btn.disabled = !Dashboard.connected || !!this.actionBusy || !!this.pendingVerifications || !!this.uploading || !!this.uploadBatch || !!QueueUI.starting;
    if(QueueUI.starting && !this.isRunning){btn.textContent='جارٍ التحقق من ملفات الجولة…';return;}

    if (this.isRunning) {
      btnCancel.style.display = 'inline-flex';
      if (this.isPaused) {
        btn.innerHTML = this.pendingVerifications ? 'أكمل تحقق زين أولًا' : 'استئناف الفحص ' + Dashboard.icon('arrow');
        btn.className = 'btn btn-primary';
        btn.onclick = () => Dashboard.sessionAction('resume');
      } else {
        btn.innerHTML = 'إيقاف مؤقت';
        btn.className = 'btn btn-secondary';
        btn.onclick = () => Dashboard.sessionAction('pause');
      }
    } else {
      btnCancel.style.display = 'none';
      if((this.uploadReviewFiles || []).length){
        btn.innerHTML='تحقق من الملفات المرفوعة · '+this.uploadReviewFiles.length;
        btn.className='btn btn-primary';btn.onclick=()=>this.reviewNextUploadedFile();
      } else if(waitingFiles){
        btn.innerHTML='بدء الجولة · '+waitingFiles+' ملف '+Dashboard.icon('arrow');
        btn.className='btn btn-primary';btn.onclick=()=>QueueUI.startQueue();
      } else if (this.detectedResultsInfo && this.detectedResultsInfo.error_count > 0) {
        btn.innerHTML = `🛠️ بدء صيانة الأخطاء (${this.detectedResultsInfo.error_count})`;
        btn.className = 'btn btn-warning';
        btn.onclick = () => this.startMaintenanceSession();
      } else {
        btn.innerHTML = 'تحقق من الملف وأضفه للجولة ' + Dashboard.icon('arrow');
        btn.className = 'btn btn-primary';
        btn.onclick = () => ColumnMapper.openModal();
        btn.disabled = btn.disabled || !document.getElementById('selectWorkbook').value;
      }
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
      panel.className = 'verification-panel';
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
    if (!await Dashboard.confirm({title:'إنهاء جلسة الفحص',text:'سيُوقف الفحص الحالي. يمكنك العودة إلى التقدم المحفوظ لاحقًا.',accept:'إنهاء الجلسة'})) return;
    try {
      await API.cancelSession();
      this.poll();
    } catch (e) {
      Dashboard.toast('تعذر إلغاء الجلسة: ' + e.message);
    }
  },

  async testTelegram() {
    return Dashboard.telegramAction('btnTelegramPing', () => API.testTelegramPing());
  },

  async checkAndPreviewResultsSheet(workbook, sheetIndex = 0) {
    if (!workbook) return false;
    const request=this.previewRequest=(this.previewRequest||0)+1;
    this.detectedResultsInfo=null;
    try {
      const res = await API.detectResultsSheet(workbook, sheetIndex);
      if (request!==this.previewRequest || this.isRunning || document.getElementById('selectWorkbook').value!==workbook || Number(document.getElementById('selectSheet').value)!==sheetIndex) return false;
      if (res.status === 'ok' && res.is_results_or_errors_sheet) {
        this.detectedResultsInfo = res;

        // Auto-select sheet in dropdown if server picked an errors tab
        const sheetDropdown = document.getElementById('selectSheet');
        if (sheetDropdown && res.sheet_index !== undefined && parseInt(sheetDropdown.value, 10) !== res.sheet_index) {
          sheetDropdown.value = res.sheet_index;
        }

        // Render preview rows in the table
        LiveFeed.renderTable(res.rows || [],true);

        // Update KPIs
        const elError = document.getElementById('countError');
        if (elError) elError.textContent = res.error_count.toLocaleString();
        const elTotal = document.getElementById('countTotal');
        if (elTotal) elTotal.textContent = res.total_records.toLocaleString();
        const kpiErr = document.getElementById('kpiError');
        if (kpiErr) kpiErr.textContent = res.error_count.toLocaleString();

        // Update toolbar repair button
        const btnRepair = document.getElementById('btnRepairErrors');
        if (btnRepair) {
          btnRepair.innerHTML = `<span>🛠️</span><span>صيانة الأخطاء</span> <span id="repairErrorsBadge" class="badge" style="background: rgba(0,0,0,0.25); color: #fff; font-size: 11px;">${res.error_count}</span>`;
          btnRepair.style.display = res.error_count > 0 ? 'inline-flex' : 'none';
        }

        // Update main action button
        this.updateControlButtons();

        // Show session notice banner
        const notice = document.getElementById('sessionNotice');
        if (notice) {
          notice.textContent = res.error_count ? `معاينة ${res.sheet_name}: ${res.error_count} سجل قابل لإعادة الفحص.` : `معاينة ${res.sheet_name}: لا توجد سجلات قابلة لإعادة الفحص في هذه الورقة.`;
          notice.style.display = 'block';
        }
        return true;
      } else {
        this.detectedResultsInfo = null;
        this.updateControlButtons();
        const notice = document.getElementById('sessionNotice');
        if (notice && !this.isRunning) notice.style.display = 'none';
        const btnRepair = document.getElementById('btnRepairErrors');
        if (btnRepair && !this.isRunning) btnRepair.style.display = 'none';
        return false;
      }
    } catch (e) {
      console.warn('Detect results sheet error:', e);
      return false;
    }
  },

  async startMaintenanceSession() {
    const wb = document.getElementById('selectWorkbook')?.value || 'نتائج فحص زين.xlsx';
    const sheetIdx = parseInt(document.getElementById('selectSheet')?.value || '0', 10);
    const count = this.detectedResultsInfo?.error_count || '';
    const confirmMsg = `هل تريد بدء صيانة وتدقيق ${count ? count + ' ' : ''}صفوف الأخطاء في (${wb}) الآن؟\nسيقوم العمال بالاتصال المباشر والبروكسي لإعادة الفحص وتحديث النتائج تلقائياً.`;
    if (!await Dashboard.confirm({text: confirmMsg})) return;

    try {
      const res = await API.startRepairSession(wb, sheetIdx);
      if (res.status === 'ok') {
        LiveFeed.switchTab('all');
        this.poll();
      } else {
        Dashboard.toast('تعذر بدء جلسة الصيانة: ' + res.message);
      }
    } catch (err) {
      Dashboard.toast('خطأ أثناء تشغيل الصيانة: ' + err.message);
    }
  },

  async repairErrors() {
    // If not running and we have detected results/errors info, start maintenance session!
    if (!this.isRunning && this.detectedResultsInfo && this.detectedResultsInfo.error_count > 0) {
      return this.startMaintenanceSession();
    }

    const errorCount = (parseInt(document.getElementById('countError')?.textContent) || 0) +
                       (parseInt(document.getElementById('countReview')?.textContent) || 0);

    const wb = document.getElementById('selectWorkbook')?.value;
    const sheetIdx = parseInt(document.getElementById('selectSheet')?.value || '0', 10);

    const confirmMsg = errorCount > 0
      ? `هل تريد بدء صيانة وإعادة فحص الأخطاء والمهلات (عددها ${errorCount}) في الشيت الحالي؟\n(ملاحظة: السجلات المطابقة بنجاح ستبقى محفوظة ولن تتأثر)`
      : `هل تريد فحص وصيانة أي سجلات أخطاء أو مراجعة في الشيت الحالي؟`;

    if (!await Dashboard.confirm({text: confirmMsg})) return;

    const btn = document.getElementById('btnRepairErrors');
    const origHtml = btn ? btn.innerHTML : '';
    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '<span>⏳</span><span>جاري بدء الصيانة...</span>';
    }

    try {
      const res = await API.repairErrors(null, wb, sheetIdx);
      if (res.status === 'ok') {
        Dashboard.toast(`🛠️ ${res.message}`);
        this.poll();
      } else if (res.status === 'info') {
        Dashboard.toast(`ℹ️ ${res.message}`);
      } else {
        Dashboard.toast(`تعذر تنفيذ صيانة الأخطاء: ${res.message}`);
      }
    } catch (e) {
      Dashboard.toast(`خطأ أثناء بدء صيانة الأخطاء: ${e.message}`);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = origHtml;
      }
    }
  },

  async repairSingleRow(rowNumber) {
    if (!await Dashboard.confirm({text: `هل تريد إعادة فحص وصيانة السطر رقم ${rowNumber}؟`})) return;
    try {
      const res = await API.repairErrors(rowNumber);
      if (res.status === 'ok') {
        this.poll();
      } else {
        Dashboard.toast(res.message);
      }
    } catch (e) {
      Dashboard.toast(`خطأ: ${e.message}`);
    }
  }
};

window.addEventListener('DOMContentLoaded', () => App.init());
