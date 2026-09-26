# DraftKings integration

| Feature | Status |
| --- | --- |
| **Odds** | Working — via The Odds API, not DraftKings (`oddsapi/`) |
| **Login** | Built — email/password + MFA, driven through a real browser |
| **Bet sync** | Built — reads through the authenticated browser session |
| **Bet-slip links** | Not possible. DraftKings has no pre-placement deep link. |

## Why a browser, and not an HTTP client

DraftKings' auth endpoint — `POST accounts.draftkings.com/v1/auth/login/otp/initiate`
— sits behind Akamai Bot Manager. Loading the login page seeds an `_abck`
cookie, but Akamai only promotes it to *validated* once its sensor JS has
POSTed telemetry: device fingerprint, timing, input events. No HTTP client can
produce that.

Measured, rather than assumed:

| Attempt | Result |
| --- | --- |
| Bare POST to the auth endpoint | `403 AkamaiGHost` |
| Primed cookie jar (login page first), browser headers, correct `Origin` | `403 AkamaiGHost`, `_abck` still `~-1~` |
| Playwright **headless-shell** (its default) | `403` — on the login page itself |
| Playwright **new headless** (`channel="chromium"`) | `403` |
| Playwright **headed** (`channel="chromium"`) | **`200`, form renders** |

A TLS fingerprint wouldn't change the first two: the sensor payload is the
gate, not the handshake. And Akamai rejects headless Chromium in *both* modes,
which is why the browser runs headed under Xvfb rather than headless.

The user experience is unchanged from FanDuel's — email, password and an MFA
code in Sharp Edge's own form. The browser is invisible to them, and it's a
real browser performing a real login with their own credentials.

## How it fits together

```
books.py  ──►  draftkings/auth.py     login, MFA, session persistence
               draftkings/client.py   bet history
               draftkings/browser.py  Chromium lifecycle + context registry
```

`DraftKingsAuth` satisfies the `BookAuth` protocol in `books.py`, so the
routes, per-book session persistence and the settings UI needed no changes.

**The browser context outlives one request.** FanDuel's MFA only carries a
short-lived device token between two calls; here the half-finished login lives
in a browser, held open between `login()` and `submit_mfa_code()`. That needs
an idle reaper — a leaked context is a leaked Chromium process. The window is
10 minutes, sized to DraftKings' code lifetime rather than to politeness.

**The session is cookies, not a bearer token.** So `token` returns a digest
(status responses must not leak live cookies), expiry is read from the
longest-lived session cookie rather than a JWT `exp` — with `expiry_assumed`
reported honestly when none carries one — and `can_refresh` means "the stored
cookies can resume this". The password is never persisted, same rule as
FanDuel.

## Verified vs. still soft

**Verified against the live page:** the login URL, that it's a single form
(not stepped), the email and password field ids, and the submit button.

That last one caught a real bug. The page carries **two** `type="submit"`
buttons and "Sign Up" comes first in the DOM, so a bare
`button[type="submit"]` would have clicked Sign Up — sending the user into
registration with no error to explain it. The text-matched selector has to win;
see the comment on `_SUBMIT_SELECTORS`.

**Still soft — the MFA step and the bet payload.** Neither could be reached
without completing a real login.

- The MFA field is handled both as one input and as six single-character boxes
  (filling the first box with all six characters silently drops five), plus
  page-text markers. Whichever DraftKings uses, one of them should match.
- `client.py` does **not** guess the bet-history endpoint. It opens My Bets in
  the authenticated session and keeps whatever bets-shaped JSON the page
  fetches — the endpoint is discovered rather than declared. `normalize_bet`
  then maps across plausible field names, and every payload is kept in
  `raw_json` regardless, so a wrong guess costs a column rather than a row.

**On the first real sync, check the logs.** It prints the discovered payload
count and the keys of the first bet:

```
draftkings: captured bets payload from https://...
draftkings: N payloads -> M bets (K after dedupe)
draftkings: first bet keys = [...]
```

That's the information needed to tighten `normalize_bet` against the real
shape. If it logs `no bets recognised` while My Bets clearly showed history,
`_looks_like_bet` needs the actual field names.

## Operational

- **Image**: +~400MB (Chromium + Xvfb). For a FanDuel-only build, drop that
  Dockerfile layer and remove `playwright` from `requirements.txt` — the
  DraftKings routes then answer `501` naming the missing browser, and nothing
  else changes.
- **Memory**: a headed Chromium is ~300–500MB while a login or sync runs, not
  resident. The backend's 4Gi limit has room, but it lands on top of the
  pybaseball scrape, so watch it if both run at once.
- **`DRAFTKINGS_HEADLESS`** defaults to `false` deliberately. Setting it true
  gets you blocked, not merely unsupported.
- **`/dev/shm`** is 64MB in Kubernetes and Chromium will exhaust it mid-page;
  `--disable-dev-shm-usage` is passed for that reason.

## What isn't possible

**"Open in DraftKings bet slip."** DraftKings generates a share link only
*after* a bet is placed; there's no public pre-placement equivalent to
FanDuel's `addToBetslip`, so a card can't be handed over loaded. The honest
fallback, if wanted, is a link to the event page. `bundle.betslip_url` stays
FanDuel-only.

**Odds from DraftKings directly.** Every host and path of its public board
answers `403` at Akamai's edge. Prices come through the aggregator, which
turned out better anyway: one call returns every book, so the board can shop
lines and devig two-sided quotes properly.
