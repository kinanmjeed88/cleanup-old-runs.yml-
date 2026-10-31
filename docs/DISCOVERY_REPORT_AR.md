# تقرير الاكتشاف والتشغيل التجريبي (Dry Run)

> **المستودع المركزي:** `kinanmjeed88/cleanup-old-runs.yml-`
> **الفرع:** `arena/3825d87e-cleanup-old-runs-yml`
> **التاريخ:** 2026-11-01
> **الوضع:** DRY RUN فقط — **لم يُحذف أي شيء** (لا runs ولا فروع ولا مستودعات).

---

## 1. الخلاصة

| البند | النتيجة |
|---|---|
| المستودعات المكتشفة تلقائياً | **24** |
| اكتشاف كامل (بدون أخطاء صفحات) | **نعم** |
| مستودعات قابلة للكتابة (يمكن تنظيفها) | **13** |
| مستودعات للقراءة فقط (تم تحليلها وتخطّيها) | **11** |
| مستودعات فاشلة | **0** |
| Workflow runs مفحوصة | **6,827** |
| Runs محفوظة (أحدث 20 لكل مستودع) | **337** |
| Runs مرشّحة للحذف (لم تُحذف) | **6,487** |
| فروع مفحوصة | **319** |
| فروع محفوظة | **319** |
| فروع مرشّحة للحذف | **0** |
| رمز الدخول في السجلات أو التقارير | **لا يوجد** |

الاكتشاف لا يعتمد على قائمة يدوية ولا على قائمة "Top repositories" في لوحة التحكم: يعتمد على صلاحيات `CLEANUP_TOKEN` الحقيقية في كل تشغيل.

---

## 2. المصادر التي استُخدمت في الاكتشاف

| المصدر | عدد المستودعات | الصفحات | الحالة |
|---|---|---|---|
| `GET /user/repos` (كل ما يراه الرمز: عام + خاص + مالك + متعاون) | 13 | 1 | مكتمل |
| `GET /users/techtouchAI/repos` (بعد أن ردّ `/orgs/techtouchAI/repos` بـ 404 لأن الحساب نوعه **User** وليس Organization) | 11 | 1 | مكتمل |

* كل مصدر يُقرأ حتى آخر صفحة (`per_page=100`)، وأي صفحة تفشل تجعل الاكتشاف **غير مكتمل** وتُبلَّغ صراحة.
* التكرار يُزال بمفتاح `full_name` (غير حسّاس لحالة الأحرف)، ويُدمج كل مصدر عُثر فيه على المستودع: 24 مستودعاً فريداً بلا تكرار.
* يوجد فحص تشخيصي: يقارن ما يراه المستخدم العام بما يراه الرمز، فإذا كان `CLEANUP_TOKEN` من نوع fine-grained محدوداً بـ"مستودعات مختارة" يظهر تنبيه يطلب تحويله إلى **All repositories** بدلاً من الظهور كأن الحساب فارغ.

---

## 3. قائمة المستودعات المكتشفة (كما طُبعت قبل أي عملية)

### 3.1 مستودعات قابلة للكتابة — 13

| المستودع | المالك | النوع | الظهور | الفرع الافتراضي | Fork | كتابة |
|---|---|---|---|---|---|---|
| `kinanmjeed88/Almoktaber` | kinanmjeed88 | User | عام | `main` | لا | نعم |
| `kinanmjeed88/cleanup-old-runs.yml-` *(المركزي)* | kinanmjeed88 | User | عام | `searchidf` | لا | نعم |
| `kinanmjeed88/Excel` | kinanmjeed88 | User | عام | `main` | لا | نعم |
| `kinanmjeed88/Jadwal-backup` | kinanmjeed88 | User | عام | `searchidf` | لا | نعم |
| `kinanmjeed88/jadwalv2` | kinanmjeed88 | User | عام | `main` | لا | نعم |
| `kinanmjeed88/kinanmjeed88.github.io` | kinanmjeed88 | User | عام | `main` | لا | نعم |
| `kinanmjeed88/kinantouch` | kinanmjeed88 | User | عام | `main` | لا | نعم |
| `kinanmjeed88/Mukhtar-` | kinanmjeed88 | User | عام | `main` | لا | نعم |
| `kinanmjeed88/Photo-jpg` | kinanmjeed88 | User | عام | `main` | لا | نعم |
| `kinanmjeed88/Scan-id` | kinanmjeed88 | User | عام | `searchidf` | لا | نعم |
| `kinanmjeed88/Student-Assessment` | kinanmjeed88 | User | عام | `main` | لا | نعم |
| `kinanmjeed88/SUN-` | kinanmjeed88 | User | عام | `golden-version` | **نعم** | نعم |
| `kinanmjeed88/Write-the-questions` | kinanmjeed88 | User | عام | `searchidf` | لا | نعم |

