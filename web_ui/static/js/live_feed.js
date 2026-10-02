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
    });
  },

  switchTab(tab) {
    this.activeTab = tab;
    document.querySelectorAll('.tab-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.tab === tab);
    });

    const isQueue = tab === 'queue';
    document.getElementById('regularTableContainer').style.display = isQueue ? 'none' : 'block';
    document.getElementById('queueTableContainer').style.display = isQueue ? 'block' : 'none';

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

    // Tab count badges
    document.getElementById('countAll').textContent = (kpis.completed || 0);
    document.getElementById('countMatch').textContent = (kpis.matches || 0);
    document.getElementById('countMismatch').textContent = (kpis.mismatches || 0);
    document.getElementById('countError').textContent = (kpis.errors || 0);
  },

  updateWorkerPills(workers) {
    if (!workers || workers.length === 0) return;
    const group = document.getElementById('workerPillGroup');
    if (!group) return;

    group.innerHTML = '';
    workers.forEach(w => {
      const pill = document.createElement('div');
      pill.className = 'worker-pill';

      const dotClass = w.status === 'processing' || w.status === 'ready' ? 'active' : (w.in_cooldown ? 'cooldown' : '');
      const badgeText = w.in_cooldown ? `تبريد (${w.cooldown_remaining_seconds}s)` : (w.status === 'processing' ? 'فحص نشط' : w.status);

      pill.innerHTML = `
        <span class="status-dot ${dotClass}"></span>
        <span>${w.name}</span>
        <span style="font-size: 10px; color: var(--ink-muted);">[${badgeText}]</span>
      `;
      group.appendChild(pill);
    });
  },

  renderTable(rows) {
    const tbody = document.getElementById('auditTableBody');
    if (!tbody) return;

    if (!rows || rows.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--ink-faint); padding: 30px;">لا توجد سجلات مفحوصة حالياً. اضغط "بدء الفحص" للمتابعة.</td></tr>';
      return;
    }

    tbody.innerHTML = '';
    rows.forEach(r => {
      const tr = document.createElement('tr');
      tr.className = `row-${r.status}`;
      tr.dataset.status = r.status;

      let badgeHtml = '';
      if (r.status === 'match') badgeHtml = '<span class="badge badge-match">مطابقة ✔</span>';
      else if (r.status === 'mismatch') badgeHtml = '<span class="badge badge-mismatch">فرق رصيد ⚠</span>';
      else badgeHtml = '<span class="badge badge-error">خطأ ✖</span>';

      tr.innerHTML = `
        <td><span class="mono">${r.row}</span></td>
        <td>${r.record_type === 'wallet' ? 'محفظة' : 'حساب'}</td>
        <td><strong class="mono">${r.lookup_number}</strong></td>
        <td>${r.customer_name || 'عميل غير محدد'}</td>
        <td><span class="mono">${(r.expected_amount || 0).toFixed(2)}</span> ر.س</td>
        <td><span class="mono" style="${r.status === 'mismatch' ? 'color: var(--status-mismatch); font-weight: 800;' : ''}">${(r.live_amount || 0).toFixed(2)}</span> ر.س</td>
        <td>${badgeHtml}</td>
      `;
      tbody.appendChild(tr);
    });

    this.filterTable();
  },

  filterTable() {
    const tab = this.activeTab;
    document.querySelectorAll('#auditTableBody tr').forEach(tr => {
      if (!tr.dataset.status) return;
      if (tab === 'all') tr.style.display = '';
      else if (tr.dataset.status === tab) tr.style.display = '';
      else tr.style.display = 'none';
    });
  }
};
