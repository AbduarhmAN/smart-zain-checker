# نتيجة اختبار 10 خيوط × 100 استعلام بالاتصال المستمر

- بداية الاختبار بتوقيت بغداد: 2026-10-16T15:21:35.223060+03:00
- العدد المطلوب: 1000 محاولة، عبر 10 بروكسيات مستقلة من إعداد العمال.
- العينة: 7 حسابات موجودة في عينات المشروع، تكررت بين الاستعلامات.
- الانتظار: نصف ثانية بعد اكتمال كل محاولة وقبل التالية، مع احترام Retry-After إذا أرسله الخادم.
- لا توجد إعادة تلقائية للمحاولة الفاشلة. الاتصال المنقطع يُنشأ مجددًا للمحاولة التالية.
- لم تتغير إعدادات العمال أو الطلبات أو المحافظ أو المدفوعات.

## النتيجة الإجمالية

| البند | النتيجة |
| --- | --- |
| المحاولات المنفذة | 1000 |
| أعلى عدد استعلامات متزامنة مرصود | 10 |
| نتائج فحص ناجحة HTTP 200 وبيانات صالحة | 561 |
| محاولات غير ناجحة | 439 |
| ردود HTTP المستلمة، بما فيها الأخطاء | 786 |
| اتصالات TCP المنشأة إلى البروكسي | 224 |
| إعادة إنشاء اتصال بعد الأول لكل خيط | 214 |
| طلبات إغلاق في ترويسة الرد | 0 |
| المرسل | 525348 بايت |
| المستقبَل | 1961796 بايت |
| المجموع | 2487144 بايت = 2.487144 MB |
| مدة الاختبار | 448.817 ثانية |
| أقل فاصل فعلي بين المحاولات في الخيط نفسه | 0.500199 ثانية |

## كل خيط

| الخيط | المنفذ | الناجح | HTTP 400 | أخطاء النقل | الاتصالات | أقصى ردود HTTP على اتصال واحد | البايتات | المدة بالثواني |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 100 | 57 | 22 | 21 | 22 | 55 | 247641 | 374.871 |
| 2 | 100 | 57 | 22 | 21 | 22 | 56 | 248051 | 378.372 |
| 3 | 100 | 56 | 23 | 21 | 22 | 58 | 252164 | 384.889 |
| 4 | 100 | 60 | 24 | 16 | 17 | 23 | 271539 | 326.704 |
| 5 | 100 | 45 | 18 | 37 | 38 | 63 | 200461 | 448.235 |
| 6 | 100 | 58 | 24 | 18 | 19 | 23 | 259892 | 357.987 |
| 7 | 100 | 62 | 24 | 14 | 15 | 22 | 276400 | 300.602 |
| 8 | 100 | 56 | 25 | 19 | 20 | 41 | 257029 | 375.255 |
| 9 | 100 | 55 | 22 | 23 | 24 | 77 | 237632 | 405.633 |
| 10 | 100 | 55 | 21 | 24 | 25 | 76 | 236335 | 418.079 |

## استهلاك الاتصال المعاد استخدامه

- استعلامات ناجحة عبر اتصال موجود: 547.
- متوسط البيانات لهذه الاستعلامات: 2916.272 بايت للاستعلام.
- إسقاط هذا المتوسط وحده على 200 ألف استعلام: 0.583254 GB.
- هذا إسقاط مشروط باستمرار نجاح الاتصالات؛ لا يحسب تكلفة الانقطاعات وإعادة الاتصال أو TCP/IP.

متوسط البايتات على **جميع المحاولات** لا يمثل تكلفة نتائج ناجحة إذا كانت محاولات كثيرة لم تصل إلى رد؛ بعضها يستهلك بضع بايتات ثم يفشل.

## هل أُغلق الاتصال بعد عدد ثابت؟

