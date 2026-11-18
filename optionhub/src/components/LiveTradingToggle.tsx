import { useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { Checkbox } from "@/components/ui/checkbox";
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog";
import { AlertTriangle, Radio } from "lucide-react";
import { isDashboardKiteBuild, isLiveTradingEnabled, setLiveTradingEnabled } from "@/lib/brokerConfig";

// options-checklist: in our build live orders go to Zerodha through the dashboard's Kite login (lib/kiteOrders).
const KITE = isDashboardKiteBuild();
import { toast } from "sonner";

const ACK_KEY = "optionsdesk_live_trading_ack";

function readAck(): boolean {
  try {
    return localStorage.getItem(ACK_KEY) === "true";
  } catch {
    return false;
  }
}

function writeAck(): void {
  try {
    localStorage.setItem(ACK_KEY, "true");
  } catch {
    /* storage unavailable: the acknowledgement is asked again next time */
  }
}

// Off by default, always. Real orders move real money and require a static IP
// whitelisted with Dhan. This is the account-level gate 1Cliq Trade checks
// before it may place a real order instead of a paper one.
export function LiveTradingToggle() {
  const [enabled, setEnabled] = useState(() => isLiveTradingEnabled());
  const [showAck, setShowAck] = useState(false);
  const [ackChecked, setAckChecked] = useState(false);

  const handleToggle = (next: boolean) => {
    if (!next) {
      setLiveTradingEnabled(false);
      setEnabled(false);
      toast.info("Live Trading disabled: every order is a paper trade again.");
      return;
    }
    const alreadyAcknowledged = readAck();
    if (alreadyAcknowledged) {
      setLiveTradingEnabled(true);
      setEnabled(true);
      toast.warning(KITE ? "Live Trading enabled: Paper Trading (Live mode), 1Cliq and the Orders page can now send real Zerodha orders after confirming." : "Live Trading enabled: 1Cliq Trade can now place real orders once armed.");
      return;
    }
    setShowAck(true);
  };

  const confirmAcknowledge = () => {
    writeAck();
    setLiveTradingEnabled(true);
    setEnabled(true);
    setShowAck(false);
    setAckChecked(false);
    toast.warning(KITE ? "Live Trading enabled: Paper Trading (Live mode), 1Cliq and the Orders page can now send real Zerodha orders after confirming." : "Live Trading enabled: 1Cliq Trade can now place real orders once armed.");
  };

  return (
    <Card className={enabled ? "border-bearish/40 bg-bearish/5" : "border-warning/30 bg-warning/5"}>
      <CardContent className="flex items-start gap-3 py-3">
        <AlertTriangle className={`h-5 w-5 shrink-0 mt-0.5 ${enabled ? "text-bearish" : "text-warning"}`} aria-hidden />
        <div className="text-body flex-1">
          <p className="font-medium text-foreground flex items-center gap-2">
            <span id="live-trading-label">Live trading</span>
            {enabled && <span className="inline-flex items-center gap-1 text-body-sm text-bearish"><Radio className="h-3 w-3 animate-pulse" aria-hidden /> Active</span>}
          </p>
          {KITE ? (
            <p className="text-muted-foreground text-body-sm mt-0.5">
              Off by default. Turning it on lets Paper Trading in <strong>Live · Zerodha</strong> mode, an armed 1Cliq Trade and the Orders page send <strong>real orders
              with real money</strong> to your Zerodha account through the dashboard's Kite login. Every order is checked again by the dashboard (NIFTY/BANKNIFTY only, at
              most 25 lots, a price band around the last price) and Kite takes orders only from the dashboard server's registered static IP.
            </p>
          ) : (
          <p className="text-muted-foreground text-body-sm mt-0.5">
            Off by default: 1Cliq Trade fills are paper only. Enabling this lets an armed 1Cliq Trade place <strong>real orders with real money</strong> through
            your saved Dhan account. Dhan's order APIs require your proxy's IP to be a static IP registered with Dhan, so most users running
            this locally on a home connection will have orders rejected until that is set up.
          </p>
          )}
        </div>
        <Switch checked={enabled} onCheckedChange={handleToggle} aria-labelledby="live-trading-label" />
      </CardContent>

      <AlertDialog open={showAck} onOpenChange={(open) => { if (!open) { setShowAck(false); setAckChecked(false); } }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2 text-bearish">
              <AlertTriangle className="h-4 w-4" aria-hidden /> Enable live trading?
            </AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-3 text-body text-foreground">
                <p>{KITE ? <>This lets the terminal send <strong>real orders</strong> to your Zerodha account through the dashboard, after a confirmation (or, in 1Cliq, once armed).</> : <>This lets 1Cliq Trade, once armed, place <strong>real orders</strong> with your saved Dhan account instead of paper trades.</>}</p>
                <ul className="list-disc list-inside text-body-sm text-muted-foreground space-y-1">
                  <li>Real money — a mistake here is a real trade, not a simulation.</li>
                  <li>{KITE ? "Kite accepts orders only from the static IP registered on your Kite Connect app. Otherwise Kite rejects them and shows its reason." : "Requires a static IP whitelisted with Dhan for order APIs. If your setup doesn't have one, orders will be rejected (Dhan's exact reason will be shown)."}</li>
                  <li>You can turn this off again at any time.</li>
                </ul>
                <label className="flex items-center gap-2 text-body-sm pt-1">
                  <Checkbox checked={ackChecked} onCheckedChange={(c) => setAckChecked(c === true)} />
                  I understand this places real orders with real money.
                </label>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => setAckChecked(false)}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={!ackChecked}
              onClick={confirmAcknowledge}
              className="bg-bearish text-bearish-foreground hover:bg-bearish/90"
            >
              Enable Live Trading
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  );
}
