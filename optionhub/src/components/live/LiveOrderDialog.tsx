/**
 * options-checklist: confirm and send REAL Zerodha orders (one from the order ticket, or a basket).
 *
 * Everything shown comes from the dashboard's own check (previewKiteOrders): the exact Kite contract, the
 * quantity from Kite's lot size, the last price, the rule verdict per leg and Kite's margin. Nothing is sent
 * until the owner ticks the real-money box and presses the red button. Intent ids are minted when the dialog
 * opens, so a double click or a retry of the same attempt can never send twice (the dashboard dedupes them).
 *
 * Baskets go one leg at a time, buys first, each waiting for Kite to report it COMPLETE; the first refusal,
 * rejection or unfilled leg stops the rest (placeKiteBasket). Nothing is ever unwound automatically.
 */
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, CheckCircle2, CircleDashed, Loader2, Radio, ShieldAlert, XCircle } from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { formatDateIST, formatINR, formatNumberIN } from "@/lib/options";
import { isLiveTradingEnabled } from "@/lib/brokerConfig";
import { armLive } from "@/lib/liveArm";
import {
  KiteOrderError,
  fetchKiteOrdersStatus,
  newIntentId,
  orderBasketLegs,
  placeKiteBasket,
  placeKiteOrder,
  previewKiteOrders,
  waitForKiteOrder,
  type BasketStep,
  type KiteLeg,
  type KiteOrderType,
  type KiteOrdersStatus,
  type KitePreview,
  type KiteProduct,
} from "@/lib/kiteOrders";
import { cn } from "@/lib/utils";

const SINGLE_TYPES: KiteOrderType[] = ["LIMIT", "MARKET", "SL", "SL-M"];
const BASKET_TYPES: KiteOrderType[] = ["MARKET", "LIMIT"];
const TYPE_LABEL: Record<KiteOrderType, string> = { LIMIT: "Limit", MARKET: "Market (Kite protection)", SL: "Stop-loss limit", "SL-M": "Stop-loss market" };

function toTick(v: number, tick: number): number {
  const t = tick > 0 ? tick : 0.05;
  return Number((Math.round(v / t) * t).toFixed(2));
}

function contractLabel(l: KiteLeg): string {
  if (l.tradingsymbol) return l.tradingsymbol;
  return `${l.underlying ?? ""} ${l.opt_type === "FUT" ? "FUT" : `${formatNumberIN(l.strike ?? 0)} ${l.opt_type ?? ""}`} · ${l.expiry ? formatDateIST(l.expiry) : ""}`;
}

type Phase = "review" | "sending" | "done";

