"""Збирає M3U-плейлист українських і російських каналів із двох джерел:
Free-TV/IPTV (відібрані канали, пріоритет) та iptv-org/iptv (решта).
Для кожного каналу перевіряє всі відомі посилання й бере перше робоче,
тож якщо посилання Free-TV впало, підставляється запасне з iptv-org."""
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# група -> список джерел у порядку пріоритету
SOURCES = {
    "Україна": [
        "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_ukraine.m3u8",
        "https://iptv-org.github.io/iptv/countries/ua.m3u",
    ],
    "Росія": [
        "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_russia.m3u8",
        "https://iptv-org.github.io/iptv/countries/ru.m3u",
        "sources/iptvru.m3u",  # ефірні та регіональні канали (файл у цьому репозиторії)
    ],
}
# Власні канали, яких немає в джерелах: група -> [(назва, посилання), ...]
# «Россия 1» в інших часових поясах (офіційні потоки smotrim.ru, номер потоку 1–9).
# Номер потоку = зсув від Москви в годинах (+1 … +9).
R1_LOGO = "https://upload.wikimedia.org/wikipedia/commons/thumb/6/64/Russia-1.svg/960px-Russia-1.svg.png"
EXTRA = {
    "Росія": [(f"Россия 1 HD +{n}", f"https://live.smotrim.ru/vgtrk/{n}/russia1-hd/index.m3u8")
              for n in range(1, 10)],
}
OUTPUT = "playlist.m3u"
BLOCKLIST = "https://iptv-org.github.io/api/blocklist.json"
# телепрограма, яку генерує gen_epg.py у цьому ж репозиторії
REPO = os.environ.get("GITHUB_REPOSITORY", "vasil133767-afk/my-iptv")
EPG_URL = f"https://raw.githubusercontent.com/{REPO}/main/guide.xml.gz"
TIMEOUT = 10
WORKERS = 40
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
# Ретранслятор в Амстердамі (секрет RELAY_BASE у GitHub, вигляду http://IP:8080/КЛЮЧ).
# Канали з RELAY_GROUPS перевіряються через нього, тобто так, як їх дивиться тюнер.
RELAY_BASE = os.environ.get("RELAY_BASE", "").strip().rstrip("/")
RELAY_GROUPS = {"Росія"}
relay_on = False


