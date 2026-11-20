import { Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { AssistantApi, ApiError } from '../core/api';
import { GraphEntity, GraphRelation } from '../core/models';
import { authorLabel, groupEntities, neighbourhood, typeLabel } from './knowledge-view';

@Component({
  selector: 'app-knowledge-page',
  imports: [FormsModule],
  templateUrl: './knowledge-page.html',
  styleUrl: './knowledge-page.css',
})
export class KnowledgePage {
  private readonly api = inject(AssistantApi);

  readonly entities = signal<GraphEntity[]>([]);
  readonly search = signal('');
  readonly selected = signal<GraphEntity | null>(null);
  readonly relations = signal<GraphRelation[]>([]);
  readonly error = signal('');
  readonly busy = signal(false);

  readonly subject = signal('');
  readonly predicate = signal('');
  readonly object = signal('');

  readonly groups = computed(() => groupEntities(this.entities(), this.search()));
  readonly graph = computed(() => {
    const entity = this.selected();
    return entity ? neighbourhood(entity.name, this.relations()) : { nodes: [], edges: [], hidden: 0 };
  });

  readonly typeLabel = typeLabel;
  readonly authorLabel = authorLabel;

  constructor() {
    void this.load();
  }

  async load(): Promise<void> {
    try {
      this.entities.set((await this.api.entities()).entities);
      this.error.set('');
    } catch (error) {
      this.error.set(error instanceof Error ? error.message : String(error));
    }
  }

  async open(entity: GraphEntity): Promise<void> {
    this.selected.set(entity);
    this.subject.set(entity.name);
    try {
      const result = await this.api.lookup(entity.name);
      const match = result.matches.find((m) => m.entity.id === entity.id) ?? result.matches[0];
      this.relations.set(match ? match.relations : []);
    } catch (error) {
      this.relations.set([]);
      if (!(error instanceof ApiError && error.status === 404)) {
        this.error.set(error instanceof Error ? error.message : String(error));
      }
    }
  }

  async openByName(name: string): Promise<void> {
    const entity = this.entities().find((e) => e.name === name);
    if (entity) await this.open(entity);
  }

  async addRelation(): Promise<void> {
    const [s, p, o] = [this.subject().trim(), this.predicate().trim(), this.object().trim()];
    if (!s || !p || !o || this.busy()) {
      this.error.set('اكتب الطرفين والعلاقة.');
      return;
    }
    this.busy.set(true);
    this.error.set('');
    try {
      await this.api.addRelation(s, p, o);
      this.predicate.set('');
      this.object.set('');
      await this.load();
      const center = this.entities().find((e) => e.name === s);
      if (center) await this.open(center);
    } catch (error) {
      this.error.set(error instanceof Error ? error.message : String(error));
    } finally {
      this.busy.set(false);
    }
  }

  async removeRelation(relation: GraphRelation): Promise<void> {
    const entity = this.selected();
    if (!entity || this.busy()) return;
    this.busy.set(true);
    try {
      await this.api.deleteRelation(relation.id);
      await this.open(entity);
    } catch (error) {
      this.error.set(error instanceof Error ? error.message : String(error));
    } finally {
      this.busy.set(false);
    }
  }
}
