# figmaclaw webhook proxy (Cloudflare Worker)

Receives Figma FILE_UPDATE webhooks and forwards them as GitHub repository_dispatch events.

## Deploy

1. Install Wrangler: `npm install -g wrangler`
2. Login: `wrangler login`
3. Deploy: `cd worker && wrangler deploy`
4. Set secrets:
   ```
   wrangler secret put FIGMA_WEBHOOK_SECRET
   wrangler secret put GITHUB_TOKEN       # needs repo:write scope
   wrangler secret put GITHUB_REPO        # e.g. gigaverse-app/linear-git
   ```
5. Register the worker URL as a Figma webhook:
   ```
   figmaclaw init  # if not done yet, copies workflows
   # Or manually via Figma API / figmaclaw webhook registration
   ```

## Flow

Figma → POST /  →  validate passcode  →  GitHub /dispatches  →  figmaclaw-webhook.yaml