def fetch(url):
    if not url.startswith("http"):  # файл у репозиторії
        with open(url, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace").splitlines()


def parse(lines):
    """Повертає список (блок рядків #EXTINF/#EXTVLCOPT, url)."""
    items, block = [], []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#EXTM3U"):
            continue
        if line.startswith("#EXTINF"):
            block = [line]
        elif line.startswith("#"):
            if block:
                block.append(line)
        elif block:
            items.append((block, line))
            block = []
    return items


def extra_items(group):
    return [([f'#EXTINF:-1 tvg-name="{name}" tvg-logo="{R1_LOGO}" group-title="{group}",{name}'], url)
            for name, url in EXTRA.get(group, [])]


def channel_key(extinf):
    """Ключ для пошуку дублікатів: tvg-id без суфікса @SD/@HD, інакше назва.
    Версії з іншим часовим поясом (@Plus2, @Plus4, ...) — окремі канали."""
    m = re.search(r'tvg-id="([^"]+)"', extinf)
    if m:
        base, _, feed = m.group(1).partition("@")
        if re.fullmatch(r"(Plus|Minus)\d+", feed, re.I):
            return f"{base}@{feed}".lower()
        return base.lower()
    name = extinf.rsplit(",", 1)[-1]
    name = re.sub(r"\(.*?\)|\[.*?\]|[ⓈⒼⓎⓋⓄ]", "", name)
    return re.sub(r"\W+", "", name).lower()


def name_key(extinf):
    """Назва без «HD», дужок і розділових знаків: щоб «Первый канал HD» з одного
    джерела і «Первый канал» з іншого вважались одним каналом."""
    name = extinf.rsplit(",", 1)[-1].lower()
    name = re.sub(r"\(.*?\)|\[.*?\]|[ⓈⒼⓎⓋⓄ]", "", name)
    name = re.sub(r"\b(hd|fhd|uhd|sd|4k)\b", "", name)
    return re.sub(r"\W+", "", name)


def set_group(extinf, group):
    if 'group-title="' in extinf:
        return re.sub(r'group-title="[^"]*"', f'group-title="{group}"', extinf)
    return extinf.replace("#EXTINF:-1", f'#EXTINF:-1 group-title="{group}"', 1)


def headers_for(block):
    text = "\n".join(block)
    h = {"User-Agent": DEFAULT_UA}
    m = re.search(r'http-user-agent="([^"]+)"|http-user-agent=(.+)', text)
    if m:
        h["User-Agent"] = (m.group(1) or m.group(2)).strip()
    m = re.search(r'http-referrer="([^"]+)"|http-referrer=(.+)', text)
    if m:
        h["Referer"] = (m.group(1) or m.group(2)).strip()
    return h


def b64(s):
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def relay_url(url, block):
    """Те саме посилання, що формує ретранслятор (relay.py)."""
    path = urllib.parse.urlparse(url).path.lower()
    ext = ".m3u8" if path.endswith((".m3u8", ".m3u")) else os.path.splitext(path)[1][:6]
    h = {k: v for k, v in headers_for(block).items() if not (k == "User-Agent" and v == DEFAULT_UA)}
    link = f"{RELAY_BASE}/s/{b64(url)}{ext}"
    if h:
        link += "?h=" + b64(json.dumps(h, separators=(",", ":")))
    return link


def check_relay():
    """Чи доступний ретранслятор; якщо ні — перевірка йде напряму, як раніше."""
    if not RELAY_BASE:
        print("RELAY_BASE не задано: усі канали перевіряються напряму з GitHub")
        return False
    try:
        req = urllib.request.Request(RELAY_BASE + "/playlist.m3u", headers={"User-Agent": DEFAULT_UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read(100)
    except Exception as e:
        print(f"! ретранслятор не відповідає ({type(e).__name__}): перевірка напряму")
        return False
    print("Канали груп " + ", ".join(sorted(RELAY_GROUPS)) + " перевіряються через ретранслятор")
    return True


def group_of(block):
    m = re.search(r'group-title="([^"]*)"', block[0])
    return m.group(1) if m else ""


def is_alive(item):
    block, url = item
    if not url.startswith("http"):
        return True
    via_relay = relay_on and group_of(block) in RELAY_GROUPS
    if via_relay:
        req = urllib.request.Request(relay_url(url, block), headers={"User-Agent": DEFAULT_UA})
    else:
        req = urllib.request.Request(url, headers=headers_for(block))
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT * 2 if via_relay else TIMEOUT) as r:
            data = r.read(2048)
            return not (".m3u8" in url and b"#EXT" not in data)
    except urllib.error.HTTPError as e:
        if via_relay:
            return False  # не працює навіть з Амстердама, у тюнері теж не піде
        # 403/451 — часто блок за регіоном серверів GitHub (США); у вас може працювати
        return e.code in (403, 451)
    except Exception:
        return False


def load_blocklist():
    """ID каналів, які iptv-org прибрав на вимогу правовласників (DMCA тощо)."""
    try:
        req = urllib.request.Request(BLOCKLIST, headers={"User-Agent": DEFAULT_UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            return {b["channel"].lower() for b in json.load(r)}
    except Exception as e:
        print(f"! blocklist недоступний: {e}")
        return set()


def main():
    global relay_on
    relay_on = check_relay()
    blocked = load_blocklist()
    skipped = 0
    # канали: (група, ключ) -> список кандидатів у порядку пріоритету
    channels, order, seen_urls, by_name = {}, [], set(), {}
    for group, urls in SOURCES.items():
        for src in urls + [None]:
            if src is None:  # власні канали з EXTRA після всіх джерел групи
                items = extra_items(group)
            else:
                try:
                    items = parse(fetch(src))
                except Exception as e:
                    print(f"! не вдалося завантажити {src}: {e}")
                    continue
            for block, url in items:
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                key = (group, channel_key(block[0]))
                if key[1] in blocked:
                    skipped += 1
                    continue
                # той самий канал з іншого джерела (інший tvg-id, але та сама назва)
                nk = (group, name_key(block[0]))
                if key not in channels and nk[1] and nk in by_name:
                    key = by_name[nk]
                by_name.setdefault(nk, key)
                if key not in channels:
                    channels[key] = []
                    order.append(key)
                channels[key].append(([set_group(block[0], group)] + block[1:], url))

    candidates = [c for k in order for c in channels[k]]
    with ThreadPoolExecutor(WORKERS) as ex:
        alive = dict(zip((u for _, u in candidates), ex.map(is_alive, candidates)))

    out = [f'#EXTM3U x-tvg-url="{EPG_URL}"']
    total = {}
    for key in order:
        for block, url in channels[key]:
            if alive[url]:
                out.extend(block + [url])
                total[key[0]] = total.get(key[0], 0) + 1
                break
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    stats = ", ".join(f"{g}: {n}" for g, n in total.items())
    print(f"{OUTPUT}: {sum(total.values())} живих каналів з {len(order)} ({stats}); "
          f"за blocklist відкинуто посилань: {skipped}")


if __name__ == "__main__":
    main()
