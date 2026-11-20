// Turns the analytics summary into what the dashboard draws. Pure functions:
// no Angular, no DOM, so every rule here is unit-tested and a new widget is
// one more builder.

import { AnalyticsRange, AnalyticsSummary, QueueItem } from '../core/models';
import { LineSeries } from '../shared/line-chart';

export type Tone = 'accent' | 'teal' | 'violet' | 'warn' | 'ok' | 'danger' | 'muted';

export interface KpiCard {
  id: string;
  label: string;
  value: string;
  /** 0..1, how full the ring is. */
  ring: number;
  ringText: string;
  note: string;
  tone: Tone;
  noteTone: Tone;
}

export interface MapNode {
  id: string;
  label: string;
  stat: string;
  kind: 'agent' | 'system' | 'alert' | 'new';
  active: boolean;
  /** Percent of the map box, centre at 50/50. */
  x: number;
  y: number;
  facts: { label: string; value: string }[];
}

export interface QueueRow {
  id: string;
  title: string;
  who: string;
  when: string;
  status: string;
  tone: Tone;
  initial: string;
}

export const RANGES: { id: AnalyticsRange; label: string }[] = [
  { id: 'today', label: 'اليوم' },
  { id: '7d', label: '7 أيام' },
  { id: '30d', label: '30 يوم' },
];

const PERIOD: Record<AnalyticsRange, string> = { today: 'أمس', '7d': 'الأسبوع السابق', '30d': 'الشهر السابق' };
export const MAX_AGENTS_ON_MAP = 8;

const number = new Intl.NumberFormat('ar-EG-u-nu-latn');
export const fmt = (value: number): string => number.format(value);

/** 420 -> "0.4ث", 2600 -> "2.6ث", 75000 -> "1.3د". */
export function seconds(ms: number): string {
  if (ms >= 60_000) return `${(ms / 60_000).toFixed(1)}د`;
  return `${(ms / 1000).toFixed(1)}ث`;
}

