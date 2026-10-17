/** Small, stable worker indicators. Details open only on operator request. */
const WorkerMonitor = {
  workers: [], selected: null, buttons: new Map(), busy: false,
  init() {
    document.getElementById('workerDetailsOverlay').addEventListener('click',event=>{
      if(event.target.id==='workerDetailsOverlay')this.close();
    });
  },
  key(worker,index) {return String(worker.worker_id ?? index);},
  name(worker,index) {return worker.name || (worker.worker_id==='worker_1'?'المسار المباشر ١':worker.worker_id==='worker_2'?'المسار المباشر ٢':`مسار ${index+1}`);},
  state(worker) {
    if(!Dashboard.connected)return 'offline';
    if(worker.status==='error')return 'error';
    if(worker.service_verification_required || worker.in_cooldown || worker.waiting_for_shared_service_browser)return 'warn';
    if(worker.status==='processing' && !Dashboard.live?.paused)return 'busy';
    if(worker.status==='ready')return 'ready';
    return 'idle';
  },
  label(worker) {
    if(!Dashboard.connected)return 'آخر حالة محفوظة · انقطع الاتصال';
    if(worker.service_verification_required)return 'الخدمات تنتظر التحقق';
    if(worker.status==='error')return 'تعذر تشغيل المسار';
    if(worker.in_cooldown)return 'استراحة مؤقتة';
    if(worker.waiting_for_shared_service_browser)return 'بانتظار دوره';
    if(Dashboard.live?.paused)return 'الفحص متوقف مؤقتًا';
    return {processing:'فحص نشط',ready:'جاهز',stopped:'متوقف',idle:'بانتظار الفحص'}[worker.status] || 'حالة غير محددة';
  },
  update(workers) {
    this.workers=Array.isArray(workers)?workers:[];
    const group=document.getElementById('workerPillGroup'),keys=this.workers.map((w,i)=>this.key(w,i));
    if(JSON.stringify(keys)!==this.keys){
      this.keys=JSON.stringify(keys);this.buttons=new Map();
      const fragment=document.createDocumentFragment();
      this.workers.forEach((worker,index)=>{
        const key=keys[index],button=document.createElement('button');
        button.type='button';button.dataset.workerId=key;button.innerHTML='<span class="worker-mini-dot" aria-hidden="true"></span><span class="worker-mini-index" aria-hidden="true"></span>';
        button.querySelector('.worker-mini-index').textContent=String(index+1);
        button.onclick=()=>this.open(key);this.buttons.set(key,button);fragment.appendChild(button);
      });
      if(!keys.length){const empty=document.createElement('span');empty.className='worker-empty';empty.textContent='لا توجد مسارات مفعّلة';fragment.appendChild(empty);}
      group.replaceChildren(fragment);
    }
    let active=0,attention=0;
    this.workers.forEach((worker,index)=>{
      const button=this.buttons.get(keys[index]),state=this.state(worker),className='worker-mini '+state;
      if(button.className!==className)button.className=className;
      button.title=this.name(worker,index)+' · '+this.label(worker);
      button.setAttribute('aria-label',button.title+' · عرض التفاصيل');
      if(state==='busy')active++;
      if(worker.service_verification_required || worker.status==='error')attention++;
    });
    const summary=document.getElementById('workerSummary');
    summary.textContent=!Dashboard.connected?'الاتصال منقطع':this.workers.length?`${active} نشطة${attention?' · '+attention+' تحتاج متابعة':''}`:'';
    summary.classList.toggle('needs-attention',attention>0 && Dashboard.connected);
    if(this.selected!==null)this.renderDetails();
  },
  open(key) {
    this.selected=key;this.renderDetails();
    document.getElementById('workerDetailsOverlay').style.display='flex';Dashboard.modalOpened('workerDetailsOverlay');document.getElementById('workerDetailsClose').focus({preventScroll:true});
  },
  renderDetails() {
    const index=this.workers.findIndex((worker,i)=>this.key(worker,i)===this.selected),worker=this.workers[index];
    if(!worker){this.close();return;}
    const wait=Math.max(Number(worker.cooldown_remaining_seconds)||0,Number(worker.service_retry_seconds)||0);
    const values={Title:this.name(worker,index),State:this.label(worker),Route:worker.use_proxy?'عبر بروكسي':'مباشر',Wait:wait>0?SessionTiming.duration(wait):'لا يوجد انتظار مسجل',Note:worker.service_verification_required?'أكمل التحقق المطلوب للمسار؛ تواصل المسارات الأخرى الفحص.':worker.waiting_for_shared_service_browser?'خدمة أخرى تستخدم الاتصال نفسه. يبدأ هذا المسار عند انتهاء دورها.':worker.status_reason || ''};
    Object.entries(values).forEach(([key,value])=>{document.getElementById('workerDetails'+key).textContent=value;});
    document.getElementById('workerDetailsNote').hidden=!values.Note;
    const retry=document.getElementById('workerDetailsRetry');
    retry.hidden=!(worker.service_verification_required && !worker.service_verification_pending);
    retry.disabled=this.busy || !Dashboard.connected;
  },
  close() {
    document.getElementById('workerDetailsOverlay').style.display='none';this.selected=null;
    const target=Dashboard.focusBeforeModal?.isConnected?Dashboard.focusBeforeModal:document.getElementById('resultSearch');target.focus({preventScroll:true});
  },
  async resumeService() {
    const worker=this.workers.find((w,i)=>this.key(w,i)===this.selected);
    if(!worker || this.busy || !Dashboard.connected)return;
    this.busy=true;this.renderDetails();
    try{await API.post('/api/service-route/resume',{worker_id:worker.worker_id});await App.poll();}
    catch(error){Dashboard.toast(error.message,'error');}
    finally{this.busy=false;if(this.selected!==null)this.renderDetails();}
  }
};
