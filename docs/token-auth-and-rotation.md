# Figma Token Auth and Rotation

> **Canon cross-reference:** the variables-refresh code path documented in [`figmaclaw-canon` §4 TC-1, TC-6](../skills/figmaclaw-canon/SKILL.md#tc--token-catalog) requires Enterprise scope `file_variables:read`. When that scope is unavailable, fall back to the SEEDED workflow per D14. This document remains authoritative for *secret management and rotation*.

This document defines which token is used where in `figmaclaw`, how to create it, how long it lives, and how to rotate it safely in CI.

As of April 15, 2026.

## Token matrix

| Variable | Used for | Token type | Where used in repo |
|---|---|---|---|
| `FIGMA_API_KEY` | Figma REST API calls (pull, list, webhooks, REST smoke tests) | Personal Access Token (PAT) or OAuth REST access token | `figmaclaw/figma_client.py`, `smoke-api-ci`, `smoke-webhook-ci` |
| `FIGMA_MCP_TOKEN` | Figma MCP server (`https://mcp.figma.com/mcp`) | MCP OAuth access token (Bearer) | `figmaclaw/figma_mcp.py`, `smoke-mcp-ci` |

## Can a PAT be used for `FIGMA_MCP_TOKEN`?

No.

- Figma's MCP docs require signing in via the MCP OAuth flow in a supported MCP client.
- Figma REST docs explicitly separate REST scopes/tokens from MCP and state that MCP handles its own OAuth flow.

Implication: PATs are valid for REST API auth, but are not the supported auth mechanism for the MCP endpoint in this repo.

## How to create `FIGMA_MCP_TOKEN`

### Recommended (Claude Code + remote MCP)

1. Add the MCP server:
   - `claude mcp add --transport http figma https://mcp.figma.com/mcp`
2. Start Claude Code and run `/mcp`.
3. Select `figma` and click `Authenticate`.
4. Complete browser auth and click `Allow access`.
5. Confirm Claude shows successful connection.

At this point, Claude stores MCP OAuth credentials in `~/.claude/.credentials.json`.

### Export token into environment for local smoke runs

Use a non-echo extraction to avoid printing secrets in logs:

```bash
export FIGMA_MCP_TOKEN="$(
python - <<'PY'
import json
from pathlib import Path

p = Path.home() / ".claude" / ".credentials.json"
d = json.loads(p.read_text())
m = d.get("mcpOAuth") or {}
for k, v in m.items():
    if "figma" in k.lower() and isinstance(v, dict):
        tok = (v.get("accessToken") or "").strip()
        if tok:
            print(tok)
            raise SystemExit(0)
raise SystemExit("No Figma MCP token found in ~/.claude/.credentials.json")
PY
)"
```

Then run:

```bash
uv run pytest -m smoke_mcp tests/smoke/test_figma_mcp_smoke.py -v -n 0
```

### Set for GitHub Actions

```bash
gh secret set FIGMA_MCP_TOKEN --repo <owner>/<repo> --body "$FIGMA_MCP_TOKEN"
```

In this repo, `smoke-mcp-ci` already fails loudly when this secret is missing.

## How to create `FIGMA_API_KEY` (PAT for REST)

1. Open Figma account settings -> Security.
2. Generate a new personal access token.
3. Choose required scopes.
4. Set an expiration and copy the token once.
5. Store it in:
   - local `.env` / shell as `FIGMA_API_KEY`
   - GitHub Actions secret `FIGMA_API_KEY`

## Lifetime and expiry policy

### PAT (`FIGMA_API_KEY`)

- Figma policy (April 28, 2025): PATs now have a maximum expiry of 90 days; non-expiring PATs can no longer be created.
- Treat PAT rotation as mandatory.

### MCP OAuth token (`FIGMA_MCP_TOKEN`)

- Figma MCP docs do not currently publish a stable, explicit MCP-token TTL in the MCP setup pages.
- Do not assume non-expiring behavior.
- Operational policy: treat as expiring credentials and rotate proactively on the same cadence as PATs (90-day max window) or sooner.

## Rotation runbook (CI-safe)

1. Re-authenticate MCP in Claude (`/mcp` -> `figma` -> `Authenticate`) to refresh token source.
2. Re-export `FIGMA_MCP_TOKEN` from `~/.claude/.credentials.json`.
3. Update GitHub secret:
   - `gh secret set FIGMA_MCP_TOKEN --repo <owner>/<repo> --body "$FIGMA_MCP_TOKEN"`
4. Rotate REST PAT and update `FIGMA_API_KEY` similarly.
5. Validate immediately:
   - `smoke-api-ci`
   - `smoke-webhook-ci`
   - `smoke-mcp-ci`

## Security requirements

- Never commit tokens to git.
- Never print token values in CI logs.
- Keep token scope minimal (for REST PAT).
- Use repository/org secrets for CI, not plaintext workflow vars.
- Prefer short-lived credentials plus regular rotation over long-lived static secrets.

## Repo behavior that enforces this

- `tests/smoke/live_gate.py` fails loudly when required smoke credentials are missing.
- `.github/workflows/ci.yml` validates required secrets before each smoke job and exits non-zero if missing.
- `figmaclaw/figma_mcp.py` supports:
  1. `FIGMA_MCP_TOKEN` env var
  2. fallback to Claude credentials (`~/.claude/.credentials.json`)

## Primary sources

- Figma MCP remote setup: https://developers.figma.com/docs/figma-mcp-server/remote-server-installation/
- Figma MCP access/limits: https://developers.figma.com/docs/figma-mcp-server/plans-access-and-permissions/
- REST authentication and token model: https://developers.figma.com/docs/rest-api/authentication/
- REST scopes page (MCP handles own OAuth flow): https://developers.figma.com/docs/rest-api/scopes/
- REST changelog (PAT max 90-day expiry): https://developers.figma.com/docs/rest-api/changelog/
- PAT management help article: https://help.figma.com/hc/en-us/articles/8085703771159-Manage-personal-access-tokens