- أطول اتصال استلم 77 رد HTTP، وظل مفتوحًا حتى نهاية اختباره.
- كانت آخر اتصالات الخيوط العشرة مفتوحة عند إنهاء الاختبار؛ أغلقها الاختبار بعد إكمال العدد.
- لم تظهر ترويسة تطلب إغلاق الاتصال في أي رد HTTP مستلم.
- تعطلت 14 اتصالات سبق أن استلمت ردودًا، ثم انتهت مهلة المحاولة التالية وأغلقها العميل. ليست هذه إشارة إغلاق صريحة من زين.
- ظهرت 20 حالة EOF من طرف البروكسي قبل استلام أي رد HTTP على اتصالاتها. لا يثبت هذا حدًا أعلى لطلبات HTTP.
- بعض الاتصالات تعطلت قرب 30 ثانية بعد 18–24 ردًا، لكن اتصالات أخرى تجاوزت ذلك ووصلت إلى 77 ردًا؛ لذلك لا يُستنتج حد ثابت من هذا الاختبار.

| الخيط | الاتصال | الردود المستلمة قبل التعطل | الاستعلام الذي انتهت مهلته | زمن آخر رد من بداية الاتصال بالثواني |
| --- | --- | --- | --- | --- |
| 1 | 20 | 24 | 44 | 29.705572 |
| 2 | 14 | 23 | 37 | 30.397981 |
| 3 | 8 | 21 | 29 | 28.881968 |
| 4 | 1 | 21 | 22 | 29.152390 |
| 4 | 5 | 23 | 49 | 29.915086 |
| 4 | 8 | 22 | 74 | 29.413664 |
| 6 | 9 | 21 | 30 | 29.803818 |
| 6 | 14 | 23 | 58 | 30.092432 |
| 6 | 18 | 22 | 84 | 28.923354 |
| 7 | 1 | 22 | 23 | 29.699716 |
| 7 | 8 | 22 | 52 | 29.615360 |
| 7 | 10 | 21 | 75 | 29.887749 |
| 8 | 12 | 22 | 34 | 30.601778 |
| 8 | 17 | 18 | 57 | 30.786219 |

## الأخطاء

- HTTP: `{"200": 561, "400": 225}`.
- أخطاء النقل: `{"TimeoutError": 175, "GeneralProxyError": 35, "RemoteDisconnected": 4}`.

العينتان 4 و5 مصنفتان بلا نتيجة متوقعة في ملف عينات المشروع الأصلي. يوضح التحليل توزيع الردود حسب رقم العينة، دون حفظ أرقام الحسابات. HTTP 400 رد فعلي من الخدمة، ويختلف عن فشل الاتصال الذي لم يستلم رد HTTP.

الأرقام التالية أرقام الاستعلام داخل كل خيط، وليست أرقام حسابات:

### الخيط 1

- HTTP 400: 25–26, 32–33, 39–40, 46–47, 53–54, 60–61, 67–68, 74–75, 81–82, 88–89, 95–96.
- أخطاء النقل: 1–19, 44–45.
- أرقام المحاولات التي بدأت اتصالًا جديدًا بعد الأول: 2–20, 45–46.
- طلبات إغلاق في ترويسة HTTP: لا يوجد.

| الاتصال | أول استعلام | آخر استعلام | عدد المحاولات | الناجحة | آخر استعلام ناجح | سبب انتهاء رصد الاتصال | موضعه |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 1 | 1 | 0 | None | client_eof | client_read |
| 2 | 2 | 2 | 1 | 0 | None | client_eof | client_read |
| 3 | 3 | 3 | 1 | 0 | None | client_eof | client_read |
| 4 | 4 | 4 | 1 | 0 | None | client_eof | client_read |
| 5 | 5 | 5 | 1 | 0 | None | client_eof | client_read |
| 6 | 6 | 6 | 1 | 0 | None | client_eof | client_read |
| 7 | 7 | 7 | 1 | 0 | None | client_eof | client_read |
| 8 | 8 | 8 | 1 | 0 | None | client_eof | client_read |
| 9 | 9 | 9 | 1 | 0 | None | client_eof | client_read |
| 10 | 10 | 10 | 1 | 0 | None | upstream_eof | upstream_read |
| 11 | 11 | 11 | 1 | 0 | None | client_eof | client_read |
| 12 | 12 | 12 | 1 | 0 | None | client_eof | client_read |
| 13 | 13 | 13 | 1 | 0 | None | client_eof | client_read |
| 14 | 14 | 14 | 1 | 0 | None | client_eof | client_read |
| 15 | 15 | 15 | 1 | 0 | None | client_eof | client_read |
| 16 | 16 | 16 | 1 | 0 | None | client_eof | client_read |
| 17 | 17 | 17 | 1 | 0 | None | client_eof | client_read |
| 18 | 18 | 18 | 1 | 0 | None | client_eof | client_read |
| 19 | 19 | 19 | 1 | 0 | None | client_eof | client_read |
| 20 | 20 | 44 | 25 | 18 | 43 | client_eof | client_read |
| 21 | 45 | 45 | 1 | 0 | None | client_eof | client_read |
| 22 | 46 | 100 | 55 | 39 | 100 | client_eof | client_read |

