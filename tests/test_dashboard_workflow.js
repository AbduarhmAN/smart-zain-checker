// Offline interaction tests for the actual dashboard controllers. No live APIs.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const nodes = new Map();
function element(id) {
  if (!nodes.has(id)) nodes.set(id, {value:'',innerHTML:'',style:{},options:[],disabled:false,
    appendChild(option){this.options.push(option);},insertBefore(option){this.options.unshift(option);},
    focus(){},textContent:'',open:false});
  return nodes.get(id);
}
let uploads = [], inFlight = 0, maximum = 0, reviews = [], roundStarts = 0;
const context = vm.createContext({console,setTimeout,clearTimeout,Promise,Array,
  window:{addEventListener(){}},
  document:{getElementById:element,createElement:()=>({}),querySelector:()=>({value:'remaining'})},
  FileReader:class {
    readAsDataURL(file){setTimeout(()=>{this.result='data:application/octet-stream;base64,TEST';this.onload();},1);}
  },
  API:{async uploadWorkbook(filename){
    inFlight++;maximum=Math.max(maximum,inFlight);uploads.push(filename);
    await new Promise(resolve=>setTimeout(resolve,2));inFlight--;
    if(filename==='bad.xlsx')throw new Error('Unreadable workbook');
    return {status:'ok',filename:'saved_'+filename,sheets:['Customers','Other']};
  }},
  QueueUI:{lastJobs:[],startQueue:()=>roundStarts++},
  Dashboard:{toast(){},connected:true,icon:()=>'',focusBeforeModal:null},
  ColumnMapper:{async openModal(){reviews.push(element('selectWorkbook').value);}}
});
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
vm.runInContext(fs.readFileSync(path.join(root,'web_ui/static/js/app.js'),'utf8'),context);
const app=vm.runInContext('App',context);
const updateControls=app.updateControlButtons.bind(app);
app.updateControlButtons=()=>{};
app.isRunning=false;

(async()=>{
  await app.handleFilesUpload([{name:'one.xlsx'},{name:'bad.xlsx'},{name:'two.xlsx'}]);
  assert.equal(maximum,1,'Uploads must run sequentially, including after a rejection');
  assert.deepEqual(uploads,['one.xlsx','bad.xlsx','two.xlsx']);
  assert.deepEqual(Array.from(app.uploadReviewFiles,f=>f.filename),['saved_one.xlsx','saved_two.xlsx']);
  assert.deepEqual(reviews,['saved_one.xlsx']);
  assert.equal(app.uploading,false);assert.equal(app.uploadBatch,false);
  await app.reviewNextUploadedFile('saved_one.xlsx');
  assert.equal(element('selectWorkbook').value,'saved_two.xlsx');
  assert.equal(app.uploadReviewFiles.length,1);
  await app.reviewNextUploadedFile('saved_two.xlsx');
  assert.equal(app.uploadReviewFiles.length,0);
  context.QueueUI.lastJobs=[{status:'pending'},{status:'pending'}];
  updateControls();
  assert.match(element('btnActionRun').innerHTML,/بدء الجولة/);
  element('btnActionRun').onclick();assert.equal(roundStarts,1);
  app.uploadBatch=true;updateControls();assert.equal(element('btnActionRun').disabled,true);
  app.uploadBatch=false;context.QueueUI.starting=true;updateControls();
  assert.match(element('btnActionRun').textContent,/جارٍ التحقق/);
  context.QueueUI.starting=false;
  app.uploadReviewFiles=[{filename:'unreviewed.xlsx'}];
  updateControls();
  assert.match(element('btnActionRun').innerHTML,/تحقق من الملفات المرفوعة/);
  assert.equal(roundStarts,1,'Unreviewed uploads must not immediately start the queue');

  vm.runInContext(fs.readFileSync(path.join(root,'web_ui/static/js/column_mapper.js'),'utf8'),context);
  const mapper=vm.runInContext('ColumnMapper',context);
  mapper.currentAnalysis={letters:{lookup_col:'A',service_col:'B',amount_col:'C',customer_col:'D'}};
  element('mapLookupCol').value='';element('mapServiceCol').value='B';element('mapRemainingCol').value='C';
  element('mapContractCol').value='';element('mapCustomerCol').value='';element('mapCollectorCol').value='';
  element('mapperAdvanced').open=true;
  const explicit=mapper.getMappingPayload();
  assert.equal(explicit.lookup_col,'');assert.equal(explicit.service_col,'B');
  assert.equal(explicit.customer_col,'');assert.equal(explicit.amount_col_2,'');
  element('mapperAdvanced').open=false;element('mapLookupCol').value='B';
  assert.equal(mapper.getMappingPayload().service_col,'B');
  const validationCalls=[];
  context.API.validateSheet=payload=>new Promise((resolve,reject)=>validationCalls.push({payload,resolve,reject}));
  element('selectAuditMode').value='smart_hybrid';
  const outdated=mapper.validateCurrent();
  element('mapRemainingCol').value='E';
  const latest=mapper.validateCurrent();
  validationCalls[1].resolve({validation:{ok:true,rows:2}});await latest;
  validationCalls[0].reject(new Error('Old invalid mapping'));await outdated;
  assert.equal(mapper.currentValidated,true,'A stale validation response must not disable the latest valid mapping');
  assert.equal(element('btnMapperQueue').disabled,false);
  const invalid=mapper.validateCurrent();
  validationCalls[2].reject(new Error('Missing amount in row 3'));await invalid;
  assert.equal(element('btnMapperQueue').disabled,true);
  assert.match(element('mapValidationMessage').textContent,/row 3/);
  element('mapHasHeaders').checked=false;
  assert.equal(mapper.getMappingPayload().has_headers,false);
  mapper.currentAnalysis={has_headers:false,columns:[{col_index:1,col_letter:'A',header:'<script>'},
    {col_index:2,col_letter:'B',header:''},{col_index:3,col_letter:'C',header:'Extra'}],
    preview_rows:[{row_number:1,values:['2000000001','0','<img onerror="bad">']}]};
  mapper.renderPreview();
  assert.match(element('mapperPreviewTable').innerHTML,/C · Extra/);
  assert.match(element('mapperPreviewTable').innerHTML,/&lt;img/);
  assert.doesNotMatch(element('mapperPreviewTable').innerHTML,/<img|<script/);
  mapper.currentAnalysis={letters:{}};
  element('mapLookupCol').value='';element('mapRemainingCol').value='';
  await mapper.validateCurrent();
  assert.equal(validationCalls.length,3,'Missing selections should prompt mapping before requesting row validation');
  assert.match(element('docAcceptBadge').textContent,/اختر عمود الرقم/);
  assert.equal(element('btnMapperQueue').disabled,true);
  console.log('PASS: sequential multi-file uploads, review order, rejected-file recovery, round start gating, and explicit column selections');
})().catch(error=>{console.error(error);process.exitCode=1;});
