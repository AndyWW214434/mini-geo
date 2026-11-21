"use client";

import { useRef, useState } from "react";

type Msg = { role: "user" | "assistant"; content: string };

const SUGGESTIONS = [
  "Show me Stripe vs Adyen visibility on ChatGPT this month as a line chart",
  "Which sites does Perplexity cite most for payouts questions?",
  "Bar chart of Stripe's visibility by assistant over the last 7 days",
];

export function Chat({ dashboardId, onChange }: { dashboardId: string; onChange: () => void }) {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);

  async function send(text: string) {
    if (!text.trim() || busy) return;
    const history = messages;
    setMessages([...history, { role: "user", content: text }]);
    setInput("");
    setBusy(true);
    try {
      const res = await fetch("/api/proxy/chat", {
        method: "POST",
        body: JSON.stringify({ message: text, history, dashboard_id: dashboardId }),
      });
      const body = await res.json().catch(() => ({ detail: res.statusText }));
      const reply = res.ok ? body.reply : `Error: ${body.detail ?? res.status}`;
      setMessages((m) => [...m, { role: "assistant", content: reply }]);
      if (res.ok && body.actions?.length) onChange();
    } finally {
      setBusy(false);
      setTimeout(() => logRef.current?.scrollTo({ top: logRef.current.scrollHeight }), 50);
    }
  }

  return (
    <aside className="card chat">
      <h2>Ask for a chart</h2>
      <div className="log" ref={logRef}>
        {messages.length === 0 && SUGGESTIONS.map((s) => (
          <button key={s} className="ghost" style={{ textAlign: "left" }} onClick={() => send(s)}>{s}</button>
        ))}
        {messages.map((m, i) => <div key={i} className={`msg ${m.role}`}>{m.content}</div>)}
        {busy && <div className="msg assistant muted">Working…</div>}
      </div>
      <form onSubmit={(e) => { e.preventDefault(); send(input); }} className="row">
        <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="Ask about the data…" style={{ flex: 1 }} disabled={busy} />
        <button type="submit" disabled={busy || !input.trim()}>Send</button>
      </form>
    </aside>
  );
}