### 3.2 مستودعات ظهرت لكن الرمز يراها للقراءة فقط — 11 (تُعالَج كـ `SKIPPED`)

`techtouchAI/Debt-register` · `File-editor` · `Islamic` · `PhotoEditor-k1` · `Q-s` · `Student-hodor` · `SUN-` · `techtouchAI` · `The-believer-s-bag.` · `UmAyan` · `Writing-questions`

> سبب التخطّي: استجابة `GET /repos/{owner}/{repo}` لا تحمل أي `permissions.push` لهذا الرمز، أي **لا صلاحية حذف**. لم يُحاول النظام حذف أي شيء فيها، ولم يُشغّل مرحلة الفروع أصلاً (لا استنساخ ولا حذف)، لكنه عرض عدد الـ runs التي *كانت* ستُحذف لو مُنح الرمز صلاحية الكتابة. اختلاف المالك **ليس** سبباً للتخطّي، والعكس صحيح: `techtouchAI` لو مُنح الرمز صلاحية عليها فستُعالج مثل غيرها.

---

## 4. نتيجة التشغيل التجريبي على الـ24 مستودعاً

| المستودع | الحالة | Runs مفحوصة | محفوظة | مرشّحة للحذف | فروع مفحوصة | فروع للحذف |
|---|---|---:|---:|---:|---:|---:|
| `kinanmjeed88/Almoktaber` | DRY RUN | 114 | 20 | 94 | 45 | 0 |
| `kinanmjeed88/cleanup-old-runs.yml-` | DRY RUN | 12 | 12 | 0 | 0 | 0 |
| `kinanmjeed88/Excel` | DRY RUN | 14 | 14 | 0 | 1 | 0 |
| `kinanmjeed88/Jadwal-backup` | DRY RUN | 120 | 20 | 100 | 6 | 0 |
| `kinanmjeed88/jadwalv2` | DRY RUN | 441 | 20 | 421 | 108 | 0 |
| `kinanmjeed88/kinanmjeed88.github.io` | DRY RUN | 622 | 20 | 602 | 16 | 0 |
| `kinanmjeed88/kinantouch` | DRY RUN | 170 | 20 | 150 | 1 | 0 |
| `kinanmjeed88/Mukhtar-` | DRY RUN | 8 | 8 | 0 | 1 | 0 |
| `kinanmjeed88/Photo-jpg` | DRY RUN | 294 | 20 | 274 | 86 | 0 |
| `kinanmjeed88/Scan-id` | DRY RUN | 613 | 20 | 590 | 39 | 0 |
| `kinanmjeed88/Student-Assessment` | DRY RUN | 170 | 20 | 150 | 8 | 0 |
| `kinanmjeed88/SUN-` | DRY RUN | 0 | 0 | 0 | 8 | 0 |
| `kinanmjeed88/Write-the-questions` | DRY RUN | 0 | 0 | 0 | 0 | 0 |
| `techtouchAI/Debt-register` | SKIPPED | 120 | 20 | 100 | 0 | 0 |
| `techtouchAI/File-editor` | SKIPPED | 0 | 0 | 0 | 0 | 0 |
| `techtouchAI/Islamic` | SKIPPED | 1727 | 20 | 1707 | 0 | 0 |
| `techtouchAI/PhotoEditor-k1` | SKIPPED | 2 | 2 | 0 | 0 | 0 |
| `techtouchAI/Q-s` | SKIPPED | 1 | 1 | 0 | 0 | 0 |
| `techtouchAI/Student-hodor` | SKIPPED | 295 | 20 | 275 | 0 | 0 |
| `techtouchAI/SUN-` | SKIPPED | 147 | 20 | 127 | 0 | 0 |
| `techtouchAI/techtouchAI` | SKIPPED | 178 | 20 | 158 | 0 | 0 |
| `techtouchAI/The-believer-s-bag.` | SKIPPED | 0 | 0 | 0 | 0 | 0 |
| `techtouchAI/UmAyan` | SKIPPED | 65 | 20 | 45 | 0 | 0 |
| `techtouchAI/Writing-questions` | SKIPPED | 1714 | 20 | 1694 | 0 | 0 |
| **الإجمالي** | | **6,827** | **337** | **6,487** | **319** | **0** |

