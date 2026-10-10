"""Телепрограма для каналів з playlist.m3u на основі iptv-org/epg.

  python gen_epg.py prepare <playlist.m3u> <epg/sites> <custom.channels.xml>
      Підбирає джерело програми для кожного каналу. Канали з epg.iptvx.one
      (один великий XMLTV-файл на всі канали) йдуть у iptvx_map.json поруч,
      решта — у custom.channels.xml для завантажувача iptv-org.

  python gen_epg.py build <custom.channels.xml> <grab.xml> <guide.xml.gz>
      Потоково завантажує файл iptvx.one, вибирає лише наші канали
      і зливає з результатом завантажувача (grab.xml, якщо він є).
"""
import gzip
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

IPTVX_SITE = "epg.iptvx.one"
IPTVX_URL = "https://iptvx.one/epg/epg_noarch.xml.gz"
DAYS = 3
CHANNEL_RE = re.compile(r"<channel\s+([^>]*)>(.*?)</channel>", re.S)
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def map_path(channels_xml):
    return Path(channels_xml).with_name("iptvx_map.json")


# ---------- prepare ----------

def playlist_ids(path):
    """базовий id (без @SD/@HD) -> точні tvg-id у плейлисті"""
    ids = {}
    for m in re.finditer(r'tvg-id="([^"]+)"', Path(path).read_text(encoding="utf-8")):
        full = m.group(1)
        ids.setdefault(full.split("@")[0].lower(), set()).add(full)
    return ids


def prepare(playlist, sites_dir, out):
    wanted = playlist_ids(playlist)
    candidates = defaultdict(list)  # базовий id -> [(site, site_id, lang, name)]
    for f in Path(sites_dir).glob("*/*.channels.xml"):
        for attrs, name in CHANNEL_RE.findall(f.read_text(encoding="utf-8", errors="replace")):
            a = dict(ATTR_RE.findall(attrs))
            base = a.get("xmltv_id", "").split("@")[0].lower()
            if base in wanted and a.get("site") and a.get("site_id"):
                candidates[base].append((a["site"], a["site_id"], a.get("lang", ""), name.strip()))

    # сайт, що покриває більше наших каналів, кращий: менше сайтів -> швидше й надійніше
    coverage = Counter(s for base in candidates for s in {c[0] for c in candidates[base]})

    def score(base, c):
        native = {"ua": "uk", "ru": "ru"}.get(base.rsplit(".", 1)[-1])
        lang = 2 if c[2] == native else 1 if c[2] in ("uk", "ru") else 0
        return (lang, coverage[c[0]])

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<channels>"]
    iptvx = defaultdict(list)  # site_id -> [наші tvg-id]
    sites = Counter()
    for base, cands in sorted(candidates.items()):
        site, site_id, lang, name = max(cands, key=lambda c: score(base, c))
        sites[site] += 1
        for full in sorted(wanted[base]):
            if site == IPTVX_SITE:
                iptvx[site_id].append(full)
            else:
                lines.append(
                    f'  <channel site="{escape(site)}" lang="{escape(lang)}" '
                    f'xmltv_id="{escape(full)}" site_id="{escape(site_id)}">{escape(name)}</channel>'
                )
    lines.append("</channels>")
    Path(out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    map_path(out).write_text(json.dumps(iptvx, ensure_ascii=False), encoding="utf-8")
    print(f"EPG: знайдено {len(candidates)} з {len(wanted)} каналів; "
          + ", ".join(f"{s}: {n}" for s, n in sites.most_common()))
    # для workflow: чи є що запускати завантажувачу
    print(f"grab_needed={'true' if len(lines) > 3 else 'false'}")


# ---------- build ----------

def in_window(start, lo, hi):
    return lo <= (start or "")[:8] <= hi


def iptvx_elements(mapping):
    """Потоково читає великий XMLTV з iptvx.one, віддає наші <channel>/<programme>."""
    today = datetime.now(timezone.utc).date()
    lo = (today - timedelta(days=1)).strftime("%Y%m%d")
    hi = (today + timedelta(days=DAYS)).strftime("%Y%m%d")
    req = urllib.request.Request(IPTVX_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as resp, gzip.GzipFile(fileobj=resp) as gz:
        for _, el in ET.iterparse(gz, events=("end",)):
            if el.tag == "channel":
                for our_id in mapping.get(el.get("id"), []):
                    ch = ET.Element("channel", id=our_id)
                    ch.extend(list(el))
                    yield ET.tostring(ch, encoding="unicode")
                el.clear()
            elif el.tag == "programme":
                ids = mapping.get(el.get("channel"))
                if ids and in_window(el.get("start"), lo, hi):
                    for our_id in ids:
                        el.set("channel", our_id)
                        yield ET.tostring(el, encoding="unicode")
                el.clear()


def grab_elements(path):
    if not Path(path).exists():
        return
    for _, el in ET.iterparse(path, events=("end",)):
        if el.tag in ("channel", "programme"):
            yield ET.tostring(el, encoding="unicode")
            el.clear()


def build(channels_xml, grab_xml, out_gz):
    mapping = json.loads(map_path(channels_xml).read_text(encoding="utf-8"))
    channels, programmes = [], []
    stats = Counter()

    sources = [("iptvx.one", lambda: iptvx_elements(mapping)), ("інші сайти", lambda: grab_elements(grab_xml))]
    for label, gen in sources:
        try:
            for s in gen():
                if s.startswith("<channel"):
                    channels.append(s)
                    stats[label + " канали"] += 1
                else:
                    programmes.append(s)
                    stats[label + " передачі"] += 1
        except Exception as e:
            print(f"! {label}: {e}")

    if not programmes:
        sys.exit("EPG: жодної передачі не отримано, guide.xml.gz не змінюю")
    with gzip.open(out_gz, "wt", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv>\n')
        f.write("\n".join(channels) + "\n" + "\n".join(programmes) + "\n</tv>\n")
    print("EPG: " + ", ".join(f"{k}: {v}" for k, v in stats.items()))


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    {"prepare": prepare, "build": build}[cmd](*args)
