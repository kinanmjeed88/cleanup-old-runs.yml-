# المراجعة النهائية المركزة لـ PR #4

- المستودع: `kinanmjeed88/cleanup-old-runs.yml-`
- PR: [#4](https://github.com/kinanmjeed88/cleanup-old-runs.yml-/pull/4) نحو `searchidf`
- أساس المراجعة: `7851c0cbc721493244bb2eadccfc61af14c71b81`
- الحالة عند بدء المراجعة: `OPEN`، غير مدموج.
- **لم يُحذف أي تشغيل أو فرع أو مستودع حقيقي.** كل اختبارات الحذف تستخدم API وهمياً في الذاكرة.

> يُثبَّت SHA النهائي ونتيجة CI في تعليق PR وتقرير التسليم، لأن أي تعديل توثيقي يغيّر SHA. هذه الوثيقة تصف المراجعة والإصلاحات ولا تدّعي أن SHA أساس المراجعة هو SHA النهائي.

## 1. حكم المراجعة على الملاحظات

| الملاحظة | الحكم | الدليل |
|---|---|---|
| `Scan-id` يعرض 754/752/50/702 | صحيحة حسابياً | 752 مكتملة − 50 محتفظاً = 702 مرشح؛ النشطتان مستثناة، والمحذوف الفعلي 0 في dry-run |
| المركزي 18/18/0/0 ويحتاج تفسيراً | اختلاف تعريف عدّادات، وليس خطأ احتفاظ | JSON الفعلي يحتوي `runs_retained=18`، `runs_planned_deletions=0`، `runs_deleted=0`، `runs_remaining=18` |
| توثيق 404 و422 | التنفيذ كان قريباً من التوثيق، لكن التقارير كانت تحتاج تدقيقاً يمنع التباس الفشل الجزئي | أُضيفت تغطية workflow فعلية، وتوضيح أن 404 ليس حذفاً، وأن 422 يبقى فشلاً، وأن العدد المتوقع يشمل الفاشل وغير المحاولة |

الاختصار السابق `18/18/0/0` كان يعني: **مفحوصة / مكتملة / مرشحو حذف / محذوف فعلي**. لا يوجد في تقرير المستودع المركزي حقل `retained=0`.

## 2. تتبع العدادات المركزية من API إلى JSON وMarkdown

المسار الفعلي:

```text
GET /repos/{owner}/{repo}/actions/runs?per_page=100&page=N
  → tool/github_client.py::list_workflow_runs()
  → tool/run_retention.py::fetch_all_runs()
      يتحقق من الصفحات و total_count والتكرار والسجلات
  → tool/run_retention.py::plan_retention()
      keep = أحدث min(50, عدد المكتملة المؤهلة)
      delete = الباقي من الأقدم للأحدث
  → tool/central_cleanup.py::_process_runs()
      report.runs_retained = len(plan.keep)
      report.runs_planned_deletions = len(outcome.planned)
      report.runs_deleted = len(outcome.deleted)
      report.runs_remaining = العدد المرصود في dry-run
  → tool/reports.py::RepoReport.as_dict()
      counts.runs_retained / runs_planned_deletions / runs_deleted / runs_remaining
  → tool/reports.py::ReportWriter.write_repository()
      reports/<owner>__<repo>.json و .md
  → ReportWriter.write_aggregate() و append_step_summary()
      summary.json و REPORT.md و $GITHUB_STEP_SUMMARY
```

من دليل المعاينة الحقيقية السابقة للقراءة فقط، كان JSON المركزي:

```json
{
  "runs_examined": 18,
  "runs_completed": 18,
  "runs_retained": 18,
  "runs_planned_deletions": 0,
  "runs_deleted": 0,
  "runs_failed": 0,
  "runs_remaining": 18
}
```

وMarkdown وافقه: `Retained (newest 50 completed): 18`، `Planned deletions: 0`، `Deleted: 0`، `Remaining: 18`. لذلك فالصفر ليس عدد المحتفظ به.

## 3. استثناء الفروع لا يستثني التشغيلات

في `CentralCleanup.process()` يوجد مساران مستقلان:

1. إذا كان `options.run_runs` مفعلاً وقراءة Actions متاحة، يُستدعى `_process_runs()` للمستودع، بما فيه المركزي.
2. بعد ذلك فقط، يفحص مسار الفروع `options.branch_cleanup_excluded(repository)` ويتخطى **الفروع فقط** للمستودع المركزي أو المستثنى بالإعداد.

`skip_branches` يعطل مرحلة الفروع عامة، ولا يعطل مرحلة التشغيلات. الاختبار الجديد يثبت أن مستودعاً مركزياً فيه 18 تشغيلة مكتملة يحتفظ بها كلها، وأن مستودعاً آخر غير مركزي ما زال يدخل مرحلة الفروع وحذف التشغيلات في الوضع الوهمي.

## 4. تدقيق 404 و422

| الاستجابة | التنفيذ | التقرير/الخروج |
|---|---|---|
| 200/204 | حذف ناجح | `runs_deleted` فقط |
| 404 على DELETE الخاص بتشغيلة | لا إعادة محاولة؛ `GhCliClient` يعيدها كـ`ok=True, status=404`، و`apply_retention` يضعها في `already_absent` لا `deleted` | يجب أن يؤكد إعادة السرد غيابها؛ إذا بقيت موجودة فالتحقق `mismatch` والحالة `FAILED` وليس نجاحاً |
| 422 | لا إعادة محاولة؛ تسجل كفشل ثم تستمر التشغيلات التالية | `runs_failed` ونص الفشل يبقيان ظاهرين؛ الحالة غير ناجحة والخروج `1` |
| 401/403/429/5xx أو فشل نقل | توقف حذف ذلك المستودع | سبب التوقف والفشل ظاهران؛ الخروج `1` |

404 على سرد التشغيلات أو فحص المستودع أو حماية الفروع ليست حالة الحذف Idempotent نفسها؛ تلك المسارات تحتفظ بسلوكها المحافظ.

### إصلاحات منع إخفاء الفشل

- `expected_remaining` بعد التنفيذ يشمل الآن protected + failed + unattempted، بدلاً من protected فقط.
- سجل التحقق يعرض `failed_ids` و`unattempted_ids` و`still_present_failed_ids` و`still_present_unattempted_ids`.
- `verification=verified` يعني أن ثوابت الحالة النهائية فُحصت، ولا يلغي فشل عملية؛ قد يبقى المستودع `PARTIAL_FAILURE`.
- 404 وحده لا يحول المستودع إلى `PARTIAL_FAILURE` إلا إذا أكد التحقق الغياب؛ وإلا يبقى `FAILED`.
- ناقل الاختبارات الوهمي يحافظ على دلالة 404 ولا يحولها إلى 204.

## 5. الاختبارات المضافة والنتيجة

أُضيفت 9 حالات اختبار، فأصبح المجموع **252 passed** بدلاً من 243:

- انحدار المركزي ذي 18 تشغيلة في dry-run وlive، مع مقارنة JSON وMarkdown وaggregate وstep summary.
- اختبار سياسة نقي يثبت أن 18 تشغيلة مكتملة تبقى كلها بلا مرشحي حذف.
- اختبار workflow للمركزي ذي 18 تشغيلة يثبت أن مرحلة التشغيلات عملت وأن الفروع فقط مستثناة.
- workflow يفشل أول حذف بـ422 ثم يكمل: فشل ظاهر، `PARTIAL_FAILURE`، خروج 1، و`expected_remaining` يشمل الفاشل.
- workflow يعيد 404 لتشغيلة غابت فعلاً: `already_absent=1` وليس `deleted`، والتحقق يؤكد الغياب.
- workflow يعيد 404 لكن التشغيلة ما زالت موجودة: التحقق `mismatch` والحالة `FAILED` والخروج 1.
- عميل GitHub الحقيقي مع ناقل وهمي يثبت أن 422 لا يُعاد محاوله ويبقى فشلاً.
- اختبار حالة التقرير يمنع 404 غير المتحقق من تحويل الفشل إلى فشل جزئي.

الأوامر المنفذة محلياً:

```bash
python -m pytest -q
python -m compileall -q tool tests
python tool/check_workflow_permissions.py .github/workflows
python -m bandit -r tool -ll -q
python -m bandit -r tool -f json -o bandit-all-severities.json
```

## 6. فحوص الأمان

- بوابة CI لـBandit عند متوسط/عالٍ: **0 نتائج**.
- فحص Bandit لكل المستويات: **11 نتيجة LOW، 0 MEDIUM، 0 HIGH**. روجعت ولم تُخفَ:
  - B404/B603: استخدام `subprocess` لقوائم أوامر ثابتة لـ`gh`/`git`/Python، بلا `shell=True`؛ مدخلات المستودع مقيدة ومسارات السكربتات مطلقة.
  - B105: `CLEANUP_TOKEN` اسم متغير بيئة وليس كلمة مرور مضمنة.
  - B112 في قراءة PR المدموجة: تجاهل سجل غير قابل للتحليل يسقط دليل دمج، وهو اتجاه محافظ يمنع الحذف بناءً على دليل ناقص.
  - B110 في كتابة step summary الاختياري أو ترميز احتياطي: لا يبتلع فشل حذف؛ فشل الحذف يبقى في JSON/Markdown وسبب الخروج.
- فحص صلاحيات workflows: ناجح؛ رمز workflow المركزي قراءة فقط، وCI لا يستقبل `CLEANUP_TOKEN`.
- اختبارات سلامة الرمز وإخفائه من التقارير موجودة وناجحة.

## 7. النشر والمراقبة

تُنشر الإصلاحات والتوثيق والاختبارات في دفعة واحدة على فرع PR نفسه. بعد الدفع يجب التأكد من:

1. أن `headRefOid` في PR يساوي SHA المحلي النهائي.
2. أن وظيفة `Tests, syntax and workflow permissions` خضراء على ذلك SHA.
3. أن خطوات Bandit، full pytest، workflow Bash-to-CLI، المحاكاة الجافة، وdiscovery كلها ناجحة.
4. أن PR ما زال `OPEN` وغير مدموج.

لا يوجد في هذه المراجعة أي حذف حقيقي، ولا تغيير لصلاحيات الرمز، ولا إثبات لـ`Actions: Write`. أي live لاحق يحتاج موافقة منفصلة ومعاينة جديدة.
