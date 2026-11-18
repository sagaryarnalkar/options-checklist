/**
 * options-checklist: Live Trading (/trade) — the order ticket and basket of Paper Trading, sending REAL Zerodha orders
 * through the dashboard's Kite login (pages/PaperTrading.tsx TradingDesk mode="live"). Outside our build there is
 * no live path, so the route falls back to Paper Trading.
 */
import { Navigate, useLocation } from "react-router-dom";
import { kiteOrdersAvailable } from "@/lib/kiteOrders";
import { TradingDesk } from "./PaperTrading";

export default function LiveTrading() {
  const { search } = useLocation();
  if (!kiteOrdersAvailable()) return <Navigate to={`/paper-trading${search}`} replace />;
  return <TradingDesk mode="live" />;
}
