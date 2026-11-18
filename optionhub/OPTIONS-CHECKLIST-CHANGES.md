# Changes made in options-checklist

This folder is **Option Hub by Mr. Chartist**, upstream
https://github.com/MrChartist/india-s-best-option-hub, imported at commit `18e1d52` as a git
subtree. **This copy has been changed** (Option Hub Personal Use Licence 1.0, section 2.4(b)). The
licence files (`LICENSE`, `NOTICE.md`) are kept unchanged in this folder. The visible branding was replaced
at the owner's direction (#115, below).

It is built and run by the options-checklist dashboard (`option_hub.py`) for its owner's personal
use only, behind an owner-only lock.

## Changes

| Where | What changed |
|---|---|
| `server/dashboard.mjs` (new) | Dashboard mode, on when `HUB_FEED_TOKEN` is set by the dashboard. It answers Kite reads from the dashboard's own Kite login, routes NSE website reads through the dashboard's NSE session, and adds previous-session OI from NSE's bhavcopy. Off otherwise: upstream behaviour. |
| `server/brokers/zerodhaClient.mjs` | `kiteFetch` asks the dashboard instead of `api.kite.trade` in dashboard mode (read-only for now: no orders, portfolio, funds or history). The error hints point to the dashboard login. |
| `server/brokers/zerodha.mjs` | The option chain gets `previous_oi` / `previous_close_price` per leg in dashboard mode, so OI change and build-up work. Kite quotes carry no previous OI. |
| `proxy-server.mjs` | In dashboard mode `nseJson` reads www.nseindia.com through the dashboard, because NSE's bot wall refuses Node's client from a datacenter IP. There is also no cookie warm-up. |
| `src/lib/brokerConfig.ts` | When built with `VITE_DASHBOARD_KITE=1`, Zerodha is a built-in saved broker (the dashboard's login), never written to browser storage. |

### HeroZero terminal (#115)

This copy is rebranded as **HeroZero** and is the main shell for the options-checklist dashboard.

| Where | What changed |
|---|---|
| `src/lib/desk.ts`, `src/pages/Desk.tsx`, `src/components/desk/*` (new) | The dashboard's own tabs (Dashboard, Positions, By Strategy, OI Flow, Paper Trades, Cash Puts) form a "HeroZero" group at the top of the sidebar, under `/desk/*`. One persistent iframe of the dashboard in embed mode (`?embed=1`) is switched by postMessage, and reloads only when the theme flips (`hub-dark` / `hub-light`). On when `VITE_DASHBOARD_URL` is set. |
| `src/components/desk/DeskAccountChip.tsx`, `server/dashboard.mjs`, `proxy-server.mjs` | Toolbar chip: Kite available margin, today's net P&L and open positions from the dashboard's `/account`, via `GET /api/dashboard/account` (cached 20 s). When logged out it shows "Log in to Kite". |
| `src/components/brand/HeroZeroLogo.tsx` (new), `public/*` icons | HeroZero mark and lockup (orange accent) in the sidebar, toolbar, palette, 404, onboarding and PNG export. favicon/PWA icons redrawn. |
| Brand removal | Sidebar "More from …" block and socials, footer site/GitHub links, command-palette brand actions, the ecosystem page (deleted) and its route and redirects, cross-promotion links and the partner banner (render nothing), NISMExams co-branding in Learn, the "Report on GitHub" error link, page titles (`· HeroZero`), HTML head and manifest name, JSON-LD author/publisher/repository. His "Dashboard" is now "Market dashboard" and "Positions" is "Position tracker" (ours take the plain names). |
| Tests | `brand.test.tsx`, `seo.test.ts` and `learn.test.tsx` updated for HeroZero; new `src/lib/desk.test.ts`. |

### Learn and Ecosystem removed (#116)

- **Learn section deleted:** the `/learn` and `/learn/:slug` pages, lessons, quizzes, simulators, glossary, progress tracker, the sidebar group and the onboarding step. The ⓘ metric popovers stay, without "Learn more" links, at `components/metrics/MetricHelp.tsx` and `content/metrics/*`. The strategy panel lost its "Lesson" link.
- **Ecosystem code deleted:** the cross-promotion component and its call sites, the social and outbound link components, and his product/ecosystem/social link data. `lib/brand/links.ts` is now just the HeroZero names.
- Exports and alerts are tagged `herozero-*`. The "report on GitHub" helper is gone. Participants' FAQ heading is now "How to read this".

### Real Zerodha orders through the dashboard (#123)

Live orders go through the dashboard's own Kite login. This app never holds the Kite token. The dashboard (`kite_orders.py`) re-checks every order: NIFTY/BANKNIFTY options and futures only, at most 25 lots, quantity from Kite's lot size, a price band around LTP, a daily cap, a halt switch, and an intent id that makes each attempt idempotent.

| Where | What changed |
|---|---|
| `server/dashboard.mjs`, `proxy-server.mjs` | `GET/POST /api/dashboard/orders?op=status\|book\|order\|log\|preview\|place\|modify\|cancel\|halt` forwards to the dashboard's `/hub/orders/*` with the feed token. POST needs an allowed Origin (or a loopback caller) and the `x-hz-orders: 1` header. A send that gets no answer says the order MAY have gone through. |
| `src/lib/kiteOrders.ts` (new) | Client: preview, place, modify and cancel. `placeKiteOrder` and `modifyKiteOrder` require a `LiveArmToken` (liveArm.ts). Baskets go one leg at a time, buys first, and each leg must report COMPLETE before the next is sent. The first refusal, rejection or unfilled leg stops the basket, with no automatic unwind. |
| `src/components/live/LiveOrderDialog.tsx` (new) | Real-order confirmation. It shows Kite's contract, quantity, LTP, rule verdicts and Kite margin. Product and order type (LIMIT / MARKET / SL / SL-M) are chosen here. Nothing is sent until the real-money box is ticked. Intent ids are minted when the dialog opens. |
| `src/pages/PaperTrading.tsx`, `src/components/paper/OrderTicket.tsx` | (Replaced by the Live Trading page in #124.) A **Paper / Live · Zerodha** switch, which starts on Paper at every visit. In Live mode the ticket and the basket open the confirmation, and the paper-only controls (stop/target, max loss, paper guardrails, naked-sell hiding) step aside. The paper account is untouched. |
| `src/pages/Orders.tsx`, `src/components/live/KiteOrders.tsx` (new) | In our build the Orders page is the Kite order book, with cancel, modify (price / trigger / lots) and the server's **Halt all live orders** switch. Cancelling works while halted. |
| `src/features/one-cliq/hooks/useQuickOrder.ts`, `OneCliqTerminal.tsx`, `components/TradingLockBanner.tsx` | Armed live keys send Kite orders (MARKET; market protection as an aggressive LIMIT; LIMIT at LTP). They respect the trading lock, which was display-only. A fresh install's "lock file missing" engine-fault lock can be cleared from the banner in our build only. |
| `src/components/LiveTradingToggle.tsx` | Copy for the dashboard build: Zerodha via the dashboard, and Kite's static-IP rule. |
| Tests | `server/dashboard.test.mjs` (orders forwarding), `src/lib/kiteOrders.test.ts`, `src/components/live/LiveMode.test.tsx`. |

### Live Trading page, separate from Paper Trading (#124)

The owner found it misleading to place real orders from a page named Paper Trading, so live trading now has its own page.

| Where | What changed |
|---|---|
| `src/pages/LiveTrading.tsx` (new), `src/pages/PaperTrading.tsx`, `src/lib/routes.ts`, `src/lib/routeIcons.ts` | The page body is now `TradingDesk({ mode })`. `/paper-trading` is paper only again, with no Live switch, plus a "Live Trading" link in our build. `/trade` ("Live Trading", first in the Trade group, our build only) has the same ticket and basket sending real orders, no paper account, and today's Kite orders beside the basket. Outside our build `/trade` redirects to Paper Trading. |
| `src/components/option-chain/basket.ts`, `BasketDrawer.tsx`, `src/pages/StrategyBuilder.tsx`, `src/pages/Optimizer.tsx`, `src/components/optimizer/*`, `src/components/contract/ContractPanels.tsx` | A **Trade live** button sits beside every "Paper trade". It stages the legs at `/trade?legs=`, and nothing is sent until you confirm there. There is a new `"live"` send target. |
| `src/components/live/KiteOrders.tsx` | `KiteOrderBook` (status, halt, book, cancel, modify) is split from the Orders page so Live Trading can show it. |
| `vite.config.ts` | Fixed the "VITE_SITE_URL is not set" warning, whose unescaped quotes turned the message into `NaN` and failed builds without VITE_SITE_URL (#116 slip; production sets it). |

`LICENSE`, `NOTICE.md` and `COMMERCIAL-LICENSE.md` are kept in this folder, along with the copyright notes in source comments.

Taking upstream updates:

```bash
git subtree pull --prefix=optionhub https://github.com/MrChartist/india-s-best-option-hub.git <ref> --squash
```

Then update the commit above.
