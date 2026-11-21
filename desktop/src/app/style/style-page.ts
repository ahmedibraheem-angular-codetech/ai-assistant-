import { Component, computed, inject, signal } from '@angular/core';

import { AssistantApi } from '../core/api';
import { StyleDataset } from '../core/models';

export const TEST_PROMPT = 'اكتب لي رسالة قصيرة أعتذر فيها عن تأخيري في الرد';
export const MAX_SAMPLES = 10;
const message = (e: unknown): string => (e instanceof Error ? e.message : String(e));

@Component({
  selector: 'app-style-page',
  templateUrl: './style-page.html',
  styleUrl: './style-page.css',
})
export class StylePage {
  private readonly api = inject(AssistantApi);

  readonly enabled = signal(false);
  readonly description = signal('');
  readonly samples = signal<string[]>([]);
  readonly dataset = signal<StyleDataset | null>(null);
  readonly loading = signal(true);
  readonly error = signal('');
  readonly notice = signal('');
  readonly busy = signal('');
  readonly testReply = signal('');
  readonly exportedPath = signal('');

  readonly maxSamples = MAX_SAMPLES;
  readonly percent = computed(() => {
    const d = this.dataset();
    return d ? Math.min(100, Math.round((d.pairs / d.min_needed) * 100)) : 0;
  });

  constructor() {
    void this.load();
  }

  private async load(): Promise<void> {
    try {
      const [profile, dataset] = await Promise.all([this.api.styleProfile(), this.api.styleDataset()]);
      this.enabled.set(profile.enabled);
      this.description.set(profile.description);
      this.samples.set([...profile.samples]);
      this.dataset.set(dataset);
    } catch (e) {
      this.error.set(message(e));
    } finally {
      this.loading.set(false);
    }
  }

  addSample(): void {
    if (this.samples().length < MAX_SAMPLES) this.samples.update((s) => [...s, '']);
  }

  removeSample(index: number): void {
    this.samples.update((s) => s.filter((_, i) => i !== index));
  }

  setSample(index: number, value: string): void {
    this.samples.update((s) => s.map((x, i) => (i === index ? value : x)));
  }

  async save(): Promise<void> {
    this.busy.set('save');
    this.error.set('');
    this.notice.set('');
    try {
      const saved = await this.api.saveStyleProfile({
        enabled: this.enabled(),
        description: this.description(),
        samples: this.samples().filter((s) => s.trim()),
      });
      this.samples.set([...saved.samples]);
      this.notice.set('تم حفظ أسلوبك.');
    } catch (e) {
      this.error.set(message(e));
    } finally {
      this.busy.set('');
    }
  }

  async test(): Promise<void> {
    this.busy.set('test');
    this.error.set('');
    this.testReply.set('');
    try {
      this.testReply.set((await this.api.chat(TEST_PROMPT)).answer);
    } catch (e) {
      this.error.set(message(e));
    } finally {
      this.busy.set('');
    }
  }

  async exportData(): Promise<void> {
    this.busy.set('export');
    this.error.set('');
    try {
      const result = await this.api.exportStyleDataset();
      this.exportedPath.set(result.path);
      this.dataset.set(await this.api.styleDataset());
    } catch (e) {
      this.error.set(message(e));
    } finally {
      this.busy.set('');
    }
  }
}
