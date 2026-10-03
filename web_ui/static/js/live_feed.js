/**
 * Live Feed and Table Monitor for Smart Zain Checker
 */
const LiveFeed = {
  activeTab: 'all',

  init() {
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

    const tableBadge = document.getElementById('tableTabBadge');
    if (tableBadge) {
      const counts = {
        all: kpis.completed || 0,
        match: kpis.matches || 0,
        mismatch: kpis.mismatches || 0,
        error: kpis.errors || 0,
        review: reviewCount,
      };
      tableBadge.textContent = `${counts[this.activeTab] || 0} سجل`;
    }
  },

  updateWorkerPills(workers) {
    const group = document.getElementById('workerPillGroup');
    if (!group) return;
    group.innerHTML = '';
    if (!workers || workers.length === 0) {
      group.textContent = 'لا توجد قنوات فحص مفعّلة';return;
    }
    workers.forEach(w => {
      const pill = document.createElement('div');
      pill.className = 'worker-pill';

      const dotClass = w.status === 'processing' || w.status === 'ready' ? 'active' : (w.in_cooldown ? 'cooldown' : '');
      const badgeText = w.waiting_for_shared_service_browser ? 'ينتظر الاتصال المشترك' :
        (w.in_cooldown ? `تبريد (${w.cooldown_remaining_seconds}s)` :
        (w.status === 'processing' ? 'فحص نشط' : ({ready:'جاهز',stopped:'متوقف',error:'تعذر التشغيل',idle:'بانتظار الفحص'}[w.status] || w.status)));
      if (w.waiting_for_shared_service_browser) {
        pill.title = 'عامل آخر يفحص رقم خدمة عبر نفس الاتصال؛ يبدأ هذا العامل بعد انتهاء الفحص وفاصل الانتظار.';
      }

      pill.innerHTML = `
        <span class="status-dot ${dotClass}"></span>
        <span class="worker-name" dir="auto">${Dashboard.escape(w.name)}</span>
        <span class="worker-state">${Dashboard.escape(badgeText)}</span>
      `;
      group.appendChild(pill);
    });
  },

  renderTable(rows, preview = false) {
    const tbody = document.getElementById('auditTableBody');
    if (!tbody) return;

    if (!rows || rows.length === 0) {
      if(!preview && (App.isRunning || Dashboard.live?.round_pending)){
        tbody.innerHTML='<tr><td colspan="9"><div class="empty-state">'+Dashboard.icon('file')+'<strong>النتائج بعد اكتمال الجولة</strong><p>تابع التقدم من أعلى الصفحة. تُفحص الملفات بالتتابع ويمكنك إيقاف الفحص مؤقتًا.</p></div></td></tr>';
        this.filterTable();return;
      }
      tbody.innerHTML = '<tr><td colspan="7"><div class="empty-state">'+Dashboard.icon('file')+'<strong>'+(preview?'لا توجد سجلات للمعاينة':'كل نتيجة تبدأ من ملف')+'</strong><p>'+(preview?'يمكنك تحميل الملف أو اختيار ورقة أخرى.':'اختر شيت العملاء، راجع الأعمدة، ثم ابدأ الفحص.')+'</p></div></td></tr>';
      this.filterTable();
      return;
    }

    tbody.innerHTML = '';
    rows.forEach(r => {
      const tr = document.createElement('tr');
      tr.className = `row-${r.status}`;
      tr.dataset.status = r.status;

      let badgeHtml = '';
      let actionHtml = '';
      if (r.status === 'match') {
        badgeHtml = '<span class="badge badge-match">مطابقة ✔</span>';
      } else if (r.status === 'mismatch') {
        badgeHtml = '<span class="badge badge-mismatch">فرق رصيد ⚠</span>';
      } else if (r.status === 'not_found') {
        badgeHtml = '<span class="badge">غير موجود</span>';
      } else if (r.status === 'needs_review') {
        badgeHtml = '<span class="badge">تحتاج مراجعة</span>';
        actionHtml = `<button class="btn-repair-row" onclick="App.repairSingleRow(${r.row})" title="صيانة وإعادة فحص هذا السطر الآن">🛠️ صيانة</button>`;
      } else {
        badgeHtml = '<span class="badge badge-error">خطأ ✖</span>';
        actionHtml = `<button class="btn-repair-row" onclick="App.repairSingleRow(${r.row})" title="صيانة وإعادة فحص هذا السطر الآن">🛠️ صيانة</button>`;
      }

      const recType = (r.record_type || r.type) === 'wallet' ? 'خدمة' : 'عقد';
      const searchNum = r.lookup_number || r.number || '—';
      const custName = r.customer_name || r.name || 'عميل غير محدد';
      const expAmt = Number(r.expected_amount != null ? r.expected_amount : (r.expected_sar != null ? r.expected_sar : 0));
      const liveAmt = (r.live_amount != null ? Number(r.live_amount) : (r.live_sar != null ? Number(r.live_sar) : null));

      tr.innerHTML = `
        <td><span class="mono">${Dashboard.escape(r.row)}</span></td>
        <td>${recType}</td>
        <td><strong class="mono">${Dashboard.escape(searchNum)}</strong></td>
        <td>${Dashboard.escape(custName)}</td>
        <td><span class="mono">${Number.isFinite(expAmt) ? expAmt.toFixed(2) : '—'}</span> ر.س</td>
        <td><span class="mono" style="${r.status === 'mismatch' ? 'color: var(--status-mismatch); font-weight: 800;' : ''}">${liveAmt == null ? '—' : liveAmt.toFixed(2)}</span>${liveAmt == null ? '' : ' ر.س'}</td>
        <td>
          <div style="display: flex; gap: 6px; align-items: center;">
            ${badgeHtml}
            ${actionHtml}
          </div>
        </td>
      `;
      tbody.appendChild(tr);
    });

    this.filterTable();
  },

  filterTable() {
    const tab = this.activeTab;
    const query = (document.getElementById('resultSearch')?.value || '').trim().toLocaleLowerCase();
    let visibleCount = 0;
    document.querySelectorAll('#auditTableBody tr').forEach(tr => {
      if (!tr.dataset.status) return;
      let show = false;
      if (tab === 'all') show = true;
      else if (tab === 'review' && ['needs_review', 'not_found'].includes(tr.dataset.status)) show = true;
      else if (tr.dataset.status === tab) show = true;
      if (show && query && !tr.textContent.toLocaleLowerCase().includes(query)) show = false;
      tr.style.display = show ? '' : 'none';
      if (show) visibleCount++;
    });
    const totalRows = document.querySelectorAll('#auditTableBody tr[data-status]').length;
    let empty=document.getElementById('resultFilterEmpty');
    if (totalRows && !empty) {
      empty=document.createElement('tr');empty.id='resultFilterEmpty';
      empty.innerHTML='<td colspan="7"><div class="empty-state"><strong>لا توجد نتائج مطابقة</strong><p>غيّر عبارة البحث أو فلتر النتائج.</p></div></td>';
      document.getElementById('auditTableBody').appendChild(empty);
    }
    if (empty) empty.style.display=visibleCount===0?'':'none';
    document.getElementById('visibleResultsLabel').textContent = totalRows ? `يعرض ${visibleCount} من ${totalRows} نتيجة محمّلة` : 'لا توجد نتائج معروضة';

    const tableBadge = document.getElementById('tableTabBadge');
    if (tableBadge && tab !== 'all') {
      tableBadge.textContent = `${visibleCount} سجل معروض`;
    }
  }
};
