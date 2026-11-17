/**
 * Minimal EventSource replacement built on fetch so the Authorization header can be sent
 * (the browser EventSource cannot set headers, which made authenticated scans owner "anonymous").
 * Supports addEventListener(type, handler), close() and onerror, like the subset the dashboard uses.
 */
type Handler = (e: { data: string }) => void;

export default class AuthEventSource {
    onerror: (() => void) | null = null;
    private listeners = new Map<string, Handler[]>();
    private controller = new AbortController();
    private closed = false;

    constructor(url: string) {
        this.start(url);
    }

    addEventListener(type: string, handler: Handler): void {
        this.listeners.set(type, [...(this.listeners.get(type) ?? []), handler]);
    }

    close(): void {
        this.closed = true;
        this.controller.abort();
    }

    private dispatch(type: string, data: string): void {
        (this.listeners.get(type) ?? []).forEach(h => h({ data }));
    }

    private fail(): void {
        if (!this.closed) this.onerror?.();
    }

    private async start(url: string): Promise<void> {
        const token = localStorage.getItem('auth_token');
        try {
            const res = await fetch(url, {
                headers: { Accept: 'text/event-stream', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
                signal: this.controller.signal,
            });
            if (!res.ok || !res.body) return this.fail();
            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            for (;;) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                let idx: number;
                while ((idx = buffer.indexOf('\n\n')) >= 0) {
                    const block = buffer.slice(0, idx);
                    buffer = buffer.slice(idx + 2);
                    let type = 'message';
                    const data: string[] = [];
                    for (const line of block.split('\n')) {
                        if (line.startsWith('event:')) type = line.slice(6).trim();
                        else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
                    }
                    if (data.length) this.dispatch(type, data.join('\n'));
                }
            }
            this.fail(); // stream ended without close(): treat like a dropped connection
        } catch {
            this.fail();
        }
    }
}