export function LiveOrderDialog({
  open,
  onOpenChange,
  legs: initialLegs,
  source,
  defaultProduct = "NRML",
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  legs: KiteLeg[];
  source: "ticket" | "basket";
  defaultProduct?: KiteProduct;
}) {
  const uid = useId();
  const basket = source === "basket";
  const [legs, setLegs] = useState<KiteLeg[]>(initialLegs);
  const [product, setProduct] = useState<KiteProduct>(defaultProduct);
  const [status, setStatus] = useState<KiteOrdersStatus | null>(null);
  const [statusErr, setStatusErr] = useState<string | null>(null);
  const [preview, setPreview] = useState<KitePreview | null>(null);
  const [previewErr, setPreviewErr] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [ack, setAck] = useState(false);
  const [phase, setPhase] = useState<Phase>("review");
  const [steps, setSteps] = useState<BasketStep[]>([]);
  const [outcome, setOutcome] = useState<{ tone: "ok" | "warn" | "bad"; text: string } | null>(null);
  const intents = useRef<string[]>([]);
  const prefilled = useRef(false);
  const liveOn = isLiveTradingEnabled();

  // Reset on every open: fresh legs, fresh intent ids (one attempt = one set of ids).
  useEffect(() => {
    if (!open) return;
    setLegs(initialLegs.map((l) => ({ ...l })));
    setProduct(defaultProduct);
    setAck(false);
    setPhase("review");
    setSteps([]);
    setOutcome(null);
    setPreview(null);
    setPreviewErr(null);
    prefilled.current = false;
    intents.current = initialLegs.map(() => newIntentId(basket ? "bk" : "tk"));
    fetchKiteOrdersStatus()
      .then((s) => {
        setStatus(s);
        setStatusErr(null);
      })
      .catch((e: Error) => setStatusErr(e.message));
    // initialLegs identity changes per open by the caller; reading it once per open is the intent.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const sendLegs = useMemo(() => legs.map((l) => ({ ...l, product })), [legs, product]);

  // Ask the dashboard to check the orders whenever they change (debounced). Nothing is sent by a preview.
  const runPreview = useCallback(async (ls: KiteLeg[]) => {
    setChecking(true);
    try {
      const p = await previewKiteOrders(ls);
      setPreview(p);
      setPreviewErr(null);
    } catch (e) {
      setPreview(null);
      setPreviewErr((e as Error).message);
    } finally {
      setChecking(false);
    }
  }, []);
  useEffect(() => {
    if (!open || phase !== "review") return;
    const t = window.setTimeout(() => void runPreview(sendLegs), 350);
    return () => window.clearTimeout(t);
  }, [open, phase, sendLegs, runPreview]);

  // First preview: fill empty limit prices with the last price (on the tick), so LIMIT starts sensible.
  useEffect(() => {
    if (!preview || prefilled.current) return;
    prefilled.current = true;
    setLegs((cur) =>
      cur.map((l, i) => {
        const p = preview.legs[i];
        if (!p?.ltp || !(l.order_type === "LIMIT" || l.order_type === "SL") || (l.price ?? 0) > 0) return l;
        return { ...l, price: toTick(p.ltp, p.tick_size ?? 0.05) };
      }),
    );
  }, [preview]);

  const setLeg = (i: number, patch: Partial<KiteLeg>) => setLegs((cur) => cur.map((l, k) => (k === i ? { ...l, ...patch } : l)));
  const setTypeAll = (t: KiteOrderType) =>
    setLegs((cur) =>
      cur.map((l, i) => {
        const p = preview?.legs[i];
        const needsPrice = t === "LIMIT" || t === "SL";
        const price = needsPrice && !(l.price && l.price > 0) && p?.ltp ? toTick(p.ltp, p.tick_size ?? 0.05) : l.price;
        return { ...l, order_type: t, price: needsPrice ? price : undefined, trigger_price: t === "SL" || t === "SL-M" ? l.trigger_price : undefined };
      }),
    );

  const serverReady = !!status && status.enabled && !status.halted && status.logged_in !== false;
  const canSend = phase === "review" && serverReady && liveOn && ack && !!preview?.ok && !checking;
  const n = legs.length;

  const send = async () => {
    if (!canSend) return;
    const arm = armLive(false);
    if (!arm.token) {
      setOutcome({ tone: "bad", text: arm.message ?? "Live trading is off." });
      return;
    }
    setPhase("sending");
    if (!basket) {
      setSteps([{ state: "sending" }]);
      try {
        const placed = await placeKiteOrder(sendLegs[0], "ticket", intents.current[0], arm.token);
        setSteps([{ state: "sent", orderId: placed.order_id }]);
        const order = await waitForKiteOrder(placed.order_id, { timeoutMs: 10_000 });
        const st = String(order?.status ?? "").toUpperCase();
        if (st === "COMPLETE" && order) {
          setSteps([{ state: "done", orderId: placed.order_id, order }]);
          setOutcome({ tone: "ok", text: `Filled ${order.filled_quantity ?? order.quantity} @ ${formatNumberIN(order.average_price ?? 0, 2)} (order ${placed.order_id}).` });
        } else if (st === "REJECTED" || st === "CANCELLED") {
          setSteps([{ state: "failed", orderId: placed.order_id, message: order?.status_message || st }]);
          setOutcome({ tone: "bad", text: `Kite ${st.toLowerCase()} order ${placed.order_id}: ${order?.status_message || "no reason given"}` });
        } else {
          setSteps([{ state: "open", orderId: placed.order_id, order }]);
          setOutcome({ tone: "warn", text: `Order ${placed.order_id} is ${order?.status ? order.status.toLowerCase() : "sent"} — it waits at your price. Manage it on the Orders page.` });
        }
      } catch (e) {
        const err = e instanceof KiteOrderError ? e : new KiteOrderError("ERROR", (e as Error).message, 0);
        setSteps([{ state: "failed", message: err.message, uncertain: err.uncertain }]);
        setOutcome({ tone: err.uncertain ? "warn" : "bad", text: err.message });
      }
      setPhase("done");
      return;
    }
    // Basket: send order = buys first; keep each leg's own intent id with it.
    // The list is re-shown in send order first, so each row's progress sits next to its own leg.
    const indexed = legs.map((l, i) => ({ ...l, product, _i: i }));
    const ordered = orderBasketLegs(indexed);
    const sendOrder = ordered.map(({ _i, ...l }) => l);
    setLegs(sendOrder);
    setPreview((p) => (p ? { ...p, legs: ordered.map((l) => p.legs[l._i]) } : p));
    setSteps(ordered.map(() => ({ state: "waiting" })));
    const res = await placeKiteBasket(
      sendOrder,
      ordered.map((l) => intents.current[l._i]),
      arm.token,
      { onStep: (i, s) => setSteps((cur) => cur.map((x, k) => (k === i ? s : x))) },
    );
    setOutcome(
      res.stoppedAt === null
        ? { tone: "ok", text: `All ${ordered.length} legs filled.` }
        : { tone: res.steps.some((s) => s.state === "done") ? "warn" : "bad", text: res.reason ?? "Stopped." },
    );
    setPhase("done");
  };

  const statusLine = statusErr ? (
    <p className="flex items-start gap-1.5 text-bearish">
      <XCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" /> {statusErr}
    </p>
  ) : status ? (
    <div className="flex flex-wrap items-center gap-1.5">
      <Badge variant={status.enabled ? "bullish" : "bearish"}>{status.enabled ? "Server: live orders on" : "Server: live orders OFF"}</Badge>
      {status.halted ? <Badge variant="bearish">HALTED</Badge> : null}
      <Badge variant={status.logged_in ? "neutral" : "bearish"}>{status.logged_in ? "Kite logged in" : "Kite not logged in"}</Badge>
      {status.sent_today !== null ? (
        <Badge variant="neutral">
          {status.sent_today}/{status.rules.max_orders_per_day} today
        </Badge>
      ) : null}
      <Badge variant="neutral">max {status.rules.max_lots} lots/order</Badge>
    </div>
  ) : (
    <p className="flex items-center gap-1.5 text-muted-foreground">
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> Checking the dashboard…
    </p>
  );

  const blockers: string[] = [];
  if (!liveOn) blockers.push("Live trading is off in Broker settings.");
  if (status && !status.enabled) blockers.push("The server has live orders switched off (KITE_ORDERS=on in the dashboard's .env).");
  if (status?.halted) blockers.push("Live orders are halted. Resume them on the Orders page.");
  if (status && status.logged_in === false) blockers.push("The dashboard is not logged in to Kite today.");

  return (
    <Dialog open={open} onOpenChange={(v) => (phase === "sending" ? undefined : onOpenChange(v))}>
      <DialogContent className="max-h-[92dvh] max-w-2xl overflow-y-auto border-bearish/50">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-bearish">
            <Radio className="h-4 w-4 animate-pulse" aria-hidden="true" /> {basket ? `Place ${n} real orders on Zerodha` : "Place a real order on Zerodha"}
          </DialogTitle>
          <DialogDescription>
            Sent with the dashboard's Kite login. Contracts, quantities and prices below are Kite's own, checked by the dashboard just now.
            {basket ? " Legs go one at a time, buys first; each must fill before the next is sent, and the first refusal or unfilled leg stops the rest. Nothing is unwound automatically." : ""}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 text-body-sm">
          {statusLine}

          {phase === "review" ? (
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="grid gap-1.5">
                <Label htmlFor={`${uid}-prod`}>Product</Label>
                <Select value={product} onValueChange={(v) => setProduct(v as KiteProduct)}>
                  <SelectTrigger id={`${uid}-prod`}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="NRML">NRML (carry forward)</SelectItem>
                    <SelectItem value="MIS">MIS (intraday, squared off by Zerodha)</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="grid gap-1.5">
                <Label htmlFor={`${uid}-type`}>Order type</Label>
                <Select value={legs[0]?.order_type ?? "LIMIT"} onValueChange={(v) => setTypeAll(v as KiteOrderType)}>
                  <SelectTrigger id={`${uid}-type`}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {(basket ? BASKET_TYPES : SINGLE_TYPES).map((t) => (
                      <SelectItem key={t} value={t}>
                        {TYPE_LABEL[t]}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>
          ) : null}

          <ul className="divide-y divide-separator overflow-hidden rounded-tile border border-separator" aria-label="Orders">
            {legs.map((l, i) => {
              const p = preview?.legs[i];
              const step = steps[i];
              const tick = p?.tick_size ?? 0.05;
              const needsPrice = l.order_type === "LIMIT" || l.order_type === "SL";
              const needsTrigger = l.order_type === "SL" || l.order_type === "SL-M";
              return (
                <li key={`${contractLabel(l)}-${l.side}-${i}`} className="space-y-1.5 px-3 py-2">
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <span className="min-w-0">
                      <Badge variant={l.side === "BUY" ? "bullish" : "bearish"}>{l.side}</Badge>{" "}
                      <span className="font-semibold">{p?.tradingsymbol ?? contractLabel(l)}</span>
                    </span>
                    <span className="font-num tabular-nums text-muted-foreground">
                      {l.lots} lot{l.lots === 1 ? "" : "s"}
                      {p?.lot_size ? ` × ${p.lot_size} = ${formatNumberIN(l.lots * p.lot_size)}` : ""}
                      {p?.ltp ? ` · LTP ${formatNumberIN(p.ltp, 2)}` : ""}
                    </span>
                  </div>
                  {phase === "review" && (needsPrice || needsTrigger) ? (
                    <div className="flex flex-wrap items-center gap-2">
                      {needsPrice ? (
                        <label className="flex items-center gap-1.5">
                          <span className="text-muted-foreground">Price</span>
                          <Input
                            type="number"
                            inputMode="decimal"
                            step={tick}
                            min={tick}
                            className="h-8 w-28"
                            value={l.price ?? ""}
                            onChange={(e) => setLeg(i, { price: e.target.value === "" ? undefined : Number(e.target.value) })}
                            aria-label={`Limit price for ${p?.tradingsymbol ?? contractLabel(l)}`}
                          />
                        </label>
                      ) : null}
                      {needsTrigger ? (
                        <label className="flex items-center gap-1.5">
                          <span className="text-muted-foreground">Trigger</span>
                          <Input
                            type="number"
                            inputMode="decimal"
                            step={tick}
                            min={tick}
                            className="h-8 w-28"
                            value={l.trigger_price ?? ""}
                            onChange={(e) => setLeg(i, { trigger_price: e.target.value === "" ? undefined : Number(e.target.value) })}
                            aria-label={`Trigger price for ${p?.tradingsymbol ?? contractLabel(l)}`}
                          />
                        </label>
                      ) : null}
                      {p?.band ? (
                        <span className="text-micro text-muted-foreground">
                          allowed {formatNumberIN(p.band[0], 2)}–{formatNumberIN(p.band[1], 2)}
                        </span>
                      ) : null}
                    </div>
                  ) : null}
                  {phase === "review" && p && !p.ok ? (
                    <p className="flex items-start gap-1.5 text-bearish">
                      <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" /> {p.message}
                    </p>
                  ) : null}
                  {step ? <StepLine step={step} /> : null}
                </li>
              );
            })}
          </ul>

          {phase === "review" ? (
            <>
              {previewErr ? (
                <p className="flex items-start gap-1.5 text-bearish">
                  <XCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" /> {previewErr}
                </p>
              ) : null}
              {preview?.margin ? (
                <p className="text-muted-foreground">
                  {preview.margin.error ? (
                    <>Kite margin unavailable: {preview.margin.error}</>
                  ) : (
                    <>
                      Kite margin <span className="font-num font-semibold text-foreground">{formatINR(preview.margin.initial ?? NaN, { decimals: 0 })}</span>
                      {preview.margin.final !== null && preview.margin.final !== undefined && preview.margin.final !== preview.margin.initial ? (
                        <>
                          {" "}
                          · with your open positions <span className="font-num font-semibold text-foreground">{formatINR(preview.margin.final, { decimals: 0 })}</span>
                        </>
                      ) : null}
                    </>
                  )}
                </p>
              ) : null}
              {blockers.length ? (
                <ul className="space-y-1 rounded-tile border border-warning/30 bg-wash-warning/60 px-3 py-2 text-foreground">
                  {blockers.map((b) => (
                    <li key={b} className="flex items-start gap-1.5">
                      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
                      <span>
                        {b}
                        {b.startsWith("Live trading is off") ? (
                          <>
                            {" "}
                            <Link to="/broker-settings" className="text-primary-ink underline underline-offset-4" onClick={() => onOpenChange(false)}>
                              Open Broker settings
                            </Link>
                          </>
                        ) : null}
                      </span>
                    </li>
                  ))}
                </ul>
              ) : null}
              <label className="flex items-center gap-2 rounded-tile border border-bearish/40 bg-bearish/5 px-3 py-2 font-medium">
                <Checkbox checked={ack} onCheckedChange={(c) => setAck(c === true)} />
                {basket ? `These are ${n} real orders with real money.` : "This is a real order with real money."}
              </label>
            </>
          ) : null}

          {outcome ? (
            <p
              className={cn(
                "flex items-start gap-1.5 rounded-tile border px-3 py-2",
                outcome.tone === "ok" ? "border-bullish/40 text-bullish" : outcome.tone === "warn" ? "border-warning/40 bg-wash-warning/60 text-foreground" : "border-bearish/40 text-bearish",
              )}
              role="status"
            >
              {outcome.tone === "ok" ? <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" /> : <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />}
              {outcome.text}
            </p>
          ) : null}
        </div>

        <DialogFooter className="gap-2">
          {phase === "done" ? (
            <>
              <Button asChild variant="outline">
                <Link to="/orders" onClick={() => onOpenChange(false)}>
                  Orders page
                </Link>
              </Button>
              <Button onClick={() => onOpenChange(false)}>Close</Button>
            </>
          ) : (
            <>
              <Button variant="outline" onClick={() => onOpenChange(false)} disabled={phase === "sending"}>
                Cancel
              </Button>
              <Button className="bg-bearish text-bearish-foreground hover:bg-bearish/90" disabled={!canSend} onClick={() => void send()}>
                {phase === "sending" ? <Loader2 className="animate-spin" aria-hidden="true" /> : null}
                {basket ? `Place ${n} real orders` : "Place real order"}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function StepLine({ step }: { step: BasketStep }) {
  switch (step.state) {
    case "waiting":
      return (
        <p className="flex items-center gap-1.5 text-muted-foreground">
          <CircleDashed className="h-3.5 w-3.5" aria-hidden="true" /> Waiting
        </p>
      );
    case "sending":
      return (
        <p className="flex items-center gap-1.5 text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> Sending…
        </p>
      );
    case "sent":
      return (
        <p className="flex items-center gap-1.5 text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" /> Sent as {step.orderId}, waiting for Kite…
        </p>
      );
    case "done":
      return (
        <p className="flex items-center gap-1.5 text-bullish">
          <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" /> Filled {formatNumberIN(step.order.filled_quantity ?? step.order.quantity)} @ {formatNumberIN(step.order.average_price ?? 0, 2)} · {step.orderId}
        </p>
      );
    case "open":
      return (
        <p className="flex items-center gap-1.5 text-warning">
          <AlertTriangle className="h-3.5 w-3.5" aria-hidden="true" /> {step.order?.status ?? "Unconfirmed"} at your price · {step.orderId}
        </p>
      );
    case "failed":
      return (
        <p className={cn("flex items-start gap-1.5", step.uncertain ? "text-warning" : "text-bearish")}>
          <XCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" /> {step.orderId ? `${step.orderId}: ` : ""}
          {step.message}
        </p>
      );
    case "skipped":
      return <p className="text-muted-foreground">Not sent (the basket stopped earlier).</p>;
  }
}
