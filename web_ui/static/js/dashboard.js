const Dashboard = {
  connected: false,
  live: null,
  telegram: null,
  telegramBusy: false,
  confirmResolver: null,
  focusBeforeModal: null,

  init() {
    if (typeof SessionTiming !== 'undefined') SessionTiming.init();
    document.getElementById('todayDate').textContent = new Intl.DateTimeFormat('ar-EG', {
      weekday:'long', day:'numeric', month:'long', timeZone:'Asia/Baghdad'
    }).format(new Date());
    document.addEventListener('keydown', event => {
      const overlay = [...document.querySelectorAll('.modal-overlay')].find(el => el.style.display !== 'none');
      if (!overlay) return;
      if (event.key === 'Escape') {
        if (overlay.id === 'confirmOverlay') this.resolveConfirm(false);
        else if (overlay.id === 'guideOverlay') this.closeGuide();
        else if (overlay.id === 'modalOverlay') ColumnMapper.closeModal();
        else RestartModal.close();
      }
      if (event.key === 'Tab') {
        const targets = [...overlay.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),a[href]')].filter(el => el.offsetParent !== null);
        if (!targets.length) return;
        const first = targets[0], last = targets[targets.length-1];
        if (event.shiftKey && document.activeElement === first) {event.preventDefault();last.focus();}
        else if (!event.shiftKey && document.activeElement === last) {event.preventDefault();first.focus();}
      }
    });
    this.refreshTelegram();
  },

  icon(name) { return '<svg class="icon" aria-hidden="true"><use href="#i-' + name + '"/></svg>'; },
  escape(value) {return String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));},
  toast(message, type = 'info') {
    const region = document.getElementById('toastRegion');
    const item = document.createElement('div');
    item.className = 'toast' + (type === 'error' || /تعذر|خطأ|فشل/.test(message) ? ' error' : '');
    const content = document.createElement('p');content.textContent = message;
    const close = document.createElement('button');close.textContent = '×';close.setAttribute('aria-label','إغلاق التنبيه');
    close.onclick = () => item.remove();
    item.append(content,close);region.append(item);
    if (region.children.length > 4) region.firstElementChild.remove();
    setTimeout(() => item.remove(), 8000);
  },
  modalOpened(id) {
    this.focusBeforeModal = document.activeElement;
    const panel = document.querySelector('#' + id + ' .modal-dialog');
    panel?.focus();
  },
  confirm({title='تأكيد الإجراء', text, accept='متابعة'}) {
    if (this.confirmResolver) this.resolveConfirm(false);
    document.getElementById('confirmTitle').textContent = title;
    document.getElementById('confirmText').textContent = text;
    document.getElementById('confirmAccept').textContent = accept;
    document.getElementById('confirmOverlay').style.display='flex';
    this.modalOpened('confirmOverlay');
    return new Promise(resolve => {this.confirmResolver=resolve;});
  },
  resolveConfirm(answer) {
    document.getElementById('confirmOverlay').style.display='none';
    const resolve=this.confirmResolver;this.confirmResolver=null;
    this.focusBeforeModal?.focus();resolve?.(answer);
  },
  openGuide() {document.getElementById('guideOverlay').style.display='flex';this.modalOpened('guideOverlay');},
  closeGuide() {document.getElementById('guideOverlay').style.display='none';this.focusBeforeModal?.focus();},
  activateView(view) {
    document.querySelectorAll('.header-link').forEach(el=>el.classList.toggle('active',el.dataset.view===view));
  },
  showOverview() {this.activateView('overview');LiveFeed.switchTab('all');document.getElementById('workspace').scrollIntoView({behavior:'smooth',block:'start'});},
  showQueue() {this.activateView('queue');LiveFeed.switchTab('queue');document.getElementById('queueTableContainer').scrollIntoView({behavior:'smooth',block:'center'});},
  showTelegram() {this.activateView('telegram');document.getElementById('telegramPanel').scrollIntoView({behavior:'smooth',block:'center'});document.getElementById('btnTelegramToggle').focus();},

  update(data) {
    this.live=data;this.connected=true;
    if (typeof SessionTiming !== 'undefined') SessionTiming.update(data);
    const status=document.getElementById('connectionStatus');
    status.className='connection-label';status.innerHTML='<i class="status-dot active"></i>متصل بالبرنامج';
    const k=data.kpis || {},total=Number(k.total)||0,done=Number(k.completed)||0;
    const percentage=total && !data._preview ? Math.min(100,Math.max(0,Math.round(done/total*100))) : 0;
    const running=data.running, paused=data.paused, pending=(data.verifications||[]).length;
    const title=data._preview ? 'معاينة ملف محفوظ' : pending ? 'بانتظار تحقق زين' : running ? (paused ? 'متوقف مؤقتًا' : 'الفحص جارٍ') : (total ? 'انتهت المعالجة' : 'جاهز لفحصك');
    const badge=document.getElementById('sessionState');badge.textContent=title;badge.classList.toggle('paused',paused);
    document.getElementById('sessionHeading').textContent=data.workbook || 'ابدأ من شيت العملاء';
    document.getElementById('sessionWorkbook').textContent=data._preview ? 'معاينة محتوى الملف المحدد' : data.workbook ? 'ملف الجلسة الحالية' : 'اختر ملفًا من القائمة لبدء التدقيق.';
    document.getElementById('progressPercent').innerHTML=data._preview ? '—' : percentage+'<span>%</span>';
    document.getElementById('progressFill').style.width=percentage+'%';
    document.getElementById('progressCircle').style.strokeDashoffset=320.44*(1-percentage/100);
    document.getElementById('sessionProgress').setAttribute('aria-valuenow',percentage);
    document.getElementById('sessionCountLabel').textContent=data._preview?'صفوف معروضة':'تمت معالجتها';
    document.getElementById('sessionHint').textContent=data._preview ? 'معاينة محفوظة؛ لا توجد جلسة فحص جديدة قيد التشغيل.' : pending ? 'أكمل التحقق في Chrome أو في اللوحة؛ يستأنف الفحص بعد نجاحه.' : paused ? 'التقدم محفوظ. راجع سبب التوقف قبل الاستئناف.' : 'تقدم المعالجة منفصل عن عدد الأرصدة المؤكدة.';
    const report=document.getElementById('btnDownloadResults');
    const reportAvailable=!!data.workbook && (data._preview || (!running && !data.round_pending && total>0 && done>=total));
    report.setAttribute('aria-disabled',String(!reportAvailable));
    report.href=data._preview ? '/api/download-results?file='+encodeURIComponent(data.workbook) : '/api/download-results?current=1';
    report.innerHTML=this.icon('download')+(data._preview?' تحميل ملف المعاينة':' تحميل النتائج');
    ['selectWorkbook','selectSheet','selectAuditMode'].forEach(id=>{document.getElementById(id).disabled=!!running;});
    document.getElementById('btnBrowseFile').disabled=!!running || !!App.uploading;
    if (running && [...document.getElementById('selectWorkbook').options].some(o=>o.value===data.workbook)) {
      document.getElementById('selectWorkbook').value=data.workbook;
    }
    document.getElementById('lastUpdated').textContent='آخر تحديث '+new Intl.DateTimeFormat('ar-EG',{hour:'2-digit',minute:'2-digit',second:'2-digit',timeZone:'Asia/Baghdad'}).format(new Date());
    this.renderTelegram();
  },
  offline() {
    this.connected=false;
    if (typeof SessionTiming !== 'undefined') SessionTiming.offline();
    const status=document.getElementById('connectionStatus');
    status.className='connection-label offline';status.innerHTML='<i class="status-dot"></i>انقطع الاتصال';
    document.getElementById('sessionState').textContent='البرنامج غير متصل';
    document.getElementById('sessionHint').textContent='العدادات المعروضة آخر حالة محفوظة في الصفحة.';
    document.getElementById('btnActionRun').disabled=true;
    document.getElementById('btnDownloadResults').setAttribute('aria-disabled','true');
    this.renderTelegram();
  },
  async sessionAction(action) {
    if (App.actionBusy || !this.connected) return;
    App.actionBusy=true;App.updateControlButtons();
    try {
      const response=await (action==='resume' ? API.resumeSession() : API.pauseSession());
      this.toast(action==='resume'?'استؤنف الفحص':'أُوقف الفحص مؤقتًا');
      await App.poll();
    } catch (error) {this.toast(error.message,'error');}
    finally {App.actionBusy=false;App.updateControlButtons();}
  },

  async refreshTelegram() {
    try {this.telegram=await API.getTelegramStatus();this.renderTelegram();}
    catch {if (!this.connected) this.renderTelegram();}
  },
  renderTelegram() {
    const state=this.telegram || {},enabled=!!state.enabled;
    const badge=document.getElementById('tgStatusBadge');
    badge.textContent=enabled ? (state.connected===true?'اتصال مؤكد':'مفعّل') : (state.connected===true?'متصل · متوقف':'غير مفعّل');
    badge.className='badge'+(enabled?' badge-match':'');
    document.getElementById('telegramDescription').textContent=enabled ? (state.username ? '@'+state.username+' · تنبيهات الجلسات مفعّلة' : 'التنبيهات مفعّلة. افحص الاتصال للتحقق من البوت.') : 'فعّل البوت لتلقي تنبيهات الجلسات وإرسال النتائج إلى حسابك.';
    const toggle=document.getElementById('btnTelegramToggle');
    toggle.textContent=enabled?'إيقاف التلقرام':'تفعيل التلقرام';toggle.disabled=this.telegramBusy || !this.connected;
    document.getElementById('btnTelegramCheck').disabled=this.telegramBusy || !this.connected;
    document.getElementById('btnTelegramPing').disabled=this.telegramBusy || !this.connected || !enabled;
    document.getElementById('btnTelegramReport').disabled=this.telegramBusy || !this.connected || !enabled || !!this.live?._preview || !!this.live?.running || !!this.live?.round_pending || !(this.live?.kpis?.total>0 && this.live?.kpis?.completed>=this.live?.kpis?.total);
  },
  async telegramAction(buttonId, action) {
    if (this.telegramBusy || !this.connected) return;
    this.telegramBusy=true;this.renderTelegram();
    const feedback=document.getElementById('telegramFeedback');feedback.textContent='جارٍ تنفيذ الطلب…';
    try {const response=await action();feedback.textContent=response.message || 'اكتمل الطلب';this.toast(feedback.textContent);await this.refreshTelegram();return response;}
    catch(error) {feedback.textContent=error.message;this.toast(error.message,'error');}
    finally {this.telegramBusy=false;this.renderTelegram();}
  },
  async toggleTelegram() {
    const enabled=!!this.telegram?.enabled;
    if (!enabled && !await this.confirm({title:'تفعيل متابعة التلقرام',text:'سيُفعّل البوت المحفوظ في إعدادات البرنامج، ويرسل تنبيهات الجلسات وملف النتائج إلى الحساب المحدد فيه.',accept:'تفعيل البوت'})) return;
    return this.telegramAction('btnTelegramToggle',()=>API.post('/api/telegram/toggle',{enabled:!enabled}));
  },
  checkTelegram() {return this.telegramAction('btnTelegramCheck',()=>API.post('/api/telegram/check-connection'));},
  async sendTelegramReport() {
    if (!await this.confirm({title:'إرسال نتائج الجلسة',text:'سيُرسل ملف نتائج '+(this.live?.workbook || 'الجلسة الحالية')+' إلى الحساب المحدد في إعدادات التلقرام.',accept:'إرسال الملف'})) return;
    return this.telegramAction('btnTelegramReport',()=>API.post('/api/telegram/send-results'));
  }
};
