# DraftKings integration — status, and what's left to capture

Four things were wanted: login, bet sync, odds display, and "Open in
DraftKings" bet-slip links. Two are done, one is impossible, and one needs a
capture from a browser.

| Feature | Status |
| --- | --- |
| **Odds display** | **Done** — via the aggregator, not DraftKings. See below. |
| **Bet-slip links** | **Not possible.** DraftKings has no pre-placement deep link. |
| **Login** | Plumbing built; needs a captured request shape. |
| **Bet sync** | Plumbing built; needs a captured request shape. |

## What's already working

DraftKings prices are on the board now, through The Odds API
(`backend/src/sharp_edge/oddsapi/`), which licenses them. That route was taken
because DraftKings' own board is unreachable: every host and path answers `403`
from `AkamaiGHost` before the request reaches DraftKings — the v5 eventgroups
endpoint, the newer sportscontent endpoint, and the per-state hosts, from a dev
machine, a datacenter egress, and Anthropic's fetch egress alike. That is edge
bot protection and an HTTP client cannot negotiate with it.

Going through an aggregator turned out better than the original plan:

- **One call returns every book**, so line shopping is free. The board now has
  a *Best* column naming whichever book pays most for a leg.
- **Quotes are two-sided**, so the margin is measurable. `pricing.devig_probability`
  has always carried an `overround` argument defaulting to 1.0 with a comment
  admitting a single FanDuel runner can't be devigged properly; over/under
  props fix that.

Set `ODDS_API_KEY` (free key at the-odds-api.com) to switch it on. Without one
the board shows FanDuel alone, exactly as before. **Quota is the real
constraint** — player props cost one credit per market per game, so a 15-game
slate is 15–30 credits against a 500-credit free month. `oddsapi/client.py`
holds a reserve back (`QUOTA_FLOOR`) so the board degrades to stale prices
rather than blanking when the month runs low.

## What is not possible

**"Open in DraftKings bet slip" cannot be built.** DraftKings generates a share
link only *after* a bet is placed; there is no public pre-placement equivalent
to FanDuel's `addToBetslip`, and industry tooling treats these as book-specific
for that reason. The honest fallback, if it's wanted later, is a link to the
event page — it saves the navigation but not the selection. `bundle.betslip_url`
stays FanDuel-only, and `books.py` records this next to the code.

## What still needs capturing

Login and bet sync. The FanDuel modules were built from a real browser capture
and say so; the same is needed here, and guessing at field names would produce
a login that fails in a way no error message explains.

### The geolocation plugin is not the blocker it looks like

DraftKings asks for a location plugin that has no Linux build. That gates
**wagering**, not reading — syncing settled bets places nothing, so the backend
never needs it. The plugin is only in the way of getting *through the UI* to
capture the request.

Cleanest way around it on Linux: **capture from an Android phone over USB.**
Mobile uses GPS instead of the desktop plugin, and `chrome://inspect` on the
desktop gives you the phone's full DevTools Network tab.

1. Phone: Settings → Developer options → USB debugging on.
2. Plug in, accept the debugging prompt.
3. Desktop Chrome → `chrome://inspect/#devices` → **inspect** under the phone's
   DraftKings tab.
4. Network tab → **Preserve log**, filter **Fetch/XHR**.

On iPhone, Safari Web Inspector needs macOS, so it's mitmproxy with the phone
routed through the desktop instead — more setup, same result.

### Before you start

- **Never paste your password anywhere it lands in a file.** Redact it in the
  request body; the field *name* is what matters.
- **A captured session token is a live credential.** Scrub the values, keep the
  key names and rough shape (`eyJ…`, three dot-separated parts) — that is what
  the parser is written against.
- **Structure beats values.** `"betId": "REDACTED"` is perfect.
- Nothing from the capture should be committed.

### 1. Login → the DraftKings auth adapter

Log in on the phone, then capture the request carrying your credentials and any
follow-up that completes an MFA/device challenge:

- Full URL and method
- Request headers — especially `Authorization`, any `x-*` header, and any
  stable per-device id. FanDuel's `X-Installation-Id` is what keeps a verified
  device verified; whether DraftKings has an equivalent decides if every login
  re-triggers MFA.
- Request body, password redacted
- Response status and body, token values scrubbed

**The one question that shapes everything:** does the session come back as a
**bearer token in the body** or as a **`Set-Cookie`**? FanDuel's is a token,
and `FanDuelAuth`'s whole design — refresh tokens, `ensure_token`, serialising
to `sync_state` — rests on that. A cookie-based session is built differently.
The `BookAuth` protocol in `backend/src/sharp_edge/books.py` is the contract
the adapter has to satisfy either way.

### 2. Bet history → the DraftKings client

Go to **My Bets → Settled**, scroll to trigger a second page.

- The bets request URL, method, and every query param
- How paging is expressed (FanDuel uses `fromRecord`/`toRecord` plus a
  `moreAvailable` flag)
- One settled **win**, one settled **loss**, and one **parlay**, as full
  response JSON with ids and amounts redacted but keys intact

The parlay matters most: `normalize_bet` flattens legs into our canonical
schema, and leg nesting is the part that cannot be guessed.

### Handing it over

A HAR export covers everything, **but it contains your cookies and tokens
verbatim** — scrub it, or paste per-request summaries:

```
### <what this request is>
URL:      POST https://…
Headers:  x-foo: bar
          authorization: Basic <redacted, ~40 chars base64>
Body:     {"email": "…", "password": "<redacted>", "…": "…"}
Status:   200
Response: {"sessions": [{"id": "<redacted jwt, eyJ… 3 parts>", …}]}
```

## Where it drops in

The plumbing is built and FanDuel already runs through it, so wiring
DraftKings is filling two slots in `backend/src/sharp_edge/books.py`:

```python
"draftkings": Book(
    key="draftkings", name="DraftKings",
    auth_factory=...,      # currently None -> routes answer 501
    state_factory=...,
    client_factory=...,
    odds_api_key="draftkings",
),
```

Until then `/auth/draftkings/login` and `/bets/sync?book=draftkings` return
**501** naming this file, the settings panel shows DraftKings as "odds only",
and nothing else changes. Routes, per-book session persistence, and the
frontend are already book-agnostic.
