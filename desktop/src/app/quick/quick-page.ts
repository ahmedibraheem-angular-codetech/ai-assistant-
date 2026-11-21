import { Component, DestroyRef, ElementRef, inject, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { AssistantApi } from '../core/api';
import { ApprovalsStore } from '../core/approvals-store';
import { DesktopBridge } from '../core/desktop';
import { VoiceService } from '../core/voice';

interface Exchange {
  question: string;
  answer: string;
  pending: boolean;
  failed: boolean;
}

export const SUGGESTIONS = [
  { label: 'ملخص اليوم', prompt: 'لخص لي باختصار ما حدث اليوم: المحادثات والمهام والموافقات.' },
  { label: 'ماذا تعلم الفريق؟', prompt: 'ما الذي تعلمه الفريق مؤخراً؟ أجب في ثلاث نقاط.' },
];

/** The ⌥ Space window: one quick question, shortcuts to the main window. */
@Component({
  selector: 'app-quick-page',
  imports: [FormsModule],
  templateUrl: './quick-page.html',
  styleUrl: './quick-page.css',
  host: { '(document:keydown.escape)': 'hide()' },
})
export class QuickPage {
  private readonly api = inject(AssistantApi);
  private readonly desktop = inject(DesktopBridge);
  readonly approvals = inject(ApprovalsStore);
  readonly voice = inject(VoiceService);

  readonly draft = signal('');
  readonly exchanges = signal<Exchange[]>([]);
  readonly conversationId = signal<string | null>(null);
  readonly sending = signal(false);
  readonly suggestions = SUGGESTIONS;

  private readonly input = viewChild<ElementRef<HTMLTextAreaElement>>('input');

  constructor() {
    // The window is reused: focus the box each time it is shown.
    const focus = () => this.input()?.nativeElement.focus();
    window.addEventListener('focus', focus);
    inject(DestroyRef).onDestroy(() => {
      window.removeEventListener('focus', focus);
      this.voice.stop();
    });
    void this.voice.loadStatus();
  }

  /** Mic button: press to start, press again to stop; the transcript is asked automatically. */
  toggleMic(): void {
    if (this.voice.recording()) return this.voice.stop();
    void this.voice.start((text) => void this.ask(text));
  }

  onKeydown(event: KeyboardEvent): void {
    if (event.key !== 'Enter' || event.isComposing) return;
    if (event.metaKey) {
      event.preventDefault();
      void this.openFull();
    } else if (!event.shiftKey) {
      event.preventDefault();
      void this.ask();
    }
  }

  async ask(text = this.draft()): Promise<void> {
    const question = text.trim();
    if (!question || this.sending()) return;
    this.sending.set(true);
    this.draft.set('');
    this.exchanges.update((list) => [...list.slice(-2), { question, answer: '', pending: true, failed: false }]);
    try {
      const reply = await this.api.chat(question, this.conversationId());
      if (reply.conversation_id) this.conversationId.set(reply.conversation_id);
      this.finish({ answer: reply.answer, failed: false });
      if (this.voice.autoread()) void this.voice.speak(reply.answer);
    } catch (error) {
      this.finish({ answer: error instanceof Error ? error.message : String(error), failed: true });
    } finally {
      this.sending.set(false);
    }
  }

  private finish(result: { answer: string; failed: boolean }): void {
    this.exchanges.update((list) => list.map((item) => (item.pending ? { ...item, ...result, pending: false } : item)));
  }

  /** ⌘ Enter: continue in the main window, in this conversation when there is one. */
  openFull(): Promise<void> {
    const id = this.conversationId();
    return this.open(id ? `/chat/${id}` : '/chat');
  }

  open(path: string): Promise<void> {
    return this.desktop.openMain(path).catch(() => undefined);
  }

  hide(): Promise<void> {
    return this.desktop.hideQuick();
  }
}
