/**
 * Figma → GitHub repository_dispatch proxy with per-file debounce.
 *
 * Receives FILE_UPDATE webhooks from Figma (per-file context),
 * validates the passcode, debounces rapid changes per file (default 10 min),
 * then forwards as a repository_dispatch event for figmaclaw-webhook.yaml.
 *
 * Required Cloudflare secrets (wrangler secret put <NAME>):
 *   FIGMA_WEBHOOK_SECRET   — must match the passcode used when registering Figma webhooks
 *   GITHUB_TOKEN           — PAT or app token with `repo` scope on the target repo
 *
 * Wrangler vars (wrangler.toml [vars]):
 *   GITHUB_REPO            — e.g. "gigaverse-app/linear-git"
 *   DEBOUNCE_SECONDS       — seconds to suppress repeated events per file (default: 600)
 *
 * KV binding:
 *   DEBOUNCE               — stores last-dispatched timestamps keyed by file_key
 */

const DEFAULT_DEBOUNCE_SECONDS = 600;

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("Method not allowed", { status: 405 });
    }

    let payload;
    try {
      payload = await request.json();
    } catch {
      return new Response("Bad JSON", { status: 400 });
    }

    // PING events are sent by Figma when a webhook is first registered — just ack them
    if (payload.event_type === "PING") {
      console.log(`PING received for webhook ${payload.webhook_id}`);
      return new Response("ok", { status: 200 });
    }

    // Validate passcode
    if (payload.passcode !== env.FIGMA_WEBHOOK_SECRET) {
      console.error("Invalid passcode");
      return new Response("Unauthorized", { status: 401 });
    }

    const fileKey = payload.file_key;
    const debounceSeconds = parseInt(env.DEBOUNCE_SECONDS ?? DEFAULT_DEBOUNCE_SECONDS, 10);
    const nowMs = Date.now();

    // Debounce: skip if this file was dispatched recently
    const lastDispatchedStr = await env.DEBOUNCE.get(`file:${fileKey}`);
    if (lastDispatchedStr) {
      const lastMs = parseInt(lastDispatchedStr, 10);
      const elapsedSeconds = (nowMs - lastMs) / 1000;
      if (elapsedSeconds < debounceSeconds) {
        const remainingSeconds = Math.ceil(debounceSeconds - elapsedSeconds);
        console.log(`Debounced ${fileKey} — ${remainingSeconds}s remaining in cooldown`);
        return new Response("debounced", { status: 200 });
      }
    }

    // Forward to GitHub as repository_dispatch
    const repo = env.GITHUB_REPO;
    const githubUrl = `https://api.github.com/repos/${repo}/dispatches`;

    const ghResponse = await fetch(githubUrl, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "figma-webhook-proxy/1.0",
      },
      body: JSON.stringify({
        event_type: "figma-webhook",
        client_payload: payload,
      }),
    });

    if (ghResponse.status === 204) {
      // Record dispatch time in KV with TTL = debounce window
      await env.DEBOUNCE.put(`file:${fileKey}`, String(nowMs), {
        expirationTtl: debounceSeconds,
      });
      console.log(`Dispatched figma-webhook for file ${fileKey} (${payload.event_type})`);
      return new Response("ok", { status: 200 });
    } else {
      const body = await ghResponse.text();
      console.error(`GitHub dispatch failed: ${ghResponse.status} ${body}`);
      return new Response("upstream error", { status: 502 });
    }
  },
};
