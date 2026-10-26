import type { ChatAnswer, Company, EventCandidate, Focus, Insight, Overview } from './types';

const BASE = (import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8080').replace(/\/$/, '');
const DEMO = import.meta.env.VITE_DEMO_MODE === 'true';
const TOKEN_KEY = 'alfa_context_token';

export function getToken(): string {
  try { return sessionStorage.getItem(TOKEN_KEY) || import.meta.env.VITE_API_TOKEN || ''; }
  catch { return import.meta.env.VITE_API_TOKEN || ''; }
}
export function setToken(token: string): void {
  try { if (token) sessionStorage.setItem(TOKEN_KEY, token); else sessionStorage.removeItem(TOKEN_KEY); } catch { /* хранилище недоступно: токен действует до перезагрузки */ }
}

export class ApiError extends Error {
  constructor(readonly status: number, readonly code: string, message: string) { super(message); this.name = 'ApiError'; }
}
export function describeError(error: unknown, fallback: string): string {
  if (!(error instanceof ApiError)) return fallback;
  if (error.status === 401) return 'Нет доступа: токен не задан или недействителен.';
  if (error.status === 404) return 'Запрошенные данные недоступны для вашей учётной записи или не найдены.';
  if (error.status === 429) return error.message || 'Достигнут дневной лимит обращений к модели. Повторите попытку позже.';
  if (error.status === 0) return 'Нет связи с сервером. Проверьте адрес API и подключение.';
  return error.message ? `${fallback} ${error.message}` : fallback;
}

const companies: Company[] = [
  { company_id: 'U_5639', display_name: 'Северсталь-Снабжение' },
  { company_id: 'U_25605', display_name: 'ТехноПром Групп' },
  { company_id: 'U_428697', display_name: 'Вектор Инжиниринг' },
];
const candidates: EventCandidate[] = [
  { event_id: 'evt-001', title: 'Изменение правил расчёта экологического сбора', published_at: '24 окт 2026', source_name: 'Официальный портал правовой информации', source_url: 'https://publication.pravo.gov.ru/', event_type: 'Регулирование', priority: 'high', relevance: 'grounded', reason: 'Компания работает в промышленном сегменте; событие затрагивает затраты при обращении с продукцией и упаковкой.', mechanism: 'Возможное изменение обязательных платежей может повлиять на операционные расходы. Фактический эффект зависит от номенклатуры продукции и применимости нормы.', tags: ['Регулирование', 'Затраты'], source_count: 3, limitations: ['В доступном профиле нет детализации по соответствующим категориям продукции.'] },
  { event_id: 'evt-002', title: 'Крупный контрагент объявил о расширении производственных мощностей', published_at: '23 окт 2026', source_name: 'Деловой обзор', source_url: 'https://www.kommersant.ru/', event_type: 'Рынок', priority: 'medium', relevance: 'conditional', reason: 'В платёжных данных есть существенный поток к контрагенту, связанному с событием; характер деловых отношений не подтверждён.', mechanism: 'Расширение мощностей может менять объёмы закупок или сроки расчётов, если контрагент входит в цепочку поставок компании.', tags: ['Контрагенты', 'Операции'], source_count: 2, limitations: ['Платёжная связь сама по себе не подтверждает поставку, зависимость или обязательство.'] },
  { event_id: 'evt-003', title: 'Банк России опубликовал обзор условий кредитования бизнеса', published_at: '22 окт 2026', source_name: 'Банк России', source_url: 'https://cbr.ru/', event_type: 'Банковская аналитика', priority: 'medium', relevance: 'conditional', reason: 'Материал описывает макроусловия кредитования, но не содержит отдельной оценки компании.', mechanism: 'Изменение стоимости и доступности финансирования может быть значимо при наличии потребности в оборотном капитале или рефинансировании.', tags: ['Финансирование', 'Макро'], source_count: 1, limitations: ['В доступном профиле не подтверждена текущая потребность в новом финансировании.'] },
  { event_id: 'evt-004', title: 'В регионе обсуждают обновление транспортной инфраструктуры', published_at: '21 окт 2026', source_name: 'Региональные новости', source_url: 'https://ria.ru/', event_type: 'Инфраструктура', priority: 'low', relevance: 'conditional', reason: 'Совпадает география, однако прямое влияние на деятельность компании не установлено.', mechanism: 'Изменение логистики может повлиять на сроки и стоимость поставок при использовании соответствующих маршрутов.', tags: ['Логистика'], source_count: 1, limitations: ['Региональное совпадение недостаточно для подтверждения влияния.'] },
  { event_id: 'evt-005', title: 'Отраслевой обзор: спрос на промышленное оборудование', published_at: '20 окт 2026', source_name: 'Отраслевой бюллетень', source_url: 'https://www.rbc.ru/', event_type: 'Отраслевой обзор', priority: 'low', relevance: 'conditional', reason: 'Материал может быть полезен для контекста отрасли, но не содержит конкретных фактов о компании.', mechanism: 'Динамика спроса может влиять на загрузку и переговорную позицию участников рынка.', tags: ['Отрасль', 'Спрос'], source_count: 2, limitations: ['Вывод носит общий характер и требует сопоставления с данными компании.'] },
];

const sourceNews = (id: string, title: string, publisher: string, date: string, url: string, kind: 'news' | 'bank' | 'registry' = 'news') => ({ source_id: id, title, publisher, published_at: date, url, kind });
const demoInsight = (candidate: EventCandidate, companyId: string, focus: Focus): Insight => ({
  insight_id: `ins-${candidate.event_id}-${companyId}`, insight_version: 1, company_id: companyId, analytic_focus: focus,
  status: candidate.event_id === 'evt-001' ? 'ready' : 'limited', generated_at: new Date().toISOString(), title: candidate.title,
  conclusion: candidate.event_id === 'evt-001'
    ? 'Изменение правил экологического сбора может увеличить обязательные расходы компании, если её продукция попадает в регулируемые категории.'
    : 'Событие может иметь значение для компании, но доступных данных недостаточно, чтобы подтвердить прямой финансовый эффект.',
  event: `${candidate.title}. Источник опубликован ${candidate.published_at}. В текущем наборе доступно ${candidate.source_count} материала(ов), связанных с событием.`,
  mechanism: candidate.mechanism,
  bank_position: 'В доступном демонстрационном наборе не найдено подтверждённой банковской позиции, адресованной именно этой компании. Общие материалы о рынке не следует трактовать как позицию банка по клиенту.',
  bank_retrieval_status: 'completed', bank_analytics_mode: 'no_relevant_report',
  risks: candidate.event_id === 'evt-001' ? ['Возможный рост операционных расходов при применимости новых правил.', 'Риск ошибочной оценки бюджета без проверки товарных категорий и даты вступления норм в силу.'] : ['Возможное изменение затрат или условий работы — при подтверждении связи с деятельностью компании.'],
  opportunities: focus === 'CFO' ? ['Проверить применимость нормы к номенклатуре и уточнить бюджетный сценарий.', 'Сопоставить потенциальные затраты с платёжным календарём после подтверждения исходных условий.'] : ['Проверить, влияет ли событие на ключевые операционные процессы и контрагентов.', 'Назначить владельца проверки после подтверждения прямой связи с бизнесом.'],
  next_steps: ['Проверить первоисточник и дату вступления изменений в силу.', 'Сопоставить событие с актуальным профилем компании и соответствующими статьями затрат.', 'Зафиксировать вывод только после подтверждения применимости.'],
  unknowns_and_conditions: candidate.limitations || ['Не подтверждены конкретные договоры и обязательства, на которые может повлиять событие.', 'Вывод зависит от полноты и актуальности данных профиля.'],
  sources: [sourceNews('src-1', candidate.title, candidate.source_name, candidate.published_at, candidate.source_url), sourceNews('src-2', 'Официальные материалы и разъяснения по теме', 'Официальный источник', '24 окт 2026', 'https://publication.pravo.gov.ru/', 'registry')],
  evidence: [
    { evidence_id: 'ev-01', claim: 'Событие опубликовано в указанную дату.', source_id: 'src-1', quote: `${candidate.title} — ${candidate.published_at}.` },
    { evidence_id: 'ev-02', claim: 'Возможный механизм влияния является условным.', source_id: 'src-2', quote: 'Применимость к конкретной компании должна быть подтверждена по первоисточнику и данным профиля.' },
  ],
  limitations: ['Данные в демонстрационном режиме иллюстрируют поведение интерфейса и не являются реальной аналитикой клиента.', 'Платёжная связь не доказывает поставку, операционную зависимость или кредитное обязательство.'],
});

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json', ...(init?.body ? { 'Content-Type': 'application/json' } : {}) };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let response: Response;
  try { response = await fetch(`${BASE}${path}`, { ...init, headers: { ...headers, ...(init?.headers as Record<string, string> | undefined) } }); }
  catch { throw new ApiError(0, 'network_error', 'Сервер недоступен.'); }
  const contentType = response.headers.get('content-type') || '';
  const payload = contentType.includes('application/json') ? await response.json().catch(() => null) : await response.text().catch(() => '');
  if (!response.ok) {
    const body = payload && typeof payload === 'object' ? payload as { code?: unknown; message?: unknown; detail?: unknown } : {};
    const message = typeof body.message === 'string' ? body.message : typeof body.detail === 'string' ? body.detail : '';
    throw new ApiError(response.status, typeof body.code === 'string' ? body.code : 'http_error', message || `Ошибка API: ${response.status}`);
  }
  if (response.status === 204 || payload === '') return undefined as T;
  if (payload === null) throw new ApiError(response.status, 'invalid_json', 'Сервер вернул некорректный JSON-ответ.');
  return payload as T;
}
const formatDate = (value: string): string => {
  const parsed = /^\d{4}-\d{2}-\d{2}/.test(value) ? new Date(value) : null;
  return parsed && !Number.isNaN(parsed.getTime()) ? parsed.toLocaleDateString('ru-RU', { day: '2-digit', month: 'short', year: 'numeric' }) : value;
};
const currentDemoTimestamp = () => new Date().toLocaleString('ru-RU', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });

