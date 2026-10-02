import fs from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { SpreadsheetFile, Workbook } from '@oai/artifact-tool';

const here = dirname(fileURLToPath(import.meta.url));
const out = resolve(here, '../../../outputs/zain-check-marketing-20260924');
const wb = Workbook.create();
const C = { ink: '#142F40', teal: '#009681', mint: '#E7F8F2', amber: '#FFF1D9', rose: '#FDECE8', line: '#D8E6E4' };

function format(sheet, lastCol, lastRow, widths) {
  sheet.showGridLines = false;
  sheet.freezePanes.freezeRows(4);
  sheet.getRange(`A1:${lastCol}${lastRow}`).format.font = { name: 'Tahoma', size: 11, color: C.ink };
  sheet.getRange('A1').format.font = { name: 'Tahoma', size: 20, bold: true, color: C.ink };
  sheet.getRange('A2:A3').format.font = { name: 'Tahoma', size: 10, color: '#536D77' };
  sheet.getRange(`A4:${lastCol}4`).format = {
    fill: C.ink, font: { name: 'Tahoma', size: 10, bold: true, color: '#FFFFFF' },
  };
  sheet.getRange(`A4:${lastCol}4`).format.rowHeight = 39;
  sheet.getRange(`A5:${lastCol}${lastRow}`).format.rowHeight = 37;
  sheet.getRange(`A4:${lastCol}${lastRow}`).format.verticalAlignment = 'center';
  sheet.getRange(`A4:${lastCol}${lastRow}`).format.borders = {
    preset: 'inside', style: 'thin', color: C.line,
  };
  widths.forEach((width, index) => sheet.getRangeByIndexes(0, index, lastRow, 1).format.columnWidthPx = width);
}

