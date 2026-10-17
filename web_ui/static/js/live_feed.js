/**
 * Live Feed and Table Monitor for Smart Zain Checker
 */
const LiveFeed = {
  activeTab: 'all',

  init() {
    ResultBrowser.init();
    WorkerMonitor.init();
    document.querySelectorAll('.tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const tab = btn.dataset.tab;
        this.switchTab(tab);
      });
      btn.addEventListener('keydown', event => {
        if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
        const tabs=[...document.querySelectorAll('.tab-btn')];
        const index=tabs.indexOf(btn);
        const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowLeft'?1:-1)+tabs.length)%tabs.length;
        event.preventDefault();tabs[next].focus();this.switchTab(tabs[next].dataset.tab);
      });
    });
  },

  switchTab(tab) {
    this.activeTab = tab;
    document.querySelectorAll('.tab-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.tab === tab);
      b.setAttribute('aria-selected', String(b.dataset.tab === tab));
    });

    const isQueue = tab === 'queue';
    document.getElementById('regularTableContainer').style.display = isQueue ? 'none' : 'block';
    document.getElementById('queueTableContainer').style.display = isQueue ? 'block' : 'none';
    document.getElementById('resultFilters').hidden=isQueue;
    document.querySelector('.results-shortcut').hidden=isQueue;

    // Update section label in table toolbar
    const labelEl = document.getElementById('tableTabLabel');
    if (labelEl) {
      const titles = {
        all: 'نتائج الشيت الحالي',
        match: 'المطابقات التامة',
        mismatch: 'حالات فروقات الرصيد',
        error: 'المهلات والأخطاء',
        review: 'حالات المراجعة / غير موجودة'
      };
      labelEl.textContent = titles[tab] || '📋 جدول نتائج الشيت';
    }

    if (isQueue) {
      QueueUI.refresh();
    } else {
      this.filterTable();
    }
  },

  updateKPIs(kpis) {
    if (!kpis) return;
    document.getElementById('kpiTotal').textContent = (kpis.total || 0).toLocaleString();
    document.getElementById('kpiDone').textContent = (kpis.completed || 0).toLocaleString();
    document.getElementById('kpiRemaining').textContent = (kpis.remaining || 0).toLocaleString();
    document.getElementById('kpiMatch').textContent = (kpis.matches || 0).toLocaleString();
    document.getElementById('kpiMismatch').textContent = `${(kpis.mismatch_total || 0).toLocaleString(undefined, { minimumFractionDigits: 2 })} ر.س`;
    document.getElementById('kpiError').textContent = (kpis.errors || 0).toLocaleString();
    document.getElementById('kpiVerified').textContent = (kpis.verified || 0).toLocaleString();
    const reviewCount = (kpis.needs_review || 0) + (kpis.not_found || 0);
    document.getElementById('kpiReview').textContent = reviewCount.toLocaleString();
    document.getElementById('kpiDeferred').textContent = (kpis.deferred || 0).toLocaleString();
    document.getElementById('retryNotice').textContent = kpis.deferred
      ? `أقرب موعد لمحاولة مؤجلة: ${kpis.next_retry_seconds || 0} ثانية` : '';

    // Tab count badges
    document.getElementById('countAll').textContent = (kpis.completed || 0);
    document.getElementById('countMatch').textContent = (kpis.matches || 0);
    document.getElementById('countMismatch').textContent = (kpis.mismatches || 0);
    document.getElementById('countError').textContent = (kpis.errors || 0);
    document.getElementById('countReview').textContent = reviewCount;

    // Repair Errors Controls
    const totalRepairable = (kpis.errors || 0) + (kpis.needs_review || 0);
    const previewRepairable = (typeof App !== 'undefined' && App.detectedResultsInfo?.error_count) || 0;
    const effectiveRepairCount = totalRepairable || previewRepairable;

    const repairBadge = document.getElementById('repairErrorsBadge');
    if (repairBadge) repairBadge.textContent = effectiveRepairCount;

    const btnRepair = document.getElementById('btnRepairErrors');
    if (btnRepair) {
      if (effectiveRepairCount > 0 || this.activeTab === 'error' || this.activeTab === 'review') {
        btnRepair.style.display = 'inline-flex';
        btnRepair.innerHTML = `<span>🛠️</span><span>صيانة الأخطاء</span> <span id="repairErrorsBadge" class="badge" style="background: rgba(0,0,0,0.25); color: #fff; font-size: 11px;">${effectiveRepairCount}</span>`;
      } else {
        btnRepair.style.display = 'none';
      }
    }

    const btnKpiRepair = document.getElementById('btnKpiRepair');
    if (btnKpiRepair) {
      btnKpiRepair.style.display = (effectiveRepairCount > 0) ? 'block' : 'none';
    }

    ResultBrowser.syncPagination();
  },

  updateWorkerPills(workers) {WorkerMonitor.update(workers);},

  renderTable(rows, preview=false) {ResultBrowser.renderTable(rows,preview);},
  filterTable() {ResultBrowser.filterTable();}
};