* المستودع المركزي: أُجريت له إدارة الـ runs فقط (حفاظاً على نظافته) لكن **مرحلة الفروع مستثناة له بالكامل** (`branches scanned = 0`) — هذه حماية إلزامية.
* `kinanmjeed88/SUN-` و`Write-the-questions` ليس فيهما أي runs، لذلك 0.
* `Mukhtar-` فيه 8 runs فقط (< 20) فحُفظت كلها؛ نفس المنطق لـ`Excel` و`cleanup-old-runs.yml-`.

---

## 5. لماذا لم يُرشَّح أي فرع للحذف؟

| السبب | عدد الفروع |
|---|---:|
| **لا يمكن التحقق من الحماية (403) — يُحتفظ بالفرع بأمان** | **277** |
| غير مدموج: للفرع تغييرات فريدة غير موجودة في الفرع الافتراضي | 30 |
| الفرع الافتراضي نفسه | 11 |
| لا يمكن إثبات الدمج الكامل (لا ancestor، ولا merge commit، ولا diff فارغ) | 1 |

السبب الأساسي هو **قاعدة أمان مقصودة** وليست خطأ: `tool/branch_cleanup.py` يُبقي أي فرع لا يستطيع إثبات أن الحماية غير مفعّلة عليه. رمز التشغيل المحلي لا يملك `administration: read`، لذلك كل استعلام حماية يعيد 403 → يُحتفظ بالفرع. **لا يوجد أي مسار في الكود يحذف فرعاً بدون إثبات دمج**، ولم يُغيَّر ترتيب قرارات الحماية ولا شروط إثبات الدمج ولا طريقة الحذف (`DELETE /repos/{repo}/git/refs/heads/{branch}` فقط، بدون `git push`).

لتفعيل حذف الفروع المدموجة لاحقاً يجب أن يملك `CLEANUP_TOKEN` صلاحية **Administration: Read** (أو `repo` في classic PAT) إضافة إلى `Contents: Read and write` و`Pull requests: Read` و`Actions: Read and write`.

---

## 6. الصلاحيات كما تحقّق منها النظام فعلياً

| النطاق | الحالة | الملاحظة |
|---|---|---|
| `metadata: read` | ✅ لكل الـ24 | قراءة بيانات المستودع |
| `contents: write` | ✅ 13 / ❌ 11 | الـ11 للقراءة فقط → `SKIPPED` |
| `pull-requests: read` | ✅ للقابلة للكتابة | لازم لمعرفة الفروع ذات PR مفتوح |
| `actions: read` | ✅ | قراءة كل الـ runs بصفحات كاملة |
| `actions: write` | ⏳ غير قابل للفحص المسبق | يُختبر عند أول حذف، وأي 403 يُوقف ذلك المستودع فقط |
| `administration: read` | ❌ | بسببها حُفظت 277 فروعاً "بأمان" |
| `CLEANUP_TOKEN` في GitHub Actions | لم يُستخدم محلياً | التشغيل المحلي استخدم رمز جلسة `gh`؛ الرمز الحقيقي يُستخدم داخل الـ workflow فقط ولا يُطبع أبداً |

---

## 7. ما تم تغييره في المشروع

