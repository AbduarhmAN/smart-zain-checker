# Smart Zain Checker (Gemini Edition) — `smart_zainchecker_gemini`

**المواصفة المعمارية والتنفيذية الشاملة لمنظومة معالجة وتدقيق ملفات المديونيات وربط نتائج نظام زين (Zain)**

تم بناء هذا المشروع في مجلد مستقل (`smart_zainchecker_gemini`) كتطبيق هندسي متكامل ينفذ **خط أنابيب المعالجة المكون من 16 مرحلة (`16-Stage Pre-Zain & Post-Zain Pipeline`)** ويفرض **القواعد الأربع الحديدية (`The Four Unbreakable Laws`)** و**الممنوعات المنطقية الـ 20 (`The 20 Logical Prohibitions`)**.

---

## 1. المكونات البرمجية الأساسية (`Core Modules`)

| الملف / الوحدة | الوظيفة المعمارية |
| :--- | :--- |
| [`zain_checker/domain_models.py`](file:///e:/Projects/smart_zainchecker_gemini/zain_checker/domain_models.py) | نموذج البيانات رباعي الطبقات (`SourceRow`, `ServiceEntity`, `AccountGroup`, `SearchTask`, `PreZainSearchPlan`) + التعدادات الصارمة (`AmountState`, `RowValidationStatus`, `AccountFileScopeStatus`, `AccountConceptTag`, `ReconciliationStatus`). |
| [`zain_checker/pipeline.py`](file:///e:/Projects/smart_zainchecker_gemini/zain_checker/pipeline.py) | محرك خط الأنابيب قبل زين (`Stages 1..15`): اكتشاف الأوراق الظاهرة والمخفية، قفل عمود المبلغ المعتمد (`Immutable Column Lock`)، فحص الخلايا المدمجة والمعادلات، القراءة النصية الصارمة مع استعادة الأصفار البادئة (`Format Mask Reconstruction`)، كشف الصيغ العلمية التالفة، التصنيف الخماسي للمبالغ، كشف التكرارات والتعارضات عالميًا **قبل** الفلترة، التجميع غير المدمر للحسابات، وتوليد مهام البحث مع منع تكرار `Account Search`. |
| [`zain_checker/reconciliation.py`](file:///e:/Projects/smart_zainchecker_gemini/zain_checker/reconciliation.py) | محرك ربط النتائج بعد زين (`Stage 16 / Task 9`): تطبيق `Strict Scope Symmetry` و`Zero Residual Deduction` و`No Unfounded Root-Cause Inference` و`Error != Zero`، وتصدير مصنف التدقيق الشامل المكون من 7 أوراق عمل (`export_comprehensive_audit_workbook`). |
| [`zain_checker/simulation_suite.py`](file:///e:/Projects/smart_zainchecker_gemini/zain_checker/simulation_suite.py) | محاكاة شاملة (`Full End-to-End 20-Row Multi-Sheet Simulation` — القسم 11 / المهمة 10) تبني المصنف ذا الورقتين (`Sheet1_Main` + `Sheet2_Branch`) وتتحقق من كافة المخرجات ومعادلة الاتزان الحسابي (`20 = 6 + 7 + 1 + 6`). |
| [`zain_checker/workbook.py`](file:///e:/Projects/smart_zainchecker_gemini/zain_checker/workbook.py) | طبقة الربط المباشر مع محرك التشغيل الفعلي (`main.py` و`web_bridge.py`) المحدثة لمنع تكرار `Account Search`، وحماية النطاق، وإلغاء التبديل اللاحق لعمود المبلغ. |
| [`smart_audit_cli.py`](file:///e:/Projects/smart_zainchecker_gemini/smart_audit_cli.py) | أداة سطر الأوامر (`CLI`) لتشغيل التدقيق الشامل قبل زين أو تشغيل المحاكاة الكاملة وإنتاج تقارير JSON وExcel. |
| [`test/test_full_spec_audit.py`](file:///e:/Projects/smart_zainchecker_gemini/test/test_full_spec_audit.py) | حزمة الاختبارات الشاملة (`Pytest`) التي تختبر القوانين الأربعة، قواعد التحقق التسع، المفاهيم التسعة، الأمثلة الـ 28، الحالات الشاذة الـ 38، والمحاكاة الكاملة. |

---

## 2. أوامر التشغيل السريع (`Quick Start`)

### أ. تشغيل المحاكاة الشاملة (20 صفًا عبر ورقتين — المهمة رقم 10):
```powershell
python -m zain_checker.simulation_suite
```
أو عبر أداة الـ CLI:
```powershell
python smart_audit_cli.py --simulate
```

### ب. تدقيق أي ملف Excel وإصدار خطة البحث وتقرير الـ 7 أوراق قبل زين:
```powershell
python smart_audit_cli.py --workbook zain_data.xlsx --output-dir audit_outputs
```

### ج. تشغيل اختبارات التحقق الشاملة (`Pytest`):
```powershell
python -m pytest test/test_full_spec_audit.py -v
```
