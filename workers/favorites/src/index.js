const ALLOWED_ORIGINS = ['https://maxswinkels.github.io', 'http://localhost:4321'];
const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;

// In-isolate rate limiting (best-effort)
const rateMap = new Map();

function corsHeaders(request) {
  const origin = request.headers.get('Origin') || '';
  const allowed = ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0];
  return {
    'Access-Control-Allow-Origin': allowed,
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Vary': 'Origin',
  };
}

function json(data, status, request) {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      ...corsHeaders(request),
      'Content-Type': 'application/json',
      'Cache-Control': 'no-store',
    },
  });
}

function checkRateLimit(request) {
  const ip = request.headers.get('cf-connecting-ip') || 'unknown';
  const now = Date.now();
  const window = 60_000;
  const limit = 60;

  if (rateMap.size > 1000) rateMap.clear();

  const timestamps = (rateMap.get(ip) || []).filter(t => now - t < window);
  if (timestamps.length >= limit) return false;
  timestamps.push(now);
  rateMap.set(ip, timestamps);
  return true;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const { pathname } = url;

    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders(request) });
    }

    if (request.method === 'GET' && pathname === '/counts') {
      const listed = await env.FAVORITES.list({ prefix: 'count:' });
      const entries = await Promise.all(
        listed.keys.map(async ({ name }) => {
          const val = await env.FAVORITES.get(name);
          const slug = name.slice('count:'.length);
          return [slug, Number(val) || 0];
        })
      );
      return json({ counts: Object.fromEntries(entries) }, 200, request);
    }

    if (request.method === 'POST' && pathname === '/favorite') {
      if (!checkRateLimit(request)) {
        return json({ error: 'rate_limited' }, 429, request);
      }

      let body;
      try {
        body = await request.json();
      } catch {
        return json({ error: 'bad_json' }, 400, request);
      }

      const { slug, action } = body || {};
      if (
        typeof slug !== 'string' ||
        !SLUG_RE.test(slug) ||
        (action !== 'add' && action !== 'remove')
      ) {
        return json({ error: 'bad_request' }, 400, request);
      }

      const key = `count:${slug}`;
      const current = Number(await env.FAVORITES.get(key)) || 0;
      const next = Math.max(0, Math.min(1_000_000, action === 'add' ? current + 1 : current - 1));
      await env.FAVORITES.put(key, String(next));

      return json({ slug, count: next }, 200, request);
    }

    return json({ error: 'not_found' }, 404, request);
  },
};
