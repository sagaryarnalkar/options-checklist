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

`LICENSE`, `NOTICE.md` and `COMMERCIAL-LICENSE.md` are kept in this folder, along with the copyright notes in source comments.

Taking upstream updates:

```bash
git subtree pull --prefix=optionhub https://github.com/MrChartist/india-s-best-option-hub.git <ref> --squash
```

Then update the commit above.