### الخيط 2

- HTTP 400: 17–18, 24–25, 31–32, 45–46, 52–53, 59–60, 66–67, 73–74, 80–81, 87–88, 94–95.
- أخطاء النقل: 1–13, 37–44.
- أرقام المحاولات التي بدأت اتصالًا جديدًا بعد الأول: 2–14, 38–45.
- طلبات إغلاق في ترويسة HTTP: لا يوجد.

| الاتصال | أول استعلام | آخر استعلام | عدد المحاولات | الناجحة | آخر استعلام ناجح | سبب انتهاء رصد الاتصال | موضعه |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 1 | 1 | 0 | None | client_eof | client_read |
| 2 | 2 | 2 | 1 | 0 | None | client_eof | client_read |
| 3 | 3 | 3 | 1 | 0 | None | client_eof | client_read |
| 4 | 4 | 4 | 1 | 0 | None | client_eof | client_read |
| 5 | 5 | 5 | 1 | 0 | None | client_eof | client_read |
| 6 | 6 | 6 | 1 | 0 | None | client_eof | client_read |
| 7 | 7 | 7 | 1 | 0 | None | client_eof | client_read |
| 8 | 8 | 8 | 1 | 0 | None | client_eof | client_read |
| 9 | 9 | 9 | 1 | 0 | None | client_eof | client_read |
| 10 | 10 | 10 | 1 | 0 | None | client_eof | client_read |
| 11 | 11 | 11 | 1 | 0 | None | client_eof | client_read |
| 12 | 12 | 12 | 1 | 0 | None | client_eof | client_read |
| 13 | 13 | 13 | 1 | 0 | None | client_eof | client_read |
| 14 | 14 | 37 | 24 | 17 | 36 | client_eof | client_read |
| 15 | 38 | 38 | 1 | 0 | None | client_eof | client_read |
| 16 | 39 | 39 | 1 | 0 | None | client_eof | client_read |
| 17 | 40 | 40 | 1 | 0 | None | client_eof | client_read |
| 18 | 41 | 41 | 1 | 0 | None | client_eof | client_read |
| 19 | 42 | 42 | 1 | 0 | None | client_eof | client_read |
| 20 | 43 | 43 | 1 | 0 | None | client_eof | client_read |
| 21 | 44 | 44 | 1 | 0 | None | client_eof | client_read |
| 22 | 45 | 100 | 56 | 40 | 100 | client_eof | client_read |

### الخيط 3

- HTTP 400: 9–10, 16–17, 23–24, 44–45, 51–52, 58–59, 65–66, 72–73, 79–80, 86–87, 93–94, 100.
- أخطاء النقل: 1–7, 29–42.
- أرقام المحاولات التي بدأت اتصالًا جديدًا بعد الأول: 2–8, 30–43.
- طلبات إغلاق في ترويسة HTTP: لا يوجد.

