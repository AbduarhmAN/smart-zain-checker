import fs from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { SpreadsheetFile, Workbook } from '@oai/artifact-tool';

const here = dirname(fileURLToPath(import.meta.url));
const outputDir = resolve(here, '../../../outputs/zain-check-marketing-20260924');
const workbook = Workbook.create();
const sheet = workbook.worksheets.add('كل العملاء');
sheet.showGridLines = false;
sheet.freezePanes.freezeRows(4);

sheet.getRange('A1').values = [['كل العملاء — نموذج نتائج تشيك شيت زين']];
sheet.getRange('A2').values = [['بيانات افتراضية لتوضيح شكل مقترح، وليست مخرجات البرنامج الحالية']];
sheet.getRange('A3').values = [['هذه الصفحة تشمل كل صف تم فحصه؛ صفحة المتابعة تجمع الفروقات والحالات غير المحسومة.']];
sheet.getRange('A4:L4').values = [[
  'رقم الحساب', 'رقم المديونية/الخدمة', 'رقم البحث المستخدم', 'نطاق المقارنة',
  'مبلغ الشيت', 'مبلغ زين', 'الفرق', 'الفرز الأولي', 'ملاحظة المحصّل',
  'القرار بعد التحقق', 'دليل القرار', 'وقت الفحص',
]];
sheet.getRange('A5:L8').values = [
  ['1000004182', '2055••••731', '2055••••731', 'نفس النطاق', 300, 180, null, null, 'تأكد هل حدث سداد', 'بانتظار تحقق', '', new Date('2026-09-24T09:00:00Z')],
  ['1000004182', '01••••842', '1000004182', 'نطاق مختلف', 100, 300, null, null, 'الموقع يعرض إجمالي الحساب', 'مراجعة نطاق', '', new Date('2026-09-24T09:05:00Z')],
  ['1000009170', '1000009170', '1000009170', 'نفس النطاق', 0, 250, null, null, 'تسوية محتملة لم تظهر', 'بانتظار تحقق', '', new Date('2026-09-24T09:10:00Z')],
  ['1000002345', '2055••••561', '2055••••561', 'نفس النطاق', 500, 500, null, null, 'لا فرق', 'لا يلزم', '', new Date('2026-09-24T09:15:00Z')],
];
sheet.getRange('G5').formulas = [['=E5-F5']];
sheet.getRange('G5:G8').fillDown();
sheet.getRange('H5').formulas = [['=IF(D5<>"نفس النطاق","راجع النطاق",IF(ABS(G5)<=0.2,"مطابق",IF(G5>0,"فرق موجب","فرق سالب")))']];
sheet.getRange('H5:H8').fillDown();

sheet.getRange('A1:L8').format.font = { name: 'Tahoma', size: 11, color: '#102B3D' };
sheet.getRange('A1').format.font = { name: 'Tahoma', size: 17, bold: true, color: '#102B3D' };
sheet.getRange('A2:A3').format.font = { name: 'Tahoma', size: 10, color: '#537071' };
sheet.getRange('A4:L4').format = {
  fill: '#102B3D',
  font: { name: 'Tahoma', size: 10, bold: true, color: '#FFFFFF' },
};
sheet.getRange('A4:L4').format.rowHeight = 35;
sheet.getRange('A5:L8').format.rowHeight = 36;
sheet.getRange('A6:L6').format.fill = '#FFF4EA';
sheet.getRange('A7:L7').format.fill = '#EAF8F2';
sheet.getRange('I5:K8').format.fill = '#FFF7DF';
sheet.getRange('E5:G8').setNumberFormat('#,##0.00');
sheet.getRange('L5:L8').setNumberFormat('yyyy-mm-dd hh:mm');
sheet.getRange('A4:L8').format.verticalAlignment = 'center';
const widths = [130, 175, 165, 135, 120, 120, 120, 135, 240, 160, 150, 165];
for (let index = 0; index < widths.length; index += 1) {
  sheet.getRangeByIndexes(0, index, 8, 1).format.columnWidthPx = widths[index];
}
sheet.getRange('A4:L4').format.borders = { preset: 'inside', style: 'thin', color: '#527080' };
sheet.getRange('A5:L8').format.borders = { preset: 'inside', style: 'thin', color: '#E0E8E4' };

