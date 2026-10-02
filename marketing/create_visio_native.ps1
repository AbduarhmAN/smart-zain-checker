$ErrorActionPreference = 'Stop'

$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$outputPath = Join-Path $baseDir 'مخطط_تدفق_فحص_زين_أشكال_قابلة_للتحرير_نسخة_واضحة.vsdx'
$visio = New-Object -ComObject Visio.Application

function Add-Node {
    param($page, $master, [string]$label, [double]$x, [double]$y, [double]$width = 3.4, [double]$height = 1.0)
    $node = $page.Drop($master, $x, $y)
    $node.CellsU('Width').FormulaU = "$width in"
    $node.CellsU('Height').FormulaU = "$height in"
    $node.CellsU('Char.Size').FormulaU = '12 pt'
    $node.CellsU('FillForegnd').FormulaU = 'RGB(244,247,255)'
    $node.CellsU('LineColor').FormulaU = 'RGB(59,80,143)'
    $node.Text = $label
    return $node
}

function Connect-Nodes {
    param($page, $connectorMaster, $from, $to, [string]$label = '')
    $line = $page.Drop($connectorMaster, 0, 0)
    $line.CellsU('BeginX').GlueTo($from.CellsU('PinX'))
    $line.CellsU('EndX').GlueTo($to.CellsU('PinX'))
    $line.CellsU('EndArrow').FormulaU = '13'
    $line.CellsU('LineColor').FormulaU = 'RGB(83,95,123)'
    $line.CellsU('LineWeight').FormulaU = '1 pt'
    if ($label) {
        $line.Text = $label
        $line.CellsU('Char.Size').FormulaU = '10 pt'
    }
}

function Set-Page {
    param($page, [string]$name)
    $page.Name = $name
    $page.PageSheet.CellsU('PageWidth').FormulaU = '13 in'
    $page.PageSheet.CellsU('PageHeight').FormulaU = '18 in'
}