| الاتصال | أول استعلام | آخر استعلام | عدد المحاولات | الناجحة | آخر استعلام ناجح | سبب انتهاء رصد الاتصال | موضعه |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 1 | 1 | 0 | None | client_eof | client_read |
| 2 | 2 | 2 | 1 | 0 | None | client_eof | client_read |
| 3 | 3 | 3 | 1 | 0 | None | upstream_eof | upstream_read |
| 4 | 4 | 4 | 1 | 0 | None | client_eof | client_read |
| 5 | 5 | 5 | 1 | 0 | None | client_eof | client_read |
| 6 | 6 | 6 | 1 | 0 | None | client_eof | client_read |
| 7 | 7 | 7 | 1 | 0 | None | client_eof | client_read |
| 8 | 8 | 29 | 22 | 15 | 28 | client_eof | client_read |
| 9 | 30 | 30 | 1 | 0 | None | client_eof | client_read |
| 10 | 31 | 31 | 1 | 0 | None | client_eof | client_read |
| 11 | 32 | 32 | 1 | 0 | None | client_eof | client_read |
| 12 | 33 | 33 | 1 | 0 | None | client_eof | client_read |
| 13 | 34 | 34 | 1 | 0 | None | client_eof | client_read |
| 14 | 35 | 35 | 1 | 0 | None | client_eof | client_read |
| 15 | 36 | 36 | 1 | 0 | None | client_eof | client_read |
| 16 | 37 | 37 | 1 | 0 | None | client_eof | client_read |
| 17 | 38 | 38 | 1 | 0 | None | client_eof | client_read |
| 18 | 39 | 39 | 1 | 0 | None | client_eof | client_read |
| 19 | 40 | 40 | 1 | 0 | None | upstream_eof | upstream_read |
| 20 | 41 | 41 | 1 | 0 | None | upstream_eof | upstream_read |
| 21 | 42 | 42 | 1 | 0 | None | client_eof | client_read |
| 22 | 43 | 100 | 58 | 41 | 99 | client_eof | client_read |

### الخيط 4

- HTTP 400: 1–2, 8–9, 15–16, 29–30, 36–37, 43–44, 57–58, 64–65, 71–72, 85–86, 92–93, 99–100.
- أخطاء النقل: 22–25, 49–51, 74–82.
- أرقام المحاولات التي بدأت اتصالًا جديدًا بعد الأول: 23–26, 50–52, 75–83.
- طلبات إغلاق في ترويسة HTTP: لا يوجد.

| الاتصال | أول استعلام | آخر استعلام | عدد المحاولات | الناجحة | آخر استعلام ناجح | سبب انتهاء رصد الاتصال | موضعه |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 22 | 22 | 15 | 21 | client_eof | client_read |
| 2 | 23 | 23 | 1 | 0 | None | client_eof | client_read |
| 3 | 24 | 24 | 1 | 0 | None | client_eof | client_read |
| 4 | 25 | 25 | 1 | 0 | None | client_eof | client_read |
| 5 | 26 | 49 | 24 | 17 | 48 | client_eof | client_read |
| 6 | 50 | 50 | 1 | 0 | None | client_eof | client_read |
| 7 | 51 | 51 | 1 | 0 | None | client_eof | client_read |
| 8 | 52 | 74 | 23 | 16 | 73 | client_eof | client_read |
| 9 | 75 | 75 | 1 | 0 | None | client_eof | client_read |
| 10 | 76 | 76 | 1 | 0 | None | client_eof | client_read |
| 11 | 77 | 77 | 1 | 0 | None | client_eof | client_read |
| 12 | 78 | 78 | 1 | 0 | None | upstream_eof | upstream_read |
| 13 | 79 | 79 | 1 | 0 | None | client_eof | client_read |
| 14 | 80 | 80 | 1 | 0 | None | client_eof | client_read |
| 15 | 81 | 81 | 1 | 0 | None | client_eof | client_read |
| 16 | 82 | 82 | 1 | 0 | None | client_eof | client_read |
| 17 | 83 | 100 | 18 | 12 | 98 | client_eof | client_read |

### الخيط 5

- HTTP 400: 42–43, 49–50, 56–57, 63–64, 70–71, 77–78, 84–85, 91–92, 98–99.
- أخطاء النقل: 1–37.
- أرقام المحاولات التي بدأت اتصالًا جديدًا بعد الأول: 2–38.
- طلبات إغلاق في ترويسة HTTP: لا يوجد.

