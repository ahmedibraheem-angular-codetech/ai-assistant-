import { TestBed } from '@angular/core/testing';
import { Router, provideRouter, withComponentInputBinding } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';

import { routes } from '../app.routes';
import { Invoke, TAURI_INVOKE } from '../core/api';
import { ChatPage } from './chat-page';

type Handler = (method: string, path: string, body: unknown) => { status: number; body: unknown };

const conversation = {
  id: 'c-1',
  title: 'أول محادثة',
  agent_id: null,
  created_at: '',
  updated_at: '',
  is_archived: false,
  message_count: 2,
};

function message(sequence: number, role: string, content: string) {
  return { id: `m${sequence}`, conversation_id: 'c-1', role, content, agent_id: null, model: 'qwen', created_at: '', sequence_number: sequence };
}

/** Default fake server: one conversation, chat creates c-1. Tests override `chat`. */
function fakeServer(overrides: { chat?: Handler } = {}): { requests: string[]; invoke: Invoke } {
  const requests: string[] = [];
  const handler: Handler = (method, path, body) => {
    requests.push(`${method} ${path}`);
    if (path.startsWith('/api/v1/conversations?')) {
      return { status: 200, body: { conversations: [conversation], count: 1, limit: 50, offset: 0 } };
    }
    if (path.startsWith('/api/v1/conversations/c-1/messages')) {
      const messages = [message(2, 'assistant', 'أهلاً بك'), message(1, 'user', 'مرحبا'), message(3, 'tool', '{}')];
      return { status: 200, body: { messages, count: 3, limit: 200, offset: 0 } };
    }
    if (path === '/api/v1/chat') {
      return (overrides.chat ?? (() => ({ status: 200, body: { model: 'qwen', answer: 'تم', conversation_id: 'c-1' } })))(method, path, body);
    }
    return { status: 404, body: { detail: 'not found' } };
  };
  const invoke: Invoke = async <T>(command: string, args?: Record<string, unknown>) => {
    if (command === 'api_health') return true as T;
    return handler(String(args?.['method']), String(args?.['path']), args?.['body']) as T;
  };
  return { requests, invoke };
}

async function open(url: string, server = fakeServer()) {
  TestBed.configureTestingModule({
    providers: [provideRouter(routes, withComponentInputBinding()), { provide: TAURI_INVOKE, useValue: server.invoke }],
  });
  const harness = await RouterTestingHarness.create();
  const page = await harness.navigateByUrl(url, ChatPage);
  await harness.fixture.whenStable();
  harness.detectChanges();
  return { harness, page, server, element: harness.routeNativeElement as HTMLElement };
}

describe('ChatPage', () => {
  it('shows the conversation list and the server status', async () => {
    const { element } = await open('/chat');
    expect(element.querySelector('.conversation')?.textContent).toContain('أول محادثة');
    expect(element.querySelector('.status')?.textContent).toContain('الخادم يعمل');
    expect(element.querySelector('.empty h1')?.textContent).toContain('كيف أساعدك');
  });

  it('opens a conversation from the URL, oldest first, without tool messages', async () => {
    const { page } = await open('/chat/c-1');
    expect(page.lines().map((line) => [line.role, line.content])).toEqual([
      ['user', 'مرحبا'],
      ['assistant', 'أهلاً بك'],
    ]);
  });

  it('sends a new chat, shows the answer and moves to the new conversation without reloading it', async () => {
    const { page, harness, server } = await open('/chat');
    page.draft.set('  مرحبا  ');

    await page.send();
    await harness.fixture.whenStable();

    expect(server.requests).toContain('POST /api/v1/chat');
    expect(TestBed.inject(Router).url).toBe('/chat/c-1');
    expect(harness.routeDebugElement?.componentInstance).toBe(page); // same page, not rebuilt
    expect(page.lines().map((line) => line.content)).toEqual(['مرحبا', 'تم']);
    expect(server.requests.filter((request) => request.includes('/messages'))).toEqual([]);
    expect(page.draft()).toBe('');
  });

  it('continues an open conversation by sending its id', async () => {
    let sent: unknown;
    const server = fakeServer({
      chat: (_method, _path, body) => {
        sent = body;
        return { status: 200, body: { model: 'qwen', answer: 'ok', conversation_id: 'c-1' } };
      },
    });
    const { page } = await open('/chat/c-1', server);
    page.draft.set('وبعدين؟');
    await page.send();
    expect(sent).toEqual({ message: 'وبعدين؟', conversation_id: 'c-1' });
  });

  it('on failure marks the message, shows the error and keeps the text for a retry', async () => {
    const server = fakeServer({ chat: () => ({ status: 502, body: { detail: 'connection refused' } }) });
    const { page, harness, element } = await open('/chat', server);
    page.draft.set('مرحبا');

    await page.send();
    harness.detectChanges();

    expect(page.lines()).toEqual([{ role: 'user', content: 'مرحبا', failed: true }]);
    expect(page.draft()).toBe('مرحبا');
    expect(element.querySelector('[role="alert"]')?.textContent).toContain('Ollama');
  });

  it('Enter sends, Shift+Enter and IME composition do not', async () => {
    const { page } = await open('/chat');
    const sent: number[] = [];
    page.send = async () => void sent.push(1);

    page.onKeydown(new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true }));
    page.onKeydown(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true }));
    expect(sent).toEqual([]);

    page.onKeydown(new KeyboardEvent('keydown', { key: 'Enter' }));
    expect(sent).toEqual([1]);
  });

  it('cannot send an empty message', async () => {
    const { page, server } = await open('/chat');
    page.draft.set('   ');
    expect(page.canSend()).toBe(false);
    await page.send();
    expect(server.requests).not.toContain('POST /api/v1/chat');
  });
});
