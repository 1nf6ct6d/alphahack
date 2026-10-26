export type Focus = 'CFO' | 'CEO';
export type InsightStatus = 'ready' | 'limited' | 'unavailable';
export type OverviewStatus = 'ready' | 'empty' | 'not_prepared';

export interface Company { company_id: string; display_name: string; }
export interface Source { source_id: string; title: string; publisher: string; published_at: string; url: string; kind: 'news' | 'bank' | 'registry'; }
export interface EventCandidate {
  event_id: string; title: string; published_at: string; source_name: string; source_url: string;
  event_type: string; priority: 'high' | 'medium' | 'low'; relevance: 'grounded' | 'conditional' | 'none';
  reason: string; mechanism: string; tags: string[]; source_count: number; limitations?: string[];
}
export interface Overview { company_id: string; analytic_focus: Focus; status: OverviewStatus; updated_at: string; candidates: EventCandidate[]; }
export interface Insight {
  insight_id: string; insight_version: number; company_id: string; analytic_focus: Focus; status: InsightStatus;
  generated_at: string; title: string; conclusion: string; event: string; mechanism: string; bank_position: string;
  bank_retrieval_status?: 'completed' | 'unavailable' | 'error' | null;
  bank_analytics_mode?: 'direct_match' | 'macro_background' | 'no_relevant_report' | null;
  risks: string[]; opportunities: string[]; next_steps: string[]; unknowns_and_conditions: string[];
  sources: Source[]; evidence: { evidence_id: string; claim: string; source_id: string; quote: string; }[];
  limitations: string[];
}
export interface ChatAnswer { conversation_id: string; answer: string; sources: Source[]; unknowns_and_conditions: string[]; }
