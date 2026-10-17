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
        points: [], smoothedRate: null, rates: {}, prediction: null
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
    // Worker recovery never erases history, including seven-minute cooldown gaps.
    // Separate kinds prevent fast contracts from setting the speed of services.
    // New runtimes provide precise completion events; older runtimes use bounded
    // progress observations from the existing poll, without another request.
    if (Array.isArray(timing.recent_completions)) {
      let events=timing.recent_completions;
      if(type!=='mixed' && timing.recent_by_type?.[type]) events=timing.recent_by_type[type];
      else if(type!=='mixed' && type!=='general') events=events.filter(e=>e.kind===type);
      if(phase==='retry' && timing.recent_by_phase?.retry?.length>=3){
        const retries=timing.recent_by_phase.retry.filter(e=>type==='mixed' || type==='general' || e.kind===type);
        if(retries.length>=3)events=retries;
      }
      events=events.slice(-11);
      state.points=events.map((event,i)=>({at:serverNow-Math.max(0,Number(event.age_seconds))*1000,
        done:done-events.length+i+1})).filter(point=>Number.isFinite(point.at));
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
      const first=points[0],sample=latest.done-first.done;
      const span=(latest.at-first.at)/1000, sinceLast=Math.max(0,(serverNow-latest.at)/1000);
      const bucket=(timing.window_id || '')+'|'+type+'|'+phase;
      let model=state.rates[bucket];
      let refreshed=false;
      // Fixed, bounded samples; update the estimate every 15 seconds, not every tick.
      if(sample>=2 && span>=1 && (!model || serverNow-model.at>=15000)){
        const period=span/sample;
        // Count an unfinished, unusually long interval instead of assuming progress
        // continues at the old speed while the browser is recovering.
        const observed=sinceLast>period ? (span+sinceLast)/(sample+1) : period;
        const secondsPerRow=model ? (observed>model.period?observed:0.25*observed+0.75*model.period) : observed;
        model={at:serverNow,period:secondsPerRow,sample};state.rates[bucket]=model;
        refreshed=true;
      }
      state.smoothedRate=model?1/model.period:null;
      if(model && !data.paused && remaining && (refreshed || !state.prediction || state.prediction.bucket!==bucket || state.done!==done)){
        state.prediction={endsAt:now+remaining*model.period*1000,seconds:remaining*model.period,at:now,bucket};
      }
    } else {
      state.smoothedRate=null;
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
    if (d.paused) return {...base,status:'paused'};
    if (!remaining) return {...base,status:'saving'};
    const workers=d.workers || [];
    const available=workers.filter(w=>!w.in_cooldown && !w.service_verification_required && ['ready','processing'].includes(w.status));
    const noWork=!(Number(d.kpis.active_leases)>0) && !workers.some(w=>w.status==='processing' && !w.in_cooldown);
    const retryDelay=Math.max(0,Number(d.kpis.next_retry_seconds)||0);
    if(noWork && (retryDelay>0 || workers.length && !available.length && workers.some(w=>w.in_cooldown))){
      const delays=workers.map(w=>Math.max(Number(w.cooldown_remaining_seconds)||0,Number(w.service_retry_seconds)||0)).filter(v=>v>0);
      const wait=retryDelay || (delays.length?Math.min(...delays):0);
      return {...base,status:'cooldown',wait};
    }
    const points=s.points || [],last=points[points.length-1], first=points[0];
    const sample=last && first?last.done-first.done:0;
    const rate=s.smoothedRate;
    const sinceLast=last?Math.max(0,(now-s.offset-last.at)/1000):0;
    if(noWork && workers.length && workers.every(w=>w.service_verification_required))return {...base,status:'verification'};
    if(!rate || sample<2)return {...base,status:'sampling'};
    const seconds = s.prediction?Math.max(0,(s.prediction.endsAt-now)/1000):remaining/rate;
    if(sinceLast>Math.max(45,3/rate))return {...base,status:'waiting',rate,seconds,sample,endsAt:s.prediction?.endsAt};
    return {...base,status:'estimated',rate,seconds,sample,sampleType:s.sampleType,phase:s.phase,
      endsAt:s.prediction?.endsAt || now + seconds * 1000};
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
  shortDuration(seconds) {
    const n=Math.max(0,Math.ceil(seconds));
    return [Math.floor(n/3600),Math.floor(n%3600/60),n%60].map(value=>String(value).padStart(2,'0')).join(':');
  },
  render(now = Date.now()) {
    const set = (id, text) => {
      const el = document.getElementById(id);
      if (el && el.textContent !== text) el.textContent = text;
    };
    const e = this.estimate(now);
    set('timingStartLabel', 'وقت بدء فحص الملف');
    set('timingStart', e.startedAt ? this.clock(e.startedAt) : '—');
    set('timingElapsed', e.elapsed != null ? this.shortDuration(e.elapsed) : '—');
    set('timingRemaining', e.status === 'estimated' ? 'نحو ' + this.shortDuration(e.seconds) :
      e.status === 'finished' ? 'اكتمل' : e.status === 'cooldown' ? 'بانتظار التبريد' : e.status === 'waiting' ? 'بانتظار نتيجة' : '—');
    const lastPrediction=this.state?.prediction;
    const showLast=lastPrediction && ['cooldown','waiting','paused','offline','verification','stopped','sampling'].includes(e.status);
    set('timingFinish', e.status === 'estimated' ? this.clock(e.endsAt, false) : showLast ? this.clock(lastPrediction.endsAt,false)+' (آخر تقدير)' : '—');
    const labels = {
      idle:'يظهر التقدير عند بدء الفحص.',
      unknown_start:'وقت بدء هذه الجلسة غير متاح من النسخة الجارية؛ تسجيله متاح بعد تشغيل النسخة المحدّثة.',
      sampling:'نجمع عينة حديثة لحساب الوقت المتبقي…',
      waiting:'لم تكتمل فحوصات جديدة مؤخرًا؛ يتحدث التقدير عند وصول نتيجة.',
      verification:'مسارات الخدمة المتاحة تنتظر التحقق. وقت البدء والتقدم محفوظان.',
      cooldown:e.wait?'استئناف متوقع بعد '+this.duration(e.wait)+'؛ يُحسب موعد الانتهاء بعد عودة الفحص.':'بانتظار عودة أحد مسارات الفحص من التبريد.',
      paused:'الفحص متوقف مؤقتًا؛ يتحدث موعد الانتهاء بعد الاستئناف.',
      offline:'بانتظار تحديث الاتصال؛ موعد الانتهاء غير متاح الآن.',
      finished:'اكتمل فحص الملف.',
      stopped:'توقف الفحص قبل اكتمال الملف.',
      saving:'اكتمل فحص العملاء؛ جارٍ حفظ النتائج.'
    };
    const rate = e.rate ? (e.rate * 60).toLocaleString('ar-EG', {maximumFractionDigits:1}) : '';
    set('timingNote', e.status === 'estimated'
      ? 'تقدير يشمل الانتظار · حوالي ' + rate + ' عميلًا/دقيقة · آخر '+e.sample.toLocaleString('ar-EG')+' فواصل إنجاز'+
        (e.phase==='retry'?' مؤجلة':e.sampleType==='service'?' خدمة':e.sampleType==='account'?' حساب / عقد':'')+' · توقيت بغداد'
      : labels[e.status]);
  }
};
