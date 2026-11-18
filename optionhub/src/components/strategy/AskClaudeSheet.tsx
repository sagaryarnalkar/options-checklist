/**
 * options-checklist: Ask Claude from the Strategy Builder. The dashboard's assistant (your Claude plan, read-only
 * tools, the live Kite book for this underlying) also receives the legs on screen with the builder's payoff
 * figures and, when they came from a live position, what you changed. It cannot place orders.
 */
import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";
import { Loader2, Plus, Send, Sparkles, Square } from "lucide-react";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { askClaude, scopeFor, type AskEvent } from "@/lib/askClaude";
import { cn } from "@/lib/utils";

interface Turn {
  id: number;
  role: "user" | "assistant";
  text: string;
  error?: string;
}

const LIVE_PROMPTS = [
  "Assess this position: what is my risk right now?",
  "How should I adjust it? Quantify each option.",
  "Compare the legs on screen with my live position. Is the change worth it?",
  "What happens if the index moves 1% either way by tomorrow?",
];
const PLAN_PROMPTS = [
  "Assess this strategy: risks, breakevens, what could go wrong?",
  "How does this fit with my live positions on this underlying?",
  "Suggest a cheaper or safer variant and quantify it.",
];

/** Inline **bold** and `code`; everything else stays text (no HTML is ever injected). */
function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    out.push(
      tok.startsWith("**") ? <strong key={i++}>{tok.slice(2, -2)}</strong> : <code key={i++} className="rounded bg-control-track px-1 font-mono text-[0.92em]">{tok.slice(1, -1)}</code>,
    );
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/** A small, safe markdown subset: headings, bullet/numbered lists, tables (kept monospaced), paragraphs. */
export function MarkdownLite({ text }: { text: string }) {
  const lines = text.split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  let k = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    if (line.trim().startsWith("|")) {
      const rows: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(lines[i++]);
      blocks.push(
        <pre key={k++} className="overflow-x-auto rounded-tile bg-control-track px-2 py-1.5 font-mono text-micro leading-snug">
          {rows.filter((r) => !/^\s*\|[\s:|-]+\|\s*$/.test(r)).join("\n")}
        </pre>,
      );
      continue;
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) {
      blocks.push(
        <p key={k++} className="mt-1 font-semibold text-foreground">
          {inline(h[2])}
        </p>,
      );
      i++;
      continue;
    }
    if (/^\s*([-*•]|\d+[.)])\s+/.test(line)) {
      const items: Array<{ key: number; text: string }> = [];
      const ordered = /^\s*\d+[.)]/.test(line);
      while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) items.push({ key: k++, text: lines[i++].replace(/^\s*([-*•]|\d+[.)])\s+/, "") });
      const List = ordered ? "ol" : "ul";
      blocks.push(
        <List key={k++} className={cn("space-y-0.5 pl-5", ordered ? "list-decimal" : "list-disc")}>
          {items.map((it) => (
            <li key={it.key}>{inline(it.text)}</li>
          ))}
        </List>,
      );
      continue;
    }
    const para: Array<{ key: number; text: string }> = [];
    while (i < lines.length && lines[i].trim() && !/^\s*([-*•]|\d+[.)])\s+|^#{1,4}\s|^\s*\|/.test(lines[i])) para.push({ key: k++, text: lines[i++] });
    const first = para[0]?.key;
    blocks.push(
      <p key={k++}>
        {para.map((ln) => (
          <Fragment key={ln.key}>
            {ln.key !== first ? <br /> : null}
            {inline(ln.text)}
          </Fragment>
        ))}
      </p>,
    );
  }
  return <div className="space-y-2">{blocks}</div>;
}

