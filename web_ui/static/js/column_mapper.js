/**
 * Column Mapper Modal Controller for Smart Zain Checker
 */
const ColumnMapper = {
  currentAnalysis: null,

  async openModal() {
    const wb = document.getElementById('selectWorkbook')?.value || '2.xlsx';
    const sheetIdx = parseInt(document.getElementById('selectSheet')?.value || '0', 10);
    
    document.getElementById('modalOverlay').style.display = 'flex';
    document.getElementById('docTypeLabel').textContent = 'جاري تحليل الصف الأول...';
    document.getElementById('docAcceptBadge').textContent = 'قيد الفحص';

    try {
      const res = await API.inspectSheetColumns(wb, sheetIdx);
      if (res.status === 'ok') {
        this.currentAnalysis = res.analysis;
        this.renderAnalysis(res.analysis);
      } else {
        alert('تعذر فحص الشيت: ' + res.message);
      }
    } catch (e) {
      alert('خطأ أثناء الاتصال بالخادم: ' + e.message);
    }
  },

  closeModal() {
    document.getElementById('modalOverlay').style.display = 'none';
  },

  renderAnalysis(analysis) {
    document.getElementById('docTypeLabel').textContent = analysis.document_type || 'شيت مديونيات زين';
    
    const badge = document.getElementById('docAcceptBadge');
    if (analysis.is_acceptable) {
      badge.textContent = '✓ الشيت صالح ومقبول للتدقيق';
      badge.style.background = 'var(--status-match-bg)';
      badge.style.color = 'var(--status-match)';
    } else {
      badge.textContent = '⚠️ الشيت غير مكتمل';
      badge.style.background = 'var(--status-mismatch-bg)';
      badge.style.color = 'var(--status-mismatch)';
    }

    document.getElementById('docEstimatedRows').textContent = `${(analysis.estimated_rows || 0).toLocaleString()} سجل`;

    // Populate Dropdowns
    const columns = analysis.columns || [];
    const letters = analysis.letters || {};

    this.populateSelect('mapLookupCol', columns, letters.lookup_col || 'L');
    this.populateSelect('mapServiceCol', columns, letters.service_col || 'AR');
    this.populateSelect('mapRemainingCol', columns, letters.remaining_col || letters.amount_col || 'P');
    this.populateSelect('mapContractCol', columns, letters.contract_col || letters.amount_col_2 || 'AW');
    this.populateSelect('mapCustomerCol', columns, letters.customer_col || 'G', true);
    this.populateSelect('mapCollectorCol', columns, letters.collector_col || 'V', true);

    // Default amount target selection: 'contract' is prioritized
    const rContract = document.querySelector('input[name="amtTarget"][value="contract"]');
    if (rContract) rContract.checked = true;
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

    columns.forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.col_letter;
      opt.textContent = `[العمود ${c.col_letter}] ${c.header}`;
      if (c.col_letter.toUpperCase() === defaultLetter.toUpperCase()) {
        opt.selected = true;
      }
      sel.appendChild(opt);
    });
  },

  getMappingPayload() {
    const lookup = document.getElementById('mapLookupCol')?.value || 'L';
    const service = document.getElementById('mapServiceCol')?.value || 'AR';
    const remAmt = document.getElementById('mapRemainingCol')?.value || 'P';
    const contAmt = document.getElementById('mapContractCol')?.value || 'AW';
    const cust = document.getElementById('mapCustomerCol')?.value || '';
    const coll = document.getElementById('mapCollectorCol')?.value || '';
    const amtTarget = document.querySelector('input[name="amtTarget"]:checked')?.value || 'contract';

    return {
      lookup_col: lookup,
      service_col: service,
      amount_col: remAmt,
      amount_col_2: contAmt,
      customer_col: cust,
      collector_col: coll,
      amount_target: amtTarget,
    };
  },

  async applyAndStartDirect() {
    const mapping = this.getMappingPayload();
    const wb = document.getElementById('selectWorkbook')?.value || '2.xlsx';
    const sheetIdx = parseInt(document.getElementById('selectSheet')?.value || '0', 10);
    const auditMode = document.getElementById('selectAuditMode')?.value || 'smart_hybrid';

    const payload = {
      workbook: wb,
      sheet_index: sheetIdx,
      column_mapping: mapping,
      mode: auditMode,
      amount_target: mapping.amount_target,
    };

    try {
      const check = await API.checkSheetStatus(wb, sheetIdx);
      if (check.is_completed || check.has_progress) {
        this.closeModal();
        const matchesText = check.matches ? `${check.matches} مطابقة | ` : '';
        const mismatchesText = check.mismatches ? `${check.mismatches} فرق` : '';
        const detailStats = (check.matches || check.mismatches) ? ` (${matchesText}${mismatchesText})` : '';

        RestartModal.show({
          filename: wb,
          sheetName: check.sheet_name || `ورقة ${sheetIdx + 1}`,
          statsText: `الحالة: ${check.is_completed ? 'مكتملة بالكامل ✔' : 'يوجد تقدم محفوظ سابقاً ⏳'} | المفحوص: ${check.completed_count.toLocaleString()} سجل${detailStats}`,
          onConfirm: async () => {
            payload.force_restart = true;
            try {
              const res = await API.startSession(payload);
              App.poll();
              LiveFeed.switchTab('all');
            } catch (err) {
              alert('تعذر بدء الفحص: ' + err.message);
            }
          }
        });
        return;
      }
    } catch (e) {
      console.warn('Check sheet status error:', e);
    }

    try {
      const res = await API.startSession(payload);
      this.closeModal();
      App.poll();
    } catch (e) {
      alert('تعذر بدء الفحص: ' + e.message);
    }
  },

  async applyAndAddToQueue() {
    const mapping = this.getMappingPayload();
    const wb = document.getElementById('selectWorkbook')?.value || '2.xlsx';
    const sheetIdx = parseInt(document.getElementById('selectSheet')?.value || '0', 10);
    const estRows = this.currentAnalysis?.estimated_rows || 0;
    const auditMode = document.getElementById('selectAuditMode')?.value || 'smart_hybrid';

    const payload = {
      workbook: wb,
      sheet_index: sheetIdx,
      sheet_name: this.currentAnalysis?.sheet_name || `ورقة ${sheetIdx+1}`,
      column_mapping: mapping,
      mode: auditMode,
      amount_target: mapping.amount_target,
      total_records: estRows,
    };

    try {
      const check = await API.checkSheetStatus(wb, sheetIdx);
      if (check.is_completed || check.has_progress) {
        this.closeModal();
        RestartModal.show({
          filename: wb,
          sheetName: check.sheet_name || payload.sheet_name,
          statsText: `الحالة: ${check.is_completed ? 'مكتملة بالكامل ✔' : 'يوجد تقدم محفوظ سابقاً ⏳'} | المفحوص: ${check.completed_count.toLocaleString()} سجل`,
          onConfirm: async () => {
            try {
              if (check.job_id) {
                await API.restartQueueJob({ job_id: check.job_id, auto_start: false });
              } else {
                await API.addQueueJob(payload);
              }
              LiveFeed.switchTab('queue');
            } catch (err) {
              alert('خطأ أثناء إضافة الشيت للطابور: ' + err.message);
            }
          }
        });
        return;
      }
    } catch (e) {
      console.warn('Check sheet status error:', e);
    }

    try {
      const res = await API.addQueueJob(payload);
      this.closeModal();
      LiveFeed.switchTab('queue');
    } catch (e) {
      alert('تعذر إضافة الشيت إلى الطابور: ' + e.message);
    }
  }
};
