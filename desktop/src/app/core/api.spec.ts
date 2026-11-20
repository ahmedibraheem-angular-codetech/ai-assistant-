import { TestBed } from '@angular/core/testing';

import { ApiError, AssistantApi, Invoke, NOT_IN_APP, TAURI_INVOKE, describeFailure } from './api';

interface Call {
  command: string;
  args?: Record<string, unknown>;
}

function setup(handler: (call: Call) => unknown): { api: AssistantApi; calls: Call[] } {
  const calls: Call[] = [];
  const invoke: Invoke = async <T>(command: string, args?: Record<string, unknown>) => {
    calls.push({ command, args });
    return (await handler({ command, args })) as T;
  };
  TestBed.configureTestingModule({ providers: [{ provide: TAURI_INVOKE, useValue: invoke }] });
  return { api: TestBed.inject(AssistantApi), calls };
}

describe('AssistantApi', () => {
  it('sends chat through the Rust bridge with only method, path and body', async () => {
    const { api, calls } = setup(() => ({ status: 200, body: { model: 'm', answer: 'أهلاً' } }));

    const reply = await api.chat('مرحبا', 'c-1');

    expect(reply.answer).toBe('أهلاً');
    expect(calls).toEqual([
      {
        command: 'api_request',
        args: { method: 'POST', path: '/api/v1/chat', body: { message: 'مرحبا', conversation_id: 'c-1' } },
      },
    ]);
  });

  it('leaves conversation_id out for a new chat', async () => {
    const { api, calls } = setup(() => ({ status: 200, body: { model: 'm', answer: 'x' } }));
    await api.chat('hi');
    expect(calls[0].args?.['body']).toEqual({ message: 'hi' });
  });

  it('turns an HTTP error into an ApiError with an Arabic sentence', async () => {
    const { api } = setup(() => ({ status: 423, body: { detail: 'kill switch' } }));
    const error = await api.chat('hi').catch((caught) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(423);
    expect(error.message).toBe('المساعد متوقف مؤقتاً: kill switch');
  });

  it('reports a bridge failure (server down, bad token file) as status 0', async () => {
    const { api } = setup(() => {
      throw 'The assistant server is not running on port 8000.';
    });
    const error = await api.chat('hi').catch((caught) => caught);
    expect(error.status).toBe(0);
    expect(error.message).toContain('not running');
  });

  it('reads the newest page when a conversation has more than 200 messages', async () => {
    const { api, calls } = setup(({ args }) => {
      const path = String(args?.['path']);
      const offset = path.includes('offset=') ? 50 : 0;
      const messages = [2, 1].map((n) => ({ sequence_number: offset + n, role: 'user', content: `${offset + n}` }));
      return { status: 200, body: { messages, count: 250, limit: 200, offset } };
    });

    const page = await api.messages('a/b');

    expect(calls.map((call) => call.args?.['path'])).toEqual([
      '/api/v1/conversations/a%2Fb/messages?limit=200',
      '/api/v1/conversations/a%2Fb/messages?limit=200&offset=50',
    ]);
    expect(page.messages.map((message) => message.sequence_number)).toEqual([51, 52]);
  });

  it('health is false when the bridge fails', async () => {
    const { api } = setup(() => {
      throw 'boom';
    });
    expect(await api.health()).toBe(false);
  });

  it('refuses to run outside the Tauri app', async () => {
    const api = TestBed.inject(AssistantApi); // default invoke: no Tauri in jsdom
    const error = await api.conversations().catch((caught) => caught);
    expect(error.message).toBe(NOT_IN_APP);
  });
});

describe('describeFailure', () => {
  it('explains a rejected token', () => {
    expect(describeFailure(401, { detail: 'x' })).toContain('rotate');
  });

  it('joins FastAPI validation messages', () => {
    const body = { detail: [{ msg: 'too long' }, { msg: 'missing' }] };
    expect(describeFailure(422, body)).toBe('too long، missing');
  });

  it('falls back to the status code', () => {
    expect(describeFailure(500, null)).toBe('فشل الطلب (HTTP 500).');
  });
});
