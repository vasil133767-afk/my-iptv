"""IPTV-ретранслятор.

Бере плейлист із GitHub і віддає його тюнеру за адресою
    http://<сервер>:<порт>/<TOKEN>/playlist.m3u
Канали з груп RELAY_GROUPS (за замовчуванням "Росія") переписуються так,
що відео йде через цей сервер; решта каналів іде напряму, як і раніше.

Налаштування (змінні оточення, файл /opt/iptv-relay/env):
  TOKEN           секретний ключ у адресі (обов'язково)
  PLAYLIST_URL    звідки брати плейлист
  RELAY_GROUPS    групи через кому, які йдуть через сервер
  PORT            порт (8080)
  UPSTREAM_PROXY  необов'язково: http://IP:порт або socks5://IP:порт,
                  через який сервер сам ходитиме за потоками
"""
import base64
import json
import os
import re
import time
import urllib.parse

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web

TOKEN = os.environ["TOKEN"]
PLAYLIST_URL = os.environ.get(
    "PLAYLIST_URL", "https://raw.githubusercontent.com/vasil133767-afk/my-iptv/main/playlist.m3u")
RELAY_GROUPS = {g.strip() for g in os.environ.get("RELAY_GROUPS", "Росія").split(",") if g.strip()}
PORT = int(os.environ.get("PORT", "8080"))
UPSTREAM_PROXY = os.environ.get("UPSTREAM_PROXY", "").strip()
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
PLAYLIST_TTL = 600  # кешувати плейлист 10 хвилин

_cache = {"time": 0.0, "text": ""}


# ---------- кодування адрес ----------

