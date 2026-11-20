// Rules for the knowledge and projects pages. Pure functions, unit-tested.

import { EntityType, EntryKind, GraphEntity, GraphRelation } from '../core/models';

export const TYPES: { id: EntityType; label: string }[] = [
  { id: 'person', label: 'أشخاص' },
  { id: 'project', label: 'مشاريع' },
  { id: 'organization', label: 'جهات' },
  { id: 'place', label: 'أماكن' },
  { id: 'topic', label: 'مواضيع' },
  { id: 'other', label: 'أخرى' },
];

export function typeLabel(type: EntityType): string {
  return TYPES.find((t) => t.id === type)?.label ?? type;
}

/** Entities grouped by type in a fixed order, filtered by a search text. */
export function groupEntities(entities: GraphEntity[], search = ''): { type: EntityType; label: string; items: GraphEntity[] }[] {
  const needle = search.trim().toLocaleLowerCase();
  const match = (e: GraphEntity) =>
    !needle || e.name.toLocaleLowerCase().includes(needle) || e.description.toLocaleLowerCase().includes(needle);
  return TYPES.map((t) => ({ type: t.id, label: t.label, items: entities.filter((e) => e.type === t.id && match(e)) })).filter(
    (group) => group.items.length,
  );
}

export interface GraphNode {
  name: string;
  type: EntityType;
  x: number;
  y: number;
}

export interface GraphEdge {
  id: string;
  x: number;
  y: number;
  /** Label as a sentence from the centre's point of view. */
  label: string;
  outgoing: boolean;
}

export const MAX_NEIGHBOURS = 10;

/** The chosen entity in the middle, its neighbours on a circle (percent units). */
export function neighbourhood(center: string, relations: GraphRelation[]): { nodes: GraphNode[]; edges: GraphEdge[]; hidden: number } {
  const others = new Map<string, EntityType>();
  for (const r of relations) {
    if (r.subject !== center) others.set(r.subject, r.subject_type);
    if (r.object !== center) others.set(r.object, r.object_type);
  }
  const names = [...others.keys()].slice(0, MAX_NEIGHBOURS);
  const nodes = names.map((name, i) => {
    const angle = ((-90 + (360 / Math.max(1, names.length)) * i) * Math.PI) / 180;
    return { name, type: others.get(name)!, x: 50 + 36 * Math.cos(angle), y: 50 + 36 * Math.sin(angle) };
  });
  const edges = relations.flatMap((r) => {
    const outgoing = r.subject === center;
    const node = nodes.find((n) => n.name === (outgoing ? r.object : r.subject));
    return node ? [{ id: r.id, x: node.x, y: node.y, label: r.predicate, outgoing }] : [];
  });
  return { nodes, edges, hidden: Math.max(0, others.size - names.length) };
}

export const KINDS: { id: EntryKind; label: string }[] = [
  { id: 'status', label: 'حالة' },
  { id: 'task', label: 'مهمة' },
  { id: 'decision', label: 'قرار' },
  { id: 'note', label: 'ملاحظة' },
];

export function authorLabel(author: 'user' | 'agent'): string {
  return author === 'agent' ? 'من وكيل' : 'منك';
}
