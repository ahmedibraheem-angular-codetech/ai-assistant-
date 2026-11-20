import { ago, kpiCards, seconds, mapNodes, MAX_AGENTS_ON_MAP, queueRows, toolBars, trendLabels, trendSeries } from './dashboard-view';
import { NOW, agent, summary } from './summary.testing';

describe('kpiCards', () => {
  it('shows real values, change against the previous period, and ring fill', () => {
    const cards = Object.fromEntries(kpiCards(summary(), NOW).map((card) => [card.id, card]));
    expect(cards['turns'].value).toBe('12');
    expect(cards['turns'].note).toBe('▲ 50% عن الأسبوع السابق');
    expect(cards['turns'].ring).toBe(1);
    expect(cards['conversations'].note).toBe('مثل الأسبوع السابق');
    expect(cards['success'].value).toBe('75%');
    expect(cards['success'].note).toBe('3 تمت · 1 فشلت');
    expect(cards['approvals'].note).toBe('أقدمها منذ 14 د');
    expect(cards['budget'].value).toBe('25%');
    expect(cards['lessons'].note).toBe('1 بانتظار مراجعتك');
  });

  it('says what is missing instead of inventing numbers', () => {
    const empty = summary();
    empty.kpis = {
      chat_turns: { value: 0, previous: 0 }, conversations: { value: 0, previous: 0 },
      tool_actions: { value: 0, completed: 0, failed: 0, success_rate: null },
      approvals_pending: { value: 0, oldest_at: null }, lessons_new: { value: 0, pending: 0 },
      budget: { tool_calls_used: 0, tool_calls_limit: 0, chat_turns_used: 0, chat_turns_limit: 0 },
    };
    const cards = Object.fromEntries(kpiCards(empty, NOW).map((card) => [card.id, card]));
    expect(cards['success'].value).toBe('—');
    expect(cards['success'].ring).toBe(0);
    expect(cards['budget'].value).toBe('بلا حد');
    expect(cards['approvals'].note).toBe('لا شيء بانتظارك');
    expect(cards['turns'].note).toBe('لا نشاط بعد');
  });

  it('shows a drop in warning colour', () => {
    const fewer = summary({ kpis: { ...summary().kpis, chat_turns: { value: 5, previous: 10 } } });
    const turns = kpiCards(fewer, NOW)[0];
    expect(turns.note).toBe('▼ 50% عن الأسبوع السابق');
    expect(turns.noteTone).toBe('warn');
  });
});

describe('mapNodes', () => {
  it('places every agent and system on a circle around the centre', () => {
    const { nodes } = mapNodes(summary(), NOW);
    expect(nodes.map((n) => n.label)).toEqual(['مدير الفريق', 'الباحث', 'الموافقات', 'الذاكرة', 'المعرفة', 'المشاريع', 'وظّف وكيلاً']);
    for (const node of nodes) {
      expect(Math.hypot(node.x - 50, node.y - 50)).toBeCloseTo(38, 5);
    }
    expect(nodes[0].stat).toBe('يعمل الآن');
    expect(nodes[1].stat).toBe('4 رد');
    expect(nodes.find((n) => n.id === 'approvals')?.kind).toBe('alert');
  });

  it('caps the agents shown and reports the rest', () => {
    const many = summary({ agents: Array.from({ length: 11 }, (_, i) => agent(`وكيل ${i}`)) });
    const { nodes, hiddenAgents } = mapNodes(many, NOW);
    expect(nodes.filter((n) => n.kind === 'agent')).toHaveLength(MAX_AGENTS_ON_MAP);
    expect(hiddenAgents).toBe(3);
  });

  it('works with no agents at all', () => {
    const { nodes } = mapNodes(summary({ agents: [] }), NOW);
    expect(nodes[0].label).toBe('الموافقات');
  });
});

describe('queue, trend and tools', () => {
  it('names statuses in Arabic and falls back to the tool name', () => {
    const [pending, done] = queueRows(summary().queue, NOW);
    expect(pending).toMatchObject({ title: 'تحليل ملف', status: 'بانتظار موافقة', tone: 'warn', when: 'منذ 2 د', initial: 'ا' });
    expect(done).toMatchObject({ title: 'write_file', who: 'المساعد · write_file', status: 'تمت', tone: 'ok' });
  });

  it('labels days or hours and builds three chart series', () => {
    const s = summary();
    expect(trendLabels(s)).toHaveLength(7);
    const lines = trendSeries(s);
    expect(lines.map((line) => line.name)).toEqual(['ردود المساعد', 'إجراءات تمت', 'فشلت']);
    expect(lines[0].data).toEqual(s.trend.chat_turns);
    expect(lines[0].color).toBe('var(--accent)');
  });

  it('scales tool bars to the busiest tool and counts denials', () => {
    expect(toolBars(summary())).toEqual([
      { tool: 'run_code', total: '3', width: 100, failed: 1, time: '' },
      { tool: 'write_file', total: '2', width: 67, failed: 1, time: '' },
    ]);
  });

  it('shows average tool time when the server sends it', () => {
    const s = summary();
    s.kpis.tool_actions.avg_duration_ms = 420;
    s.tools[0].avg_duration_ms = 2600;
    expect(kpiCards(s, NOW)[1].note).toBe('3 تمت · 1 فشلت · متوسط 0.4ث');
    expect(toolBars(s)[0].time).toBe('2.6ث');
    expect(seconds(75_000)).toBe('1.3د');
  });

  it('formats time since', () => {
    expect(ago(null, NOW)).toBe('—');
    expect(ago('2026-11-21T09:59:30Z', NOW)).toBe('الآن');
    expect(ago('2026-11-21T07:00:00Z', NOW)).toBe('منذ 3 س');
    expect(ago('2026-11-18T10:00:00Z', NOW)).toBe('منذ 3 يوم');
  });
});
