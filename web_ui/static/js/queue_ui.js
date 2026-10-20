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
    if(typeof Pricing !== 'undefined') Pricing.onJobs(this.lastJobs);
    this.renderRecovery(jobs || []);
    const tbody = document.getElementById('queueTableBody');
    if (!tbody) return;

    if (!jobs || jobs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8"><div class="empty-state"><strong>أضف أول ملف للجولة</strong><p>اختر الملف من الجانب، ثم راجع الأعمدة واضغط «أضف إلى الجولة».</p></div></td></tr>';
      this.updateTabBadge([]);this.updateQueueControls([]);
      return;
    }

    tbody.innerHTML = '';
    jobs.forEach((job, idx) => {
      const tr = document.createElement('tr');
      let statusBadge = '';
      if (job.status === 'active') statusBadge = '<span class="badge badge-match">جارٍ الفحص</span>';
      else if (job.status === 'completed') statusBadge = '<span class="badge badge-match">انتهت المعالجة</span>';
      else if (job.status === 'failed') statusBadge = '<span class="badge badge-error">يحتاج متابعة</span>';
      else if (job.status === 'archived') statusBadge = '<span class="badge">جولة سابقة محفوظة</span>';
      else statusBadge = '<span class="badge">قيد الانتظار</span>';

      tr.innerHTML = `
        <td><strong class="mono">#${idx + 1}</strong></td>
        <td><strong>${Dashboard.escape(job.filename)}</strong></td>
        <td>${Dashboard.escape(job.sheet_name)}</td>
        <td><span class="mono">${(job.total_records || 0).toLocaleString()}</span> سجل</td>
        <td><span class="queue-price" data-billing-job="${Dashboard.escape(job.id)}">مخفي</span>${job.status==='pending' && !job.completed ? `<button class="queue-price-edit" aria-label="تعديل سعر ${Dashboard.escape(job.filename)}" onclick="Pricing.open('${job.id}')">تعديل السعر</button>`:''}</td>
        <td>${statusBadge}</td>
        <td>
          <span class="mono" style="color:var(--status-match);">${job.matches || 0}</span> مطابقة | 
          <span class="mono" style="color:var(--status-mismatch);">${job.mismatches || 0}</span> فرق
        </td>
        <td style="white-space: nowrap;">
          ${['completed','archived'].includes(job.status) && job.completed>=job.total_records && jobs.filter(j=>j.status!=='archived').every(j=>j.status==='completed') ? `<a href="/api/download-results?job_id=${job.id}" class="btn btn-secondary queue-download" download>${Dashboard.icon('download')} تحميل النتائج</a>` : ''}
          ${['pending','failed'].includes(job.status) && job.completed>0 ? `<button class="btn btn-primary queue-resume" onclick="QueueUI.startQueue('${Dashboard.escape(job.id)}')">${job.completed>=job.total_records?'إعادة حفظ النتيجة':'متابعة المتبقي · '+(job.total_records-job.completed).toLocaleString('ar-EG')}</button>`:''}
          ${job.status!=='active' && job.status!=='archived' ? `<button class="btn btn-secondary" onclick="QueueUI.promptRestartJob('${Dashboard.escape(job.id)}')">فحص جديد</button>`:''}
          <button class="btn btn-secondary queue-remove" ${job.status==='active'?'disabled':''} onclick="QueueUI.removeJob('${job.id}')">إزالة من القائمة</button>
        </td>
      `;
      tbody.appendChild(tr);
    });

    this.updateTabBadge(jobs);
    this.updateQueueControls(jobs);
    if(typeof Pricing !== 'undefined') Pricing.render();
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

  renderRecovery(jobs) {
    const panel=document.getElementById('recoveryPanel');
    if(!panel)return;
    const resumable=jobs.filter(j=>['pending','failed'].includes(j.status) && j.completed>0);
    panel.hidden=!!App.isRunning || !resumable.length;
    const list=document.getElementById('recoveryList');
    if(!list || panel.hidden)return;
    list.innerHTML=resumable.map(job=>'<div class="recovery-row"><div><strong>'+Dashboard.escape(job.filename)+' · '+Dashboard.escape(job.sheet_name)+'</strong><p>تم فحص '+job.completed.toLocaleString('ar-EG')+' من '+job.total_records.toLocaleString('ar-EG')+' صف. النتائج السابقة محفوظة.</p></div><div class="recovery-actions"><button class="btn btn-primary" onclick="QueueUI.startQueue(\''+Dashboard.escape(job.id)+'\')">'+(job.completed>=job.total_records?'إعادة حفظ النتيجة':'متابعة المتبقي')+'</button><button class="btn btn-secondary" onclick="QueueUI.promptRestartJob(\''+Dashboard.escape(job.id)+'\')">فحص جديد</button></div></div>').join('');
  },

  updateQueueControls(jobs) {
    const btn = document.getElementById('btnStartQueue');
    if (!btn) return;
    const hasActive = jobs.some(j => j.status === 'active');
    const hasPending = jobs.some(j => j.status === 'pending');
    const current=jobs.filter(j=>j.status!=='archived');
    const allCompleted = current.length > 0 && current.every(j => j.status === 'completed');

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
      statsText: `ستبدأ جولة جديدة لكل شيت. تبقى النتائج والتقدم السابق محفوظين.`,
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
      statsText: `تم فحص ${(job.completed || 0).toLocaleString('ar-EG')} من ${job.total_records.toLocaleString('ar-EG')} صف. سننشئ جولة جديدة ونحتفظ بنتائج الجولة السابقة.`,
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

  async startQueue(jobId=null) {
    if (this.starting || App.isRunning) return;
    this.starting = true;
    App.updateControlButtons();
    const btn = document.getElementById('btnStartQueue');
    const origText = btn ? btn.innerHTML : 'بدء معالجة الطابور';
    if (btn) btn.innerHTML = '⏳ جاري البدء...';
    try {
      const res = await API.startQueue(jobId);
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