| الاتصال | أول استعلام | آخر استعلام | عدد المحاولات | الناجحة | آخر استعلام ناجح | سبب انتهاء رصد الاتصال | موضعه |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 1 | 1 | 0 | None | client_eof | client_read |
| 2 | 2 | 2 | 1 | 0 | None | client_eof | client_read |
| 3 | 3 | 3 | 1 | 0 | None | client_eof | client_read |
| 4 | 4 | 4 | 1 | 0 | None | client_eof | client_read |
| 5 | 5 | 5 | 1 | 0 | None | client_eof | client_read |
| 6 | 6 | 6 | 1 | 0 | None | client_eof | client_read |
| 7 | 7 | 7 | 1 | 0 | None | client_eof | client_read |
| 8 | 8 | 8 | 1 | 0 | None | client_eof | client_read |
| 9 | 9 | 9 | 1 | 0 | None | client_eof | client_read |
| 10 | 10 | 10 | 1 | 0 | None | client_eof | client_read |
| 11 | 11 | 11 | 1 | 0 | None | client_eof | client_read |
| 12 | 12 | 12 | 1 | 0 | None | client_eof | client_read |
| 13 | 13 | 13 | 1 | 0 | None | client_eof | client_read |
| 14 | 14 | 14 | 1 | 0 | None | client_eof | client_read |
| 15 | 15 | 15 | 1 | 0 | None | client_eof | client_read |
| 16 | 16 | 16 | 1 | 0 | None | client_eof | client_read |
| 17 | 17 | 17 | 1 | 0 | None | client_eof | client_read |
| 18 | 18 | 18 | 1 | 0 | None | client_eof | client_read |
| 19 | 19 | 19 | 1 | 0 | None | client_eof | client_read |
| 20 | 20 | 20 | 1 | 0 | None | client_eof | client_read |
| 21 | 21 | 21 | 1 | 0 | None | client_eof | client_read |
| 22 | 22 | 22 | 1 | 0 | None | client_eof | client_read |
| 23 | 23 | 23 | 1 | 0 | None | client_eof | client_read |
| 24 | 24 | 24 | 1 | 0 | None | upstream_eof | upstream_read |
| 25 | 25 | 25 | 1 | 0 | None | upstream_eof | upstream_read |
| 26 | 26 | 26 | 1 | 0 | None | upstream_eof | upstream_read |
| 27 | 27 | 27 | 1 | 0 | None | upstream_eof | upstream_read |
| 28 | 28 | 28 | 1 | 0 | None | upstream_eof | upstream_read |
| 29 | 29 | 29 | 1 | 0 | None | upstream_eof | upstream_read |
| 30 | 30 | 30 | 1 | 0 | None | upstream_eof | upstream_read |
| 31 | 31 | 31 | 1 | 0 | None | upstream_eof | upstream_read |
| 32 | 32 | 32 | 1 | 0 | None | upstream_eof | upstream_read |
| 33 | 33 | 33 | 1 | 0 | None | upstream_eof | upstream_read |
| 34 | 34 | 34 | 1 | 0 | None | upstream_eof | upstream_read |
| 35 | 35 | 35 | 1 | 0 | None | upstream_eof | upstream_read |
| 36 | 36 | 36 | 1 | 0 | None | upstream_eof | upstream_read |
| 37 | 37 | 37 | 1 | 0 | None | socket_error | client_read |
| 38 | 38 | 100 | 63 | 45 | 100 | client_eof | client_read |

### الخيط 6

- HTTP 400: 13–14, 20–21, 27–28, 35, 41–42, 48–49, 55–56, 62–63, 69–70, 76–77, 83, 90–91, 97–98.
- أخطاء النقل: 1–8, 30–34, 58–61, 84.
- أرقام المحاولات التي بدأت اتصالًا جديدًا بعد الأول: 2–9, 31–35, 59–62, 85.
- طلبات إغلاق في ترويسة HTTP: لا يوجد.

