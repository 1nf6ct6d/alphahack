# Финальная передача ML-библиотеки Backend

Этот документ заменяет прежний ML_INTEGRATION_HANDOFF.md в вопросах текущего экспорта метрик и публичных импортов. Область передачи: ml_audit, ml_features, ml_ranking, ml_evaluation. Архив BCE, bce_lab, промпты и агентная оркестрация принадлежат Prompt Engineer. Backend разрабатывается отдельно. Их реализация не входит в эту поставку.

## 1. Состояние и границы

Итоговый статус библиотеки — **ML COMPLETE WITH EXTERNAL DEPENDENCIES** после выполнения приёмочных проверок ниже. Это означает завершённые самостоятельно запускаемые функции текущего ML-объёма с внешними зависимостями для реального сквозного сценария. Это не подтверждение качества на реальных новостях и не заявление о готовом production-сервисе.

Реализовано:

| Пакет | Фактическая функция |
|---|---|
| ml_audit | Обнаружение трёх CSV, аудит структуры, пропусков, дубликатов, конфликтов, чисел и покрытия связей |
| ml_features | Платёжные концентрации, покрытие, самоплатежи, групповые границы; состав группы и полнота выручки; экспорт существующих DerivedMetric с breakdown |
| ml_ranking | Проверка готовых MLView/CompanyBusinessProfile и NewsEvent; релевантность, смысловые проверки регуляторных фрагментов, детерминированный порядок, CFO/CEO и JSON-сериализация |
| ml_evaluation | Замороженные пулы и версии, независимая разметка и арбитраж, защита разбиения, отраслевой baseline, Precision/Recall/nDCG, проверка доказательств и отчёты |

CompanyBusinessProfile assembly, проекция MLView, хранилище, API, аутентификация, сбор новостей и интеграция с LLM-агентом остаются у Backend. Генерация и промпты остаются у Prompt Engineer. Новые HTTP-сервисы, БД, scraping, RAG, embeddings и модели не создавались.

## 2. Установка и состав поставки

Проверено на Python 3.14.0. Исходники используют возможности Python 3.11+, но отдельная совместимость со всеми младшими версиями в этом прогоне не подтверждалась.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-ml.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -B -X utf8 -m examples.ml_library_usage
```

В текущем рабочем окружении используется `.venv_win\Scripts\python.exe`. requirements-ml.txt фиксирует уже проверенные runtime-зависимости jsonschema==4.23.0 и referencing==0.37.0. Новые сторонние библиотеки не добавлены. Проверки выполнялись на установленных версиях, без необходимости скачивать пакеты или обращаться к LLM.

Минимальная runtime-поставка: четыре ML-каталога и три неизменённых файла схем рядом с ними: common.v1.schema.json, company_business_profile.v1.schema.json, profile_views.v1.schema.json в schemas/. Схемы — предоставленные общие контракты; их владение ML не принимает на себя. Проверено выполнение ранжирования в отдельном каталоге без bce_lab, prompts и остальных частей архива.

Сохранить текущую относительную раскладку каталогов: валидатор разрешает схемы через `__file__`. Это исходная Python-библиотека; отдельная wheel-сборка и deployment Backend не реализованы. Тестовые данные, examples/ и документация нужны для воспроизведения примеров, а не для обычного вызова rank_news с объектами Backend.

## 3. Публичные функции для импорта

Основные импорты стабилизированы через __all__. Прежние импорты из модулей serialization остаются рабочими и ссылаются на те же функции, без копий алгоритмов.

```python
from ml_audit import audit_data, write_reports
from ml_features import (
    PaymentFeatures, GroupFeatures,
    compute_payment_features, compute_group_features,
    serialize_features, serialize_derived_metrics,
    serialize_group_features, serialize_group_derived_metrics,
)
from ml_ranking import (
    ArticleEvidence, NewsEvent, RelevanceResult,
    classify_event, rank_news, serialize_result, serialize_ranking,
)
from ml_evaluation import (
    load_dataset, validate_dataset, evaluate_dataset, industry_only_rank,
    evaluate_ranking, dcg, precision_at_k, recall_at_k, ndcg_at_k,
)
```

Точные сигнатуры вычислительных функций:

```text
ml_audit.audit_data(data_dir: str | Path, encoding: str = 'auto') -> dict[str, Any]
ml_audit.write_reports(report: dict[str, Any], output_dir: str | Path) -> tuple[Path, Path]

