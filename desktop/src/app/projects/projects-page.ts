import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { AssistantApi } from '../core/api';
import { EntryKind, Project, ProjectBrief } from '../core/models';
import { ago } from '../dashboard/dashboard-view';
import { KINDS, authorLabel } from '../knowledge/knowledge-view';

@Component({
  selector: 'app-projects-page',
  imports: [FormsModule],
  templateUrl: './projects-page.html',
  styleUrl: './projects-page.css',
})
export class ProjectsPage {
  private readonly api = inject(AssistantApi);

  readonly projects = signal<Project[]>([]);
  readonly showArchived = signal(false);
  readonly selectedId = signal<string | null>(null);
  readonly brief = signal<ProjectBrief | null>(null);
  readonly error = signal('');
  readonly busy = signal(false);

  readonly creating = signal(false);
  readonly newName = signal('');
  readonly newDescription = signal('');
  readonly kind = signal<EntryKind>('task');
  readonly text = signal('');

  readonly kinds = KINDS;
  readonly authorLabel = authorLabel;
  readonly ago = ago;

  constructor() {
    void this.load();
  }

  async load(): Promise<void> {
    try {
      const { projects } = await this.api.projects(this.showArchived());
      this.projects.set(projects);
      const current = this.selectedId();
      const keep = projects.find((p) => p.id === current) ?? projects[0] ?? null;
      if (keep) await this.select(keep.id);
      else this.brief.set(null);
      this.error.set('');
    } catch (error) {
      this.fail(error);
    }
  }

  async select(id: string): Promise<void> {
    this.selectedId.set(id);
    try {
      this.brief.set(await this.api.brief(id));
    } catch (error) {
      this.fail(error);
    }
  }

  async toggleArchivedView(): Promise<void> {
    this.showArchived.update((v) => !v);
    await this.load();
  }

  async create(): Promise<void> {
    const name = this.newName().trim();
    if (!name || this.busy()) return;
    await this.run(async () => {
      const project = await this.api.createProject(name, this.newDescription().trim());
      this.newName.set('');
      this.newDescription.set('');
      this.creating.set(false);
      this.selectedId.set(project.id);
    });
  }

  async addEntry(): Promise<void> {
    const id = this.selectedId();
    const text = this.text().trim();
    if (!id || !text || this.busy()) return;
    await this.run(async () => {
      await this.api.addEntry(id, this.kind(), text);
      this.text.set('');
    });
  }

  async setDone(entryId: string, done: boolean): Promise<void> {
    await this.run(() => this.api.setTaskDone(entryId, done));
  }

  async setArchived(archived: boolean): Promise<void> {
    const id = this.selectedId();
    if (!id) return;
    await this.run(() => this.api.setProjectArchived(id, archived));
  }

  private async run(action: () => Promise<unknown>): Promise<void> {
    this.busy.set(true);
    this.error.set('');
    try {
      await action();
      await this.load();
    } catch (error) {
      this.fail(error);
    } finally {
      this.busy.set(false);
    }
  }

  private fail(error: unknown): void {
    this.error.set(error instanceof Error ? error.message : String(error));
  }
}
