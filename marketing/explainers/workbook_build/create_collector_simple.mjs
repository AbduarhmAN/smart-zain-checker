import fs from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { SpreadsheetFile, Workbook } from '@oai/artifact-tool';

const here=dirname(fileURLToPath(import.meta.url));
const outputDir=resolve(here,'../../../outputs/zain-check-collector-simple-20260924');
const workbook=Workbook.create();
const colors={ink:'#1C3747',muted:'#536B78',white:'#FFFFFF',blue:'#E9F3F7',amber:'#FFF2DC',line:'#DCE7EB'};
const stamp=time=>new Date(`2026-09-24T${time}Z`);
const collector='محصل تجريبي';
const link=(kind,number)=>kind==='رقم حساب'
  ?`https://app.sa.zain.com/ar/contract-payment?contract=${encodeURIComponent(number)}`
  :`https://app.sa.zain.com/ar/quickpay?account=${encodeURIComponent(number)}`;

function addSheet(name,title,context,headers,rows,widths,tableName){
  const ws=workbook.worksheets.add(name);
  const last=String.fromCharCode(64+headers.length),end=4+rows.length;
  ws.showGridLines=false;
  ws.freezePanes.freezeRows(4);
  ws.getRange('A1').values=[[title]];
  ws.getRange('A2').values=[[context]];
  ws.getRange(`A4:${last}4`).values=[headers];
  ws.getRange(`A5:${last}${end}`).values=rows;
  ws.getRange(`A1:${last}${end}`).format.font={name:'Tahoma',size:11,color:colors.ink};
  ws.getRange('A1').format.font={name:'Tahoma',size:17,bold:true,color:colors.ink};
  ws.getRange('A2').format.font={name:'Tahoma',size:10,color:colors.muted};
  ws.getRange(`A4:${last}4`).format={fill:colors.ink,font:{name:'Tahoma',size:10,bold:true,color:colors.white}};
  ws.getRange(`A4:${last}4`).format.rowHeight=40;
  ws.getRange(`A5:${last}${end}`).format.rowHeight=42;
  ws.getRange(`A4:${last}${end}`).format.verticalAlignment='center';
  ws.getRange(`A4:${last}${end}`).format.borders={preset:'inside',style:'thin',color:colors.line};
  for(let i=0;i<widths.length;i++)ws.getRangeByIndexes(0,i,end,1).format.columnWidthPx=widths[i];
  for(let row=5;row<=end;row+=2)ws.getRange(`A${row}:${last}${row}`).format.fill=colors.blue;
  const table=ws.tables.add(`A4:${last}${end}`,true,tableName);
  table.showFilterButton=true;
  return {ws,end,last};
}

// Results: one row per comparable service, or one aggregate row for a searched account.
// The account row never allocates its total across its services.
const resultsData=[
  ['محفظة','2000000101','2000000101','أحمد',21,300,180,'09:00:12'],
  ['محفظة','2000000202','2000000202','منى',22,500,500,'09:01:04'],
  ['محفظة','2000000303','2000000303','سارة',23,10,100,'09:02:17'],
  ['رقم حساب','1000000404','','خالد','24، 25، 26',450,400,'09:04:02'],
  ['محفظة','2000000701','2000000701','ليلى',27,0,250,'09:05:19'],
  ['محفظة','2000000801','2000000801','نور',28,120,0,'09:06:31'],
  ['محفظة','2000002101','2000002101','بسمة',41,200,150,'09:17:41'],
  ['محفظة','2000002201','2000002201','إيمان',42,300,300,'09:18:22'],
  ['محفظة','2000002301','2000002301','كريم',43,100,80,'09:20:09'],
  ['محفظة','2000002401','2000002401','وليد',44,75,70,'09:21:03'],
  ['رقم حساب','1000002525','5000002501','وليد',45,200,190,'09:22:41'],
  ['محفظة','2000002601','2000002601','صفاء',46,250,240,'09:23:34'],
];
const resultsRows=resultsData.map(([kind,number,service,name,source,file,site,time])=>[
  kind,number,service,link(kind,number),name,source,file,site,null,stamp(time),collector,
]);
const results=addSheet('النتائج','نتائج فحص زين','المبالغ بالريال السعودي. عند تعدد الخدمات يظهر فرق الحساب مرة واحدة. البيانات مثال توضيحي.',
  ['نوع السجل','رقم السجل','رقم الخدمة','رابط التحقق','اسم العميل','رقم صف الإكسل','المبلغ في الملف','المبلغ في موقع زين','الفرق','وقت الفحص','اسم المحصل'],
  resultsRows,[110,155,155,405,135,140,135,150,115,190,145],'CollectorResults');
results.ws.freezePanes.freezeColumns(3);
for(let i=0;i<resultsData.length;i++){
  const row=5+i;
  results.ws.getRange(`I${row}`).formulas=[[`=G${row}-H${row}`]];
}
results.ws.getRange(`D5:D${results.end}`).format.font={name:'Tahoma',size:8,color:'#176798'};
results.ws.getRange(`G5:I${results.end}`).setNumberFormat('#,##0.00');
results.ws.getRange(`J5:J${results.end}`).setNumberFormat('yyyy-mm-dd hh:mm:ss');
results.ws.getRange('A8:K8').format.fill=colors.amber;