ml_features.compute_payment_features(company_id: str, company_csv: str | Path, payment_csv: str | Path, *, assume_additive_rows: bool = False, encoding: str = 'auto') -> PaymentFeatures
ml_features.compute_group_features(company_id: str, company_csv: str | Path, payment_csv: str | Path | None = None, *, payment_features: PaymentFeatures | None = None, assume_additive_rows: bool = False, encoding: str = 'auto') -> GroupFeatures
ml_features.serialize_features(features: PaymentFeatures) -> dict[str, Any]
ml_features.serialize_derived_metrics(features: PaymentFeatures) -> list[dict]
ml_features.serialize_group_features(features: GroupFeatures) -> dict[str, Any]
ml_features.serialize_group_derived_metrics(features: GroupFeatures) -> list[dict]

ml_ranking.NewsEvent.from_dict(value: dict[str, Any]) -> NewsEvent
ml_ranking.classify_event(ml_view: dict, event: NewsEvent, *, company_profile: dict | None = None, as_of: datetime | None = None, urgency_horizon_days: int = 7) -> RelevanceResult
ml_ranking.rank_news(ml_view: dict, events: Iterable[NewsEvent], *, company_profile: dict | None = None, analytic_focus: str | None = None, top_k: int = 5, as_of: datetime | None = None, urgency_horizon_days: int = 7) -> list[RelevanceResult]
ml_ranking.serialize_result(result: RelevanceResult) -> dict
ml_ranking.serialize_ranking(results: list[RelevanceResult]) -> list[dict]

