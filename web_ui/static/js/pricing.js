const Pricing = {
  settings: null, available: false, loading: true, visible: false, jobs: [], live: null, busy: false, scope: null,
  defaults: {mode:'sheet', customer_price:'0.00', sheet_price:'0.00'},
  async init() {
    try {const res=await API.get('/api/pricing');this.settings=res.settings;this.available=true;}
    catch {this.available=false;}
    this.loading=false;
    this.render();
  },
  money(minor) {return (Number(minor || 0)/100).toLocaleString('ar-EG',{minimumFractionDigits:2,maximumFractionDigits:2})+' ج.م';},
  readPrice(value) {
    const text=String(value).trim().replace(/[٠-٩]/g,n=>'٠١٢٣٤٥٦٧٨٩'.indexOf(n)).replace(/[۰-۹]/g,n=>'۰۱۲۳۴۵۶۷۸۹'.indexOf(n)).replace('٫','.');
    if(!/^\d+(\.\d{1,2})?$/.test(text) || Number(text)>1000000)throw new Error('أدخل سعرًا غير سالب، بمنزلتين عشريتين كحد أقصى.');
    return Number(text).toFixed(2);
  },
  calculate(quote,total,completed,status) {
    if(!quote)return null;
    const checked=Math.max(0,Math.min(total,completed)), unit=Number(quote.unit_minor);
    const billed=quote.mode==='customer'?unit*checked:['completed','archived'].includes(status) && total>0 && checked===total?unit:0;
    const partner=Math.round(billed*30/100);
    return {...quote,total_rows:total,checked_rows:checked,expected_minor:unit*(quote.mode==='customer'?total:total>0?1:0),
      billed_minor:billed,partner_minor:partner,owner_minor:billed-partner,configured:unit>0};
  },
  onJobs(jobs) {this.jobs=jobs;this.render();},
  update(data) {this.live=data;this.render();},
  toggle() {this.visible=!this.visible;this.render();},
  summaries() {
    return this.jobs.map(job=>{
      const live=this.live;
      const matches=live && !live._preview && live.current_job_id && live.current_job_id===job.id;
      return matches && live.billing ? live.billing : job.billing || this.calculate(job.pricing,job.total_records||0,job.completed||0,job.status);
    });
  },
  render() {
    const body=document.getElementById('billingValues'),button=document.getElementById('btnBillingVisibility');
    if(!body || !button)return;
    body.hidden=false;
    button.setAttribute('aria-pressed',String(this.visible));
    button.setAttribute('aria-label',this.visible?'إخفاء مبالغ الخدمة':'إظهار مبالغ الخدمة');
    button.title=this.visible?'إخفاء مبالغ الخدمة':'إظهار مبالغ الخدمة';
    button.innerHTML=Dashboard.icon(this.visible?'eye-off':'eye');
    const items=this.summaries(), priced=items.filter(b=>b?.configured), sum=key=>priced.reduce((n,b)=>n+b[key],0);
    document.getElementById('billingStatus').textContent=this.loading?'جارٍ تحميل إعدادات التسعير…':!this.available?'إعدادات التسعير تحتاج تشغيل النسخة المحدّثة':
      !this.jobs.length?'حدّد السعر من الإعدادات أو عند إضافة الشيت':items.some(b=>!b?.configured)?'بعض الشيتات لم يُحدّد سعرها':'أسعار محفوظة لكل شيت';
    ['billed_minor','owner_minor','partner_minor','expected_minor'].forEach((key,i)=>{
      document.getElementById(['billingTotal','billingOwner','billingPartner','billingExpected'][i]).textContent=this.visible?this.money(sum(key)):'•••';
    });
    document.querySelectorAll('[data-billing-job]').forEach(el=>{
      const job=this.jobs.find(j=>j.id===el.dataset.billingJob),b=job?.billing || this.calculate(job?.pricing,job?.total_records||0,job?.completed||0,job?.status);
      el.textContent=!b?.configured?'غير مسعّر':(b.mode==='customer'?'بالعميل':'بالشيت')+' · '+(this.visible?this.money(b.unit_minor):'•••• ج.م')+(b.mode==='customer'?' / صف':'')+' · '+(this.visible?this.money(b.expected_minor):'•••• ج.م')+' متوقع';
    });
  },
  open(jobId=null) {
    if(this.loading){Dashboard.toast('جارٍ تحميل إعدادات التسعير؛ حاول بعد لحظة.');return;}
    this.scope=jobId;
    const job=jobId?this.jobs.find(j=>j.id===jobId):null,defaults=this.settings || this.defaults;
    document.getElementById('pricingTitle').textContent=job?'تسعير هذا الشيت':'إعدادات تسعير الخدمة';
    document.getElementById('pricingContext').textContent=job?job.filename+' · '+job.sheet_name:'أسعار افتراضية للشيتات الجديدة. حصة الشريك ٣٠٪ وحصتك ٧٠٪.';
    document.getElementById('pricingMode').value=job?.pricing?.mode || defaults.mode;
    document.getElementById('pricingCustomer').value=job?.pricing?.mode==='customer'?(job.pricing.unit_minor/100).toFixed(2):defaults.customer_price;
    document.getElementById('pricingSheet').value=job?.pricing?.mode==='sheet'?(job.pricing.unit_minor/100).toFixed(2):defaults.sheet_price;
    document.getElementById('pricingFeedback').textContent=this.available?'': 'أعد تشغيل البرنامج لتفعيل حفظ إعدادات التسعير.';
    document.getElementById('btnPricingSave').disabled=!this.available;
    document.getElementById('pricingOverlay').style.display='flex';Dashboard.modalOpened('pricingOverlay');
    this.changeMode();document.getElementById('pricingMode').focus();
  },
  changeMode() {
    const mode=document.getElementById('pricingMode').value;
    document.getElementById('pricingCustomerField').hidden=this.scope!==null && mode!=='customer';
    document.getElementById('pricingSheetField').hidden=this.scope!==null && mode!=='sheet';
    document.getElementById('pricingCustomer').disabled=this.scope!==null && mode!=='customer';
    document.getElementById('pricingSheet').disabled=this.scope!==null && mode!=='sheet';
  },
  close() {if(this.busy)return;document.getElementById('pricingOverlay').style.display='none';Dashboard.focusBeforeModal?.focus();},
  async save() {
    if(this.busy || !this.available)return;
    const feedback=document.getElementById('pricingFeedback');
    try {
      const mode=document.getElementById('pricingMode').value;
      let payload;
      if(this.scope!==null){payload={job_id:this.scope,mode,unit_price:this.readPrice(document.getElementById(mode==='customer'?'pricingCustomer':'pricingSheet').value)};}
      else payload={mode,customer_price:this.readPrice(document.getElementById('pricingCustomer').value),sheet_price:this.readPrice(document.getElementById('pricingSheet').value)};
      this.busy=true;document.getElementById('btnPricingSave').disabled=true;feedback.textContent='جارٍ الحفظ…';
      const res=await API.post(this.scope!==null?'/api/queue/pricing':'/api/pricing',payload);
      if(this.scope===null)this.settings=res.settings;
      await QueueUI.refresh();this.busy=false;this.close();Dashboard.toast('حُفظ السعر');
    } catch(error){feedback.textContent=error.message;}
    finally{this.busy=false;document.getElementById('btnPricingSave').disabled=!this.available;}
  },
  prepareMap() {
    const settings=this.settings || this.defaults;
    document.getElementById('mapPricingMode').value=settings.mode;
    document.getElementById('mapPricingPrice').value=settings[settings.mode+'_price'];
    document.getElementById('mapperPricing').open=true;this.updateMapEstimate();
  },
  changeMapMode() {
    const mode=document.getElementById('mapPricingMode').value;
    document.getElementById('mapPricingPrice').value=(this.settings || this.defaults)[mode+'_price'];this.updateMapEstimate();
  },
  mapQuote() {
    if(!this.available)return undefined;
    return {mode:document.getElementById('mapPricingMode').value,unit_price:this.readPrice(document.getElementById('mapPricingPrice').value)};
  },
  updateMapEstimate() {
    const label=document.getElementById('mapPriceLabel'),hint=document.getElementById('mapPriceEstimate');
    const mode=document.getElementById('mapPricingMode').value;
    label.textContent=mode==='customer'?'سعر الصف المفحوص · ج.م':'سعر الشيت كاملًا · ج.م';
    if(!this.available){hint.textContent='التسعير متاح بعد إعادة تشغيل البرنامج بالنسخة المحدّثة.';return;}
    try{
      const unit=Math.round(Number(this.readPrice(document.getElementById('mapPricingPrice').value))*100),rows=Number(ColumnMapper.currentAnalysis?.estimated_rows)||0;
      hint.textContent=unit? 'المتوقع: '+this.money(unit*(mode==='customer'?rows:1))+
        (mode==='customer'?' · الحساب النهائي على عدد الصفوف المفحوصة.':' · يُحتسب بعد اكتمال الشيت.'):'السعر لم يُحدّد. يمكنك الفحص دون تسعير.';
      if(document.getElementById('mapPricingPrice').disabled)hint.textContent+=' · المتابعة تحتفظ بسعر الجولة السابقة. لتغيير السعر، اختر فحصًا جديدًا.';
    }catch{hint.textContent='أدخل سعرًا صحيحًا.';}
  }
};