// This sheet shows source service numbers without attributing the account total to them.
const accountRows=[
  ['1000000404','خالد','2000000401',24,100,80],
  ['1000000404','خالد','5000000402',25,150,null],
  ['1000000404','خالد','0100000403',26,200,null],
  ['1000001111','محمود','5000001101',31,130,null],
  ['1000001111','محمود','0100001102',32,170,null],
];
const accounts=addSheet('الحسابات المتعددة','الحسابات المتعددة','كل خدمة في صفها الأصلي. مبلغ زين للخدمة يظهر فقط إذا قُرئ لها مباشرةً؛ إجمالي الحساب في ورقة النتائج.',
  ['رقم الحساب','اسم العميل','رقم الخدمة','رقم صف الإكسل','مبلغ الخدمة في الملف','مبلغ زين للخدمة إن توفر'],
  accountRows,[160,145,170,145,180,220],'MultipleAccounts');
accounts.ws.getRange(`E5:F${accounts.end}`).setNumberFormat('#,##0.00');
accounts.ws.getRange('A8:F9').format.fill=colors.amber;

const errorData=[
  ['محفظة','2000000901','2000000901','رامي',29,'صفر غير مؤكد','ظهرت خانة مبلغ فارغة؛ لم يعتمدها التقرير صفراً.','09:07:06'],
  ['محفظة','2000001001','2000001001','هبة',30,'انتهاء مهلة الصفحة بعد جلسة جديدة','لم يظهر مبلغ ثابت بعد إعادة البحث والجلسة الجديدة.','09:10:42'],
  ['مسار حساب','5000001101','5000001101','محمود',31,'إعادة توجيه (طبيعي)','بحث البرنامج برقم الخدمة في مسار الحساب المكرر؛ لم يقرأ مبلغاً.','09:11:23'],
  ['مسار حساب','0100001102','0100001102','محمود',32,'إعادة توجيه (طبيعي)','اسم الخطأ لا يثبت سداد هذه الخدمة.','09:12:04'],
  ['محفظة','2000001301','2000001301','هند',33,'إعادة توجيه (غير طبيعي)','استمر التوجيه بعد إعادة المحاولة.','09:14:42'],
  ['محفظة','2000001401','2000001401','باسم',34,'خطأ في صفحة زين','ظهرت رسالة خطأ بدل المبلغ.','09:15:24'],
  ['محفظة','2000001501','2000001501','ياسمين',35,'مبلغ غير صالح من زين','النص المقروء لم يتحول إلى مبلغ رقمي.','09:16:33'],
  ['غير محدد','','','سامح',36,'بيانات غير صالحة','رقم الحساب ورقم الخدمة مفقودان من الملف.',''],
  ['محفظة','2000001717','2000001717','مازن',37,'تعارض بيانات','رقم الخدمة مكرر في صف آخر ببيانات مختلفة.',''],
  ['محفظة','2000001717','2000001717','نادر',38,'تعارض بيانات','يلزم تصحيح الصفين قبل الفحص.',''],
];
const errorRows=errorData.map(([kind,number,service,name,source,error,detail,time])=>[
  kind,number,service,number?link(kind==='مسار حساب'?'رقم حساب':kind,number):null,name,source,error,detail,time?stamp(time):null,collector,
]);
const errors=addSheet('الأخطاء','الأخطاء','أخطاء الفحص وبيانات المصدر. «إعادة توجيه (طبيعي)» تصنيف للبرنامج ولا يثبت السداد.',
  ['نوع السجل','رقم السجل','رقم الخدمة','رابط التحقق','اسم العميل','رقم صف الإكسل','نوع الخطأ','تفاصيل الخطأ','وقت الخطأ','اسم المحصل'],
  errorRows,[125,155,155,405,135,140,260,430,190,145],'CollectorErrors');
errors.ws.freezePanes.freezeColumns(3);
errors.ws.getRange(`D5:D${errors.end}`).format.font={name:'Tahoma',size:8,color:'#176798'};
errors.ws.getRange(`I5:I${errors.end}`).setNumberFormat('yyyy-mm-dd hh:mm:ss');
errors.ws.getRange('A7:J8').format.fill=colors.amber;

for(const [name,range] of [['النتائج','A4:K16'],['الحسابات المتعددة','A4:F9'],['الأخطاء','A4:J14']]){
  const found=await workbook.inspect({kind:'table',range:`${name}!${range}`,include:'values,formulas',tableMaxRows:15,tableMaxCols:11,maxChars:5400});
  console.log(found.ndjson);
}
const formulaErrors=await workbook.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:100},maxChars:1600});
console.log(formulaErrors.ndjson);

await fs.mkdir(outputDir,{recursive:true});
for(const [name,range,file] of [
  ['النتائج','A1:K11','معاينة_النتائج.png'],
  ['الحسابات المتعددة','A1:F9','معاينة_الحسابات.png'],
  ['الأخطاء','A1:J10','معاينة_الأخطاء.png'],
]){
  const preview=await workbook.render({sheetName:name,range,scale:1,format:'png'});
  await fs.writeFile(join(outputDir,file),new Uint8Array(await preview.arrayBuffer()));
}
const file=await SpreadsheetFile.exportXlsx(workbook);
await file.save(join(outputDir,'نتيجة_فحص_زين_للمحصل_مختصرة.xlsx'));
