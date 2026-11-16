# Changes made in options-checklist

This folder is **Option Hub by Mr. Chartist**, upstream
https://github.com/MrChartist/india-s-best-option-hub, imported at commit `18e1d52` as a git
subtree. **This copy has been changed** (Option Hub Personal Use Licence 1.0, section 2.4(b)). The
licence (`LICENSE`), notices (`NOTICE.md`) and every copyright and "Required Notice" line are kept
unchanged; they must stay (sections 2.4(a) and 3.7).

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

Taking upstream updates:

```bash
git subtree pull --prefix=optionhub https://github.com/MrChartist/india-s-best-option-hub.git <ref> --squash
```

Then update the commit above.
