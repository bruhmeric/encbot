"""Zero-knowledge one-time web viewer.

How it stays zero-knowledge
---------------------------
* The bot builds ``https://<host>/v/<token>#<base64url DEK>``.
* Browsers NEVER send the URL fragment (#...) to the server - the AES key
  exists only in the user's browser and in the link itself.
* ``GET /v/<token>``          -> static HTML shell (no secrets).
* ``GET /v/<token>/data``     -> one-time JSON with ciphertext, nonce and the
  AAD (image id).  The page passes the AAD back as AES-GCM ``additionalData``
  — the ciphertext is bound to it server-side, and omitting it is exactly
  what produces a WebCrypto ``OperationError``.
  The claim is atomic (UPDATE ... WHERE link_used=0), so a second fetch gets
  410 Gone.  The server never decrypts anything for viewing.
* The page decrypts with the browser's built-in WebCrypto (AES-GCM), shows
  the image from a blob: URL, strips the key from the address bar, and blanks
  itself after 60 seconds.

Note: WebCrypto requires a secure context - serve this over HTTPS (or use
localhost during development), otherwise the page will show an error.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

from crypto import b64url

if TYPE_CHECKING:  # pragma: no cover
    from db import Vault

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>One-time decrypted view</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; min-height:100vh; display:flex; align-items:center;
         justify-content:center; background:#101317; color:#e8eaed;
         font:15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  main { max-width: min(92vw, 900px); text-align:center; padding:24px; }
  h1 { font-size:19px; font-weight:600; margin:0 0 6px; }
  #status { color:#9aa3ad; margin:0 0 18px; }
  img#img { max-width:100%; max-height:72vh; border-radius:10px;
            box-shadow:0 8px 40px rgba(0,0,0,.5); }
  .hint { color:#7c848d; font-size:13px; margin-top:18px; }
  code { background:#1b2027; padding:1px 5px; border-radius:4px; }
</style>
</head>
<body>
<main>
  <h1>&#128275; One-Time Decrypted View</h1>
  <p id="status">Decrypting in your browser&hellip;</p>
  <img id="img" hidden alt="decrypted image">
  <p class="hint">The key travels only in the URL <code>#fragment</code> &mdash; it was
     never sent to the server.<br>This link was single-use and is now dead.
     The page blanks itself after 60&nbsp;s.</p>
</main>
<script>
function b64uToBytes(s) {
  s = s.replace(/-/g, '+').replace(/_/g, '/');
  while (s.length % 4) s += '=';
  const bin = atob(s);
  return Uint8Array.from(bin, c => c.charCodeAt(0));
}
(async () => {
  const status = document.getElementById('status');
  const img = document.getElementById('img');
  const frag = location.hash.slice(1);
  if (!frag) { status.textContent = '\u274c No key found in the URL fragment.'; return; }
  if (!window.crypto || !crypto.subtle) {
    status.textContent = '\u274c WebCrypto is unavailable \u2014 open this link over HTTPS.'; return;
  }
  try {
    const keyBytes = b64uToBytes(frag);
    if (keyBytes.length !== 32) throw new Error('unexpected key length');
    const key = await crypto.subtle.importKey('raw', keyBytes, {name: 'AES-GCM'},
                                              false, ['decrypt']);
    const r = await fetch(location.pathname + '/data', {cache: 'no-store'});
    if (r.status === 410) { status.textContent = '\u274c Link already used or expired.'; return; }
    if (!r.ok) { status.textContent = '\u274c Fetch failed (' + r.status + ').'; return; }
    const j = await r.json();
    const pt = await crypto.subtle.decrypt(
      {name: 'AES-GCM',
       iv: b64uToBytes(j.nonce),
       additionalData: new TextEncoder().encode(j.aad)},
      key, b64uToBytes(j.ct));
    const blob = new Blob([pt], {type: j.mime});
    const url = URL.createObjectURL(blob);
    img.src = url; img.hidden = false;
    status.textContent = '\u2705 Decrypted locally \u2014 nothing was decrypted on any server.';
    history.replaceState(null, '', location.pathname);   // strip the key from the URL
    setTimeout(() => {                                   // self-blank after 60 s
      img.hidden = true; img.src = ''; URL.revokeObjectURL(url);
      status.textContent = '\ud83e\uddf9 Cleared. Reload will not work.';
    }, 60000);
  } catch (e) {
    status.textContent = '\u274c Decryption failed: ' + e;
  }
})();
</script>
</body>
</html>"""


@web.middleware
async def security_headers(request, handler):
    resp = await handler(request)
    resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'none'; img-src blob:; script-src 'unsafe-inline'; "
        "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
        "form-action 'none'")
    return resp


async def _page(request: web.Request) -> web.Response:
    return web.Response(text=PAGE, content_type="text/html", headers=NO_STORE)


async def _data(request: web.Request) -> web.Response:
    vault = request.app["vault"]
    token = request.match_info["token"]
    row = await vault.claim_link(token)          # atomic one-time claim
    if row is None:
        return web.json_response(
            {"error": "link already used or expired"}, status=410, headers=NO_STORE)
    return web.json_response({
        "nonce": b64url(row["nonce"]),
        "ct": b64url(row["ct"]),
        "aad": row["id"],   # AES-GCM additional data the page must pass
        "mime": row["mime"],
    }, headers=NO_STORE)


async def _health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True}, headers=NO_STORE)


def build_app(vault: "Vault") -> web.Application:
    app = web.Application(middlewares=[security_headers])
    app["vault"] = vault
    app.router.add_get("/", _health)
    app.router.add_get("/v/{token}", _page)
    app.router.add_get("/v/{token}/data", _data)
    return app