export function ago(iso: string | null, now: Date = new Date()): string {
  if (!iso) return '—';
  const seconds = Math.max(0, (now.getTime() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return 'الآن';
  if (seconds < 3600) return `منذ ${Math.floor(seconds / 60)} د`;
  if (seconds < 86400) return `منذ ${Math.floor(seconds / 3600)} س`;
  return `منذ ${Math.floor(seconds / 86400)} يوم`;
}

function change(value: number, previous: number, range: AnalyticsRange): { text: string; tone: Tone } {
  if (previous === 0) return { text: value ? `جديد مقارنة بـ${PERIOD[range]}` : 'لا نشاط بعد', tone: 'muted' };
  const percent = Math.round(((value - previous) / previous) * 100);
  if (percent === 0) return { text: `مثل ${PERIOD[range]}`, tone: 'muted' };
  return { text: `${percent > 0 ? '▲' : '▼'} ${Math.abs(percent)}% عن ${PERIOD[range]}`, tone: percent > 0 ? 'ok' : 'warn' };
}

export function kpiCards(s: AnalyticsSummary, now: Date = new Date()): KpiCard[] {
  const k = s.kpis;
  const turns = change(k.chat_turns.value, k.chat_turns.previous, s.range);
  const convs = change(k.conversations.value, k.conversations.previous, s.range);
  const rate = k.tool_actions.success_rate;
  const limit = k.budget.tool_calls_limit;
  const growth = (c: { value: number; previous: number }) => (c.value === 0 ? 0 : c.value / Math.max(c.value, c.previous));
  return [
    {
      id: 'turns', label: 'ردود المساعد', value: fmt(k.chat_turns.value), ring: growth(k.chat_turns),
      ringText: fmt(k.chat_turns.value), note: turns.text, tone: 'accent', noteTone: turns.tone,
    },
    {
      id: 'success', label: 'نجاح الأدوات', value: rate === null ? '—' : `${Math.round(rate * 100)}%`, ring: rate ?? 0,
      ringText: rate === null ? '—' : `${Math.round(rate * 100)}%`,
      note: k.tool_actions.value
        ? `${fmt(k.tool_actions.completed)} تمت · ${fmt(k.tool_actions.failed)} فشلت${k.tool_actions.avg_duration_ms ? ` · متوسط ${seconds(k.tool_actions.avg_duration_ms)}` : ''}`
        : 'لا إجراءات بعد',
      tone: 'teal', noteTone: k.tool_actions.failed ? 'warn' : 'muted',
    },
    {
      id: 'approvals', label: 'موافقات معلقة', value: fmt(k.approvals_pending.value), ring: k.approvals_pending.value ? 1 : 0,
      ringText: fmt(k.approvals_pending.value),
      note: k.approvals_pending.oldest_at ? `أقدمها ${ago(k.approvals_pending.oldest_at, now)}` : 'لا شيء بانتظارك',
      tone: 'warn', noteTone: k.approvals_pending.value ? 'warn' : 'ok',
    },
    {
      id: 'budget', label: 'ميزانية الأدوات اليوم', value: limit ? `${Math.round((k.budget.tool_calls_used / limit) * 100)}%` : 'بلا حد',
      ring: limit ? Math.min(1, k.budget.tool_calls_used / limit) : 0, ringText: limit ? fmt(k.budget.tool_calls_used) : '∞',
      note: limit ? `${fmt(k.budget.tool_calls_used)} من ${fmt(limit)} استدعاء` : 'لم تُحدد حدود يومية',
      tone: 'teal', noteTone: 'muted',
    },
    {
      id: 'lessons', label: 'دروس جديدة', value: fmt(k.lessons_new.value),
      ring: k.lessons_new.value ? k.lessons_new.pending / k.lessons_new.value : 0, ringText: fmt(k.lessons_new.pending),
      note: k.lessons_new.pending ? `${fmt(k.lessons_new.pending)} بانتظار مراجعتك` : 'لا شيء للمراجعة',
      tone: 'violet', noteTone: k.lessons_new.pending ? 'warn' : 'muted',
    },
    {
      id: 'conversations', label: 'محادثات جديدة', value: fmt(k.conversations.value), ring: growth(k.conversations),
      ringText: fmt(k.conversations.value), note: convs.text, tone: 'accent', noteTone: convs.tone,
    },
  ];
}

/** Agents and systems on a circle around the assistant. Works for any count. */
export function mapNodes(s: AnalyticsSummary, now: Date = new Date()): { nodes: MapNode[]; hiddenAgents: number } {
  const shown = s.agents.slice(0, MAX_AGENTS_ON_MAP);
  const recent = (iso: string | null) => !!iso && now.getTime() - new Date(iso).getTime() < 15 * 60 * 1000;
  const items: Omit<MapNode, 'x' | 'y'>[] = [
    ...shown.map((a) => ({
      id: `agent:${a.id}`, label: a.name, kind: 'agent' as const, active: recent(a.last_active_at),
      stat: !a.enabled ? 'متوقف' : recent(a.last_active_at) ? 'يعمل الآن' : `${fmt(a.chat_turns)} رد`,
      facts: [
        { label: 'ردود', value: fmt(a.chat_turns) },
        { label: 'إجراءات', value: fmt(a.tool_actions) },
        { label: 'مهارات نشطة', value: fmt(a.skills_active) },
        { label: 'دروس معلقة', value: fmt(a.lessons_pending) },
        { label: 'آخر نشاط', value: ago(a.last_active_at, now) },
      ],
    })),
    {
      id: 'approvals', label: 'الموافقات', kind: s.kpis.approvals_pending.value ? 'alert' : 'system',
      active: s.kpis.approvals_pending.value > 0, stat: `${fmt(s.kpis.approvals_pending.value)} معلقة`,
      facts: [
        { label: 'معلقة', value: fmt(s.kpis.approvals_pending.value) },
        { label: 'إجراءات تمت', value: fmt(s.kpis.tool_actions.completed) },
        { label: 'رُفضت بالسياسة', value: fmt(s.systems.tools.denied) },
      ],
    },
    {
      id: 'memory', label: 'الذاكرة', kind: 'system', active: s.systems.memories.added > 0,
      stat: `${fmt(s.systems.memories.count)} عنصر`,
      facts: [
        { label: 'عناصر', value: fmt(s.systems.memories.count) },
        { label: 'أُضيف في الفترة', value: fmt(s.systems.memories.added) },
      ],
    },
    {
      id: 'knowledge', label: 'المعرفة', kind: 'system', active: false, stat: `${fmt(s.systems.knowledge.documents)} ملف`,
      facts: [
        { label: 'ملفات', value: fmt(s.systems.knowledge.documents) },
        { label: 'مقاطع', value: fmt(s.systems.knowledge.chunks) },
        { label: 'عُقد الخريطة', value: fmt(s.systems.graph.entities) },
        { label: 'علاقات', value: fmt(s.systems.graph.relations) },
      ],
    },
    {
      id: 'projects', label: 'المشاريع', kind: 'system', active: false, stat: `${fmt(s.systems.projects.count)} مشروع`,
      facts: [
        { label: 'مشاريع', value: fmt(s.systems.projects.count) },
        { label: 'مدخلات', value: fmt(s.systems.projects.entries) },
      ],
    },
    {
      id: 'hire', label: 'وظّف وكيلاً', kind: 'new', active: false, stat: 'من القوالب',
      facts: [{ label: 'الفريق الحالي', value: `${fmt(s.agents.length)} وكيل` }],
    },
  ];
  const count = items.length;
  const nodes = items.map((item, index) => {
    const angle = ((-90 + (360 / count) * index) * Math.PI) / 180;
    return { ...item, x: 50 + 38 * Math.cos(angle), y: 50 + 38 * Math.sin(angle) };
  });
  return { nodes, hiddenAgents: Math.max(0, s.agents.length - MAX_AGENTS_ON_MAP) };
}

const STATUS: Record<string, { label: string; tone: Tone }> = {
  PENDING: { label: 'بانتظار موافقة', tone: 'warn' },
  APPROVED: { label: 'وُفق عليها', tone: 'accent' },
  EXECUTING: { label: 'قيد التنفيذ', tone: 'accent' },
  COMPLETED: { label: 'تمت', tone: 'ok' },
  FAILED: { label: 'فشلت', tone: 'danger' },
  REJECTED: { label: 'مرفوضة', tone: 'muted' },
  EXPIRED: { label: 'انتهت مهلتها', tone: 'muted' },
  CANCELLED: { label: 'أُلغيت', tone: 'muted' },
};

export function queueRows(items: QueueItem[], now: Date = new Date()): QueueRow[] {
  return items.map((item) => {
    const status = STATUS[item.status] ?? { label: item.status, tone: 'muted' as Tone };
    const who = item.agent_name ?? 'المساعد';
    return {
      id: item.id, title: item.description || item.tool, who: `${who} · ${item.tool}`, when: ago(item.created_at, now),
      status: status.label, tone: status.tone, initial: who.slice(0, 1),
    };
  });
}

export function trendLabels(s: AnalyticsSummary): string[] {
  return s.trend.starts.map((iso) => {
    const date = new Date(iso);
    return s.trend.step === 'hour'
      ? `${String(date.getHours()).padStart(2, '0')}:00`
      : `${date.getDate()}/${date.getMonth() + 1}`;
  });
}

/** The activity chart's lines. Colours are CSS tokens, so themes apply. */
export function trendSeries(s: AnalyticsSummary): LineSeries[] {
  return [
    { name: 'ردود المساعد', color: 'var(--accent)', data: s.trend.chat_turns, area: true },
    { name: 'إجراءات تمت', color: 'var(--teal)', data: s.trend.tool_completed },
    { name: 'فشلت', color: 'var(--danger)', data: s.trend.tool_failed, dashed: true },
  ];
}

export function hasTrendData(s: AnalyticsSummary): boolean {
  return [...s.trend.chat_turns, ...s.trend.tool_completed, ...s.trend.tool_failed].some((v) => v > 0);
}

export function toolBars(s: AnalyticsSummary): { tool: string; total: string; width: number; failed: number; time: string }[] {
  const totals = s.tools.map((t) => t.actions + t.denied);
  const top = Math.max(1, ...totals);
  return s.tools.map((t, i) => ({
    tool: t.tool, total: fmt(totals[i]), width: Math.round((totals[i] / top) * 100), failed: t.failed + t.denied,
    time: t.avg_duration_ms ? seconds(t.avg_duration_ms) : '',
  }));
}
