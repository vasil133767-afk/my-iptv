"""Створює custom.channels.xml для iptv-org/epg: для кожного каналу з playlist.m3u
знаходить джерело телепрограми в epg/sites/*/*.channels.xml.

Запуск: python gen_epg.py <playlist.m3u> <шлях до epg/sites> <вихідний xml>"""
import re
import sys
from collections import Counter, defaultdict
from html import escape
from pathlib import Path

CHANNEL_RE = re.compile(r"<channel\s+([^>]*)>(.*?)</channel>", re.S)
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


def playlist_ids(path):
    """tvg-id з плейлиста: базовий id (без @SD/@HD) -> точний id у плейлисті."""
    ids = {}
    for m in re.finditer(r'tvg-id="([^"]+)"', Path(path).read_text(encoding="utf-8")):
        full = m.group(1)
        ids.setdefault(full.split("@")[0].lower(), set()).add(full)
    return ids


def main(playlist, sites_dir, out):
    wanted = playlist_ids(playlist)
    candidates = defaultdict(list)  # базовий id -> [(site, site_id, lang, name)]
    for f in Path(sites_dir).glob("*/*.channels.xml"):
        for attrs, name in CHANNEL_RE.findall(f.read_text(encoding="utf-8", errors="replace")):
            a = dict(ATTR_RE.findall(attrs))
            base = a.get("xmltv_id", "").split("@")[0].lower()
            if base in wanted and a.get("site") and a.get("site_id"):
                candidates[base].append((a["site"], a["site_id"], a.get("lang", ""), name.strip()))

    # Сайт, що покриває більше наших каналів, кращий: менше сайтів -> швидше й надійніше
    coverage = Counter(site for base in candidates for site in {c[0] for c in candidates[base]})

    def score(base, c):
        native = {"ua": "uk", "ru": "ru"}.get(base.rsplit(".", 1)[-1])
        lang = 2 if c[2] == native else 1 if c[2] in ("uk", "ru") else 0
        return (lang, coverage[c[0]])

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<channels>"]
    for base, cands in sorted(candidates.items()):
        site, site_id, lang, name = max(cands, key=lambda c: score(base, c))
        for full in sorted(wanted[base]):  # xmltv_id = точний tvg-id із плейлиста
            lines.append(
                f'  <channel site="{escape(site)}" lang="{escape(lang)}" '
                f'xmltv_id="{escape(full)}" site_id="{escape(site_id)}">{escape(name)}</channel>'
            )
    lines.append("</channels>")
    Path(out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    sites = Counter(re.search(r'site="([^"]+)"', l).group(1) for l in lines[2:-1])
    print(f"EPG: знайдено {len(candidates)} з {len(wanted)} каналів, сайтів: {len(sites)}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