const all = wb.worksheets.add('كل الفحوصات');
all.getRange('A1').values = [['كل الفحوصات | ماذا حدث لكل سجل؟']];
all.getRange('A2').values = [['كل صف مصدر يظهر مرة واحدة، حتى إن تطابق أو تعذر البحث عنه. الأرقام والأسماء هنا وهمية.']];
all.getRange('A3').values = [['وقت آخر فحص بالقاهرة حتى الثانية؛ الفرق يحسب فقط إذا كان مبلغ الملف ومبلغ زين لنفس النطاق.']];
all.getRange('A4:T4').values = [[
  'معرّف', 'صف الملف', 'اسم العميل', 'رقم الحساب', 'رقم المديونية/الخدمة', 'نوعها',
  'مبلغ الملف', 'رقم البحث المستخدم', 'نطاق الملف', 'مبلغ زين', 'نطاق زين',
  'المقارنة صالحة؟', 'الفرق', 'النتيجة', 'سبب/توضيح', 'وقت آخر فحص (القاهرة)',
  'عدد المحاولات', 'حالة التسوية من الملف', 'قرار المحصّل', 'مرجع الدليل',
]];
all.getRange('A5:T11').values = [
  ['R01', 12, 'عميل أ', '1000004182', '2055••••731', 'جوال', 300, '2055••••731', 'مديونية', 180, 'مديونية', 'نعم', null, null, 'انخفاض ظاهر؛ تحقق من السداد', '2026-09-24 09:00:12 +03:00', 3, '', 'بانتظار مراجعة', ''],
  ['R02', 13, 'عميل ب', '1000006200', '01••••842', 'أرضي', 100, '1000006200', 'مديونية', 300, 'حساب', 'لا', null, null, 'مبلغ زين يشمل الحساب كله', '2026-09-24 09:05:47 +03:00', 1, '', 'راجع الحساب', ''],
  ['R03', 14, 'عميل ب', '1000006200', '2055••••444', 'إنترنت', 200, '1000006200', 'مديونية', 300, 'حساب', 'لا', null, null, 'نفس إجمالي حساب R02؛ لا تطرحه مرتين', '2026-09-24 09:06:03 +03:00', 1, '', 'راجع الحساب', ''],
  ['R04', 15, 'عميل ج', '1000009170', '1000009170', 'حساب', 0, '1000009170', 'حساب', 250, 'حساب', 'نعم', null, null, 'تسوية مذكورة بالملف ولم تنعكس في زين', '2026-09-24 09:10:06 +03:00', 3, 'مذكورة؛ مرجع تجريبي SET-01', 'راجع سند التسوية', ''],
  ['R05', 16, 'عميل د', '1000002345', '2055••••561', 'جوال', 500, '2055••••561', 'مديونية', 500, 'مديونية', 'نعم', null, null, 'المبلغان متطابقان وقت الفحص', '2026-09-24 09:15:32 +03:00', 1, '', 'لا يلزم', ''],
  ['R06', 17, 'عميل هـ', '1000008008', '2055••••888', 'إنترنت', 400, '2055••••888', 'مديونية', null, 'غير معلوم', 'تعذر', null, null, 'لم يظهر مبلغ صالح؛ أعد الفحص', '2026-09-24 09:20:51 +03:00', 2, '', 'بانتظار إعادة فحص', ''],
  ['R07', 18, 'عميل و', '1000006532', '2055••••923', 'جوال', 100, '2055••••923', 'مديونية', 80, 'مديونية', 'نعم', null, null, 'انخفاض ظاهر؛ تحقق من السداد', '2026-09-24 09:25:09 +03:00', 3, '', 'بانتظار مراجعة', ''],
];
all.getRange('M5').formulas = [['=IF(L5<>"نعم","",G5-J5)']];
all.getRange('M5:M11').fillDown();
all.getRange('N5').formulas = [['=IF(L5="تعذر","تعذر الفحص",IF(L5<>"نعم","غير قابل للمقارنة",IF(ABS(M5)<=0.2,"مطابق",IF(M5>0,"فرق موجب","فرق سالب"))))']];
all.getRange('N5:N11').fillDown();
format(all, 'T', 11, [80, 95, 130, 145, 170, 110, 115, 170, 125, 115, 125, 130, 100, 145, 320, 225, 125, 235, 170, 150]);
all.getRange('G5:G11').setNumberFormat('#,##0.00');
all.getRange('J5:J11').setNumberFormat('#,##0.00');
all.getRange('M5:M11').setNumberFormat('#,##0.00');
all.getRange('A5:T5').format.fill = C.mint;
all.getRange('A6:T7').format.fill = C.amber;
all.getRange('A8:T8').format.fill = C.rose;
all.getRange('A10:T10').format.fill = C.amber;
all.getRange('A11:T11').format.fill = C.mint;
all.getRange('S5:T11').format.fill = '#FFF8E8';

const positive = wb.worksheets.add('الفروقات الموجبة');
positive.getRange('A1').values = [['الفروقات الموجبة القابلة للمقارنة']];
positive.getRange('A2').values = [['تظهر هنا النتائج الموجبة فقط عندما يقارن البرنامج مديونية بمديونية أو حساباً بحساب مكتمل.']];
positive.getRange('A3').values = [['الفرق الموجب مؤشر متابعة؛ «دفع مؤكد» يحتاج قرار المحصّل ودليلاً.']];
positive.getRange('A4:K4').values = [[
  'معرّف', 'اسم العميل', 'رقم الحساب', 'المديونية', 'مستوى المقارنة', 'مبلغ الملف',
  'مبلغ زين', 'الفرق الموجب', 'وقت الفحص (القاهرة)', 'الخطوة التالية', 'قرار المحصّل',
]];
positive.getRange('A5:K6').values = [
  ['R01', 'عميل أ', '1000004182', '2055••••731', 'مديونية', 300, 180, 120, '2026-09-24 09:00:12 +03:00', 'راجع سند السداد', 'بانتظار مراجعة'],
  ['R07', 'عميل و', '1000006532', '2055••••923', 'مديونية', 100, 80, 20, '2026-09-24 09:25:09 +03:00', 'راجع سند السداد', 'بانتظار مراجعة'],
];
format(positive, 'K', 6, [85, 155, 160, 175, 165, 130, 130, 145, 230, 205, 170]);
positive.getRange('F5:H6').setNumberFormat('#,##0.00');
positive.getRange('A5:K6').format.fill = C.mint;
positive.getRange('K5:K6').format.fill = '#FFF8E8';