| الاتصال | أول استعلام | آخر استعلام | عدد المحاولات | الناجحة | آخر استعلام ناجح | سبب انتهاء رصد الاتصال | موضعه |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 1 | 1 | 0 | None | client_eof | client_read |
| 2 | 2 | 2 | 1 | 0 | None | client_eof | client_read |
| 3 | 3 | 3 | 1 | 0 | None | client_eof | client_read |
| 4 | 4 | 4 | 1 | 0 | None | client_eof | client_read |
| 5 | 5 | 5 | 1 | 0 | None | client_eof | client_read |
| 6 | 6 | 6 | 1 | 0 | None | client_eof | client_read |
| 7 | 7 | 7 | 1 | 0 | None | client_eof | client_read |
| 8 | 8 | 8 | 1 | 0 | None | client_eof | client_read |
| 9 | 9 | 30 | 22 | 15 | 29 | client_eof | client_read |
| 10 | 31 | 31 | 1 | 0 | None | client_eof | client_read |
| 11 | 32 | 32 | 1 | 0 | None | client_eof | client_read |
| 12 | 33 | 33 | 1 | 0 | None | client_eof | client_read |
| 13 | 34 | 34 | 1 | 0 | None | client_eof | client_read |
| 14 | 35 | 58 | 24 | 16 | 57 | client_eof | client_read |
| 15 | 59 | 59 | 1 | 0 | None | client_eof | client_read |
| 16 | 60 | 60 | 1 | 0 | None | client_eof | client_read |
| 17 | 61 | 61 | 1 | 0 | None | client_eof | client_read |
| 18 | 62 | 84 | 23 | 15 | 82 | client_eof | client_read |
| 19 | 85 | 100 | 16 | 12 | 100 | client_eof | client_read |

### الخيط 7

- HTTP 400: 5–6, 12–13, 19–20, 33–34, 40–41, 47–48, 54–55, 61–62, 68–69, 82–83, 89–90, 96–97.
- أخطاء النقل: 23–29, 52–53, 75–79.
- أرقام المحاولات التي بدأت اتصالًا جديدًا بعد الأول: 24–30, 53–54, 76–80.
- طلبات إغلاق في ترويسة HTTP: لا يوجد.

| الاتصال | أول استعلام | آخر استعلام | عدد المحاولات | الناجحة | آخر استعلام ناجح | سبب انتهاء رصد الاتصال | موضعه |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 23 | 23 | 16 | 22 | client_eof | client_read |
| 2 | 24 | 24 | 1 | 0 | None | client_eof | client_read |
| 3 | 25 | 25 | 1 | 0 | None | client_eof | client_read |
| 4 | 26 | 26 | 1 | 0 | None | client_eof | client_read |
| 5 | 27 | 27 | 1 | 0 | None | client_eof | client_read |
| 6 | 28 | 28 | 1 | 0 | None | client_eof | client_read |
| 7 | 29 | 29 | 1 | 0 | None | client_eof | client_read |
| 8 | 30 | 52 | 23 | 16 | 51 | client_eof | client_read |
| 9 | 53 | 53 | 1 | 0 | None | client_eof | client_read |
| 10 | 54 | 75 | 22 | 15 | 74 | client_eof | client_read |
| 11 | 76 | 76 | 1 | 0 | None | client_eof | client_read |
| 12 | 77 | 77 | 1 | 0 | None | client_eof | client_read |
| 13 | 78 | 78 | 1 | 0 | None | client_eof | client_read |
| 14 | 79 | 79 | 1 | 0 | None | client_eof | client_read |
| 15 | 80 | 100 | 21 | 15 | 100 | client_eof | client_read |

### الخيط 8

- HTTP 400: 12, 18–19, 25–26, 32–33, 39–40, 46–47, 53–54, 60–61, 67–68, 74–75, 81–82, 88–89, 95–96.
- أخطاء النقل: 1–11, 34–38, 57–59.
- أرقام المحاولات التي بدأت اتصالًا جديدًا بعد الأول: 2–12, 35–39, 58–60.
- طلبات إغلاق في ترويسة HTTP: لا يوجد.

