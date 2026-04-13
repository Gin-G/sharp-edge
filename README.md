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

## TODO

- [ ] Frontend (Svelte): dashboard, calendar heatmap, chat panel
- [ ] FanDuel login flow (capture /sessions POST body format)
- [ ] MLB model (pybaseball: batter-vs-pitcher splits)
- [ ] NBA model (nba_api: PRA projections)
- [ ] NFL model integration (existing nfl_data_py models)
- [ ] Closing line value tracking
- [ ] Bankroll management / Kelly criterion
- [ ] Scheduled sync (CronJob in K8s)
- [ ] GitHub Actions CI/CD → Harbor registry
