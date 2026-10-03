// Read-only progress estimates. No requests or worker/session actions.
const SessionTiming = {
  state: null,
  data: null,
  connected: false,
  timer: null,
  formatters: null,

  init() {
    if (!this.timer) this.timer = setInterval(() => this.render(), 1000);
  },
  update(data, now = Date.now()) {
    this.connected = true;
    this.data = data;
    const total = Number(data.kpis?.total) || 0;
    const done = Number(data.kpis?.completed) || 0;
    if (data._preview || !total || !data.workbook) { this.render(now); return; }
    const timing = data.session_timing;
    const exact = Number(timing?.started_at) > 0;
    // Never substitute page load time for the actual checker start time.
    if (!exact) { this.state = null; this.render(now); return; }
    const key = String(timing.session_id);
    let state = this.state;
    // A new server session or a reset progress count must start a new sample.
    if (!state || state.key !== key || done < state.done ||
        (data.running && !state.running && state.finished)) {
      const serverNow = Number(timing?.server_time) * 1000;
      state = {
        key, exact, startedAt: Number(timing.started_at) * 1000,
        offset: Number.isFinite(serverNow) ? now - serverNow : 0,
        baseline: Number(timing.initial_completed) || 0,
        done, lastSeen: now, running: !!data.running, finished: false,
        points: [], sampleFloor: 0, signature: null, smoothedRate: null, rateVersion: null
      };
    }
    if (!Number.isFinite(state.startedAt) || !Number.isFinite(state.baseline)) {
      this.state = null;
      this.data = null;
      this.render(now);
      return;
    }
    const serverNow = now - state.offset;
    const byType = timing.remaining_by_type;
    const type = byType ? (byType.service && byType.account ? 'mixed' : byType.service ? 'service' : 'account') : 'general';
    const remaining = Math.max(0,total-done);
    const phase = remaining && Number(data.kpis.deferred) >= remaining ? 'retry' : 'regular';
    const workers = data.workers || [];
    const available = workers.filter(w=>!w.in_cooldown && ['ready','processing'].includes(w.status));
    const capacity = workers.length ? available.map(w=>w.worker_id || w.id).sort().join(',') : 'unknown';
    const signature = [timing.window_id || '',type,phase,capacity,!!data.paused,!!data.verifications?.length].join('|');
    if (state.signature !== null && state.signature !== signature) {
      state.sampleFloor = serverNow; state.points=[];state.smoothedRate=null;state.rateVersion=null;
    }
    state.signature=signature;
    // New runtimes provide precise completion events; older runtimes use bounded
    // progress observations from the existing poll, without another request.
    if (Array.isArray(timing.recent_completions)) {
      const events=timing.recent_completions.slice(-11);
      state.points=events.map((event,i)=>({at:serverNow-Math.max(0,Number(event.age_seconds))*1000,
        done:done-events.length+i+1})).filter(point=>Number.isFinite(point.at) && point.at>=state.sampleFloor);
    } else if (!state.points.length || state.points[state.points.length-1].done !== done) {
      state.points.push({at:serverNow,done});state.points=state.points.slice(-11);
    }
    const points=state.points;
    if(points.length>1){
      const latest=points[points.length-1], cutoff=latest.done-10;
      while(points.length>2 && points[1].done<=cutoff)points.shift();
      if(points[0].done<cutoff){
        const first=points[0],next=points[1];
        const fraction=(cutoff-first.done)/(next.done-first.done);
        points[0]={done:cutoff,at:first.at+(next.at-first.at)*fraction};
      }
      const first=points[0],sample=latest.done-first.done,span=(latest.at-first.at)/1000;
      const version=latest.done+'|'+first.done;
      if(sample>=2 && span>=1 && version!==state.rateVersion){
        const observed=sample/span;
        state.smoothedRate=state.smoothedRate==null?observed:0.7*observed+0.3*state.smoothedRate;
        state.rateVersion=version;
      }
    }
    state.sampleType=type;state.phase=phase;
    state.done = done;
    state.lastSeen = now;
    if (!data.running && !state.stoppedAt) state.stoppedAt = now;
    if (data.running) state.stoppedAt = null;
    state.running = !!data.running;
    state.finished = !data.running && done >= total;
    this.state = state;
    this.render(now);
  },
  offline() { this.connected = false; this.render(); },
  estimate(now = Date.now()) {
    const d = this.data, s = this.state;
    if (!d || d._preview || !d.kpis?.total) return {status:'idle'};
    if (!s) return {status:'unknown_start'};
    const end = !s.running ? s.stoppedAt || s.lastSeen : this.connected ? now : s.lastSeen;
    const elapsed = Math.max(0, (end - s.offset - s.startedAt) / 1000);
    const measured = Math.max(0, s.done - s.baseline);
    const remaining = Math.max(0, Number(d.kpis.total) - s.done);
    const base = {elapsed, measured, remaining, startedAt:s.startedAt, exact:s.exact};
    if (!this.connected || now - s.lastSeen > 10000) return {...base,status:'offline'};
    if (!d.running) return {...base,status:s.finished ? 'finished' : 'stopped'};
    if (d.paused || (d.verifications || []).length) return {...base,status:'paused'};
    if (!remaining) return {...base,status:'saving'};
    const workers=d.workers || [];
    const available=workers.filter(w=>!w.in_cooldown && ['ready','processing'].includes(w.status));
    const noWork=!(Number(d.kpis.active_leases)>0) && !workers.some(w=>w.status==='processing' && !w.in_cooldown);
    const retryDelay=Math.max(0,Number(d.kpis.next_retry_seconds)||0);
    if(noWork && (retryDelay>0 || workers.length && !available.length && workers.some(w=>w.in_cooldown))){
      const delays=workers.filter(w=>w.in_cooldown).map(w=>Number(w.cooldown_remaining_seconds)).filter(v=>v>0);
      const wait=retryDelay || (delays.length?Math.min(...delays):0);
      return {...base,status:'cooldown',wait};
    }
    const points=s.points || [],last=points[points.length-1], first=points[0];
    const sample=last && first?last.done-first.done:0;
    const rate=s.smoothedRate;
    const sinceLast=last?Math.max(0,(now-s.offset-last.at)/1000):0;
    if(sinceLast>Math.max(45,rate?3/rate:45))return {...base,status:'waiting'};
    if(!rate || sample<2)return {...base,status:'sampling'};
    const seconds = remaining / rate;
    return {...base,status:'estimated',rate,seconds,sample,sampleType:s.sampleType,phase:s.phase,
      endsAt:now + seconds * 1000};
  },
  duration(seconds) {
    const n = Math.max(0, Math.ceil(seconds));
    const h = Math.floor(n / 3600), m = Math.floor(n % 3600 / 60), s = n % 60;
    const unit=(value,singular,dual,plural)=>value===1?singular+' واحدة':value===2?dual:
      value.toLocaleString('ar-EG')+' '+(value>=3 && value<=10?plural:singular);
    const parts=[];
    if(h)parts.push(unit(h,'ساعة','ساعتان','ساعات'));
    if(m)parts.push(unit(m,'دقيقة','دقيقتان','دقائق'));
    if(s || !parts.length)parts.push(unit(s,'ثانية','ثانيتان','ثوانٍ'));
    return parts.join(' و');
  },
  clock(timestamp, includeSeconds = true) {
    if(!this.formatters)this.formatters=[false,true].map(seconds=>new Intl.DateTimeFormat('ar-EG',
      {hour:'2-digit',minute:'2-digit',...(seconds?{second:'2-digit'}:{}),timeZone:'Asia/Baghdad'}));
    return this.formatters[includeSeconds?1:0].format(new Date(timestamp));
  },
  render(now = Date.now()) {
    const set = (id, text) => {
      const el = document.getElementById(id);
      if (el && el.textContent !== text) el.textContent = text;
    };
    const e = this.estimate(now);
    set('timingStartLabel', 'وقت بدء فحص الملف');
    set('timingStart', e.startedAt ? this.clock(e.startedAt) : '—');
    set('timingElapsed', e.elapsed != null ? this.duration(e.elapsed) : '—');
    set('timingRemaining', e.status === 'estimated' ? 'نحو ' + this.duration(e.seconds) :
      e.status === 'finished' ? 'اكتمل' : e.status === 'cooldown' ? 'بانتظار التبريد' : e.status === 'waiting' ? 'بانتظار نتيجة' : '—');
    set('timingFinish', e.status === 'estimated' ? this.clock(e.endsAt, false) : '—');
    const labels = {
      idle:'يظهر التقدير عند بدء الفحص.',
      unknown_start:'وقت بدء هذه الجلسة غير متاح من النسخة الجارية؛ تسجيله متاح بعد تشغيل النسخة المحدّثة.',
      sampling:'نجمع عينة حديثة لحساب الوقت المتبقي…',
      waiting:'لم تكتمل فحوصات جديدة مؤخرًا؛ يتحدث التقدير عند وصول نتيجة.',
      cooldown:e.wait?'استئناف متوقع بعد '+this.duration(e.wait)+'؛ يُحسب موعد الانتهاء بعد عودة الفحص.':'بانتظار عودة أحد مسارات الفحص من التبريد.',
      paused:'الفحص متوقف مؤقتًا؛ يتحدث موعد الانتهاء بعد الاستئناف.',
      offline:'بانتظار تحديث الاتصال؛ موعد الانتهاء غير متاح الآن.',
      finished:'اكتمل فحص الملف.',
      stopped:'توقف الفحص قبل اكتمال الملف.',
      saving:'اكتمل فحص العملاء؛ جارٍ حفظ النتائج.'
    };
    const rate = e.rate ? (e.rate * 60).toLocaleString('ar-EG', {maximumFractionDigits:1}) : '';
    set('timingNote', e.status === 'estimated'
      ? 'حوالي ' + rate + ' عميلًا/دقيقة · آخر '+e.sample.toLocaleString('ar-EG')+' فحوصات'+
        (e.phase==='retry'?' مؤجلة':e.sampleType==='service'?' خدمة':e.sampleType==='account'?' حساب / عقد':'')+' · توقيت بغداد'
      : labels[e.status]);
  }
};
