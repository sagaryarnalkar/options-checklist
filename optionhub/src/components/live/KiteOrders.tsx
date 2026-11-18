/**
 * options-checklist: the Orders page in our build — today's REAL Zerodha order book from the dashboard's Kite
 * login, with cancel / modify for open orders and the server's HALT switch (stops every new live order and every
 * modification at once; cancelling keeps working).
 */
import { useEffect, useId, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { AlertTriangle, ListOrdered, Loader2, OctagonX, Pencil, Play, RefreshCw, X } from "lucide-react";
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { PageHeader } from "@/components/PageHeader";
import { EmptyState } from "@/components/positions/ui";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { formatNumberIN } from "@/lib/options";
import { armLive } from "@/lib/liveArm";
import {
  cancelKiteOrder,
  fetchKiteOrderBook,
  fetchKiteOrdersStatus,
  modifyKiteOrder,
  newIntentId,
  previewKiteOrders,
  setKiteOrdersHalted,
  type KiteOrderRow,
  type KitePreviewLeg,
} from "@/lib/kiteOrders";
import { cn } from "@/lib/utils";

const OPEN = new Set(["OPEN", "TRIGGER PENDING", "OPEN PENDING", "VALIDATION PENDING", "PUT ORDER REQ RECEIVED", "MODIFY PENDING", "MODIFY VALIDATION PENDING", "AMO REQ RECEIVED"]);

function statusTone(s: string): string {
  const u = s.toUpperCase();
  if (u === "COMPLETE") return "text-bullish border-bullish/40";
  if (u === "REJECTED") return "text-bearish border-bearish/40";
  if (u === "CANCELLED") return "text-muted-foreground border-muted-foreground/40";
  return "text-warning border-warning/40";
}

function timeOf(ts?: string | null): string {
  if (!ts) return "—";
  const m = /(\d{2}:\d{2}:\d{2})/.exec(ts);
  return m ? m[1] : ts;
}

export function KiteOrders() {
  useDocumentTitle("Orders");
  const qc = useQueryClient();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [editing, setEditing] = useState<KiteOrderRow | null>(null);
  const [confirmResume, setConfirmResume] = useState(false);

  const status = useQuery({ queryKey: ["kite-orders-status"], queryFn: fetchKiteOrdersStatus, refetchInterval: 15_000, retry: 1 });
  const book = useQuery({ queryKey: ["kite-orders-book"], queryFn: fetchKiteOrderBook, refetchInterval: 5_000, retry: 1 });
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["kite-orders-book"] });
    void qc.invalidateQueries({ queryKey: ["kite-orders-status"] });
  };

  const halt = async (on: boolean) => {
    try {
      const r = await setKiteOrdersHalted(on);
      toast[r.halted ? "warning" : "success"](r.halted ? "Live orders HALTED: nothing new can be placed or changed. Cancelling still works." : "Live orders resumed.");
    } catch (e) {
      toast.error("Could not change the halt switch", { description: (e as Error).message });
    }
    refresh();
  };

  const cancel = async (o: KiteOrderRow) => {
    setBusyId(o.order_id);
    try {
      await cancelKiteOrder(o.order_id, newIntentId("cx"));
      toast.success(`Cancel sent for ${o.tradingsymbol} (${o.order_id})`);
    } catch (e) {
      toast.error(`Could not cancel ${o.order_id}`, { description: (e as Error).message });
    } finally {
      setBusyId(null);
      refresh();
    }
  };

  const st = status.data;
  const rows = book.data?.orders ?? [];
  const openCount = rows.filter((o) => OPEN.has(String(o.status).toUpperCase())).length;

  return (
    <div className="mx-auto w-full max-w-[1400px] space-y-4">
      <PageHeader
        icon={ListOrdered}
        eyebrow="Trade"
        title="Orders"
        description="Today's Zerodha order book, from the dashboard's Kite login. Cancel or change open orders here; place them from Paper Trading in Live mode or from 1Cliq."
        actions={
          <Button variant="outline" size="sm" onClick={refresh} disabled={book.isFetching}>
            <RefreshCw className={cn(book.isFetching && "animate-spin")} aria-hidden="true" /> Refresh
          </Button>
        }
      />

      <Card className={st?.halted ? "border-bearish/50 bg-bearish/5" : undefined}>
        <CardContent className="flex flex-wrap items-center gap-2 py-3 text-body-sm">
          {status.error ? (
            <span className="text-bearish">{(status.error as Error).message}</span>
          ) : !st ? (
            <span className="flex items-center gap-1.5 text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" /> Checking the dashboard…
            </span>
          ) : (
            <>
              <Badge variant={st.enabled ? "bullish" : "bearish"}>{st.enabled ? "Live orders on" : "Live orders OFF on the server"}</Badge>
              {st.halted ? <Badge variant="bearish">HALTED</Badge> : null}
              <Badge variant={st.logged_in ? "neutral" : "bearish"}>{st.logged_in ? "Kite logged in" : "Kite not logged in"}</Badge>
              {st.sent_today !== null ? (
                <Badge variant="neutral">
                  {st.sent_today}/{st.rules.max_orders_per_day} placed today
                </Badge>
              ) : null}
              <span className="text-muted-foreground">
                {st.rules.underlyings.join(" & ")} options and futures · ≤ {st.rules.max_lots} lots an order
              </span>
              <span className="ml-auto">
                {st.halted ? (
                  <Button size="sm" variant="outline" onClick={() => setConfirmResume(true)}>
                    <Play aria-hidden="true" /> Resume live orders
                  </Button>
                ) : (
                  <Button size="sm" className="bg-bearish text-bearish-foreground hover:bg-bearish/90" onClick={() => void halt(true)} disabled={!st.enabled}>
                    <OctagonX aria-hidden="true" /> Halt all live orders
                  </Button>
                )}
              </span>
            </>
          )}
        </CardContent>
        {st && !st.enabled ? (
          <CardContent className="pt-0 text-body-sm text-muted-foreground">
            To turn live orders on, add <code className="rounded bg-control-track px-1">KITE_ORDERS=on</code> to the dashboard's .env and restart it. Kite accepts orders only from the IP
            registered on your Kite Connect app (IP Whitelist).
          </CardContent>
        ) : null}
      </Card>

      <Card>
        <CardContent className="p-0">
          {book.isLoading ? (
            <div className="flex items-center justify-center gap-2 py-12 text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin" aria-hidden="true" /> Loading Kite orders…
            </div>
          ) : book.error ? (
            <p className="flex items-start gap-1.5 px-4 py-6 text-body-sm text-bearish">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" /> {(book.error as Error).message}
            </p>
          ) : !rows.length ? (
            <EmptyState icon={<ListOrdered aria-hidden="true" />} title="No orders today">
              Orders placed today on this Kite account appear here, whichever app placed them.
            </EmptyState>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Time</TableHead>
                    <TableHead>Contract</TableHead>
                    <TableHead>Side</TableHead>
                    <TableHead className="text-right">Filled / qty</TableHead>
                    <TableHead>Type</TableHead>
                    <TableHead className="text-right">Price</TableHead>
                    <TableHead className="text-right">Avg</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead className="text-right">
                      <span className="sr-only">Actions</span>
                      {openCount ? `${openCount} open` : ""}
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((o) => {
                    const open = OPEN.has(String(o.status).toUpperCase());
                    const canEdit = open && (o.order_type === "LIMIT" || o.order_type === "SL") && o.exchange === "NFO";
                    return (
                      <TableRow key={o.order_id}>
                        <TableCell className="font-num tabular-nums">{timeOf(o.order_timestamp)}</TableCell>
                        <TableCell>
                          <span className="font-semibold">{o.tradingsymbol}</span>
                          <span className="block text-micro text-muted-foreground">
                            {o.product} · {o.order_id}
                            {o.tag ? ` · ${o.tag}` : ""}
                          </span>
                        </TableCell>
                        <TableCell>
                          <Badge variant={o.transaction_type === "BUY" ? "bullish" : "bearish"}>{o.transaction_type}</Badge>
                        </TableCell>
                        <TableCell className="text-right font-num tabular-nums">
                          {formatNumberIN(o.filled_quantity ?? 0)} / {formatNumberIN(o.quantity)}
                        </TableCell>
                        <TableCell>{o.order_type}</TableCell>
                        <TableCell className="text-right font-num tabular-nums">
                          {o.price ? formatNumberIN(o.price, 2) : "MKT"}
                          {o.trigger_price ? <span className="block text-micro text-muted-foreground">trg {formatNumberIN(o.trigger_price, 2)}</span> : null}
                        </TableCell>
                        <TableCell className="text-right font-num tabular-nums">{o.average_price ? formatNumberIN(o.average_price, 2) : "—"}</TableCell>
                        <TableCell className="max-w-[260px]">
                          <Badge variant="outline" className={statusTone(String(o.status))}>
                            {o.status}
                          </Badge>
                          {o.status_message ? <span className="mt-0.5 block text-micro text-muted-foreground">{o.status_message}</span> : null}
                        </TableCell>
                        <TableCell className="text-right">
                          {open ? (
                            <span className="inline-flex gap-1">
                              {canEdit ? (
                                <Button size="sm" variant="outline" onClick={() => setEditing(o)} disabled={busyId === o.order_id}>
                                  <Pencil aria-hidden="true" /> Modify
                                </Button>
                              ) : null}
                              <Button size="sm" variant="ghost" className="text-bearish" onClick={() => void cancel(o)} disabled={busyId === o.order_id}>
                                {busyId === o.order_id ? <Loader2 className="animate-spin" aria-hidden="true" /> : <X aria-hidden="true" />} Cancel
                              </Button>
                            </span>
                          ) : null}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      {st ? <p className="text-body-sm text-muted-foreground">{st.static_ip_note}</p> : null}

      <ModifyDialog order={editing} onClose={() => setEditing(null)} onDone={refresh} />

      <AlertDialog open={confirmResume} onOpenChange={setConfirmResume}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Resume live orders?</AlertDialogTitle>
            <AlertDialogDescription>New real orders and modifications will be accepted again from Paper Trading (Live mode), 1Cliq and this page.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep halted</AlertDialogCancel>
            <AlertDialogAction onClick={() => void halt(false)}>Resume</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function ModifyDialog({ order, onClose, onDone }: { order: KiteOrderRow | null; onClose: () => void; onDone: () => void }) {
  const uid = useId();
  const [price, setPrice] = useState("");
  const [trigger, setTrigger] = useState("");
  const [lots, setLots] = useState("");
  const [info, setInfo] = useState<KitePreviewLeg | null>(null);
  const [busy, setBusy] = useState(false);
  const [intent, setIntent] = useState("");

  useEffect(() => {
    if (!order) return;
    setPrice(order.price ? String(order.price) : "");
    setTrigger(order.trigger_price ? String(order.trigger_price) : "");
    setLots("");
    setInfo(null);
    setIntent(newIntentId("md"));
    // Kite's order book has no lot size: ask the dashboard (lot size, last price, allowed band).
    previewKiteOrders([{ tradingsymbol: order.tradingsymbol, side: order.transaction_type, order_type: "MARKET", lots: 1 }])
      .then((p) => setInfo(p.legs[0] ?? null))
      .catch(() => setInfo(null));
  }, [order]);

  const lotSize = info?.lot_size ?? 0;
  const curLots = lotSize && order ? Math.round(order.quantity / lotSize) : null;
  const isSL = order?.order_type === "SL";

  const submit = async () => {
    if (!order) return;
    const arm = armLive(false);
    if (!arm.token) {
      toast.error("Live trading is off", { description: arm.message ?? "Turn it on in Broker settings." });
      return;
    }
    setBusy(true);
    try {
      await modifyKiteOrder(
        order.order_id,
        {
          price: price ? Number(price) : undefined,
          trigger_price: isSL && trigger ? Number(trigger) : undefined,
          lots: lots ? Math.floor(Number(lots)) : undefined,
        },
        intent,
        arm.token,
      );
      toast.success(`Modification sent for ${order.tradingsymbol}`);
      onClose();
    } catch (e) {
      toast.error("Kite did not take the change", { description: (e as Error).message });
      setIntent(newIntentId("md"));
    } finally {
      setBusy(false);
      onDone();
    }
  };

  return (
    <Dialog open={!!order} onOpenChange={(o) => !o && !busy && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Modify {order?.tradingsymbol}</DialogTitle>
          <DialogDescription>
            {order ? `${order.transaction_type} ${formatNumberIN(order.quantity)} (${order.order_type}) · order ${order.order_id}` : null}
            {info?.ltp ? ` · LTP ${formatNumberIN(info.ltp, 2)}` : ""}
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-3 text-body-sm sm:grid-cols-3">
          <label className="grid gap-1.5" htmlFor={`${uid}-p`}>
            <span className="font-medium">Price</span>
            <Input id={`${uid}-p`} type="number" inputMode="decimal" step={info?.tick_size ?? 0.05} value={price} onChange={(e) => setPrice(e.target.value)} />
          </label>
          {isSL ? (
            <label className="grid gap-1.5" htmlFor={`${uid}-t`}>
              <span className="font-medium">Trigger</span>
              <Input id={`${uid}-t`} type="number" inputMode="decimal" step={info?.tick_size ?? 0.05} value={trigger} onChange={(e) => setTrigger(e.target.value)} />
            </label>
          ) : null}
          <label className="grid gap-1.5" htmlFor={`${uid}-l`}>
            <span className="font-medium">Lots</span>
            <Input id={`${uid}-l`} type="number" inputMode="numeric" min={1} step={1} value={lots} onChange={(e) => setLots(e.target.value)} placeholder={curLots ? `${curLots} (unchanged)` : "unchanged"} />
          </label>
        </div>
        {info?.band ? (
          <p className="text-micro text-muted-foreground">
            Allowed price range now {formatNumberIN(info.band[0], 2)}–{formatNumberIN(info.band[1], 2)} · lot {lotSize}
          </p>
        ) : null}
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            Back
          </Button>
          <Button onClick={() => void submit()} disabled={busy || !price}>
            {busy ? <Loader2 className="animate-spin" aria-hidden="true" /> : null} Send change
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