ml_evaluation.load_dataset(path: str | Path) -> dict
ml_evaluation.validate_dataset(value: dict) -> dict
ml_evaluation.evaluate_dataset(dataset: dict, *, split: str = 'development', k: int = 5, relevant_threshold: int = 1) -> dict
ml_evaluation.industry_only_rank(ml_view: dict, events: Iterable[NewsEvent], *, top_k: int = 5) -> list[str]
ml_evaluation.evaluate_ranking(ranking: Sequence[str], judgments: Mapping[str, int | None], k: int = 5, relevant_threshold: int = 1) -> dict
ml_evaluation.dcg(grades: Sequence[int]) -> float
ml_evaluation.precision_at_k(ranking: Sequence[str], judgments: Mapping[str, int | None], k: int = 5, relevant_threshold: int = 1) -> float | None
ml_evaluation.recall_at_k(ranking: Sequence[str], judgments: Mapping[str, int | None], k: int = 5, relevant_threshold: int = 1) -> float | None
ml_evaluation.ndcg_at_k(ranking: Sequence[str], judgments: Mapping[str, int | None], k: int = 5) -> float | None
```

Функции записи отдельных отчётов остаются в своих модулях: `ml_features.serialization.write_reports(features: PaymentFeatures, output_dir: str | Path) -> tuple[Path, Path]`, `ml_features.group_serialization.write_group_reports(features: GroupFeatures, output_dir: str | Path) -> tuple[Path, Path]`, `ml_evaluation.reporting.write_reports(report: dict, output_dir: str | Path) -> tuple[Path, Path]`. Одинаковое имя write_reports не означает одинаковый вход.

Вспомогательные интерфейсы: `ml_ranking.validation.validate_contract(value: dict, name: str) -> None` для name='view'/'profile'; `validate_event(event: NewsEvent) -> None`; `ml_evaluation.dataset.freeze_context(context_id: str, ml_view: dict, company_profile: dict | None, provenance: str) -> dict`; `freeze_event(event: NewsEvent, event_group_id: str, split: str) -> dict`; `ml_evaluation.evidence.audit_ranked_evidence(result: dict, event: dict, context: dict) -> list[str]`. Это существующие Python-функции, не новые endpoints.

## 4. Входы, выходы и данные

CSV читаются с `;`, строковыми идентификаторами и обработкой BOM/UTF-8/CP1251. Начальные нули и окружающие пробелы ID не обрезаются. Платёжные функции получают явные пути; audit_data обнаруживает файлы по ожидаемым именам, отвергая неоднозначность.

| Источник | Требуемые столбцы |
|---|---|
| Справочник для ML-1 | SYNTHETIC_U, GROUP_ID; REVENUE_COMPANY используется в выручке группы |
| Платежи | PAYER, BENEFICIARY, PAY_AMT, PAY_CNT |
| Связи физлиц для полного аудита | SYNTHETIC_F, SYNTHETIC_U, CITY_NAME, RC_NAME |

Аудит возвращает status, semantics, methodology, datasets и limitations. Complete означает завершённое чтение, а не отсутствие дефектов данных. Вычислительные PaymentFeatures/GroupFeatures содержат точные Fraction, диагностику, ограничения и SHA-256 источников. Для JSON нужно использовать сериализаторы.

`serialize_features` — диагностический объект, не BCE-профиль. Деньги в нём записаны точными десятичными строками; доли — числами с дополнительными exact_ratios. `serialize_group_features` сохраняет наблюдаемый состав, статусы выручки, неразрешённых участников, платежи и их ограничения. Только serialize_derived_metrics/serialize_group_derived_metrics возвращают объекты существующей схемы DerivedMetric.

ML-1A экспортирует counterparty_count_out, top1_counterparty_share_out, top5_counterparty_share_out, hhi_out, intragroup_share_out, counterparty_coverage_in_df_uid. ML-1B — group_size при подтверждённой группе. Без исходящих наблюдений платёжных DerivedMetric нет. При недостаточных данных значение остаётся null, а не 0.

Платежи передаются в группы через payment_features=payments для повторного использования расчёта. Одновременно payment_csv и payment_features запрещены. Проверяются ID компании, хеш справочника и согласованность разбиения/границ. Файл проверяется по хешу после чтения; кэшированный объект относится к указанному снимку, а не к любым новым байтам по тому же пути.

Дубликаты сохраняются. Assume_additive_rows=True — явное допущение неперекрывающихся строк, не подтверждённый факт. По умолчанию суммы отключены. REVENUE_COMPANY — 2025 год, RUB; 1000 — техническая заглушка. PAY_AMT — RUB; начало периода 1 января, год и конец неизвестны. Самоплатежи исключаются из внешних концентраций. Непустые неизвестные ID получателей остаются в знаменателе; суммы с пустым BENEFICIARY учитываются отдельно как нераспределённые. GROUP_ID не доказывает контроль или поставку; сумма выручки участников не является консолидированной выручкой.

## 5. Завершённый breakdown

Используется существующее необязательное поле `breakdown: [{key: str, share: number}]`, без изменения схемы и без добавления туда диагностических полей.

| Метрика | Содержание breakdown |
|---|---|
| top1_counterparty_share_out | Один максимальный получатель; share = его пригодная внешняя сумма / вся пригодная внешняя сумма |
| top5_counterparty_share_out | До пяти максимальных получателей с тем же общим знаменателем; сумма долей равна top5, а не обязательно 1 |
| hhi_out | Все получатели, включая неизвестных и нулевые наблюдаемые суммы; share — платёжная доля, HHI = сумма квадратов этих долей |

При равных суммах порядок определяется строковым ID. Хвост не теряется в HHI. Доли top5 не перенормируются. Для count, справочного покрытия и внутригрупповой доли такой breakdown не добавляется: у них другой смысл. При отключённом суммировании, нулевом/неизвестном знаменателе или неопределённой метрике breakdown отсутствует; пустой список не выдаётся за доказанное отсутствие связей.

Key — фактически наблюдаемый BENEFICIARY, не новое имя организации и не ID доказательства. Технический U_428697 и отсутствующие/неоднозначные профили сохраняются как исходные платёжные ID; их неопределённость остаётся в диагностике получателей. Они не становятся подтверждёнными юридическими лицами или поставщиками. Окончательное соответствие новости реальной организации требует разрешения идентичности вне этого сериализатора.

Inputs продолжают ссылаться на реальное поле PAYER и фактический ID выборки. Поддельных платежных ID или агрегированного значения, объявленного исходным PAY_AMT, нет. Полная трасса расчёта — хеши CSV, формулы, диагностические получатели и точные доли в serialize_features. DerivedMetric.inputs ограничен существующей схемой восемью элементами; длинный список получателей хранится в разрешённом breakdown, а не выдаётся за восемь полных первичных доказательств.

Версия формирования ID метрик повышена до ml1a-1.1.0: изменившееся содержимое не переиспользует прежние D-ID из кэша. Диагностический формат report_version=ml1a-1.0.0 сохранён, добавлено derived_metrics_version. Backend должен выпускать новую версию профиля и включать версию ML-вычислений в ключ кэша, не менять ранее сохранённый снимок.

Проверка по локальным банковским CSV выполнена при явном допущении аддитивности. Top1/top5/HHI содержат соответствующие фактически наблюдаемые ID и доли; они проверены независимым проходом CSV. Сумма долей HHI и квадратов долей согласована с метрикой, сохранены исходящая сумма и групповое разбиение. Все шесть объектов валидны по исходной схеме. Идентификаторы компаний и финансовые показатели этой проверки не публикуются: полные локальные результаты находятся в исключённом каталоге reports/. Это не оценка модели на реальных новостях.

## 6. Профиль и нормализованная новость

Backend передаёт MLView по profile_views.v1.schema.json#/$defs/MLView: schema_version=1.0.0, company_id, profile_id, as_of_date, usability, weight_semantics=ordinal_prior_not_probability, hooks, numeric_features. В hooks обязательны matching_profile, relevance_priors, interpretation_constraints. Пустые разрешённые списки допустимы; отсутствие обязательного ключа — ошибка.

Для grounded нужен существующий полный CompanyBusinessProfile с теми же ID, датой, usability, hooks и согласованными числовыми признаками. Проверяются FACT/DERIVED, состояния полей, конфликты и надёжность. provenance.validation.schema_valid и provenance.validation.evidence_refs_verified — доверенные результаты проверки Backend, а не самостоятельный повторный аудит CSV. Без полного профиля возможен conditional; новые FACT не создаются.

Backend должен перенести рассчитанные DerivedMetric и при необходимости их breakdown в профиль той же компании, затем спроецировать numeric_features в MLView. Наличие breakdown теперь обеспечено сериализатором; сборка, версия источников и доказанность самого профиля остаются внешней зависимостью. Ранжировщик может использовать видимую проверенную D-метрику с положительной долей для условной связи с точным ID из новости. Платёжная связь не становится поставкой.

NewsEvent.from_dict принимает внутренний объект: обязательны event_id, title, source_id, source_url, event_type. Дополнительно: content, published_at, factor_classes, okved_codes, geography, entities, evidence, deadline, mandatory, applies_to_all_in_scope, synthetic. Структурные списки преобразуются в tuple. Каждая ArticleEvidence содержит evidence_id, quote и supports; quote должен буквально находиться в content. Published_at — ISO datetime с часовым поясом либо None; deadline — ISO-дата либо None. URL проверяется по формату, не загружается.

Поддерживаемые event_type: regulation_change, tax_change, input_cost_change, demand_change, monetary_policy_change, counterparty_disruption, other. Отсутствующие поля не означают отрицательных бизнес-фактов. Одних метаданных, региона, приоров или ключевых слов недостаточно для подтверждённого механизма.

## 7. Ранжирование, роли и доказательства

Rank_news возвращает не более top_k релевантных результатов; none исключается и [] является корректной выдачей. Classify_event сохраняет none для диагностики. Выход serialize_ranking: rank, event_id, company_id, profile_id, title, source_id, source_url, published_at, synthetic, relevance, priority, relevance_explanation, impact_mechanism, conditions, evidence, limitations, interpretation_constraints, ranking_factors.

Relevance — grounded/conditional/none; priority — high/normal/low. Порядок детерминирован: подтверждённость, поддержанная срочность, специфичность механизма, обоснованный фокус роли, свежесть и event_id. Числовой вероятности, размера финансового эффекта или искусственного заполнения top-k нет.

Analytic_focus — None, CFO или CEO с указанным регистром. CFO выделяет поддержанный финансовый канал, CEO — операционный/стратегический. Фокус не меняет факты и не гарантирует разницу выдач при отсутствии достаточных оснований. Веса ordinal_prior_not_probability не являются вероятностями и не используются как самостоятельное доказательство.

As_of — aware datetime, для воспроизводимости его нужно передавать явно. Будущая публикация и дата версии профиля позже локальной календарной даты as_of отвергаются. Без as_of системные часы не подставляются и high по близости срока не создаётся. Внутридневной свежести профиля и TTL в этой проверке нет: это политика Backend.

Регуляторная проверка различает явную обязанность, её введение/усиление, отмену/освобождение и unknown. Противоречивый mandatory не даёт high. Освобождение может сохранять grounded/normal через снижение нагрузки. Проекты, двойные отрицания и сложные случаи не разрешаются догадкой. Узкие правила не заменяют юридическую экспертизу.

Ссылки evidence разрешаются в точном событии и версии профиля. Аудит теперь дополнительно обнаруживает подмену компании/профиля, статуса FACT/HYPOTHESIS, надёжности, подтверждения provenance, источника/даты, состава supports и значения MLView. Цитата проверяется по исходному тексту. В grounded нельзя выдать гипотезу за FACT/DERIVED. Наличие ссылки не доказывает истинность внешнего источника; произвольные новые текстовые interpretation_constraints автоматически не интерпретируются.

## 8. Ошибки и безопасная деградация

| Ситуация | Поведение |
|---|---|
| Неправильный ID/режим расчёта, повтор столбцов, непригодная структура CSV | ValueError/TypeError/csv.Error/UnicodeError/OSError в зависимости от причины; аудит часть ошибок фиксирует в отчёте |
| Неизвестный GROUP_ID или неоднозначный справочник | Принадлежность остаётся неизвестной, границы расширяются, конфликтующая запись не выбирается |
| Неверный MLView/профиль, расходящиеся версии и D-значения | Ошибка до ранжирования |
| Неизвестный класс фактора/тип события | ValueError, без неявного переименования в известный класс |
| Известный класс, для которого не реализован механизм | none/пустая релевантная выдача, без выдуманной связи |
| Валидный неполный MLView или отсутствие полного профиля | conditional/none в пределах доступных оснований |
| Обязательные поля NewsEvent отсутствуют | TypeError конструктора; единый публичный API-конверт ошибки не задан |
| Не найденная цитата, повтор event_id, будущая дата | Ошибка; произвольная версия не выбирается |
| Повреждённые суммы получателей в переданном объекте признаков | ValueError при несовпадении с внешним знаменателем |
| Спорная/неполная разметка | N/A для всего запроса, JSON null |

Один некорректный кандидат останавливает rank_news. Backend должен отличать ошибку от корректного [], определить карантин данных и внешний формат ошибок. Библиотека не вводит HTTP-коды и не реализует оркестрацию повторов. Ошибки установки/отсутствия схем нельзя скрывать как отсутствие релевантных новостей.

## 9. Минимальный выполняемый пример

Полный пример находится в [examples/ml_library_usage.py](../examples/ml_library_usage.py). Запуск:

```powershell
.\.venv_win\Scripts\python.exe -B -X utf8 -m examples.ml_library_usage
```

Он создаёт временные синтетические CSV, вызывает compute_payment_features и compute_group_features с payment_features, экспортирует семь существующих DerivedMetric, отдельно читает готовые s1_MLView/s1_expected_profile, создаёт NewsEvent из имеющихся фикстур, вызывает rank_news и serialize_ranking, печатает JSON.

Две части явно отделены: признаки рассчитаны для EXAMPLE_A, готовый профиль относится к U_T1. Рассчитанные данные не вставляются в чужой профиль. Это демонстрация API библиотеки при отсутствующем Backend, а не обход его сборщика. Финансовые признаки компаний из закрытых CSV с U_T1 не смешивались.

Краткий шаблон настоящего вызова после получения объектов Backend:

```python
from ml_features import (
    compute_payment_features, compute_group_features,
    serialize_derived_metrics, serialize_group_derived_metrics,
)
from ml_ranking import NewsEvent, rank_news, serialize_ranking

