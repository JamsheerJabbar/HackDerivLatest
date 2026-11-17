import { useEffect, useState } from 'react';

const API = 'http://localhost:8080/api/v1/sentinel/handoff/threads';

function authHeaders(): Record<string, string> {
    const token = localStorage.getItem('auth_token');
    return token ? { Authorization: `Bearer ${token}` } : {};
}

interface ThreadMessage {
    author_id: string | null;
    author_role: 'user' | 'human' | 'system';
    content: string;
    created_at: string;
}

interface Thread {
    thread_id: string;
    status: string;
    messages: ThreadMessage[];
}

interface HandoffChatProps {
    threadId: string;
    onClose: () => void;
}

/** Chat with a human reviewer for an insight the user rated incorrect. */
export default function HandoffChat({ threadId, onClose }: HandoffChatProps) {
    const [thread, setThread] = useState<Thread | null>(null);
    const [draft, setDraft] = useState('');
    const [error, setError] = useState<string | null>(null);

    const load = async () => {
        try {
            const res = await fetch(`${API}/${threadId}`, { headers: authHeaders() });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            setThread(await res.json());
            setError(null);
        } catch (e) {
            setError(e instanceof Error ? e.message : 'Failed to load thread');
        }
    };

    useEffect(() => {
        load();
        const timer = setInterval(load, 5000); // poll for reviewer replies
        return () => clearInterval(timer);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [threadId]);

    const send = async () => {
        const content = draft.trim();
        if (!content) return;
        const res = await fetch(`${API}/${threadId}/messages`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...authHeaders() },
            body: JSON.stringify({ content }),
        });
        if (res.ok) {
            setDraft('');
            setThread(await res.json());
        } else {
            setError(`Could not send message (HTTP ${res.status})`);
        }
    };

    return (
        <div className="handoff-overlay" role="dialog" aria-label="Chat with a reviewer">
            <div className="handoff-panel">
                <div className="handoff-header">
                    <strong>Human review</strong>
                    <span className="handoff-status">{thread?.status ?? '…'}</span>
                    <button onClick={onClose} aria-label="Close">✕</button>
                </div>
                {error && <div className="handoff-error">{error}</div>}
                <div className="handoff-messages">
                    {(thread?.messages ?? []).map((m, i) => (
                        <div key={i} className={`handoff-msg ${m.author_role}`}>
                            <span className="handoff-author">{m.author_role === 'human' ? 'Reviewer' : m.author_role === 'system' ? 'System' : 'You'}</span>
                            <p>{m.content}</p>
                        </div>
                    ))}
                </div>
                <div className="handoff-input">
                    <input
                        value={draft}
                        onChange={e => setDraft(e.target.value)}
                        onKeyDown={e => e.key === 'Enter' && send()}
                        placeholder="Explain what was wrong…"
                    />
                    <button onClick={send} disabled={!draft.trim()}>Send</button>
                </div>
            </div>
        </div>
    );
}