const review = workbook.worksheets.add('حالات للمتابعة');
review.showGridLines = false;
review.freezePanes.freezeRows(4);
review.getRange('A1').values = [['حالات للمتابعة فقط']];
review.getRange('A2').values = [['بيانات افتراضية؛ هذه الصفحة تعرض الفروقات وما يحتاج تحققاً يدوياً.']];
review.getRange('A3').values = [['الفرق الموجب لا يثبت الدفع وحده. القرار النهائي يُكتب بعد مراجعة المحصّل ودليله.']];
review.getRange('A4:L4').values = sheet.getRange('A4:L4').values;
review.getRange('A5:L7').values = [
  ['1000004182', '2055••••731', '2055••••731', 'نفس النطاق', 300, 180, null, null, 'تأكد هل حدث سداد', 'بانتظار تحقق', '', new Date('2026-09-24T09:00:00Z')],
  ['1000004182', '01••••842', '1000004182', 'نطاق مختلف', 100, 300, null, null, 'الموقع يعرض إجمالي الحساب', 'مراجعة نطاق', '', new Date('2026-09-24T09:05:00Z')],
  ['1000009170', '1000009170', '1000009170', 'نفس النطاق', 0, 250, null, null, 'تسوية محتملة لم تظهر', 'بانتظار تحقق', '', new Date('2026-09-24T09:10:00Z')],
];
review.getRange('G5').formulas = [['=E5-F5']];
review.getRange('G5:G7').fillDown();
review.getRange('H5').formulas = [['=IF(D5<>"نفس النطاق","راجع النطاق",IF(ABS(G5)<=0.2,"مطابق",IF(G5>0,"فرق موجب","فرق سالب")))']];
review.getRange('H5:H7').fillDown();
review.getRange('A1:L7').format.font = { name: 'Tahoma', size: 11, color: '#102B3D' };
review.getRange('A1').format.font = { name: 'Tahoma', size: 17, bold: true, color: '#102B3D' };
review.getRange('A2:A3').format.font = { name: 'Tahoma', size: 10, color: '#537071' };
review.getRange('A4:L4').format = { fill: '#102B3D', font: { name: 'Tahoma', size: 10, bold: true, color: '#FFFFFF' } };
review.getRange('A4:L4').format.rowHeight = 35;
review.getRange('A5:L7').format.rowHeight = 36;
review.getRange('A6:L6').format.fill = '#FFF4EA';
review.getRange('I5:K7').format.fill = '#FFF7DF';
review.getRange('E5:G7').setNumberFormat('#,##0.00');
review.getRange('L5:L7').setNumberFormat('yyyy-mm-dd hh:mm');
review.getRange('A4:L7').format.verticalAlignment = 'center';
for (let index = 0; index < widths.length; index += 1) {
  review.getRangeByIndexes(0, index, 7, 1).format.columnWidthPx = widths[index];
}
review.getRange('A4:L4').format.borders = { preset: 'inside', style: 'thin', color: '#527080' };
review.getRange('A5:L7').format.borders = { preset: 'inside', style: 'thin', color: '#E0E8E4' };

const view = await workbook.inspect({
  kind: 'table', range: 'كل العملاء!A4:L8', include: 'values,formulas',
  tableMaxRows: 5, tableMaxCols: 12, maxChars: 4500,
});
console.log(view.ndjson);
const errors = await workbook.inspect({
  kind: 'match', searchTerm: '#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',
  options: { useRegex: true, maxResults: 50 }, summary: 'formula error scan', maxChars: 1000,
});
console.log(errors.ndjson);
await fs.mkdir(outputDir, { recursive: true });
const preview = await workbook.render({ sheetName: 'كل العملاء', range: 'A1:L8', scale: 1.3, format: 'png' });
await fs.writeFile(join(outputDir, 'نموذج_نتائج_التشيك.png'), new Uint8Array(await preview.arrayBuffer()));
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(join(outputDir, 'نموذج_نتائج_التشيك.xlsx'));
