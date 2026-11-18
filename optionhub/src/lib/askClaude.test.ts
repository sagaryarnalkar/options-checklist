/** options-checklist (#126): the Ask Claude stream from the dashboard (server-sent events). */
import { afterEach, describe, expect, it, vi } from "vitest";
import { askClaude, parseSse, scopeFor, type AskEvent } from "./askClaude";

afterEach(() => vi.unstubAllGlobals());

describe("parseSse", () => {
  it("returns complete events and keeps the unfinished tail", () => {
    const buf = 'event: meta\ndata: {"type":"meta","conversation_id":"c1"}\n\nevent: text\ndata: {"type":"text","text":"Hel"}\n\nevent: text\ndata: {"type":"te';
    const { events, rest } = parseSse(buf);
    expect(events).toEqual([{ type: "meta", conversation_id: "c1" }, { type: "text", text: "Hel" }]);
    expect(rest).toBe('event: text\ndata: {"type":"te');
  });
  it("skips malformed events", () => {
    expect(parseSse('data: {nope\n\ndata: {"type":"done"}\n\n').events).toEqual([{ type: "done" }]);
  });
});

describe("scopeFor", () => {
  it("passes an underlying, falls back to overall", () => {
    expect(scopeFor("nifty")).toBe("NIFTY");
    expect(scopeFor("M&M")).toBe("M&M");
    expect(scopeFor("bad scope!")).toBe("overall");
  });
});

describe("askClaude", () => {
  it("POSTs question + builder with the x-hz-ask header and streams events across chunk boundaries", async () => {
    const enc = new TextEncoder();
    const chunks = ['event: meta\ndata: {"type":"meta","conversation_id":"c9"}\n\nevent: text\ndata: {"type":"text","te', 'xt":"Hold."}\n\n', 'event: done\ndata: {"type":"done","conversation_id":"c9"}\n\n'];
    const fetchMock = vi.fn(async () =>
      new Response(new ReadableStream({ start(c) { for (const ch of chunks) c.enqueue(enc.encode(ch)); c.close(); } }), { headers: { "content-type": "text/event-stream" } }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const got: AskEvent[] = [];
    await askClaude({ message: "Assess", scope: "nifty", builder: { legs: [1] } }, (e) => got.push(e));
    expect(got).toEqual([{ type: "meta", conversation_id: "c9" }, { type: "text", text: "Hold." }, { type: "done", conversation_id: "c9" }]);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/api\/dashboard\/assistant$/);
    expect((init.headers as Record<string, string>)["x-hz-ask"]).toBe("1");
    expect(JSON.parse(String(init.body))).toEqual({ message: "Assess", scope: "NIFTY", builder: { legs: [1] } });
  });
  it("a refusal becomes one error event", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ ok: false, message: "limit of 40 questions per hour reached" }), { status: 429 })));
    const got: AskEvent[] = [];
    await askClaude({ message: "x", scope: "NIFTY" }, (e) => got.push(e));
    expect(got).toEqual([{ type: "error", message: "limit of 40 questions per hour reached" }]);
  });
});