type JsonObject = Record<string, unknown>;
const asObject = (value: unknown): JsonObject => value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {};
const asText = (value: unknown, fallback = ''): string => typeof value === 'string' ? value : typeof value === 'number' ? String(value) : fallback;
const claimText = (value: unknown): string => {
  if (typeof value === 'string') return value;
  const claim = asObject(value);
  return asText(claim.text, asText(claim.summary));
};
const stringList = (value: unknown): string[] => Array.isArray(value) ? value.map(item => claimText(item)).filter(Boolean) : [];
const mapSource = (value: unknown, index: number): Insight['sources'][number] => {
  const s = asObject(value);
  const sourceId = asText(s.source_id, asText(s.id, `source-${index + 1}`));
  const type = asText(s.kind, asText(s.type, asText(s.source_type, 'news')));
  const kind = type === 'bank' || type === 'bank_report' ? 'bank' : type === 'registry' ? 'registry' : 'news';
  return {
    source_id: sourceId, title: asText(s.title, asText(s.name, 'Источник')),
    publisher: asText(s.publisher, asText(s.source_name, sourceId)),
    published_at: formatDate(asText(s.published_at, asText(s.date, 'Дата не указана'))),
    url: asText(s.url, asText(s.source_url)), kind,
  };
};
function normalizeOverview(value: unknown, companyId: string, focus: Focus): Overview {
  const raw = asObject(value);
  const rawItems = Array.isArray(raw.candidates) ? raw.candidates : Array.isArray(raw.items) ? raw.items : [];
  const mapped = rawItems.map((item, index): EventCandidate => {
    const e = asObject(item);
    const sources = Array.isArray(e.sources) ? e.sources.map(mapSource) : [];
    const relevance = asText(e.relevance, 'conditional');
    const priority = asText(e.priority, 'low');
    return {
      event_id: asText(e.event_id, `event-${index + 1}`), title: asText(e.title, 'Событие без названия'),
      published_at: formatDate(asText(e.published_at, 'Дата не указана')), source_name: asText(e.source_name, sources[0]?.publisher || 'Источник'),
      source_url: asText(e.source_url, sources[0]?.url || ''), event_type: asText(e.event_type, asText(e.type, 'Событие')),
      priority: priority === 'high' || priority === 'medium' ? priority : 'low',
      relevance: relevance === 'grounded' || relevance === 'none' ? relevance : 'conditional',
      reason: claimText(e.reason ?? e.relevance_reason ?? e.priority_reason) || 'Причина релевантности не указана.',
      mechanism: claimText(e.mechanism ?? e.impact_mechanism),
      tags: stringList(e.tags ?? e.factor_classes), source_count: typeof e.source_count === 'number' ? e.source_count : sources.length || (Array.isArray(e.source_ids) ? e.source_ids.length : 0),
      limitations: stringList(e.limitations ?? e.conditions),
    };
  }).filter(item => item.relevance !== 'none').slice(0, 5);
  const status = asText(raw.state, asText(raw.status, mapped.length ? 'ready' : 'empty'));
  return { company_id: asText(raw.company_id, companyId), analytic_focus: asText(raw.analytic_focus, focus) === 'CEO' ? 'CEO' : 'CFO', status: status === 'not_prepared' || status === 'empty' ? status : 'ready', updated_at: formatDate(asText(raw.updated_at, asText(raw.generated_at, 'Дата не указана'))), candidates: mapped };
}
function normalizeInsight(value: unknown, fallbackCompanyId: string, fallbackFocus: Focus): Insight {
  const raw = asObject(value);
  const ai = asObject(raw.ai_interpretation);
  const event = asObject(raw.event);
  const bank = asObject(raw.bank_position);
  const rawSources = Array.isArray(raw.sources) ? raw.sources : [];
  const sources = rawSources.map(mapSource);
  const rawEvidence = Array.isArray(raw.evidence) ? raw.evidence : [];
  const evidence: Insight['evidence'] = rawEvidence.map((item, index) => {
    const e = asObject(item);
    const sourceType = asText(e.source_type);
    const profileInputs = (Array.isArray(asObject(e.raw).inputs) ? asObject(e.raw).inputs as unknown[] : [])
      .map(input => { const field = asObject(input); return field.ref ? `${asText(field.ref)} = ${asText(field.value, 'не указано')}` : ''; })
      .filter(Boolean).join('; ');
    const fallbackClaim = sourceType === 'bank_report' ? 'Фрагмент банковского отчёта' : sourceType === 'profile' ? 'Признак профиля компании' : sourceType === 'news' ? 'Цитата из новости' : 'Основание';
    return {
      evidence_id: asText(e.evidence_id, asText(e.id, `evidence-${index + 1}`)),
      claim: claimText(e.claim ?? e.text) || fallbackClaim,
      source_id: asText(e.source_id, Array.isArray(e.source_ids) ? asText(e.source_ids[0]) : ''),
      quote: sourceType === 'profile' && profileInputs ? profileInputs : asText(e.quote, asText(e.observation, asText(e.excerpt, ''))),
    };
  });
  const claims = (v: unknown) => stringList(v);
  const summary = claimText(ai.summary ?? raw.conclusion);
  const counterparty = asObject(raw.counterparty_impact);
  const rawStatus = asText(raw.status, 'limited');
  const status = rawStatus === 'ready' || rawStatus === 'unavailable' ? rawStatus : 'limited';
  const retrieval = asText(raw.bank_retrieval_status);
  const bankText = typeof raw.bank_position === 'string' ? raw.bank_position
    : claimText(bank.text ?? bank.summary ?? bank.position)
      || (retrieval === 'unavailable' ? 'Корпус банковской аналитики недоступен: позиция банка не оценивалась.'
        : retrieval === 'error' ? 'Поиск по банковской аналитике завершился ошибкой.'
        : asText(bank.reason) || 'Подходящий проверенный материал банка не найден.');
  return {
    insight_id: asText(raw.insight_id, 'insight-unknown'), insight_version: typeof raw.insight_version === 'number' ? raw.insight_version : 1,
    company_id: asText(raw.company_id, fallbackCompanyId), analytic_focus: asText(raw.analytic_focus, fallbackFocus) === 'CEO' ? 'CEO' : 'CFO', status,
    generated_at: asText(raw.generated_at, new Date().toISOString()), title: asText(raw.title, claimText(event.title) || 'Аналитический разбор'),
    conclusion: summary || (status === 'unavailable' ? 'Недостаточно данных для содержательного вывода.' : 'Вывод не предоставлен.'),
    event: typeof raw.event === 'string' ? raw.event : claimText(event.summary ?? event.description ?? event.title) || 'Описание события не предоставлено.',
    mechanism: claimText(counterparty) || claimText(ai.mechanism) || 'Механизм влияния требует дополнительной проверки.',
    bank_position: bankText,
    bank_retrieval_status: ['completed', 'unavailable', 'error'].includes(asText(raw.bank_retrieval_status)) ? asText(raw.bank_retrieval_status) as Insight['bank_retrieval_status'] : null,
    bank_analytics_mode: ['direct_match', 'macro_background', 'no_relevant_report'].includes(asText(raw.bank_analytics_mode)) ? asText(raw.bank_analytics_mode) as Insight['bank_analytics_mode'] : null,
    risks: claims(ai.risks ?? raw.risks), opportunities: claims(ai.opportunities ?? raw.opportunities),
    next_steps: claims(ai.next_steps ?? raw.next_steps), unknowns_and_conditions: stringList(raw.unknowns_and_conditions),
    sources, evidence, limitations: Array.isArray(raw.limitations) ? raw.limitations.map(item => { const l = asObject(item); return asText(l.message, claimText(item)); }).filter(Boolean) : stringList(raw.limitations),
  };
}
export const api = {
  isDemo: DEMO,
  async companies(): Promise<Company[]> {
    if (DEMO) return companies;
    const raw = asObject(await request<unknown>('/companies'));
    return (Array.isArray(raw.items) ? raw.items : []).map(item => {
      const c = asObject(item);
      const id = asText(c.company_id, asText(c.id));
      return { company_id: id, display_name: asText(c.display_name, id) };
    }).filter(c => c.company_id);
  },
  async overview(companyId: string, focus: Focus): Promise<Overview> {
    if (!DEMO) return normalizeOverview(await request<unknown>(`/companies/${encodeURIComponent(companyId)}/overview?analytic_focus=${focus}`), companyId, focus);
    await new Promise(r => setTimeout(r, 260));
    return { company_id: companyId, analytic_focus: focus, status: 'ready', updated_at: currentDemoTimestamp(), candidates: candidates.filter(c => c.relevance !== 'none').slice(0, 5) };
  },
  async refreshOverview(companyId: string, focus: Focus): Promise<Overview> {
    if (!DEMO) return normalizeOverview(await request<unknown>(`/companies/${encodeURIComponent(companyId)}/overview/refresh`, { method: 'POST', body: JSON.stringify({ analytic_focus: focus }) }), companyId, focus);
    await new Promise(r => setTimeout(r, 900));
    return { company_id: companyId, analytic_focus: focus, status: 'ready', updated_at: `Обновлено ${currentDemoTimestamp()}`, candidates: candidates.filter(c => c.relevance !== 'none').slice(0, 5) };
  },
  async insight(companyId: string, focus: Focus, candidate: EventCandidate): Promise<Insight> {
    if (!DEMO) return normalizeInsight(await request<unknown>(`/companies/${encodeURIComponent(companyId)}/insights`, { method: 'POST', body: JSON.stringify({ analytic_focus: focus, event_id: candidate.event_id }) }), companyId, focus);
    await new Promise(r => setTimeout(r, 650));
    return demoInsight(candidate, companyId, focus);
  },
  async getInsight(insightId: string): Promise<Insight> { return normalizeInsight(await request<unknown>(`/insights/${encodeURIComponent(insightId)}`), '', 'CFO'); },
  async question(insight: Insight, question: string, conversationId: string | null): Promise<ChatAnswer> {
    if (!DEMO) {
      const raw = asObject(await request<unknown>(`/insights/${encodeURIComponent(insight.insight_id)}/questions`, { method: 'POST', body: JSON.stringify({ question, conversation_id: conversationId }) }));
      return { conversation_id: asText(raw.conversation_id, conversationId || `conv-${insight.insight_id}`), answer: claimText(raw.answer) || 'Ответ не предоставлен.', sources: (Array.isArray(raw.sources) ? raw.sources : []).map(mapSource), unknowns_and_conditions: stringList(raw.unknowns_and_conditions) };
    }
    await new Promise(r => setTimeout(r, 700));
    return { conversation_id: conversationId || `conv-${insight.insight_id}`, answer: 'По имеющимся материалам можно сформулировать только условный вывод. Сначала нужно проверить применимость события к конкретным операциям компании и подтвердить даты. Сейчас нет достаточных оснований утверждать, что финансовый эффект уже возник.', sources: insight.sources.slice(0, 1), unknowns_and_conditions: insight.unknowns_and_conditions.slice(0, 2) };
  },
  async feedback(insight: Insight, rating: 'useful' | 'not_useful', reason: string, comment: string): Promise<void> {
    if (!DEMO) { await request(`/insights/${encodeURIComponent(insight.insight_id)}/feedback`, { method: 'PUT', body: JSON.stringify({ rating, reason: reason || (rating === 'useful' ? 'relevant' : 'other'), comment: comment || null }) }); return; }
    await new Promise(r => setTimeout(r, 350));
  },
};
