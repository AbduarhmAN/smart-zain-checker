/** Browse loaded results without sending checker requests. */
const ResultBrowser = {
  rows: [], indexedRows: [], filteredRows: [], page: 1, pageSize: 50,
  init() {
    document.addEventListener('keydown', event => {
      const modalOpen=[...document.querySelectorAll('.modal-overlay')].some(el=>el.style.display!=='none');
      if((event.ctrlKey || event.metaKey) && event.key.toLowerCase()==='k' && !modalOpen){
        event.preventDefault();Dashboard.showResults();document.getElementById('resultSearch').select();
      }
    });
    document.getElementById('resultDetailOverlay').addEventListener('click', event=>{
      if(event.target.id==='resultDetailOverlay')this.closeDetail();
    });
  },
  normalize(value) {
    return String(value ?? '').normalize('NFKC').toLocaleLowerCase()
      .replace(/[٠-٩]/g,n=>String('٠١٢٣٤٥٦٧٨٩'.indexOf(n)))
      .replace(/[۰-۹]/g,n=>String('۰۱۲۳۴۵۶۷۸۹'.indexOf(n)))
      .replace(/[\u064b-\u065f\u0670\u0640]/g,'').replace(/[أإآٱ]/g,'ا').replace(/ى/g,'ي')
      .replace(/\s+/g,' ').trim();
  },
  type(row) {return ['wallet','service'].includes(row.record_type || row.type)?'wallet':'account';},
  number(row) {return row.lookup_number ?? row.number ?? '—';},
  customer(row) {return row.customer_name || row.name || 'عميل غير محدد';},
  amount(row, live=false) {
    const value=live?(row.live_amount ?? row.live_sar):(row.expected_amount ?? row.expected_sar);
    if(value==null || String(value).trim()==='')return null;
    const amount=Number(value);return Number.isFinite(amount)?amount:null;
  },
  money(value) {return value==null?'—':value.toFixed(2);},
  exactDetailAmount(row, live=false) {
    const value=live?(row.live_amount ?? row.live_sar):(row.expected_amount ?? row.expected_sar);
    if(value==null || typeof value==='boolean')return null;
    const text=String(value).trim();
    return /^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(text) && Number.isFinite(Number(text))?text:null;
  },
  statusLabel(status) {return {match:'مطابقة',mismatch:'فرق رصيد',needs_review:'تحتاج مراجعة',not_found:'غير موجود',error:'خطأ'}[status] || 'غير محددة';},
  renderTable(rows, preview=false) {
    const list=Array.isArray(rows)?rows:[];
    const context=JSON.stringify([preview,Dashboard.live?.workbook,document.getElementById('selectWorkbook').value,document.getElementById('selectSheet').value]);
    const signature=JSON.stringify([list,context,!!Dashboard.live?.running,!!Dashboard.live?.round_pending]);
    if(signature===this.signature){this.syncPagination();return;}
    if(context!==this.context){this.page=1;if(this.detailRow)this.closeDetail();}
    this.signature=signature;this.context=context;this.preview=preview;this.rows=list;
    this.indexedRows=list.map(row=>({row,text:this.normalize([row.row,this.number(row),this.customer(row),this.type(row)==='wallet'?'خدمة':'عقد',this.statusLabel(row.status),this.money(this.amount(row)),this.money(this.amount(row,true))].join(' '))}));
    this.filterTable(false);
  },
  scheduleSearch() {clearTimeout(this.searchTimer);this.searchTimer=setTimeout(()=>this.filterTable(),140);},
  clearSearch() {clearTimeout(this.searchTimer);document.getElementById('resultSearch').value='';this.filterTable();document.getElementById('resultSearch').focus({preventScroll:true});},
  filterTable(reset=true) {
    clearTimeout(this.searchTimer);
    if(reset)this.page=1;
    const query=this.normalize(document.getElementById('resultSearch').value),type=document.getElementById('resultType').value;
    document.getElementById('resultSearchClear').hidden=!document.getElementById('resultSearch').value;
    const terms=query.split(' ').filter(Boolean);
    this.filteredRows=this.indexedRows.filter(({row,text})=>
      (LiveFeed.activeTab==='all' || LiveFeed.activeTab==='queue' || (LiveFeed.activeTab==='review'?['needs_review','not_found'].includes(row.status):row.status===LiveFeed.activeTab)) &&
      (type==='all' || this.type(row)===type) && terms.every(term=>text.includes(term))).map(item=>item.row);
    this.paintPage();
  },
  changePage(delta) {this.page+=delta;this.paintPage();document.querySelector('#regularTableContainer .table-scroll').scrollTop=0;},
  syncPagination() {
    const count=this.filteredRows.length,total=this.rows.length,pages=Math.max(1,Math.ceil(count/this.pageSize));
    this.page=Math.max(1,Math.min(this.page,pages));
    document.getElementById('resultPrev').disabled=this.page===1;
    document.getElementById('resultNext').disabled=this.page===pages;
    document.getElementById('resultPageLabel').textContent=`${this.page} / ${pages}`;
    document.getElementById('tableTabBadge').textContent=`${count} سجل`;
    document.getElementById('visibleResultsLabel').textContent=count?`${(this.page-1)*this.pageSize+1}–${Math.min(this.page*this.pageSize,count)} من ${count} نتيجة · ${total} محمّلة`:total?`لا توجد نتائج مطابقة للبحث · ${total} محمّلة`:'لا توجد نتائج معروضة';
  },
  paintPage() {
    this.syncPagination();
    const tbody=document.getElementById('auditTableBody');
    if(!this.filteredRows.length){
      const pending=!this.preview && (App.isRunning || Dashboard.live?.round_pending);
      const title=this.rows.length?'لا توجد نتائج مطابقة':pending?'النتائج بعد اكتمال الجولة':this.preview?'لا توجد سجلات للمعاينة':'كل نتيجة تبدأ من ملف';
      const hint=this.rows.length?'غيّر عبارة البحث أو نوع الرقم أو تبويب الحالة.':pending?'تابع التقدم أعلاه. يمكنك إيقاف الفحص مؤقتًا من إعدادات الملف.':this.preview?'يمكنك تحميل الملف أو اختيار ورقة أخرى.':'ارفع شيت العملاء وراجع أعمدته، ثم ابدأ الفحص.';
      tbody.innerHTML=`<tr><td colspan="7"><div class="empty-state">${Dashboard.icon('file')}<strong>${title}</strong><p>${hint}</p></div></td></tr>`;
      return;
    }
    const fragment=document.createDocumentFragment();
    this.filteredRows.slice((this.page-1)*this.pageSize,this.page*this.pageSize).forEach(row=>{
      const tr=document.createElement('tr');tr.dataset.status=row.status || '';tr.className='row-'+(row.status || 'unknown');
      const expected=this.amount(row),live=this.amount(row,true),statusClass={match:'badge-match',mismatch:'badge-mismatch',error:'badge-error'}[row.status] || '';
      tr.innerHTML=`<td><span class="mono">${Dashboard.escape(row.row ?? '—')}</span></td><td>${this.type(row)==='wallet'?'خدمة':'عقد'}</td><td><button class="result-detail-button mono" aria-label="${Dashboard.escape('تفاصيل الرقم '+this.number(row)+'، السطر '+(row.row ?? '—'))}">${Dashboard.escape(this.number(row))}</button></td><td>${Dashboard.escape(this.customer(row))}</td><td><span class="mono">${this.money(expected)}</span>${expected==null?'':' ر.س'}</td><td><span class="mono ${row.status==='mismatch'?'result-mismatch-amount':''}">${this.money(live)}</span>${live==null?'':' ر.س'}</td><td><div class="result-status"><span class="badge ${statusClass}">${this.statusLabel(row.status)}</span></div></td>`;
      tr.querySelector('.result-detail-button').onclick=()=>this.openDetail(row);
      const rowNumber=Number(row.row);
      if(['error','needs_review'].includes(row.status) && Number.isInteger(rowNumber) && rowNumber>0){
        const button=document.createElement('button');button.className='btn-repair-row';button.textContent='إعادة المحاولة';button.title='إعادة فحص هذا السطر';button.onclick=()=>App.repairSingleRow(rowNumber);tr.querySelector('.result-status').appendChild(button);
      }
      fragment.appendChild(tr);
    });
    tbody.replaceChildren(fragment);
  },
  openDetail(row) {
    this.detailRow=row;
    const values={Title:'السطر '+(row.row ?? '—'),Type:this.type(row)==='wallet'?'رقم خدمة':'رقم عقد',Number:this.number(row),Customer:this.customer(row),Row:row.row ?? '—',Status:this.statusLabel(row.status),Expected:this.exactDetailAmount(row) ?? '—',Live:this.exactDetailAmount(row,true) ?? '—',Note:row.error_message || row.error_details || row.issue || row.reason || row.details || row.message || 'لا توجد ملاحظات مسجلة لهذه النتيجة.'};
    Object.entries(values).forEach(([key,value])=>document.getElementById('resultDetail'+key).textContent=String(value));
    const missing={Number:this.number(row)==='—' || String(this.number(row)).trim()==='',Expected:this.exactDetailAmount(row)===null,Live:this.exactDetailAmount(row,true)===null};
    Object.entries(missing).forEach(([field,unavailable])=>{const element=document.getElementById('resultDetail'+field);element.dataset.copyUnavailable=String(unavailable);element.setAttribute('aria-disabled',String(unavailable));element.tabIndex=unavailable?-1:0;});
    document.getElementById('resultDetailOverlay').style.display='flex';Dashboard.modalOpened('resultDetailOverlay');document.getElementById('resultDetailClose').focus();
  },
  closeDetail() {document.getElementById('resultDetailOverlay').style.display='none';this.detailRow=null;const target=Dashboard.focusBeforeModal?.isConnected?Dashboard.focusBeforeModal:document.getElementById('resultSearch');target.focus({preventScroll:true});},
  async copyDetailNumber() {
    if(!this.detailRow)return;
    if(this.number(this.detailRow)==='—' || String(this.number(this.detailRow)).trim()==='')return;
    try{await navigator.clipboard.writeText(String(this.number(this.detailRow)));Dashboard.toast('تم نسخ رقم البحث');}
    catch{const range=document.createRange();range.selectNodeContents(document.getElementById('resultDetailNumber'));window.getSelection().removeAllRanges();window.getSelection().addRange(range);Dashboard.toast('حددنا الرقم؛ اضغط Ctrl+C لنسخه.');}
  },
  async copyDetailAmount(live=false) {
    if(!this.detailRow)return;
    const value=this.exactDetailAmount(this.detailRow,live);
    if(value===null)return;
    try{await navigator.clipboard.writeText(value);Dashboard.toast('تم نسخ المبلغ كاملًا');}
    catch{const range=document.createRange();range.selectNodeContents(document.getElementById(live?'resultDetailLive':'resultDetailExpected'));window.getSelection().removeAllRanges();window.getSelection().addRange(range);Dashboard.toast('حددنا المبلغ؛ اضغط Ctrl+C لنسخه.');}
  }
};
