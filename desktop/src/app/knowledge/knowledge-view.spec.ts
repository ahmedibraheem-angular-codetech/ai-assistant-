import { GraphEntity, GraphRelation } from '../core/models';
import { MAX_NEIGHBOURS, authorLabel, groupEntities, neighbourhood, typeLabel } from './knowledge-view';

const entity = (id: string, name: string, type: GraphEntity['type'], description = ''): GraphEntity =>
  ({ id, name, type, description, author: 'user', agent_id: null, updated_at: '' });

const rel = (id: string, subject: string, predicate: string, object: string): GraphRelation =>
  ({ id, subject, subject_type: 'person', predicate, object, object_type: 'organization', note: '', author: 'user' });

describe('knowledge rules', () => {
  it('groups entities by type in a fixed order and searches name and description', () => {
    const list = [entity('1', 'أحمد', 'person'), entity('2', 'الشركة', 'organization', 'شركة برمجيات'), entity('3', 'منى', 'person')];
    expect(groupEntities(list).map((g) => [g.label, g.items.length])).toEqual([['أشخاص', 2], ['جهات', 1]]);
    expect(groupEntities(list, 'برمجيات').map((g) => g.items[0].name)).toEqual(['الشركة']);
    expect(groupEntities(list, 'zzz')).toEqual([]);
  });

  it('places neighbours on a circle and marks the direction of each relation', () => {
    const { nodes, edges } = neighbourhood('أحمد', [rel('r1', 'أحمد', 'يعمل في', 'الشركة'), rel('r2', 'منى', 'تعرف', 'أحمد')]);
    expect(nodes.map((n) => n.name)).toEqual(['الشركة', 'منى']);
    for (const n of nodes) expect(Math.hypot(n.x - 50, n.y - 50)).toBeCloseTo(36, 5);
    expect(edges.map((e) => [e.label, e.outgoing])).toEqual([['يعمل في', true], ['تعرف', false]]);
  });

  it('caps the drawn neighbours and reports the rest', () => {
    const many = Array.from({ length: 13 }, (_, i) => rel(`r${i}`, 'أحمد', 'يعرف', `شخص ${i}`));
    const { nodes, hidden } = neighbourhood('أحمد', many);
    expect(nodes).toHaveLength(MAX_NEIGHBOURS);
    expect(hidden).toBe(3);
  });

  it('labels types and authors', () => {
    expect(typeLabel('place')).toBe('أماكن');
    expect(authorLabel('agent')).toBe('من وكيل');
  });
});