| الاتصال | أول استعلام | آخر استعلام | عدد المحاولات | الناجحة | آخر استعلام ناجح | سبب انتهاء رصد الاتصال | موضعه |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 1 | 1 | 0 | None | client_eof | client_read |
| 2 | 2 | 2 | 1 | 0 | None | client_eof | client_read |
| 3 | 3 | 3 | 1 | 0 | None | client_eof | client_read |
| 4 | 4 | 4 | 1 | 0 | None | client_eof | client_read |
| 5 | 5 | 5 | 1 | 0 | None | client_eof | client_read |
| 6 | 6 | 6 | 1 | 0 | None | client_eof | client_read |
| 7 | 7 | 7 | 1 | 0 | None | client_eof | client_read |
| 8 | 8 | 8 | 1 | 0 | None | client_eof | client_read |
| 9 | 9 | 9 | 1 | 0 | None | client_eof | client_read |
| 10 | 10 | 10 | 1 | 0 | None | client_eof | client_read |
| 11 | 11 | 11 | 1 | 0 | None | client_eof | client_read |
| 12 | 12 | 34 | 23 | 15 | 31 | client_eof | client_read |
| 13 | 35 | 35 | 1 | 0 | None | client_eof | client_read |
| 14 | 36 | 36 | 1 | 0 | None | client_eof | client_read |
| 15 | 37 | 37 | 1 | 0 | None | upstream_eof | upstream_read |
| 16 | 38 | 38 | 1 | 0 | None | client_eof | client_read |
| 17 | 39 | 57 | 19 | 12 | 56 | client_eof | client_read |
| 18 | 58 | 58 | 1 | 0 | None | client_eof | client_read |
| 19 | 59 | 59 | 1 | 0 | None | client_eof | client_read |
| 20 | 60 | 100 | 41 | 29 | 100 | client_eof | client_read |

### الخيط 9

- HTTP 400: 24–25, 31–32, 38–39, 45–46, 52–53, 59–60, 66–67, 73–74, 80–81, 87–88, 94–95.
- أخطاء النقل: 1–23.
- أرقام المحاولات التي بدأت اتصالًا جديدًا بعد الأول: 2–24.
- طلبات إغلاق في ترويسة HTTP: لا يوجد.

| الاتصال | أول استعلام | آخر استعلام | عدد المحاولات | الناجحة | آخر استعلام ناجح | سبب انتهاء رصد الاتصال | موضعه |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 1 | 1 | 0 | None | client_eof | client_read |
| 2 | 2 | 2 | 1 | 0 | None | client_eof | client_read |
| 3 | 3 | 3 | 1 | 0 | None | client_eof | client_read |
| 4 | 4 | 4 | 1 | 0 | None | client_eof | client_read |
| 5 | 5 | 5 | 1 | 0 | None | client_eof | client_read |
| 6 | 6 | 6 | 1 | 0 | None | client_eof | client_read |
| 7 | 7 | 7 | 1 | 0 | None | client_eof | client_read |
| 8 | 8 | 8 | 1 | 0 | None | client_eof | client_read |
| 9 | 9 | 9 | 1 | 0 | None | client_eof | client_read |
| 10 | 10 | 10 | 1 | 0 | None | client_eof | client_read |
| 11 | 11 | 11 | 1 | 0 | None | client_eof | client_read |
| 12 | 12 | 12 | 1 | 0 | None | client_eof | client_read |
| 13 | 13 | 13 | 1 | 0 | None | client_eof | client_read |
| 14 | 14 | 14 | 1 | 0 | None | client_eof | client_read |
| 15 | 15 | 15 | 1 | 0 | None | client_eof | client_read |
| 16 | 16 | 16 | 1 | 0 | None | client_eof | client_read |
| 17 | 17 | 17 | 1 | 0 | None | client_eof | client_read |
| 18 | 18 | 18 | 1 | 0 | None | client_eof | client_read |
| 19 | 19 | 19 | 1 | 0 | None | client_eof | client_read |
| 20 | 20 | 20 | 1 | 0 | None | client_eof | client_read |
| 21 | 21 | 21 | 1 | 0 | None | client_eof | client_read |
| 22 | 22 | 22 | 1 | 0 | None | client_eof | client_read |
| 23 | 23 | 23 | 1 | 0 | None | client_eof | client_read |
| 24 | 24 | 100 | 77 | 55 | 100 | client_eof | client_read |

### الخيط 10

- HTTP 400: 30–31, 37–38, 44–45, 51–52, 58–59, 65–66, 72–73, 79–80, 86–87, 93–94, 100.
- أخطاء النقل: 1–24.
- أرقام المحاولات التي بدأت اتصالًا جديدًا بعد الأول: 2–25.
- طلبات إغلاق في ترويسة HTTP: لا يوجد.

