import { Injectable, signal } from '@angular/core';
import { ChatMessage, Phase, Suggestion } from './models';

/**
 * Streams answers from the backend.
 *
 * Uses fetch + ReadableStream rather than EventSource, because EventSource
 * cannot issue a POST and the question belongs in a request body.
 */
@Injectable({ providedIn: 'root' })
export class ChatService {
  readonly messages = signal<ChatMessage[]>([]);
  readonly busy = signal(false);
  readonly suggestions = signal<Suggestion[]>([]);

  private sessionId: string | null = null;

  async loadSuggestions(): Promise<void> {
    try {
      const response = await fetch('/api/suggestions');
      if (response.ok) {
        const body = (await response.json()) as { suggestions: Suggestion[] };
        this.suggestions.set(body.suggestions);
      }
    } catch {
      // Starter chips are a convenience; their absence is not worth surfacing.
    }
  }

  async ask(question: string): Promise<void> {
    if (this.busy() || !question.trim()) {
      return;
    }
    this.busy.set(true);

    this.append({
      id: crypto.randomUUID(),
      role: 'user',
      content: question,
      pending: false,
    });

    const replyId = crypto.randomUUID();
    this.append({
      id: replyId,
      role: 'assistant',
      content: '',
      pending: true,
      phase: 'understanding',
    });

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, session_id: this.sessionId }),
      });

      if (!response.ok || !response.body) {
        throw new Error(`Request failed with status ${response.status}`);
      }

      await this.consume(response.body, replyId);
    } catch (error) {
      this.patch(replyId, {
        pending: false,
        error: error instanceof Error ? error.message : String(error),
        content: 'Something went wrong reaching the assistant.',
      });
    } finally {
      this.busy.set(false);
    }
  }

  /** Parse the SSE frames off the byte stream. */
  private async consume(body: ReadableStream<Uint8Array>, replyId: string): Promise<void> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });

      // Frames are separated by a blank line. The server writes CRLF, so
      // splitting on "\n\n" alone never matches and the buffer grows without
      // a single event ever being parsed.
      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = frames.pop() ?? '';

      for (const frame of frames) {
        let event = 'message';
        const dataLines: string[] = [];

        for (const line of frame.split(/\r?\n/)) {
          if (line.startsWith('event:')) {
            event = line.slice(6).trim();
          } else if (line.startsWith('data:')) {
            dataLines.push(line.slice(5).trim());
          }
        }

        if (!dataLines.length) {
          continue;
        }
        try {
          this.handle(event, JSON.parse(dataLines.join('\n')), replyId);
        } catch {
          // A malformed frame should not abort a stream that is otherwise fine.
        }
      }
    }
  }

  private handle(event: string, data: any, replyId: string): void {
    switch (event) {
      case 'status':
        this.patch(replyId, { phase: data.phase as Phase });
        break;

      case 'pipeline':
        this.patch(replyId, {
          derivation: {
            pipeline: data.pipeline ?? [],
            rowCount: 0,
            truncated: false,
            elapsedMs: 0,
            attempts: data.attempt ?? 1,
          },
        });
        break;

      case 'rows': {
        const message = this.find(replyId);
        this.patch(replyId, {
          rows: data.rows ?? [],
          derivation: {
            pipeline: message?.derivation?.pipeline ?? [],
            rowCount: data.row_count ?? 0,
            truncated: data.truncated ?? false,
            elapsedMs: data.elapsed_ms ?? 0,
            attempts: message?.derivation?.attempts ?? 1,
          },
        });
        break;
      }

      case 'token': {
        const message = this.find(replyId);
        this.patch(replyId, { content: (message?.content ?? '') + (data.text ?? '') });
        break;
      }

      case 'chart':
        this.patch(replyId, { chart: data });
        break;

      case 'clarification':
        this.patch(replyId, { clarification: data.candidates ?? [] });
        break;

      case 'done':
        this.sessionId = data.session_id ?? this.sessionId;
        this.patch(replyId, { pending: false, content: data.answer ?? '' });
        break;

      case 'error':
        this.patch(replyId, {
          pending: false,
          error: data.message ?? 'Unknown error',
          content: data.message ?? 'The assistant could not answer that.',
        });
        break;
    }
  }

  private find(id: string): ChatMessage | undefined {
    return this.messages().find((message) => message.id === id);
  }

  private append(message: ChatMessage): void {
    this.messages.update((messages) => [...messages, message]);
  }

  private patch(id: string, changes: Partial<ChatMessage>): void {
    this.messages.update((messages) =>
      messages.map((message) => (message.id === id ? { ...message, ...changes } : message)),
    );
  }
}
