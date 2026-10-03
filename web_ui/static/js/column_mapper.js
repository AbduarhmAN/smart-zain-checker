/**
 * Column Mapper Modal Controller for Smart Zain Checker
 */
const ColumnMapper = {
  currentAnalysis: null,

  async openModal() {
    const wb = document.getElementById('selectWorkbook')?.value || '2.xlsx';
    const sheetIdx = parseInt(document.getElementById('selectSheet')?.value || '0', 10);
    
    if (!wb || !document.getElementById('selectWorkbook').value) {Dashboard.toast('اختر ملف العملاء أولًا');return;}
    this.currentAnalysis = null;
    this.currentValidated=false;
    const analysisRevision=this.analysisRequest=(this.analysisRequest || 0)+1;
    this.analyzing=true;
    document.getElementById('mapHasHeaders').checked=true;
    document.getElementById('mapHasHeaders').disabled=true;
    this.validationRequest=(this.validationRequest || 0)+1;
    if(!this.validationBound){
      document.getElementById('modalOverlay').addEventListener('change',()=>this.scheduleValidation());
      document.getElementById('mapperAdvanced').addEventListener('toggle',()=>{
        if(this.skipAdvancedToggle){this.skipAdvancedToggle=false;return;}
        if(this.currentAnalysis)this.scheduleValidation();
      });
      this.validationBound=true;
    }
    document.getElementById('modalOverlay').style.display = 'flex';
    Dashboard.modalOpened('modalOverlay');
    document.getElementById('btnMapperStart').disabled = true;
    document.getElementById('btnMapperQueue').disabled = true;
    document.getElementById('docTypeLabel').textContent = 'جاري تحليل الصف الأول...';
    document.getElementById('docAcceptBadge').textContent = 'قيد الفحص';

    try {
      const res = await API.inspectSheetColumns(wb, sheetIdx);
      if(analysisRevision!==this.analysisRequest)return;
      if (res.status === 'ok') {
        this.analyzing=false;
        this.currentAnalysis = res.analysis;
        this.renderAnalysis(res.analysis);
        await this.validateCurrent();
      } else {
        Dashboard.toast('تعذر فحص الشيت: ' + res.message);
      }
    } catch (e) {
      if(analysisRevision===this.analysisRequest)Dashboard.toast('خطأ أثناء الاتصال بالخادم: ' + e.message);
    } finally {
      if(analysisRevision!==this.analysisRequest)return;
      this.analyzing=false;
      document.getElementById('btnMapperStart').disabled = !this.currentAnalysis || !this.currentValidated;
      document.getElementById('btnMapperQueue').disabled = !this.currentAnalysis || !this.currentValidated;
    }
  },

  closeModal() {
    this.analysisRequest=(this.analysisRequest || 0)+1;
    this.validationRequest=(this.validationRequest || 0)+1;
    clearTimeout(this.validationTimer);
    document.getElementById('modalOverlay').style.display = 'none';
    Dashboard.focusBeforeModal?.focus();
  },

  scheduleValidation() {
    if(!this.currentAnalysis || this.analyzing)return;
    this.renderPreview();
    clearTimeout(this.validationTimer);
    this.validationRequest=(this.validationRequest || 0)+1;
    this.currentValidated=false;
    document.getElementById('btnMapperStart').disabled=true;
    document.getElementById('btnMapperQueue').disabled=true;
    this.validationTimer=setTimeout(()=>this.validateCurrent(),250);
  },

  async validateCurrent() {
    if(!this.currentAnalysis)return;
    const revision=this.validationRequest=(this.validationRequest || 0)+1;
    this.currentValidated=false;
    const badge=document.getElementById('docAcceptBadge');
    const message=document.getElementById('mapValidationMessage');
    message.textContent='';message.style.display='none';
    badge.textContent='جارٍ التحقق من البيانات…';
    document.getElementById('btnMapperStart').disabled=true;
    document.getElementById('btnMapperQueue').disabled=true;
    try {
      const mapping=this.getMappingPayload();
      if(!mapping.lookup_col && !mapping.service_col || !mapping.amount_col){
        badge.textContent='اختر عمود الرقم وعمود المبلغ';
        message.textContent='اختر العمودين من القوائم. اسم العميل وبقية الأعمدة اختيارية.';
        message.style.display='block';return;
      }
      const result=await API.validateSheet({workbook:document.getElementById('selectWorkbook').value,
        sheet_index:Number(document.getElementById('selectSheet').value || 0),column_mapping:mapping,
        mode:document.getElementById('selectAuditMode').value,amount_target:mapping.amount_target});
      if(revision!==this.validationRequest)return;
      this.currentValidated=!!result.validation?.ok;
      badge.textContent=this.currentValidated?'✓ البيانات مناسبة للفحص':'راجع البيانات والأعمدة';
      badge.style.background=this.currentValidated?'var(--status-match-bg)':'var(--status-mismatch-bg)';
      badge.style.color=this.currentValidated?'var(--status-match)':'var(--status-mismatch)';
      document.getElementById('docEstimatedRows').textContent=(result.validation?.rows || 0).toLocaleString()+' سجل';
    } catch(error) {
      if(revision!==this.validationRequest)return;
      badge.textContent='⚠ راجع الأعمدة والبيانات';badge.style.background='var(--status-mismatch-bg)';badge.style.color='var(--status-mismatch)';
      message.textContent=error.message;message.style.display='block';
    } finally {
      if(revision===this.validationRequest){
        document.getElementById('btnMapperStart').disabled=!this.currentValidated;
        document.getElementById('btnMapperQueue').disabled=!this.currentValidated;
      }
    }
  },

  async changeHeaderMode() {
    const toggle=document.getElementById('mapHasHeaders');
    const hasHeaders=toggle.checked;
    const revision=this.analysisRequest=(this.analysisRequest || 0)+1;
    this.validationRequest=(this.validationRequest || 0)+1;
    clearTimeout(this.validationTimer);
    this.analyzing=true;this.currentValidated=false;this.currentAnalysis=null;
    toggle.disabled=true;
    document.getElementById('btnMapperQueue').disabled=true;
    document.getElementById('btnMapperStart').disabled=true;
    document.getElementById('docAcceptBadge').textContent='جارٍ قراءة الصفوف…';
    try {
      const result=await API.inspectSheetColumns(document.getElementById('selectWorkbook').value,
        Number(document.getElementById('selectSheet').value || 0),hasHeaders);
      if(revision!==this.analysisRequest)return;
      if(typeof result.analysis?.has_headers!=='boolean')throw new Error('شغّل النسخة المحدّثة بعد انتهاء الجولة لتفعيل خيار الملفات بدون عناوين.');
      this.currentAnalysis=result.analysis;this.analyzing=false;
      this.renderAnalysis(result.analysis);await this.validateCurrent();
    } catch(error) {
      if(revision!==this.analysisRequest)return;
      const message=document.getElementById('mapValidationMessage');
      message.textContent=error.message;message.style.display='block';
      document.getElementById('docAcceptBadge').textContent='تعذر التحقق من الملف';
    } finally {
      if(revision===this.analysisRequest){this.analyzing=false;toggle.disabled=!this.currentAnalysis;}
    }
  },

  renderPreview() {
    const table=document.getElementById('mapperPreviewTable');
    if(!table)return;
    const analysis=this.currentAnalysis || {};
    const escape=v=>String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const number=document.getElementById('mapLookupCol')?.value;
    const amount=document.getElementById('mapRemainingCol')?.value;
    const columns=analysis.columns || [], rows=analysis.preview_rows || [];
    const style=col=>col===number?' class="preview-number"':col===amount?' class="preview-amount"':'';
    table.innerHTML='<thead><tr><th>صف</th>'+columns.map(col=>'<th'+style(col.col_letter)+'>'+escape(col.col_letter)+' · '+escape(col.header || 'بدون عنوان')+'</th>').join('')+'</tr></thead><tbody>'+rows.map(row=>'<tr><th>'+escape(row.row_number)+'</th>'+columns.map(col=>'<td'+style(col.col_letter)+'>'+escape(row.values[col.col_index-1])+'</td>').join('')+'</tr>').join('')+'</tbody>';
    document.getElementById('mapperPreviewHint').textContent=rows.length
      ? 'هذه عينة للمعاينة فقط؛ التحقق يراجع جميع الصفوف قبل الإضافة.'
      : 'المعاينة تحتاج تشغيل النسخة المحدّثة بعد انتهاء الجولة الحالية.';
  },

  async skipCurrentFile() {
    const filename=document.getElementById('selectWorkbook').value;
    this.closeModal();await App.reviewNextUploadedFile(filename);App.updateControlButtons();
  },

  renderAnalysis(analysis) {
    const toggle=document.getElementById('mapHasHeaders');
    toggle.checked=analysis.has_headers !== false;
    toggle.disabled=typeof analysis.has_headers!=='boolean';
    document.getElementById('docTypeLabel').textContent = analysis.document_type || 'شيت مديونيات زين';
    
    const badge = document.getElementById('docAcceptBadge');
    if (analysis.is_acceptable) {
      badge.textContent = analysis.verification_status || '✓ الشيت صالح ومقبول للتدقيق';
      badge.style.background = 'var(--status-match-bg)';
      badge.style.color = 'var(--status-match)';
    } else {
      badge.textContent = 'اختر عمود الرقم وعمود المبلغ';
      badge.style.background = 'var(--status-mismatch-bg)';
      badge.style.color = 'var(--status-mismatch)';
    }

    document.getElementById('docEstimatedRows').textContent = `${(analysis.estimated_rows || 0).toLocaleString()} سجل`;

    // Populate Dropdowns
    const columns = analysis.columns || [];
    const letters = analysis.letters || {};

    const defLookup = letters.lookup_col || letters.service_col || '';
    const defService = letters.service_col || '';
    const defAmt1 = letters.amount_col || letters.remaining_col || '';
    const defAmt2 = letters.amount_col_2 || letters.contract_col || defAmt1;
    const defCust = letters.customer_col || '';
    const defColl = letters.collector_col || '';

    this.populateSelect('mapLookupCol', columns, defLookup, true);
    this.populateSelect('mapServiceCol', columns, defService, true);
    this.populateSelect('mapRemainingCol', columns, defAmt1, true);
    this.populateSelect('mapContractCol', columns, defAmt2, true);
    this.populateSelect('mapCustomerCol', columns, defCust, true);
    this.populateSelect('mapCollectorCol', columns, defColl, true);

    // If it's an errors or results sheet, default to 'remaining' which maps to المبلغ بالشيت
    const targetValue = 'remaining';
    const advanced=document.getElementById('mapperAdvanced');
    const autoAdvanced=!!(letters.lookup_col && letters.service_col && letters.lookup_col!==letters.service_col);
    if(advanced.open!==autoAdvanced){this.skipAdvancedToggle=true;advanced.open=autoAdvanced;}
    const rRadio = document.querySelector(`input[name="amtTarget"][value="${targetValue}"]`);
    if (rRadio) rRadio.checked = true;
    this.renderPreview();
  },

  populateSelect(elementId, columns, defaultLetter, allowEmpty = false) {
    const sel = document.getElementById(elementId);
    if (!sel) return;
    sel.innerHTML = '';

    if (allowEmpty) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = '-- غير محدد / لا يوجد --';
      sel.appendChild(opt);
    }

    const defUpper = (defaultLetter || '').toUpperCase();
    columns.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.col_letter;
      opt.textContent = `[العمود ${c.col_letter}] ${c.header || 'بدون عنوان'}`;
      if (defUpper && c.col_letter.toUpperCase() === defUpper) {
        opt.selected = true;
      }
      sel.appendChild(opt);
    });
  },

  getMappingPayload() {
    const letters = this.currentAnalysis?.letters || {};
    const lookup = document.getElementById('mapLookupCol')?.value ?? letters.lookup_col ?? '';
    const service = document.getElementById('mapperAdvanced')?.open ? (document.getElementById('mapServiceCol')?.value ?? letters.service_col ?? '') : lookup;
    const remAmt = document.getElementById('mapRemainingCol')?.value ?? letters.amount_col ?? '';
    const contAmt = document.getElementById('mapContractCol')?.value ?? letters.amount_col_2 ?? '';
    const cust = document.getElementById('mapCustomerCol')?.value ?? letters.customer_col ?? '';
    const coll = document.getElementById('mapCollectorCol')?.value ?? letters.collector_col ?? '';
    const sourceRow = letters.source_row_col || '';
    const amtTarget = document.getElementById('mapperAdvanced')?.open ? (document.querySelector('input[name="amtTarget"]:checked')?.value || 'remaining') : 'remaining';

    return {
      lookup_col: lookup,
      service_col: service,
      amount_col: remAmt,
      amount_col_2: contAmt,
      customer_col: cust,
      collector_col: coll,
      source_row_col: sourceRow,
      has_headers:document.getElementById('mapHasHeaders')?.checked !== false,
      amount_target: amtTarget,
    };
  },


  async applyAndStartDirect() {
    if (!this.currentAnalysis || !this.currentValidated || this.starting) return;
    this.starting = true;
    document.getElementById('btnMapperStart').disabled = true;
    const mapping = this.getMappingPayload();
    const wb = document.getElementById('selectWorkbook').value;
    const sheetIdx = Number(document.getElementById('selectSheet').value || 0);
    const payload = {workbook:wb, sheet_index:sheetIdx, column_mapping:mapping,
      mode:document.getElementById('selectAuditMode').value,
      amount_target:mapping.amount_target, force_restart:false};
    try {
      const check = await API.checkSheetStatus(wb,sheetIdx);
      if (check.has_progress || check.is_completed) {
        this.closeModal();
        if (!await Dashboard.confirm({title:'يوجد تقدم محفوظ',
          text:'لهذا الشيت '+(check.completed_count || 0)+' سجل محفوظ. سنكمل السجلات المتبقية مع الاحتفاظ بالنتائج السابقة.',
          accept:'استئناف المحفوظ'})) return;
      }
      await API.startSession(payload);
      App.detectedResultsInfo = null;
      this.closeModal();await App.poll();LiveFeed.switchTab('all');
      Dashboard.toast('بدأت جلسة الفحص');
    } catch (error) {Dashboard.toast(error.message,'error');}
    finally {this.starting=false;document.getElementById('btnMapperStart').disabled=!this.currentValidated;}
  },
  async applyAndAddToQueue() {
    if (!this.currentAnalysis || !this.currentValidated || this.adding) return;
    this.adding=true;document.getElementById('btnMapperQueue').disabled=true;
    const mapping=this.getMappingPayload();
    const payload={workbook:document.getElementById('selectWorkbook').value,
      sheet_index:Number(document.getElementById('selectSheet').value || 0),
      sheet_name:this.currentAnalysis.sheet_name || 'Sheet1',
      column_mapping:mapping, amount_target:mapping.amount_target,
      mode:document.getElementById('selectAuditMode').value,
      total_records:this.currentAnalysis.estimated_rows || 0};
    try {
      await API.addQueueJob(payload);
      this.closeModal();LiveFeed.switchTab('queue');
      await QueueUI.refresh();App.updateControlButtons();
      await App.reviewNextUploadedFile(payload.workbook);
      Dashboard.toast('أضيف الشيت إلى الطابور');
    } catch(error) {Dashboard.toast(error.message,'error');}
    finally {this.adding=false;document.getElementById('btnMapperQueue').disabled=!this.currentValidated;}
  }
};