export function AskClaudeSheet({
  open,
  onOpenChange,
  symbol,
  live,
  context,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  symbol: string;
  /** True when the legs on screen came from (or still are) a live position. */
  live: boolean;
  /** The builder's legs and figures, read at the moment a question is sent. */
  context: () => Record<string, unknown>;
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const nextId = useRef(1);
  const endRef = useRef<HTMLDivElement>(null);
  const scope = scopeFor(symbol);

  // A chat is about one underlying: switching it starts a new one.
  useEffect(() => {
    setTurns([]);
    setConversationId(null);
  }, [scope]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [turns, status]);

  const ask = async (question: string) => {
    const q = question.trim();
    if (!q || busy) return;
    setDraft("");
    setBusy(true);
    setStatus("Reading your position");
    setTurns((t) => [...t, { id: nextId.current++, role: "user", text: q }, { id: nextId.current++, role: "assistant", text: "" }]);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const patchLast = (f: (t: Turn) => Turn) => setTurns((ts) => ts.map((t, i) => (i === ts.length - 1 ? f(t) : t)));
    const onEvent = (e: AskEvent) => {
      if (e.type === "meta") setConversationId(e.conversation_id);
      else if (e.type === "status") setStatus(e.text);
      else if (e.type === "text") {
        setStatus(null);
        patchLast((t) => ({ ...t, text: t.text + e.text }));
      } else if (e.type === "error") patchLast((t) => ({ ...t, error: e.message }));
      else if (e.type === "done" && e.conversation_id) setConversationId(e.conversation_id);
    };
    await askClaude({ message: q, scope, conversationId, builder: context() }, onEvent, ctrl.signal);
    abortRef.current = null;
    setBusy(false);
    setStatus(null);
  };

  const stop = () => abortRef.current?.abort();
  const fresh = () => {
    stop();
    setTurns([]);
    setConversationId(null);
  };

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="flex w-full flex-col gap-3 sm:max-w-[520px]">
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-primary-ink" aria-hidden="true" /> Ask Claude · {scope}
          </SheetTitle>
          <SheetDescription>
            Sees your live {scope} position from Kite and the legs on this screen{live ? ", including what you changed" : ""}, with the builder's payoff figures. Read-only: it
            cannot place orders. Uses your Claude plan.
          </SheetDescription>
        </SheetHeader>

        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto pr-1 text-body-sm" aria-live="polite">
          {turns.length === 0 ? (
            <div className="space-y-2">
              <p className="text-muted-foreground">Try:</p>
              {(live ? LIVE_PROMPTS : PLAN_PROMPTS).map((p) => (
                <button
                  key={p}
                  type="button"
                  onClick={() => void ask(p)}
                  className="block w-full rounded-tile border border-separator px-3 py-2 text-left hover:bg-control-track focus-visible:shadow-focus focus-visible:outline-none"
                >
                  {p}
                </button>
              ))}
            </div>
          ) : (
            turns.map((t, i) =>
              t.role === "user" ? (
                <p key={t.id} className="ml-8 rounded-tile bg-wash-brand/60 px-3 py-2 text-foreground">
                  {t.text}
                </p>
              ) : (
                <div key={t.id} className="rounded-tile border border-separator px-3 py-2 text-foreground">
                  {t.text ? <MarkdownLite text={t.text} /> : null}
                  {t.error ? <p className="text-bearish">{t.error}</p> : null}
                  {!t.text && !t.error && busy && i === turns.length - 1 ? (
                    <p className="flex items-center gap-1.5 text-muted-foreground">
                      <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> {status ?? "Thinking"}…
                    </p>
                  ) : null}
                </div>
              ),
            )
          )}
          {busy && status && turns[turns.length - 1]?.text ? (
            <p className="flex items-center gap-1.5 text-muted-foreground">
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> {status}…
            </p>
          ) : null}
          <div ref={endRef} />
        </div>

        <form
          className="space-y-2"
          onSubmit={(e) => {
            e.preventDefault();
            void ask(draft);
          }}
        >
          <Textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void ask(draft);
              }
            }}
            placeholder={`Ask about this ${scope} position…`}
            rows={3}
            maxLength={4000}
            aria-label="Question for Claude"
          />
          <div className="flex items-center justify-between gap-2">
            <Button type="button" variant="ghost" size="sm" onClick={fresh} disabled={!turns.length}>
              <Plus /> New chat
            </Button>
            {busy ? (
              <Button type="button" variant="outline" size="sm" onClick={stop}>
                <Square /> Stop
              </Button>
            ) : (
              <Button type="submit" size="sm" disabled={!draft.trim()}>
                <Send /> Ask
              </Button>
            )}
          </div>
        </form>
      </SheetContent>
    </Sheet>
  );
}