| الاتصال | أول استعلام | آخر استعلام | عدد المحاولات | الناجحة | آخر استعلام ناجح | سبب انتهاء رصد الاتصال | موضعه |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 1 | 1 | 0 | None | client_eof | client_read |
| 2 | 2 | 2 | 1 | 0 | None | client_eof | client_read |
| 3 | 3 | 3 | 1 | 0 | None | upstream_eof | upstream_read |
| 4 | 4 | 4 | 1 | 0 | None | client_eof | client_read |
| 5 | 5 | 5 | 1 | 0 | None | client_eof | client_read |
| 6 | 6 | 6 | 1 | 0 | None | client_eof | client_read |
| 7 | 7 | 7 | 1 | 0 | None | client_eof | client_read |
| 8 | 8 | 8 | 1 | 0 | None | client_eof | client_read |
| 9 | 9 | 9 | 1 | 0 | None | client_eof | client_read |
| 10 | 10 | 10 | 1 | 0 | None | client_eof | client_read |
| 11 | 11 | 11 | 1 | 0 | None | client_eof | client_read |
| 12 | 12 | 12 | 1 | 0 | None | client_eof | client_read |
| 13 | 13 | 13 | 1 | 0 | None | client_eof | client_read |
| 14 | 14 | 14 | 1 | 0 | None | client_eof | client_read |
| 15 | 15 | 15 | 1 | 0 | None | client_eof | client_read |
| 16 | 16 | 16 | 1 | 0 | None | client_eof | client_read |
| 17 | 17 | 17 | 1 | 0 | None | client_eof | client_read |
| 18 | 18 | 18 | 1 | 0 | None | client_eof | client_read |
| 19 | 19 | 19 | 1 | 0 | None | client_eof | client_read |
| 20 | 20 | 20 | 1 | 0 | None | client_eof | client_read |
| 21 | 21 | 21 | 1 | 0 | None | client_eof | client_read |
| 22 | 22 | 22 | 1 | 0 | None | client_eof | client_read |
| 23 | 23 | 23 | 1 | 0 | None | client_eof | client_read |
| 24 | 24 | 24 | 1 | 0 | None | client_eof | client_read |
| 25 | 25 | 100 | 76 | 55 | 99 | client_eof | client_read |

## قراءة أسباب انتهاء الاتصال

- `upstream_eof`: أغلق الطرف البعيد لاتصال البروكسي تدفق TCP. لا يحدد القياس وحده هل السبب زين أم البروكسي.
- `client_eof`: أغلق عميل الاختبار الاتصال. قد يحدث بعد مهلة أو عند نهاية الخيط؛ ليس دليلًا على أن زين طلب إغلاقه.
- `socket_error`: تُراجَع معه المرحلة ونوع الخطأ في ملف الاتصالات؛ خطأ قراءة جهة العميل يختلف عن خطأ جهة البروكسي.
- انتهاء مهلة ثم إغلاق العميل لا يثبت عددًا أقصى من الاستعلامات يسمح به الخادم.
- لم تُستأنف محاولة فاشلة على اتصال جديد؛ الانتقال للاتصال الجديد يحدث للمحاولة التالية فقط.

## التفاصيل القابلة للمراجعة

- [كل الاستعلامات CSV](all_requests.csv): الوقت، الخيط، رقم المحاولة والعينة، HTTP، النتيجة، الأخطاء، معرّف الاتصال، البايتات والمدة والفاصل.
- [كل الاتصالات CSV](all_connections.csv): أعمار الاتصالات، أسباب إغلاقها، الطلبات المرتبطة بها والبايتات.
- [التقرير الكامل JSON](report.json).
- [التحليل المختصر JSON](analysis.json).

القياس يجمع بيانات TCP المشفرة في الاتجاهين مع SOCKS وTLS، ويستثني DNS وترويسات TCP/IP وإعادة إرسال حزم TCP. لا يحتوي على أرقام الحسابات أو مبالغها أو بيانات اعتماد البروكسي.
