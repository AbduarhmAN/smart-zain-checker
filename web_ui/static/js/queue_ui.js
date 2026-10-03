/**
 * Queue UI Controller for Smart Zain Checker
 */
const QueueUI = {
  async refresh() {
    if (this.refreshing) return;
    this.refreshing=true;
    try {
      const res = await API.getQueue();
      if (res.status === 'ok') {
        this.renderQueueTable(res.jobs || []);
      }
    } catch (e) {
      console.warn('Failed to load queue:', e);
    } finally {this.refreshing=false;}
  },

  renderQueueTable(jobs) {
    this.lastJobs = jobs || [];
    const tbody = document.getElementById('queueTableBody');
    if (!tbody) return;

    if (!jobs || jobs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" style="text-align: center; color: var(--ink-faint); padding: 30px;">الطابور فارغ حالياً. افتح نافذة "فحص وتعيين الأعمدة" لإضافة أوراق العمل هنا.</td></tr>';
      return;
    }

    tbody.innerHTML = '';
    jobs.forEach((job, idx) => {
      const tr = document.createElement('tr');
      let statusBadge = '';
      if (job.status === 'active') statusBadge = '<span class="badge badge-match">جارٍ الفحص الآن ⚡</span>';
      else if (job.status === 'completed') statusBadge = '<span class="badge badge-match">انتهت المعالجة</span>';
      else statusBadge = '<span class="badge">قيد الانتظار</span>';

      tr.innerHTML = `
        <td><strong class="mono">#${idx + 1}</strong></td>
        <td><strong>${Dashboard.escape(job.filename)}</strong></td>
        <td>${Dashboard.escape(job.sheet_name)}</td>
        <td><span class="mono">${(job.total_records || 0).toLocaleString()}</span> سجل</td>
        <td>${statusBadge}</td>
        <td>
          <span class="mono" style="color:var(--status-match);">${job.matches || 0}</span> مطابقة | 
          <span class="mono" style="color:var(--status-mismatch);">${job.mismatches || 0}</span> فرق
        </td>
        <td style="white-space: nowrap;">
          ${job.status === 'completed' && jobs.every(j=>j.status==='completed') ? `<a href="/api/download-results?job_id=${job.id}" class="btn btn-primary" style="padding: 4px 8px; font-size: 11px; text-decoration: none; display: inline-flex; align-items: center; gap: 4px; background: #059669; margin-left: 4px;" download>⬇️ تحميل النتائج</a>` : ''}
          <button class="btn btn-secondary" style="padding: 4px 8px; font-size: 11px;" onclick="QueueUI.removeJob('${job.id}')">إزالة من الطابور</button>
        </td>
      `;
      tbody.appendChild(tr);
    });

    this.updateTabBadge(jobs);
    this.updateQueueControls(jobs);
  },

  updateTabBadge(jobs) {
    const completedCount = jobs.filter(j => j.status === 'completed').length;
    const activeCount = jobs.filter(j => j.status === 'active').length;
    const tabBtn = document.querySelector('button[data-tab="queue"]');
    if (tabBtn) {
      if (jobs.length > 0) {
        if (activeCount > 0) {
          tabBtn.innerHTML = `الطابور <span class="tab-count">${completedCount}/${jobs.length}</span>`;
        } else if (completedCount === jobs.length) {
          tabBtn.innerHTML = `الطابور <span class="tab-count">${jobs.length}</span>`;
        } else {
          tabBtn.innerHTML = `الطابور <span class="tab-count">${completedCount}/${jobs.length}</span>`;
        }
      } else {
        tabBtn.innerHTML = 'الطابور';
      }
    }
  },

  updateQueueControls(jobs) {
    const btn = document.getElementById('btnStartQueue');
    if (!btn) return;
    const hasActive = jobs.some(j => j.status === 'active');
    const hasPending = jobs.some(j => j.status === 'pending');
    const allCompleted = jobs.length > 0 && jobs.every(j => j.status === 'completed');

    if (hasActive) {
      btn.textContent = 'الطابور يعمل الآن';
      btn.className = 'btn btn-secondary';
      btn.disabled = true;
    } else if (allCompleted) {
      btn.textContent = 'انتهت معالجة الطابور';
      btn.className = 'btn btn-secondary';
      btn.disabled = true;
    } else if (hasPending) {
      btn.innerHTML = Dashboard.icon('arrow')+' بدء معالجة الطابور';
      btn.className = 'btn btn-primary';
      btn.disabled = false;
      btn.onclick = () => QueueUI.startQueue();
    } else {
      btn.textContent = 'بدء معالجة الطابور';
      btn.className = 'btn btn-secondary';
      btn.disabled = true;
    }
  },

  promptRestartAllJobs() {
    RestartModal.show({
      filename: 'كافة أوراق العمل في الطابور',
      sheetName: `${(this.lastJobs || []).length} شيتات`,
      statsText: `الحالة: جميع الشيتات مكتملة الفحص ✔ | سيتم تصفير التقدم ونقاط الاستعادة والبدء من أول شيت ومن أول سجل.`,
      onConfirm: async () => {
        try {
          const res = await API.restartAllQueueJobs(true);
          if (res.status === 'ok') {
            await this.refresh();
            App.poll();
            LiveFeed.switchTab('all');
          } else {
            Dashboard.toast(res.message || 'تعذر إعادة بدء الطابور');
          }
        } catch (err) {
          Dashboard.toast('خطأ أثناء إعادة تعيين الطابور: ' + err.message);
        }
      }
    });
  },

  promptRestartJob(jobId) {
    const job = (this.lastJobs || []).find(j => j.id === jobId);
    if (!job) return;

    RestartModal.show({
      filename: job.filename,
      sheetName: job.sheet_name,
      statsText: `الحالة: مكتملة بالكامل ✔ | تم فحص ${(job.completed || job.total_records).toLocaleString()} سجل (${job.matches || 0} مطابقة | ${job.mismatches || 0} فرق)`,
      onConfirm: async () => {
        try {
          const res = await API.restartQueueJob({ job_id: jobId, auto_start: true });
          if (res.status === 'ok') {
            await this.refresh();
            App.poll();
            LiveFeed.switchTab('all');
          } else {
            Dashboard.toast(res.message || 'تعذر إعادة بدء الفحص');
          }
        } catch (err) {
          Dashboard.toast('خطأ أثناء إعادة تعيين الشيت: ' + err.message);
        }
      }
    });
  },

  async removeJob(jobId) {
    if (!await Dashboard.confirm({title:'إزالة من الطابور',text:'هل تريد إزالة هذه الورقة من قائمة الانتظار؟',accept:'إزالة الورقة'})) return;
    try {
      await API.removeQueueJob(jobId);
      this.refresh();
    } catch (e) {
      Dashboard.toast('تعذر حذف الشيت: ' + e.message);
    }
  },

  async startQueue() {
    if (this.starting || App.isRunning) return;
    this.starting = true;
    App.updateControlButtons();
    const btn = document.getElementById('btnStartQueue');
    const origText = btn ? btn.innerHTML : 'بدء معالجة الطابور';
    if (btn) btn.innerHTML = '⏳ جاري البدء...';
    try {
      const res = await API.startQueue();
      if (res.status === 'ok') {
        this.refresh();
        App.poll();
        LiveFeed.switchTab('all');
      } else {
        Dashboard.toast(res.message || 'تعذر تشغيل الطابور');
      }
    } catch (e) {
      Dashboard.toast('خطأ أثناء تشغيل الطابور: ' + e.message);
    } finally {
      this.starting = false;
      if (btn) btn.innerHTML = origText;
      App.updateControlButtons();
    }
  }
};