| الملف | التغيير |
|---|---|
| `tool/discovery.py` | **جديد**: اكتشاف متعدد المصادر + صفحات كاملة + إزالة التكرار + تشخيص الرمز المحدود + `complete` fail-closed |
| `tool/github_client.py` | `get_authenticated_user`, `list_authenticated_user_repos`, `list_org_repositories`, `list_user_repositories` |
| `tool/reports.py` | مفردات الحالة `DISCOVERED / ACCESSIBLE / DRY RUN / LIVE / SKIPPED / FAILED` + جدول اكتشاف + إجمالي عام |
| `tool/central_cleanup.py` | تنسيق كامل: اكتشاف → طباعة القائمة → فحص صلاحيات كل مستودع → runs ثم فروع → تقرير لكل مستودع؛ فشل مستودع لا يوقف الباقي |
| `config/accounts.txt` | **جديد**: `kinanmjeed88` و`techtouchAI` |
| `config/repositories.txt` | أصبح اختيارياً، يُستخدم فقط في نمطي `list` و`union` |
| `.github/workflows/cleanup-old-runs.yml` | إدخال `discovery_mode` و`discovery_only`، تمرير `--accounts`، طباعة الإجمالي، رفع التقارير دائماً |
| `.github/workflows/ci.yml` | خطوة اختبارات اكتشاف إضافية |
| `tests/test_discovery.py` | **جديد**: 25 اختباراً (صفحات، دمج مصادر، fallback من org إلى user، رمز محدود، فشل صفحة، اختيار الأهداف، منع المستودعات غير المكتشفة، منع live عند اكتشاف ناقص) |
| `tests/fakes.py` + اختبارات موجودة | محاكاة نقاط الاكتشاف وتحديث مفردات الحالة (لم يُحذف أي اختبار) |
| `tool/branch_cleanup.py` | **لم يُمَس** (قرارات الحماية وإثبات الدمج وطريقة الحذف كما هي) |

---

## 8. حمايات مفعّلة

1. **لا قائمة يدوية**: الاكتشاف ديناميكي في كل تشغيل، ومن المستحيل معالجة مستودع لم يُكتشف (`--repository` يرفض أي اسم غير موجود في نتيجة الاكتشاف).
2. **اكتشاف ناقص = لا حذف حقيقي**: إذا فشلت أي صفحة أو مصدر، يُرفض وضع `live` قبل أي عملية.
3. **المركزي محمي**: لا تُحذف فروعه أبداً (والـ runs تُنظَّف فقط).
4. **المؤرشف `SKIPPED`** بالكامل، و**للقراءة فقط `SKIPPED`** بدون أي تحوير.
5. **لا حذف مستودعات، ولا تعديل ملفات مشاريع، ولا `git push`** — حذف الفروع عبر GitHub API فقط، وكل استنساخ مؤقت في `/tmp` ويُحذف بعد الاستخدام.
6. **الـ runs الجارية/المعلّقة/الحالية لا تُمس**، و20 الأحدث لكل مستودع لا تُحذف أبداً.
7. **لا رمز في السجلات**: كل نص يمر عبر `redact()`، والرمز لا يُكتب على القرص ولا يُدرج في أي URL.
8. **عزل الفشل**: كل مستودع في سياق Git مستقل؛ خطأ مستودع لا يوقف الباقي (مُختبر صراحة).

---

## 9. الخطوات التالية (تحتاج موافقتك قبل أي حذف حقيقي)

1. **اختبار الاكتشاف داخل GitHub Actions** (لا حذف):
   `Actions → Central Cleanup → Run workflow` ثم `discovery_only = true` و`mode = dry-run`.
2. **تشغيل Dry Run كامل** (`mode = dry-run`) ومراجعة `diagnostics/REPORT.md` والتقارير لكل مستودع.
3. إن رغبت في حذف الفروع المدموجة: أضف **Administration: Read** إلى `CLEANUP_TOKEN` ثم أعد الـ dry run؛ سترى الفروع المؤهلة تظهر مع سببها.
4. **بعد موافقتك فقط**: `mode = live` مع `confirm_live = DELETE`. التشغيل المجدول يبقى دائماً DRY RUN.

**لم يُنفَّذ أي حذف حقيقي في هذه الجولة، ولم يُستخدم `CLEANUP_TOKEN` الحقيقي محلياً.**
