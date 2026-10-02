/**
 * Queue UI Controller for Smart Zain Checker
 */
const QueueUI = {
  async refresh() {
    try {
      const res = await API.getQueue();
      if (res.status === 'ok') {
        this.renderQueueTable(res.jobs || []);
      }
    } catch (e) {
      console.warn('Failed to load queue:', e);
    }
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
      else if (job.status === 'completed') statusBadge = '<span class="badge" style="background:#065f46; color:#a7f3d0;">مكتمل ✔</span>';
      else statusBadge = '<span class="badge" style="background:#334155; color:#94a3b8;">قيد الانتظار ⏳</span>';

      tr.innerHTML = `
        <td><strong class="mono">#${idx + 1}</strong></td>
        <td><strong>${job.filename}</strong></td>
        <td>${job.sheet_name}</td>
        <td><span class="mono">${(job.total_records || 0).toLocaleString()}</span> سجل</td>
        <td>${statusBadge}</td>
        <td>
          <span class="mono" style="color:var(--status-match);">${job.matches || 0}</span> مطابقة | 
          <span class="mono" style="color:var(--status-mismatch);">${job.mismatches || 0}</span> فرق
        </td>
        <td style="white-space: nowrap;">
          ${job.status === 'completed' ? `<a href="/api/download-results?job_id=${job.id}" class="btn btn-primary" style="padding: 4px 8px; font-size: 11px; text-decoration: none; display: inline-flex; align-items: center; gap: 4px; background: #059669; margin-left: 4px;" download>⬇️ تحميل النتائج</a>` : ''}
          ${job.status === 'completed' ? `<button class="btn btn-secondary" style="padding: 4px 8px; font-size: 11px; color: #fbbf24; border-color: rgba(245, 158, 11, 0.4); margin-left: 4px;" onclick="QueueUI.promptRestartJob('${job.id}')">🔄 إعادة الفحص من الصفر</button>` : ''}
          <button class="btn btn-secondary" style="padding: 4px 8px; font-size: 11px;" onclick="QueueUI.removeJob('${job.id}')">حذف ✖</button>
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
          tabBtn.innerHTML = `📋 طابور الشيتات (${completedCount}/${jobs.length} مكتمل ⚡)`;
        } else if (completedCount === jobs.length) {
          tabBtn.innerHTML = `📋 طابور الشيتات (جميعها مكتملة ✔)`;
        } else {
          tabBtn.innerHTML = `📋 طابور الشيتات (${completedCount}/${jobs.length} مكتمل)`;
        }
      } else {
        tabBtn.innerHTML = `📋 طابور الشيتات المتسلسل`;
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
      btn.innerHTML = '⚡ الطابور يعمل حالياً...';
      btn.className = 'btn btn-secondary';
      btn.disabled = true;
    } else if (allCompleted) {
      btn.innerHTML = '🔄 اضغط لإعادة فحص شهد من الصفر';
      btn.className = 'btn btn-primary';
      btn.disabled = false;
      btn.onclick = () => {
        const shahd = (jobs || []).find(j => j.filename.includes('شهد'));
        if (shahd) {
          QueueUI.promptRestartJob(shahd.id);
        } else if (jobs && jobs.length > 0) {
          QueueUI.promptRestartJob(jobs[0].id);
        }
      };
    } else if (hasPending) {
      btn.innerHTML = '▶️ بدء تشغيل الطابور المتسلسل';
      btn.className = 'btn btn-primary';
      btn.disabled = false;
      btn.onclick = () => QueueUI.startQueue();
    } else {
      btn.innerHTML = '▶️ بدء تشغيل الطابور';
      btn.className = 'btn btn-secondary';
      btn.disabled = true;
    }
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
            alert(res.message || 'تعذر إعادة بدء الفحص');
          }
        } catch (err) {
          alert('خطأ أثناء إعادة تعيين الشيت: ' + err.message);
        }
      }
    });
  },

  async removeJob(jobId) {
    if (!confirm('هل تريد حذف هذه الورقة من الطابور؟')) return;
    try {
      await API.removeQueueJob(jobId);
      this.refresh();
    } catch (e) {
      alert('تعذر حذف الشيت: ' + e.message);
    }
  },

  async startQueue() {
    const btn = document.getElementById('btnStartQueue');
    const origText = btn ? btn.innerHTML : '▶️ بدء تشغيل الطابور';
    if (btn) btn.innerHTML = '⏳ جاري البدء...';
    try {
      const res = await API.startQueue();
      if (res.status === 'ok') {
        this.refresh();
        App.poll();
        LiveFeed.switchTab('all');
      } else {
        alert(res.message || 'تعذر تشغيل الطابور');
      }
    } catch (e) {
      alert('خطأ أثناء تشغيل الطابور: ' + e.message);
    } finally {
      if (btn) btn.innerHTML = origText;
    }
  }
};