def enc(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def dec(s: str) -> str:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()


def relay_link(base: str, url: str, headers: dict) -> str:
    path = urllib.parse.urlparse(url).path.lower()
    ext = ".m3u8" if path.endswith(".m3u8") or path.endswith(".m3u") else os.path.splitext(path)[1][:6]
    link = f"{base}/s/{enc(url)}{ext or ''}"
    if headers:
        link += "?h=" + enc(json.dumps(headers, separators=(",", ":")))
    return link


def channel_headers(block: list) -> dict:
    """user-agent / referrer, вказані в плейлисті для каналу."""
    text = "\n".join(block)
    h = {}
    m = re.search(r'http-user-agent="([^"]+)"|http-user-agent=(.+)', text)
    if m:
        h["User-Agent"] = (m.group(1) or m.group(2)).strip()
    m = re.search(r'http-referrer="([^"]+)"|http-referrer=(.+)', text)
    if m:
        h["Referer"] = (m.group(1) or m.group(2)).strip()
    return h


# ---------- HTTP-клієнт ----------

def make_session() -> ClientSession:
    timeout = ClientTimeout(total=None, sock_connect=10, sock_read=30)
    if UPSTREAM_PROXY.startswith("socks"):
        from aiohttp_socks import ProxyConnector  # pip install aiohttp-socks
        return ClientSession(connector=ProxyConnector.from_url(UPSTREAM_PROXY, limit=200),
                             timeout=timeout, auto_decompress=True)
    return ClientSession(connector=TCPConnector(limit=200), timeout=timeout)


def proxy_kw() -> dict:
    return {"proxy": UPSTREAM_PROXY} if UPSTREAM_PROXY.startswith("http") else {}


# ---------- обробники ----------

def check_token(request):
    if request.match_info["token"] != TOKEN:
        raise web.HTTPNotFound()


def base_url(request) -> str:
    return f"{request.scheme}://{request.host}/{TOKEN}"


async def playlist(request):
    check_token(request)
    session: ClientSession = request.app["session"]
    if time.time() - _cache["time"] > PLAYLIST_TTL or not _cache["text"]:
        # сам плейлист береться з GitHub напряму, без UPSTREAM_PROXY
        async with session.get(PLAYLIST_URL, headers={"User-Agent": DEFAULT_UA}) as r:
            if r.status != 200:
                raise web.HTTPBadGateway(text=f"GitHub повернув {r.status}")
            _cache["text"] = await r.text()
            _cache["time"] = time.time()

    base = base_url(request)
    out, block, relayed = [], [], 0
    for line in _cache["text"].splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#EXTM3U"):
            out.append(s)
        elif s.startswith("#EXTINF"):
            block = [s]
        elif s.startswith("#"):
            block.append(s)
        elif block:
            m = re.search(r'group-title="([^"]*)"', block[0])
            if m and m.group(1) in RELAY_GROUPS and s.startswith("http"):
                hdrs = channel_headers(block)
                # тюнеру більше не треба підставляти UA/referrer — це робить сервер
                block = [block[0]] + [b for b in block[1:] if not b.startswith("#EXTVLCOPT")]
                s = relay_link(base, s, hdrs)
                relayed += 1
            out.extend(block + [s])
            block = []
    return web.Response(text="\n".join(out) + "\n", content_type="audio/x-mpegurl",
                        headers={"X-Relayed-Channels": str(relayed)})


def rewrite_m3u8(text: str, src: str, base: str, headers: dict) -> str:
    def fix(u):
        return relay_link(base, urllib.parse.urljoin(src, u), headers)

    out = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(fix(s))
        elif 'URI="' in s:  # EXT-X-KEY, EXT-X-MEDIA, EXT-X-MAP тощо
            out.append(re.sub(r'URI="([^"]+)"', lambda m: f'URI="{fix(m.group(1))}"', s))
        else:
            out.append(line)
    return "\n".join(out) + "\n"


async def stream(request):
    check_token(request)
    data = request.match_info["data"].split(".", 1)[0]
    try:
        url = dec(data)
        headers = json.loads(dec(request.query["h"])) if "h" in request.query else {}
    except Exception:
        raise web.HTTPBadRequest()
    if not url.startswith(("http://", "https://")):
        raise web.HTTPBadRequest()

    up_headers = {"User-Agent": DEFAULT_UA, **headers}
    if "Range" in request.headers:
        up_headers["Range"] = request.headers["Range"]

    session: ClientSession = request.app["session"]
    try:
        r = await session.get(url, headers=up_headers, allow_redirects=True, **proxy_kw())
    except Exception as e:
        raise web.HTTPBadGateway(text=f"джерело недоступне: {type(e).__name__}")

    try:
        ctype = r.headers.get("Content-Type", "")
        is_playlist = "mpegurl" in ctype.lower() or url.split("?")[0].lower().endswith((".m3u8", ".m3u"))
        if r.status >= 400:
            return web.Response(status=r.status, text=f"джерело повернуло {r.status}")

        if is_playlist:
            body = await r.content.read(5_000_000)
            text = body.decode("utf-8", "replace")
            if "#EXTM3U" in text:
                return web.Response(text=rewrite_m3u8(text, str(r.url), base_url(request), headers),
                                    content_type="application/vnd.apple.mpegurl",
                                    headers={"Cache-Control": "no-cache"})
            return web.Response(body=body, status=r.status, content_type=ctype.split(";")[0] or None)

        resp = web.StreamResponse(status=r.status)
        for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
            if h in r.headers:
                resp.headers[h] = r.headers[h]
        await resp.prepare(request)
        async for chunk in r.content.iter_chunked(65536):
            await resp.write(chunk)
        await resp.write_eof()
        return resp
    finally:
        r.release()


async def on_startup(app):
    app["session"] = make_session()


async def on_cleanup(app):
    await app["session"].close()


def make_app():
    app = web.Application()
    app.router.add_get("/{token}/playlist.m3u", playlist)
    app.router.add_get("/{token}/s/{data}", stream)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    print(f"ретранслятор: порт {PORT}, групи через сервер: {', '.join(sorted(RELAY_GROUPS))}"
          + (f", вихідний проксі: {UPSTREAM_PROXY.split('@')[-1]}" if UPSTREAM_PROXY else ""))
    web.run_app(make_app(), port=PORT, access_log=None, print=None)