try {
    $visio.Visible = $false
    $stencilPath = 'C:\Program Files\Microsoft Office\root\Office16\Visio Content\1033\BASFLO_U.VSSX'
    $stencil = $visio.Documents.OpenEx($stencilPath, 64)
    $process = $stencil.Masters.ItemU('Process')
    $decision = $stencil.Masters.ItemU('Decision')
    $startEnd = $stencil.Masters.ItemU('Start/End')
    $connector = $stencil.Masters.ItemU('Dynamic connector')

    $doc = $visio.Documents.Add('')
    $p = $doc.Pages.Item(1)
    Set-Page $p '١ - تجهيز الملف والبحث'
    $a = Add-Node $p $startEnd 'ابدأ' 6.5 17.1 2.0 0.7
    $b = Add-Node $p $process 'اقرأ الملف وحدد عمود المبلغ والحالات' 6.5 15.7 5.2 0.9
    $c = Add-Node $p $decision 'الحساب والخدمة والمبلغ صالحة؟' 6.5 13.9 4.3 1.35
    $d = Add-Node $p $process 'سجل الصف في ورقة الأخطاء' 11.0 13.9 2.8 0.95
    $e = Add-Node $p $decision 'رقم خدمة مكرر ببيانات متعارضة؟' 6.5 11.9 4.3 1.35
    $f = Add-Node $p $process 'استبعد الصفوف المتعارضة وسجل الخطأ' 11.0 11.9 3.2 1.05
    $g = Add-Node $p $process 'اجمع الصفوف برقم الحساب، لا باسم العميل' 6.5 9.8 5.2 0.95
    $h = Add-Node $p $decision 'لكل خدمة: هل يبدأ رقمها بـ ٢؟' 6.5 7.7 4.3 1.35
    $i = Add-Node $p $process 'ابحث في زين برقم الخدمة' 3.0 5.7 3.5 1.0
    $j = Add-Node $p $process 'ابحث برقم الحساب مرة واحدة' 10.0 5.7 3.8 1.0
    $k = Add-Node $p $process 'احتفظ بنطاق كل قراءة وتفاصيل الحساب المتعدد' 6.5 3.5 5.6 1.0
    $l = Add-Node $p $startEnd 'تابع إلى صفحة فحص زين والأخطاء' 6.5 1.5 4.7 0.85
    Connect-Nodes $p $connector $a $b
    Connect-Nodes $p $connector $b $c
    Connect-Nodes $p $connector $c $d 'لا'
    Connect-Nodes $p $connector $c $e 'نعم'
    Connect-Nodes $p $connector $e $f 'نعم'
    Connect-Nodes $p $connector $e $g 'لا'
    Connect-Nodes $p $connector $f $g
    Connect-Nodes $p $connector $g $h
    Connect-Nodes $p $connector $h $i 'نعم'
    Connect-Nodes $p $connector $h $j 'لا'
    Connect-Nodes $p $connector $i $k
    Connect-Nodes $p $connector $j $k
    Connect-Nodes $p $connector $k $l

    $p = $doc.Pages.Add()
    Set-Page $p '٢ - فحص زين والأخطاء'
    $a = Add-Node $p $startEnd 'ابدأ البحث في زين' 6.5 17.0 3.0 0.8
    $b = Add-Node $p $process 'افتح رابط البحث وتحقق من الرقم المعروض' 6.5 15.3 5.5 1.0
    $c = Add-Node $p $decision 'ظهر مبلغ رقمي صريح؟' 6.5 13.2 4.1 1.4
    $d = Add-Node $p $decision 'القراءة مستقرة؟' 3.1 10.9 3.3 1.4
    $e = Add-Node $p $decision 'خانة المبلغ فارغة؟' 10.0 11.0 3.3 1.4
    $f = Add-Node $p $decision 'خطأ قابل لإعادة المحاولة؟' 10.0 8.6 3.5 1.4
    $g = Add-Node $p $decision 'بقيت محاولة ضمن الحد؟' 7.0 6.3 3.7 1.4
    $h = Add-Node $p $process 'سجل الخطأ؛ لا تفترض أن المبلغ صفر أو أن العميل سدد' 10.3 3.9 4.3 1.4
    $i = Add-Node $p $process 'احفظ المبلغ والنطاق ووقت الفحص' 2.8 6.3 3.7 1.1
    $j = Add-Node $p $startEnd 'تابع إلى صفحة مقارنة المبالغ' 2.8 3.9 4.3 0.9
    Connect-Nodes $p $connector $a $b
    Connect-Nodes $p $connector $b $c
    Connect-Nodes $p $connector $c $d 'نعم'
    Connect-Nodes $p $connector $c $e 'لا'
    Connect-Nodes $p $connector $d $i 'نعم'
    Connect-Nodes $p $connector $d $g 'لا'
    Connect-Nodes $p $connector $e $h 'نعم'
    Connect-Nodes $p $connector $e $f 'لا'
    Connect-Nodes $p $connector $f $g 'نعم'
    Connect-Nodes $p $connector $f $h 'لا'
    Connect-Nodes $p $connector $g $b 'نعم'
    Connect-Nodes $p $connector $g $h 'لا'
    Connect-Nodes $p $connector $i $j

    $p = $doc.Pages.Add()
    Set-Page $p '٣ - الفرق والمخرجات'
    $a = Add-Node $p $startEnd 'قراءة زين موثوقة' 6.5 17.0 3.0 0.8
    $b = Add-Node $p $decision 'هل القراءة برقم الحساب؟' 6.5 15.1 4.0 1.4
    $c = Add-Node $p $process 'قارن مبلغ هذه الخدمة بمبلغ زين للخدمة' 2.8 13.0 4.3 1.15
    $d = Add-Node $p $decision 'هل اكتمل نطاق خدمات الحساب في الملف؟' 10.0 13.0 4.3 1.55
    $e = Add-Node $p $process 'لا تحسب فرق الحساب؛ سجّل مشكلة النطاق' 10.8 10.7 4.1 1.1
    $f = Add-Node $p $process 'قارن مجموع ملف الحساب بإجمالي زين مرة واحدة' 5.1 10.7 4.7 1.15
    $g = Add-Node $p $decision 'إشارة الفرق؟' 5.2 8.5 3.1 1.3
    $h = Add-Node $p $process 'موجب: انخفاض ظاهر؛ السبب غير مؤكد' 2.2 6.4 3.8 1.2
    $i = Add-Node $p $process 'صفر أو ضمن السماحية: مطابقة حسابية' 6.5 6.4 3.8 1.2
    $j = Add-Node $p $process 'سالب: زيادة ظاهرة؛ التسوية تحتاج دليلًا' 10.7 6.4 3.8 1.2
    $k = Add-Node $p $process 'نتائج: صف حساب واحد أو صفوف خدمات مستقلة دون تكرار' 6.5 4.2 6.3 1.1
    $l = Add-Node $p $process 'أخرج ثلاث ورقات: النتائج، الحسابات المتعددة، الأخطاء' 6.5 2.4 6.0 1.0
    $m = Add-Node $p $startEnd 'انتهى' 6.5 0.9 2.0 0.7
    Connect-Nodes $p $connector $a $b
    Connect-Nodes $p $connector $b $c 'لا'
    Connect-Nodes $p $connector $b $d 'نعم'
    Connect-Nodes $p $connector $d $e 'لا'
    Connect-Nodes $p $connector $d $f 'نعم'
    Connect-Nodes $p $connector $c $g
    Connect-Nodes $p $connector $f $g
    Connect-Nodes $p $connector $g $h 'موجب'
    Connect-Nodes $p $connector $g $i 'صفر'
    Connect-Nodes $p $connector $g $j 'سالب'
    Connect-Nodes $p $connector $h $k
    Connect-Nodes $p $connector $i $k
    Connect-Nodes $p $connector $j $k
    Connect-Nodes $p $connector $k $l
    Connect-Nodes $p $connector $l $m

    $doc.SaveAs($outputPath)
    Write-Output $outputPath
    for ($index = 1; $index -le $doc.Pages.Count; $index++) {
        $exportPath = Join-Path $baseDir ("مخطط_أصلي_واضح_صفحة_$index.png")
        $doc.Pages.Item($index).Export($exportPath)
        Write-Output $exportPath
    }
    $doc.Close()
    $stencil.Close()
}
finally {
    $visio.Quit()
}