const multiple = wb.worksheets.add('أكثر من مديونية');
multiple.getRange('A1').values = [['نتيجة العميل صاحب أكثر من مديونية']];
multiple.getRange('A2').values = [['التجميع برقم الحساب، لا بالاسم وحده. إجمالي زين يخص الحساب ولا ينسب إلى دين بعينه.']];
multiple.getRange('A3').values = [['عميل ب: ملفه فيه مديونيتان 100 + 200؛ زين يعرض 300 للحساب. لا نعلن سداد أي واحدة حتى تتضح تغطية الحساب.']];
multiple.getRange('A4:J4').values = [[
  'اسم العميل', 'رقم الحساب', 'معرّفات السجلات', 'عدد المديونيات', 'مجموع الملف',
  'إجمالي زين للحساب', 'هل كل الديون موجودة؟', 'فرق الحساب', 'النتيجة الجامعة', 'الإجراء',
]];
multiple.getRange('A5:J5').values = [[
  'عميل ب', '1000006200', 'R02، R03', 2, 300, 300,
  'غير مثبت', null, 'لا يمكن تحديد دين مسدد', 'تحقق من اكتمال الديون وتاريخها',
]];
multiple.getRange('A7:G7').values = [['السجل', 'رقم المديونية', 'نوعها', 'مبلغها في الملف', 'البحث المستخدم', 'النتيجة الفردية', 'ملاحظة']];
multiple.getRange('A8:G9').values = [
  ['R02', '01••••842', 'أرضي', 100, 'رقم الحساب', 'غير قابلة للمقارنة', 'زين يعرض 300 للحساب'],
  ['R03', '2055••••444', 'إنترنت', 200, 'رقم الحساب', 'غير قابلة للمقارنة', 'نفس مبلغ زين؛ لا نحسبه مرتين'],
];
format(multiple, 'J', 9, [145, 160, 170, 145, 130, 165, 165, 125, 260, 300]);
multiple.getRange('A7:G7').format = { fill: C.teal, font: { name: 'Tahoma', size: 10, bold: true, color: '#FFFFFF' } };
multiple.getRange('D8:D9').setNumberFormat('#,##0.00');
multiple.getRange('E5:F5').setNumberFormat('#,##0.00');
multiple.getRange('A5:J5').format.fill = C.amber;

const settlement = wb.worksheets.add('التسويات والاستثناءات');
settlement.getRange('A1').values = [['تسويات وحالات تحتاج تفسيراً']];
settlement.getRange('A2').values = [['ذكر التسوية في الملف لا يعني أنها ظهرت في زين أو تأكدت؛ يلزم سند ومراجع وتاريخ.']];
settlement.getRange('A3').values = [['الحالات غير القابلة للمقارنة أو التي فشل فحصها لا تدخل صفحة الفروقات الموجبة.']];
settlement.getRange('A4:J4').values = [[
  'السجل', 'اسم العميل', 'الحالة', 'مبلغ الملف', 'مبلغ زين', 'الفرق الصالح',
  'السبب أو المرجع المبدئي', 'ما الذي نحتاجه؟', 'القرار بعد المراجعة', 'وقت آخر فحص',
]];
settlement.getRange('A5:J8').values = [
  ['R04', 'عميل ج', 'تسوية مذكورة ولم تنعكس', 0, 250, -250, 'مرجع تجريبي SET-01', 'سند التسوية وتاريخها', 'بانتظار التحقق', '2026-09-24 09:10:06 +03:00'],
  ['R02', 'عميل ب', 'نطاق مختلف', 100, 300, null, 'مديونية مقابل حساب', 'راجع تجميع الحساب', 'غير محسوم', '2026-09-24 09:05:47 +03:00'],
  ['R03', 'عميل ب', 'نطاق مختلف', 200, 300, null, 'نفس إجمالي حساب R02', 'راجع تجميع الحساب', 'غير محسوم', '2026-09-24 09:06:03 +03:00'],
  ['R06', 'عميل هـ', 'تعذر الفحص', 400, null, null, 'لم يظهر مبلغ صالح', 'أعد الفحص أو صحح الرقم', 'بانتظار إعادة فحص', '2026-09-24 09:20:51 +03:00'],
];
format(settlement, 'J', 8, [90, 150, 220, 125, 125, 130, 245, 245, 170, 230]);
settlement.getRange('D5:F8').setNumberFormat('#,##0.00');
settlement.getRange('A5:J5').format.fill = C.rose;
settlement.getRange('A6:J8').format.fill = C.amber;

