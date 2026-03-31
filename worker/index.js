export default {
  async fetch(request, env) {
    if (request.method !== 'POST') {
      return new Response('Method Not Allowed', { status: 405 });
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return new Response('Bad Request', { status: 400 });
    }

    // Validate passcode
    const passcode = request.headers.get('X-Figma-Passcode') ?? body.passcode ?? '';
    if (env.FIGMA_WEBHOOK_SECRET && passcode !== env.FIGMA_WEBHOOK_SECRET) {
      return new Response('Forbidden', { status: 403 });
    }

    // Only forward FILE_UPDATE events
    if (body.event_type !== 'FILE_UPDATE') {
      return new Response('OK (ignored)', { status: 200 });
    }

    // Forward to GitHub repository_dispatch
    const ghUrl = `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`;
    await fetch(ghUrl, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${env.GITHUB_TOKEN}`,
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
        'User-Agent': 'figmaclaw-worker/1.0',
      },
      body: JSON.stringify({
        event_type: 'figma-webhook',
        client_payload: body,
      }),
    });

    return new Response('OK', { status: 200 });
  },
};
