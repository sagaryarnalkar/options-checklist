/**
 * options-checklist: Ask Claude from this terminal — the dashboard's assistant (assistant.py, the same one as the
 * Positions tab's drawer: your Claude plan, read-only tools, live Kite book in its context) through this terminal's
 * proxy (/api/dashboard/assistant). The Strategy Builder adds its legs and payoff figures as `builder`.
 * The answer streams as server-sent events: meta, status, text (deltas), done | error.
 */
import { PROXY_BASE } from "@/lib/api/proxyClient";

export type AskEvent =
  | { type: "meta"; conversation_id: string; model?: string }
  | { type: "status"; text: string }
  | { type: "text"; text: string }
  | { type: "done"; conversation_id?: string; usage?: Record<string, number> }
  | { type: "error"; message: string };

export interface AskRequest {
  message: string;
  /** "overall" or an underlying (NIFTY, BANKNIFTY…): the assistant's chat scope. */
  scope: string;
  conversationId?: string | null;
  builder?: Record<string, unknown>;
}

/** Split a server-sent-event buffer into complete events plus the unfinished remainder. Pure. */
export function parseSse(buffer: string): { events: AskEvent[]; rest: string } {
  const events: AskEvent[] = [];
  const blocks = buffer.replace(/\r\n/g, "\n").split("\n\n");
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    const data = block
      .split("\n")
      .filter((l) => l.startsWith("data:"))
      .map((l) => l.slice(5).replace(/^ /, ""))
      .join("\n");
    if (!data) continue;
    try {
      const ev = JSON.parse(data) as AskEvent;
      if (ev && typeof ev.type === "string") events.push(ev);
    } catch {
      /* a malformed event is skipped, the stream goes on */
    }
  }
  return { events, rest };
}

export function scopeFor(symbol: string): string {
  const s = String(symbol || "").toUpperCase();
  return /^[A-Z0-9&_-]{1,24}$/.test(s) ? s : "overall";
}

/** Ask one question; every event goes to `onEvent`. Resolves when the answer ends (or fails: an error event). */
export async function askClaude(req: AskRequest, onEvent: (e: AskEvent) => void, signal?: AbortSignal): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`${PROXY_BASE}/api/dashboard/assistant`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-hz-ask": "1", Accept: "text/event-stream" },
      body: JSON.stringify({ message: req.message, scope: scopeFor(req.scope), conversation_id: req.conversationId || undefined, builder: req.builder }),
      signal,
    });
  } catch (e) {
    if ((e as Error).name !== "AbortError") onEvent({ type: "error", message: `Could not reach the dashboard: ${(e as Error).message}` });
    return;
  }
  if (!res.ok || !res.body || !String(res.headers.get("content-type") || "").includes("text/event-stream")) {
    const j = (await res.json().catch(() => null)) as { message?: string; detail?: string } | null;
    onEvent({ type: "error", message: j?.message || j?.detail || `Ask Claude failed (HTTP ${res.status})` });
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const { events, rest } = parseSse(buf);
      buf = rest;
      for (const ev of events) onEvent(ev);
    }
    const tail = parseSse(`${buf}\n\n`);
    for (const ev of tail.events) onEvent(ev);
  } catch (e) {
    if ((e as Error).name !== "AbortError") onEvent({ type: "error", message: `The answer was cut off: ${(e as Error).message}` });
  }
}