payments = compute_payment_features(
    company_id, company_csv, payment_csv, assume_additive_rows=False
)
groups = compute_group_features(
    company_id, company_csv, payment_features=payments
)
metrics_for_backend = (
    serialize_derived_metrics(payments)
    + serialize_group_derived_metrics(groups)
)
candidates = serialize_ranking(rank_news(
    backend_ml_view,
    [NewsEvent.from_dict(item) for item in normalized_events],
    company_profile=backend_profile, analytic_focus="CFO", as_of=analysis_time,
))
```

Company_id, пути, нормализованные события и analysis_time поступают от вызывающей стороны; backend_ml_view/backend_profile должны быть уже построены Backend из соответствующей версии данных. Этот шаблон не создаёт профиль или проекцию.

## 10. Таксономия и функции вне текущей реализации

FactorClass берётся из неизменённого common.v1.schema.json. Классов 13: macro, monetary_policy, fx, commodity_price, industry_demand, industry_regulation, sanctions_trade, tax_fiscal, labor_market, logistics, counterparty_credit, competition, technology. Поддержка enum не означает обработку всех возможных событий класса. Hardcoded-часть baseline — ограниченный набор соответствий event_type ↔ factor_class ↔ механизм, направленное префиксное сопоставление ОКВЭД и узкие русскоязычные регуляторные конструкции. Код не загружает классификатор ОКВЭД и не определяет реальные рынки компании по отрасли.

Business и Prompt Engineer должны подтвердить каталог factor slug ↔ FactorClass, версии и полноту классификатора ОКВЭД, правила извлечения event_type/entities/quotes и смысл аннотаций supports. Тестовые bce_lab/data/factor_catalog.json и kb_stub.json не являются производственным каталогом; новый конкурирующий каталог не создавался.

Classify_event — проверка релевантности готового NewsEvent для компании, не классификация сырой статьи. Функции «сырой текст → нормализованное событие» и извлечения кандидатов из новостного хранилища не реализованы. Сбор новостей и интеграция LLM по текущему заданию принадлежат Backend/Prompt Engineer. В workspace найден только BCE draft v0.2, более нового технического задания v2.3 нет; подтвердить иное внешнее распределение задач retrieval/classification невозможно. Если оно существует, требуются сам документ, владелец входов и критерии приёмки; эти функции не объявляются выполненными и автоматически не реализуются.

## 11. Backend и внешние зависимости

Обязательные действия Backend:

1. Забрать четыре пакета, requirements-ml.txt и три исходные схемы с согласованной раскладкой; не копировать алгоритмы в свои контроллеры.
2. Вызывать ML-1 для той же company_id и версии источников, явно утверждать допущение аддитивности, сохранять хеши, версию метрик, null, coverage, caveats и breakdown.
3. Реализовать собственные CompanyBusinessProfile assembly и MLView projection, проверить FACT/DERIVED и ссылки, выпустить новый profile_id и input_hash. Не ставить evidence_refs_verified только по результату JSON Schema и не менять исторический снимок.
4. Предоставить нормализованные NewsEvent с реальными source/evidence ID, текстом, датами и точными сущностями. Разрешать технические/неоднозначные платёжные ID до утверждений о конкретной организации.
5. Вызывать rank_news с согласованным полным профилем, ролью и явным aware as_of; сохранять JSON, условия и доказательства. Реализовать persistence, доступ, API, TTL, карантин ошибок и интеграцию с LLM-агентом у себя.
6. После согласования новостного DTO адаптировать результат. Утверждённый EventCandidate в workspace не найден: соответствие ему не заявляется и новый публичный контракт здесь не фиксируется.
7. Перед реальной оценкой получить независимую reviewed/adjudicated разметку Business, зафиксировать профили/события/группы и held-out split. Не использовать выходы ранжировщика как ground truth.

Открыты согласование EventCandidate и нормализованных новостей, пространства ID/версий/цитат, роль публичных ranking_factors, таксономия и классификатор ОКВЭД, режим аддитивности и финансовая область показателей, TTL/часовой пояс/ошибки. Формулы и экспорт breakdown реализованы; принятие их бизнес-смысла и упаковка в профиль остаются согласуемой границей, а не новым контрактом этого документа.

Реальные новости и независимая разметка не предоставлены. Поэтому real-news evaluation, статистическая обобщаемость и финальная бизнес-приёмка ещё невозможны. Синтетические development-пулы не объявляются реальным или held-out качеством модели.

Подтверждённые проблемы текущего ML-review:

| Приоритет | Воспроизведённая проблема | Исправление |
|---|---|---|
| P1 | Экспорт концентраций терял ID и доли получателей, необходимые для проверки конкретной платёжной связи | Существующий breakdown для top1/top5/HHI, правильный внешний знаменатель, неизвестные ID и версия D-ID |
| P1 | audit_ranked_evidence возвращал пустой список проблем после подмены company_id/profile_id/epistemic_status ссылки | Проверка принадлежности, статуса, надёжности, provenance и ссылочных значений; grounded не принимает гипотезу за факт |

Дополнительно проверяются дата публикации, нахождение цитаты в тексте и сохранность supports. Новые подтверждённые P0-дефекты в исследованных сценариях не обнаружены. Финансовые формулы и границы не менялись; повторно проверены сохранение сумм, неизвестные контрагенты, даты, регуляторный hotfix, детерминизм и изоляция оценочных событий. Это не утверждение об отсутствии любых возможных ошибок за пределами проверенных условий.

## 12. Приёмочный чек-лист

Внутренняя ML-приёмка:

- [x] Четыре пакета импортируются и ранжируют без BCE-лаборатории, промптов и API-ключа.
- [x] Публичные функции и прежние импорты сериализаторов совместимы.
- [x] Деньги считаются точно, самоплатежи и нераспределённые суммы отделены; неизвестные получатели остаются в знаменателе.
- [x] Групповые границы и сохранение сумм проверяются; пропуски и конфликты не разрешаются произвольно.
- [x] Top1/top5/HHI содержат схематически допустимый breakdown; идентификаторы и доли сверены с локальными CSV.
- [x] Смена версии экспорта отражена в D-ID и диагностике.
- [x] Аудит обнаруживает подмену принадлежности и epistemic_status ссылок; HYPOTHESIS не повышается до FACT.
- [x] Регуляторный hotfix, даты, CFO/CEO, пустая выдача и независимость оценки покрыты тестами.
- [x] Выполняемый пример повторяем и не собирает профиль вместо Backend.
- [x] Полный финальный прогон: 244 модульных теста, включая 24 новых; исходные контракты 147/147; безопасные локальные самопроверки лаборатории 95/95; pip check без ошибок. Проверки лаборатории и генерации фикстур выполнялись во временной копии, не меняя владение компонентами Prompt Engineer.
- [x] CLI примера возвращает корректный JSON с семью DerivedMetric и четырьмя ranked candidates; повторный вызов функции даёт побайтово одинаковый отсортированный JSON.
- [x] Сравнены SHA-256 172 ранее существовавших файлов под контролем сохранности. Общие схемы, фикстуры, CSV, bce_lab, prompts и прежние отчёты неизменны; изменения ограничены ML-кодом, его документацией и исключениями новых локальных отчётов.

Внешняя приёмка перед реальным запуском:

- [ ] Backend передал один реальный согласованный CompanyBusinessProfile и MLView из тех же источников.
- [ ] Backend/Prompt Engineer согласовали EventCandidate, нормализованные новости и разрешение ID.
- [ ] Business подтвердил таксономию, классификатор, аддитивность/ограничения данных и критерии релевантности.
- [ ] Получены реальные тексты/цитаты и независимая разметка с арбитражем и разбиением по событиям.
- [ ] Backend проверил свою упаковку зависимостей, кэш, TTL, ошибки, доступ и эксплуатационные требования.

Эти внешние пункты не входят в самостоятельную реализацию ML-библиотеки. До их выполнения статус остаётся ML COMPLETE WITH EXTERNAL DEPENDENCIES, не ML COMPLETE для всего реального конвейера.
