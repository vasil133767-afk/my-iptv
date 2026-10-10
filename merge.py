"""Збирає M3U-плейлист українських і російських каналів із двох джерел:
Free-TV/IPTV (відібрані канали, пріоритет) та iptv-org/iptv (решта).
Для кожного каналу перевіряє всі відомі посилання й бере перше робоче,
тож якщо посилання Free-TV впало, підставляється запасне з iptv-org."""
import json
import os
import re
import urllib.error
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
    ],
}
OUTPUT = "playlist.m3u"
BLOCKLIST = "https://iptv-org.github.io/api/blocklist.json"
# телепрограма, яку генерує gen_epg.py у цьому ж репозиторії
REPO = os.environ.get("GITHUB_REPOSITORY", "vasil133767-afk/my-iptv")
EPG_URL = f"https://raw.githubusercontent.com/{REPO}/main/guide.xml.gz"
TIMEOUT = 10
WORKERS = 40
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def fetch(url):
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


def channel_key(extinf):
    """Ключ для пошуку дублікатів: tvg-id без суфікса @SD/@HD, інакше назва."""
    m = re.search(r'tvg-id="([^"]+)"', extinf)
    if m:
        return m.group(1).split("@")[0].lower()
    name = extinf.rsplit(",", 1)[-1]
    name = re.sub(r"\(.*?\)|\[.*?\]|[ⓈⒼⓎⓋⓄ]", "", name)
    return re.sub(r"\W+", "", name).lower()


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


def is_alive(item):
    block, url = item
    if not url.startswith("http"):
        return True
    req = urllib.request.Request(url, headers=headers_for(block))
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = r.read(2048)
            return not (".m3u8" in url and b"#EXT" not in data)
    except urllib.error.HTTPError as e:
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
    blocked = load_blocklist()
    skipped = 0
    # канали: (група, ключ) -> список кандидатів у порядку пріоритету
    channels, order, seen_urls = {}, [], set()
    for group, urls in SOURCES.items():
        for src in urls:
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
