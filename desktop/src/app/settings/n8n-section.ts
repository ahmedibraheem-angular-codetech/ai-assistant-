import { Component, inject, signal } from '@angular/core';

import { AssistantApi } from '../core/api';
import { N8nStatus, N8nWorkflow } from '../core/models';

const message = (e: unknown): string => (e instanceof Error ? e.message : String(e));
const NAME = /^[a-z0-9-]{1,40}$/;
const PATH = /^[A-Za-z0-9/_-]{1,200}$/;

export function validateWorkflow(name: string, description: string, path: string): { ok: true } | { ok: false; error: string } {
  if (!NAME.test(name.trim())) return { ok: false, error: 'الاسم: حروف إنجليزية صغيرة وأرقام و - فقط (حتى 40).' };
  if (description.length > 300) return { ok: false, error: 'الوصف أطول من 300 حرف.' };
  const p = path.trim();
  if (!PATH.test(p) || p.includes('..') || p.includes('//') || p.startsWith('/')) {
    return { ok: false, error: 'المسار: حروف وأرقام و / و _ و - فقط، بدون .. وبدون / في البداية.' };
  }
  return { ok: true };
}

@Component({
  selector: 'app-n8n-section',
  templateUrl: './n8n-section.html',
  styleUrl: './n8n-section.css',
})
export class N8nSection {
  private readonly api = inject(AssistantApi);

  readonly status = signal<N8nStatus | null>(null);
  readonly workflows = signal<N8nWorkflow[]>([]);
  readonly error = signal('');
  readonly busy = signal('');
  readonly confirmDelete = signal<string | null>(null);

  // editing === null: form closed, '' : new workflow.
  readonly editing = signal<string | null>(null);
  readonly name = signal('');
  readonly description = signal('');
  readonly path = signal('');
  readonly method = signal<'POST' | 'GET'>('POST');
  readonly formError = signal('');

  constructor() {
    void this.load();
  }

  private async load(): Promise<void> {
    try {
      this.workflows.set((await this.api.n8nWorkflows()).workflows);
    } catch (e) {
      this.error.set(message(e));
    }
    await this.check();
  }

  async check(): Promise<void> {
    try {
      this.status.set(await this.api.n8nStatus());
    } catch (e) {
      this.status.set({ ok: false, base_url: '', error: message(e) });
    }
  }

  openNew(): void {
    this.fill('', '', '', '', 'POST');
  }

  edit(w: N8nWorkflow): void {
    this.fill(w.id, w.name, w.description, w.webhook_path, w.method);
  }

  private fill(id: string, name: string, description: string, path: string, method: 'POST' | 'GET'): void {
    this.editing.set(id);
    this.name.set(name);
    this.description.set(description);
    this.path.set(path);
    this.method.set(method);
    this.formError.set('');
  }

  cancel(): void {
    this.editing.set(null);
  }

  async save(): Promise<void> {
    const id = this.editing();
    if (id === null || this.busy()) return;
    const check = validateWorkflow(this.name(), this.description(), this.path());
    if (!check.ok) return this.formError.set(check.error);
    const body = {
      name: this.name().trim(), description: this.description().trim(),
      webhook_path: this.path().trim(), method: this.method(),
    };
    await this.act('save', async () => {
      const saved = id ? await this.api.updateN8nWorkflow(id, body) : await this.api.createN8nWorkflow({ ...body, enabled: true });
      this.replace(saved);
      this.editing.set(null);
    }, true);
  }

  async toggle(w: N8nWorkflow): Promise<void> {
    if (this.busy()) return;
    await this.act(w.id, async () => this.replace(await this.api.updateN8nWorkflow(w.id, { enabled: !w.enabled })));
  }

  async remove(w: N8nWorkflow): Promise<void> {
    if (this.busy()) return;
    if (this.confirmDelete() !== w.id) return this.confirmDelete.set(w.id);
    await this.act(w.id, async () => {
      await this.api.deleteN8nWorkflow(w.id);
      this.workflows.update((all) => all.filter((x) => x.id !== w.id));
      this.confirmDelete.set(null);
    });
  }

  private replace(w: N8nWorkflow): void {
    this.workflows.update((all) => (all.some((x) => x.id === w.id) ? all.map((x) => (x.id === w.id ? w : x)) : [...all, w]));
  }

  private async act(key: string, action: () => Promise<void>, inForm = false): Promise<void> {
    this.busy.set(key);
    this.error.set('');
    this.formError.set('');
    try {
      await action();
    } catch (e) {
      (inForm ? this.formError : this.error).set(message(e));
    } finally {
      this.busy.set('');
    }
  }
}
