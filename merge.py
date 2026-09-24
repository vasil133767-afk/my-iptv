"""Збирає один M3U-плейлист з українських і російських каналів iptv-org."""
import re
import urllib.request

# Джерела: назва групи -> URL плейлиста iptv-org
SOURCES = {
    "Україна": "https://iptv-org.github.io/iptv/countries/ua.m3u",
    "Росія": "https://iptv-org.github.io/iptv/countries/ru.m3u",
    # Альтернатива — за мовою мовлення (включає канали з інших країн):
    # "Українською": "https://iptv-org.github.io/iptv/languages/ukr.m3u",
    # "Російською": "https://iptv-org.github.io/iptv/languages/rus.m3u",
}
OUTPUT = "playlist.m3u"


def fetch(url: str) -> list[str]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace").splitlines()


def set_group(extinf: str, group: str) -> str:
    if 'group-title="' in extinf:
        return re.sub(r'group-title="[^"]*"', f'group-title="{group}"', extinf)
    return extinf.replace("#EXTINF:-1", f'#EXTINF:-1 group-title="{group}"', 1)


def main() -> None:
    out = ["#EXTM3U"]
    seen: set[str] = set()
    for group, url in SOURCES.items():
        block: list[str] = []
        for line in fetch(url):
            line = line.strip()
            if not line or line.startswith("#EXTM3U"):
                continue
            if line.startswith("#EXTINF"):
                block = [set_group(line, group)]
            elif line.startswith("#"):
                block.append(line)  # #EXTVLCOPT тощо
            elif block:
                if line not in seen:  # прибираємо дублікати потоків
                    seen.add(line)
                    out.extend(block + [line])
                block = []
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print(f"{OUTPUT}: {len(seen)} каналів")


if __name__ == "__main__":
    main()