const guide = wb.worksheets.add('طريقة القراءة');
guide.getRange('A1').values = [['قواعد تمنع الحكم الخاطئ']];
guide.getRange('A2').values = [['هذه القواعد هي أساس صفحات النتيجة؛ التطبيق الحالي يحتاج تعديل تصديره ليعمل بها.']];
guide.getRange('A3').values = [['الفرق = مبلغ الملف − مبلغ زين؛ السماحية الحالية 0.20 ريال. الوقت في المثال بتوقيت القاهرة.']];
guide.getRange('A4:D4').values = [['الحالة', 'شرطها', 'ما يظهر في التقرير', 'قرار المحصّل']];
guide.getRange('A5:D10').values = [
  ['فرق موجب', 'نفس المديونية أو حساب مكتمل؛ مبلغ زين أقل', 'يظهر في كل الفحوصات والموجبة', 'يفحص دليل السداد قبل التأكيد'],
  ['فرق سالب', 'نفس النطاق؛ مبلغ زين أعلى', 'يظهر في كل الفحوصات', 'يراجع التسوية وتاريخ المصدر'],
  ['أكثر من مديونية', 'مبلغ زين إجمالي للحساب', 'لا فرق فردياً؛ تجميع بالحساب', 'يتحقق من اكتمال كل الديون'],
  ['تسوية', 'وردت في الملف أو أضافها المحصّل', 'سبب محتمل، لا حكم نهائي', 'يسجل السند والتاريخ والمراجع'],
  ['تعذر الفحص', 'لا مبلغ صالح من زين', 'لا فرق ولا إدراج في الموجبة', 'يعيد المحاولة أو يصحح الرقم'],
  ['دفع مؤكّد', 'دليل سداد معتمد ومراجع', 'قرار بشري موثق', 'يسجل المرجع ووقت القرار'],
];
format(guide, 'D', 10, [180, 400, 380, 360]);
guide.getRange('A7:D9').format.fill = C.amber;
guide.getRange('A10:D10').format.fill = C.mint;

const check = await wb.inspect({ kind: 'table', range: 'كل الفحوصات!L4:N11', include: 'values,formulas', tableMaxRows: 8, tableMaxCols: 3, maxChars: 3000 });
console.log(check.ndjson);
const errors = await wb.inspect({ kind: 'match', searchTerm: '#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!', options: { useRegex: true, maxResults: 50 }, maxChars: 800 });
console.log(errors.ndjson);
await fs.mkdir(out, { recursive: true });
for (const [sheetName, range, name] of [
  ['كل الفحوصات', 'A1:P11', 'كل_الفحوصات_معاينة.png'],
  ['الفروقات الموجبة', 'A1:K6', 'الفروقات_الموجبة_معاينة.png'],
]) {
  const picture = await wb.render({ sheetName, range, scale: 1.25, format: 'png' });
  await fs.writeFile(join(out, name), new Uint8Array(await picture.arrayBuffer()));
}
const file = await SpreadsheetFile.exportXlsx(wb);
await file.save(join(out, 'نموذج_تشيك_زين_المحصل.xlsx'));
