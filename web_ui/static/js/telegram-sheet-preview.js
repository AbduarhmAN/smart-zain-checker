/* Interactive example data only. No fetch, Telegram, Zain, or queue API calls. */
window.TelegramSheetPreview = (() => {
  let demo, draft, jobs, state, editing = null, removing = null, starting = null;
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const button = (text, action, style) => ({text, callback_data:'zc:'+action, ...(style ? {style} : {})});
  const nav = () => [button('‹ الرئيسية','home'),button('📊 الحالة','status')];
  function reset() {
    demo = window.telegramSheetDemo;
    draft = structuredClone(demo.draft); jobs = structuredClone(demo.jobs); state = structuredClone(demo.state);
    editing = null; removing = null; starting = null;
    state.running=false;state.paused=false;state.workbook=null;
    Object.keys(state.kpis).forEach(key=>state.kpis[key]=0);
    jobs.forEach(job=>{job.status='pending';job.completed=0;job.total_records=2;});
  }
  function validation() {
    const m = draft.mapping, problems = [];
    const required = draft.mode === 'account_only' ? ['lookup_col'] : draft.mode === 'service_only' ? ['service_col'] : ['lookup_col','service_col'];
    if (!required.some(k => m[k])) problems.push('حدد عمود الرقم المناسب لطريقة الفحص.');
    if (!m.amount_col) problems.push('حدد عمود المبلغ المسجل بالشيت.');
    if (draft.amount_target !== 'remaining' && !m.amount_col_2) problems.push('حدد المبلغ الثاني أو اختر المبلغ المسجل بالشيت.');
    const selectedAmounts = draft.amount_target === 'contract' ? [m.amount_col_2] : draft.amount_target === 'smart_dual' ? [m.amount_col,m.amount_col_2] : [m.amount_col];
    if (!problems.length && demo.values.some(row => {
      const value = col => row[draft.columns.findIndex(c => c.col_letter === col)];
      const pattern = draft.mode === 'account_only' ? /^1\d{8,11}$/ : draft.mode === 'service_only' ? /^2\d{8,11}$/ : /^[12]\d{8,11}$/;
      return !required.some(k => pattern.test(String(value(m[k]) ?? ''))) || selectedAmounts.some(col => value(col) == null || value(col) === '' || !Number.isFinite(Number(value(col))));
    })) problems.push('بيانات الأعمدة المختارة لا تحتوي رقمًا ومبلغًا صالحين. في هذه العينة: المرجع B، والمبلغ C.');
    return problems;
  }
  function review() {
    const problems = validation();
    const text = '<b>📋 مراجعة الشيت</b>\n\n' + escape(draft.filename) + '\nالورقة: <b>' + escape(draft.sheet_name) + '</b>\n' +
      (problems.length ? '⚠ يحتاج تعديل قبل إضافته' : '✓ مناسب للفحص') + '\nصفوف البيانات: 2\n\n' +
      problems.map(escape).join('\n') + '\n\nاضغط على نوع البيانات لاختيار عموده. جميع الأعمدة متاحة، حتى بدون عنوان.';
    const rows = draft.advanced ? Object.entries(demo.fields).map(([key,label]) => [button(label+': '+(draft.mapping[key] || 'غير محدد'),'map:demo:field:'+key+':0')]) :
      [[button('رقم الحساب أو الخدمة: '+(draft.mapping.lookup_col||draft.mapping.service_col||'اختر العمود'),'map:demo:field:lookup_col:0')],
       [button('المبلغ: '+(draft.mapping.amount_col||'اختر العمود'),'map:demo:field:amount_col:0')]];
    if(draft.advanced){
      rows.push(Object.entries(demo.modes).map(([mode,label])=>button((draft.mode === mode ? '✓ ' : '')+label,'map:demo:mode:'+mode)));
      rows.push([['remaining','مبلغ الشيت'],['contract','المبلغ الثاني'],['smart_dual','كلاهما']].map(([target,label])=>button((draft.amount_target===target?'✓ ':'')+label,'map:demo:target:'+target)));
    }
    rows.push([button('كل الأعمدة','map:demo:all:0'),...(!editing ? [button('تغيير الورقة','map:demo:sheets:0')] : [])]);
    rows.push([button(draft.advanced?'إعدادات أبسط':'أعمدة إضافية / خيارات متقدمة','map:demo:advanced')],
      [button('↻ التحقق من البيانات','map:demo:review')],
      [button(editing?'✓ حفظ الأعمدة':'✓ إضافة إلى الطابور','map:demo:save','success')],nav());
    return {text,rows};
  }
  function queue() {
    let text = '<b>📁 طابور الملفات</b>\n'+jobs.length+' ملف · بيانات تجريبية\n';
    jobs.forEach((job,i)=>{text+='\n<b>'+(i+1)+'. '+escape(job.filename)+'</b>\n'+escape(job.sheet_name)+' · '+(job.status==='completed'?'✓ انتهت المعالجة':job.status==='active'?'🟢 قيد المعالجة':'◷ في الانتظار')+'\nمعالجة: '+job.completed+' / '+job.total_records+'\n';});
    return {text,rows:[...jobs.map((j,i)=>[button('إدارة الشيت '+(i+1),'job:'+j.id)]),
      ...(jobs.some(j=>j.status==='pending')?[[button('▶ بدء الجولة','start_queue','success')]]:[]),
      [button('↻ تحديث الطابور','queue:0'),button('📎 إضافة ملف','upload')],nav()]};
  }
  function status(panels) {
    const k=state.kpis, percent=k.total ? Math.round(k.completed/k.total*100) : 0;
    const finished=!state.running && k.total>0 && k.completed>=k.total;
    const text='<b>📊 حالة الجولة</b>\n'+(state.running ? state.paused?'⏸ متوقف مؤقتًا':'🟢 الفحص جارٍ' : finished?'✓ اكتملت الجولة':'جاهز للبدء')+
      '\n\n'+escape(state.workbook||'أضف ملفاتك ثم ابدأ الجولة')+'\n<code>'+('▰'.repeat(Math.floor(percent/10))+ '▱'.repeat(10-Math.floor(percent/10)))+'  '+percent+'%</code>\n'+
      'تمت معالجتها: '+k.completed+' من '+k.total+'\nمتبقية: '+k.remaining+'\n\n'+
      (finished?'النتائج جاهزة. يمكنك استلامها أو إعادة فحص الحالات التي تحتاج مراجعة.':'النتائج تظهر بعد اكتمال الجولة.');
    const rows=[[button('↻ تحديث الحالة','status','primary')]];
    if(state.running)rows.push([button(state.paused?'▶ استئناف الفحص':'⏸ إيقاف مؤقت',state.paused?'resume':'pause')],
      [button('محاكاة اكتمال الجولة · عينة فقط','finish-demo')]);
    else if(jobs.some(j=>j.status==='pending'))rows.push([button('▶ بدء الجولة','start_queue','success')]);
    if(finished)rows.push([button('📥 استلام نتائج الجولة','results','primary')]);
    if(k.errors||k.needs_review)rows.push([button('🛠 إصلاح الأخطاء / إعادة الفحص','repair')]);
    rows.push([button('📁 الطابور','queue:0'),button('‹ الرئيسية','home')]);
    return {text,rows};
  }
  function notice(title,body) {return {text:'<b>'+escape(title)+'</b>\n\n'+escape(body),rows:[[button('📁 عرض الطابور','queue:0')],nav()]};}
  function panel(view,panels) {
    if (!demo) reset();
    if (view==='workflow-reset') {reset();return review();}
    if (view==='upload') {editing=null;draft=structuredClone(demo.draft);return {...panels.upload,rows:[[button('تجربة شيت يحتاج ضبط الأعمدة','map:demo:review','primary')],...panels.upload.rows]};}
    if (view==='queue:0') return queue();
    if (view==='status') return status(panels);
    if (view==='home') return {text:'<b>تشيك.</b>\nملفاتك تُفحص خطوة بخطوة.\n\n① أضف الملفات\n② تحقق من الرقم والمبلغ\n③ ابدأ الجولة\n④ استلم النتائج عند اكتمالها\n\nملفات في الانتظار: '+jobs.filter(j=>j.status==='pending').length,
      rows:[[button('📎 إضافة ملف','upload','primary')],[button('📁 ملفات الجولة','queue:0')],
        ...(jobs.some(j=>j.status==='pending')?[[button('▶ بدء الجولة','start_queue','success')]]:[]),[button('📊 الحالة','status')]]};
    if(view==='drafts:0')return {text:'<b>ملفات تحتاج مراجعة · عينة</b>',rows:[[button(draft.filename,'map:demo:review')],nav()]};
    if(view==='start_queue') {
      if(state.running)return notice('الجولة تعمل بالفعل','افتح الحالة للإيقاف أو الاستئناف.');
      starting=jobs.filter(j=>j.status==='pending').map(j=>j.id);
      if(!starting.length)return notice('لا توجد ملفات في الانتظار','أضف ملفًا إلى الطابور أولًا.');
      const p=structuredClone(panels.start_queue);
      p.text=p.text.replace('ملفات الجولة',starting.length+' ملف')+'\n\nهذه محاكاة. لبدء فحص حقيقي افتح التطبيق الفعلي.';
      return p;
    }
    if(view==='confirm:start-demo') {
      if(!starting || state.running)return notice('بدء غير متاح','راجع الطابور مجددًا.');
      const pending=jobs.filter(j=>starting.includes(j.id));starting=null;
      if(!pending.length)return notice('الطابور تغير','راجع الملفات مجددًا.');
      pending[0].status='active';state.running=true;state.paused=false;state.workbook=pending[0].filename;
      state.kpis.total=pending.reduce((sum,j)=>sum+j.total_records,0);state.kpis.remaining=state.kpis.total;state.kpis.completed=0;
      return status(panels);
    }
    if(view==='finish-demo') {
      jobs.forEach(j=>{j.status='completed';j.completed=j.total_records;});
      state.running=false;state.paused=false;state.kpis.completed=state.kpis.total;state.kpis.remaining=0;state.kpis.needs_review=1;
      return status(panels);
    }
    if(view==='results' && (state.running || !state.kpis.total || state.kpis.completed<state.kpis.total))return notice('النتائج بعد اكتمال الجولة','تابع التقدم من شاشة الحالة.');
    if (view==='confirm:pause-demo') {state.paused=true;return status(panels);}
    if (view==='confirm:resume-demo') {state.paused=false;return status(panels);}
    if (view==='repair') return (state.paused || !state.running) ? panels.repair : notice('أوقف الجلسة مؤقتًا أولًا','انتظر انتهاء الفحوص الجارية، ثم أعد فحص الأخطاء. هذه العينة لا تتصل بزين.');
    if (view==='confirm:repair-demo') {
      if (state.running && !state.paused) return notice('تغيرت الحالة','أوقف الجلسة مؤقتًا أولًا.');
      state.kpis.errors=0;state.kpis.needs_review=0;
      return notice('محاكاة إعادة فحص الأخطاء','جُرّب الإجراء على بيانات المعاينة فقط. لم يبدأ فحص حقيقي ولم يتغير ملفك.');
    }
    if (view==='confirm:remove-demo') {
      const id=removing;removing=null;
      if (!id) return notice('انتهى التأكيد','افتح الشيت واطلب الإجراء مجددًا.');
      jobs=jobs.filter(j=>j.id!==id);return notice('✓ أُخرج الشيت التجريبي من الطابور','تغيرت قائمة المعاينة فقط. ملف الإكسل محفوظ.');
    }
    if (/^(job|edit|remove):/.test(view)) {
      const [action,id]=view.split(':');const job=jobs.find(j=>j.id===id);
      if (!job) return notice('الشيت غير موجود','حدّث الطابور.');
      if (action==='remove') {
        if (job.status==='active') return notice('الشيت قيد المعالجة','لا يمكن إخراج الشيت الجاري أثناء فحصه.');
        removing=id;const p=structuredClone(panels.remove);p.text=p.text.replace('متابعة السبت.xlsx',escape(job.filename));return p;
      }
      if (action==='edit') {editing=id;draft=structuredClone(demo.draft);draft.filename=job.filename;if(job.mapping)draft.mapping=structuredClone(job.mapping);return review();}
      const rows=[];
      if(job.status==='pending')rows.push([button('📋 اختيار الأعمدة','edit:'+id)]);
      if(job.status!=='active')rows.push([button('إخراج من الطابور','remove:'+id,'danger')]);
      if(job.status==='active')rows.push([button('🛠 إصلاح أخطاء الجلسة','repair')]);
      rows.push([button('‹ الطابور','queue:0')]);
      return {text:'<b>'+escape(job.filename)+'</b>\nالورقة: '+escape(job.sheet_name)+'\n\nيمكن تعديل أعمدة الشيت المنتظر قبل بدء معالجته.',rows};
    }
    if(view.startsWith('map:demo:')) {
      const parts=view.split(':'),action=parts[2];
      if(action==='field'||action==='all')return panels[view] || review();
      if(action==='sheets')return {text:'<b>اختر ورقة العمل · عينة</b>',rows:[[button('العملاء','map:demo:sheet:0')],[button('‹ مراجعة الشيت','map:demo:review')]]};
      if(action==='sheet')return review();
      if(action==='set') {draft.mapping[parts[3]]=parts[4]==='-'?null:parts[4];if(parts[3]==='lookup_col'&&!draft.advanced)draft.mapping.service_col=draft.mapping.lookup_col;}
      if(action==='advanced'){draft.advanced=!draft.advanced;if(!draft.advanced)draft.amount_target='remaining';}
      if(action==='mode')draft.mode=parts[3];
      if(action==='target')draft.amount_target=parts[3];
      if(action==='save') {
        if(validation().length)return review();
        if(editing){const job=jobs.find(j=>j.id===editing);if(job)job.mapping=structuredClone(draft.mapping);editing=null;}
        else jobs.push({id:'demo_'+Date.now(),filename:draft.filename,sheet_name:draft.sheet_name,status:'pending',completed:0,total_records:2,mapping:structuredClone(draft.mapping)});
        const p=notice('✓ حُفظت الأعمدة في المعاينة','الشيت التجريبي في الطابور. لم تُضف أي مهمة إلى البرنامج الحقيقي.');
        p.rows.unshift([button('▶ بدء الجولة','start_queue','success')],[button('📎 إضافة ملف آخر','upload')]);return p;
      }
      return review();
    }
    return panels[view] || notice('زر غير متاح','افتح القائمة الرئيسية.');
  }
  return {panel};
})();
