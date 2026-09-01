# Sharp Edge

Sports betting analytics platform with FanDuel integration and Claude-powered analysis.

## Architecture

```
Frontend (Svelte)  ──→  FastAPI Backend  ──→  SQLite (dev) / PostgreSQL via CNPG (prod)
                            │
Chat Panel  ──→  /chat  ──→ Anthropic API w/ tool use (same backend functions)
                            │
Claude Desktop  ──→  MCP Server (stdio)  ──→  same core logic
```

## Quick Start

```bash
cd backend
cp ../.env.example ../.env  # Fill in your credentials
pip install -e .

# Import existing Pikkit data
python -c "
import asyncio
from sharp_edge.db import create_database
async def go():
    db = await create_database('sqlite:///~/.sharp-edge/bets.db')
    count = await db.import_pikkit_csv('/path/to/transactions.csv')
    print(f'Imported {count} bets')
    await db.close()
asyncio.run(go())
"

# Run the API server
sharp-edge-api

# Or run the MCP server (for Claude Desktop/Code)
sharp-edge-mcp
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/auth/login` | FanDuel login (email/password) |
| POST | `/auth/token` | Set manual JWT from browser |
| GET | `/auth/status` | Check FanDuel auth state |
| POST | `/bets/sync` | Pull history from FanDuel |
| POST | `/bets/import` | Import Pikkit CSV |
| POST | `/bets/history` | Query bets with filters |
| GET | `/bets/stats` | Aggregate stats |
| GET | `/bets/breakdown/{dim}` | P/L by league/book/type |
| GET | `/bets/calendar` | Daily P/L for heatmap |
| POST | `/bets/score` | Score a proposed bet |
| GET | `/bets/insights` | AI-generated insights |
| GET | `/nfl/screen` | This week's NFL prop board, priced |
| GET | `/nfl/screen/status` | NFL board warm-up state |
| POST | `/chat` | Claude chat with tools |

## MCP Tools (Claude Desktop/Code)

| Tool | Description |
|------|-------------|
| `se_stats` | Aggregate stats with filters |
| `se_breakdown` | P/L by dimension |
| `se_score_bet` | Score a proposed bet |
| `se_history` | Query bet history |
| `se_insights` | Generate insights |
| `se_sync_fanduel` | Pull from FanDuel |
| `se_import_csv` | Import Pikkit CSV |

### Claude Desktop Config

```json
{
  "mcpServers": {
    "sharp-edge": {
      "command": "sharp-edge-mcp",
      "env": {
        "DATABASE_URL": "sqlite:///~/.sharp-edge/bets.db",
        "FANDUEL_AUTH_TOKEN": "<jwt-from-devtools>"
      }
    }
  }
}
```

## Production (K3s + CNPG)

```bash
# Deploy via ArgoCD or helm install
helm install sharp-edge ./helm/sharp-edge \
  --namespace sharp-edge \
  --create-namespace
```

Stack: CloudNativePG (PostgreSQL), ExternalSecrets (OpenBao), Cilium ingress, cert-manager TLS, Rook-Ceph storage.

## NFL props

The football board reuses the projection model that already runs in
[NFL-API](https://nfl-api.nickknows.net) rather than fitting one here, prices it
against FanDuel's public odds, and flags where the two disagree by more than the
owner's threshold (10 yards, 2 receptions).

One thing about it is worth knowing before reading a number off it: the
projections regress toward the mean and betting lines don't, so a raw
"projection minus line" gap is largely an artefact — read that way the rule
fires UNDER on nearly every star. The screen refits the projection→line
relationship weekly and measures the signal on the residual. Both numbers are
shown. See `EXPERIMENTS_NFL.md` for the measurement, and for why the moneyline
is listed but never picked.

```bash
# Refit the prop models against nflverse history and print the coefficients
python backend/scripts/calibrate_nfl.py
```

## TODO

- [ ] NBA model (nba_api: PRA projections)
- [ ] Record and settle NFL picks — the board has no track record yet
- [ ] Archive NFL closing lines so the disagreement shrink can be fit, not guessed
- [ ] Closing line value tracking
- [ ] Scheduled sync (CronJob in K8s)
