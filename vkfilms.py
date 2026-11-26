#!/usr/bin/env python3
"""
VK Film Harvester — собирает свежие фильмы со стен VK и обновляет efir-films.m3u.

Источник — анонимный виджет сообщества (vk.com/widget_community.php), работает
без токенов и логина. Метаданные (постер, фон, год, жанры, описание, трейлер)
подтягиваются из TMDB. Новинки добавляются в НАЧАЛО плейлиста.

Запускается GitHub Actions каждое утро в 08:00 МСК.
"""

import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

# ── стены VK (по ссылкам vkvideo.ru/@.../all) ──────────────────────────────
# gid — числовой id сообщества (без минуса), oid в ссылке = -gid
WALLS = [
    (114653103, "playkinooo"),
    (213555101, "kino_____max"),
    (220018529, "kinobro"),
    (218393997, "public218393997"),
]

FILMS_FILE = "efir-films.m3u"
GROUP_TITLE = "Фильмы"
MAX_NEW_FILMS = 40      # максимум новых фильмов за один прогон
FEATURED_TOP = 3        # сколько первых новинок помечать tvg-featured="1"

TMDB_KEY = os.environ.get("TMDB_KEY") or "269d08b7a165c8d4f1ed8ae4612811da"
TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMG = "https://image.tmdb.org/t/p"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# ── сеть ───────────────────────────────────────────────────────────────────

def _get(url, encoding="utf-8", headers=None, tries=3):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode(encoding, errors="replace")
        except Exception as e:            # noqa: BLE001
            last = e
            time.sleep(2)
    raise last


def fetch_widget(gid):
    """HTML виджета сообщества: свежие посты + видео, анонимно."""
    url = ("https://vk.com/widget_community.php?app=0&width=300&_ver=1"
           "&gid=%s&mode=4&w=300" % gid)
    return _get(url, encoding="cp1251", headers={"Cookie": "remixlang=0"})


def tmdb_json(path):
    sep = "&" if "?" in path else "?"
    url = "%s%s%sapi_key=%s" % (TMDB_API, path, sep, TMDB_KEY)
    return json.loads(_get(url, encoding="utf-8"))


# ── разбор виджета ─────────────────────────────────────────────────────────

def parse_widget(widget_html):
    """Возвращает список постов: [{date, text, videos:[{oid,id,label,hash}]}]."""
    posts = []
    for chunk in re.split(r'<div id="wpt-\d+_\d+"', widget_html)[1:]:
        d = re.search(r'class="wcommunity_post_date"[^>]*>([^<]+)<', chunk)
        date = d.group(1).strip() if d else ""
        t = re.search(r'class="wall_post_text"[^>]*>(.*?)</div>', chunk, re.S)
        text = ""
        if t:
            text = re.sub(r'<[^>]+>', " ", t.group(1))
            text = re.sub(r'\s+', " ", html.unescape(text)).strip()

        videos = []
        for m in re.finditer(
                r'data-video="(-?\d+)_(\d+)"[^>]*?aria-label="([^"]*)"', chunk):
            videos.append({
                "oid": m.group(1),
                "id": m.group(2),
                "label": html.unescape(m.group(3)).strip(),
                "hash": None,
            })
        # hash из ссылки (нужен для части видео)
        for hm in re.finditer(r'href="/video(-?\d+_\d+)\?([^"]+)"', chunk):
            hh = re.search(r'hash=([0-9a-f]+)', hm.group(2))
            for v in videos:
                if "%s_%s" % (v["oid"], v["id"]) == hm.group(1):
                    v["hash"] = hh.group(1) if hh else None
        posts.append({"date": date, "text": text, "videos": videos})
    return posts


def duration_seconds(label):
    total = 0
    for amount, unit in re.findall(r'(\d+)\s+(час\w*|минут\w*|секунд\w*)', (label or "").lower()):
        value = int(amount)
        if unit.startswith("час"):
            total += value * 3600
        elif unit.startswith("минут"):
            total += value * 60
        else:
            total += value
    return total


def video_kind(video, post_text):
    text = ((video.get("label") or "") + " " + (post_text or "")).lower()
    if any(marker in text for marker in ("сериал", "все серии", "сезон", "эпизод", "series", "season", "episode")):
        return "сериал"
    if any(marker in text for marker in ("коллекция", "сборник")):
        return "подборка"
    seconds = duration_seconds(video.get("label"))
    if seconds and seconds < 45 * 60:
        return "короткое видео"
    if seconds and seconds > 4 * 3600 + 30 * 60:
        return "многочасовая подборка"
    return None


# ── название и год из подписи видео ────────────────────────────────────────

def parse_title_year(label):
    """«Одиссея | The Odyssey (2026) длительностью 2 часа…» -> («Одиссея», 2026)."""
    s = html.unescape(label or "").strip()
    s = re.sub(r'^(Видео|Video)\s+', "", s, flags=re.I)
    # отрезаем служебный хвост VK (длительность / возрастное ограничение)
    s = re.split(r'\s+(?:длительностью|продолжительностью|Duration|Видео с возрастн)',
                 s, flags=re.I)[0]

    year = None
    my = re.search(r'\((\d{4})', s)
    if my:
        year = int(my.group(1))

    # всё после первой скобки — служебный хвост (4K, Дубляж, сезон, актёры …)
    title = s.split("(")[0]
    if not title.strip():
        title = s
    # «Русское название | Original (year)» -> берём русскую часть
    title = title.split("|")[0]
    title = re.split(r'\s+(?:HD|4K|UHD|Дубляж|Локализ)', title, flags=re.I)[0]
    title = re.sub(r'\b\d{4}\b', " ", title)
    title = re.sub(r'\[[^\]]*\]', " ", title)
    title = re.sub(r'\s+', " ", title).strip(" .|·-,–—-")
    if not title:
        title = s.split("(")[0].strip()
    return title, year


# ── TMDB ───────────────────────────────────────────────────────────────────

def _best(results, year):
    if year:
        for r in results:
            rd = (r.get("release_date") or "")[:4]
            if rd.isdigit() and abs(int(rd) - year) <= 1:
                return r
    return results[0]


def tmdb_lookup(title, year):
    """Возвращает метаданные фильма или None."""
    query = urllib.parse.quote(title)
    search = "/search/movie?language=ru-RU&query=%s" % query
    if year:
        search += "&year=%d" % year
    try:
        data = tmdb_json(search)
    except Exception:                     # noqa: BLE001
        return None
    results = data.get("results") or []
    if not results and year:              # год мог быть неверным — ищем без него
        try:
            data = tmdb_json("/search/movie?language=ru-RU&query=%s" % query)
            results = data.get("results") or []
        except Exception:                 # noqa: BLE001
            results = []
    if not results:
        return None

    hit = _best(results, year)
    try:
        det = tmdb_json("/movie/%d?language=ru-RU&append_to_response=videos"
                        % hit["id"])
    except Exception:                     # noqa: BLE001
        return None

    poster = det.get("poster_path")
    if not poster:                        # без постера карточка в приложении пустая
        return None

    trailer = None
    for v in (det.get("videos") or {}).get("results", []):
        if v.get("site") == "YouTube" and v.get("type") == "Trailer":
            trailer = "https://www.youtube.com/watch?v=%s" % v["key"]
            break

    genres = ", ".join(g["name"].lower() for g in det.get("genres", []))
    release = det.get("release_date") or ""
    return {
        "name": det.get("title") or det.get("original_title") or title,
        "year": release[:4] if release[:4].isdigit() else (str(year) if year else ""),
        "poster": "%s/w500%s" % (TMDB_IMG, poster),
        "backdrop": ("%s/w780%s" % (TMDB_IMG, det["backdrop_path"])
                     if det.get("backdrop_path") else ""),
        "genres": genres,
        "description": (det.get("overview") or "").strip(),
        "trailer": trailer or "",
    }


# ── построение строк m3u ───────────────────────────────────────────────────

def _attr(name, value):
    if value is None or value == "":
        return ""
    v = re.sub(r'[\r\n\t]+', " ", str(value))
    v = v.replace('"', "").strip()
    return ' %s="%s"' % (name, v) if v else ""


def build_entry(video, meta, featured):
    oid_abs = video["oid"].lstrip("-")
    key = "%s_%s" % (oid_abs, video["id"])
    name = meta["name"]
    year = meta["year"]

    line = "#EXTINF:-1"
    line += _attr("tvg-id", "vk-%s-%s" % (oid_abs, video["id"]))
    line += _attr("tvg-name", name)
    line += _attr("tvg-logo", meta["poster"])
    line += _attr("tvg-backdrop", meta["backdrop"])
    line += _attr("tvg-year", year)
    line += _attr("tvg-genre", meta["genres"])
    line += _attr("tvg-description", meta["description"])
    line += _attr("tvg-trailer", meta["trailer"])
    if featured:
        line += ' tvg-featured="1"'
    line += _attr("group-title", GROUP_TITLE)
    line += ",%s%s" % (name, (" (%s)" % year) if year else "")

    url = "https://vk.ru/video_ext.php?oid=%s&id=%s" % (video["oid"], video["id"])
    if video.get("hash"):
        url += "&hash=%s" % video["hash"]
    return key, line + "\n" + url + "\n"


def load_existing(path):
    """Возвращает (тело_плейлиста_без_#EXTM3U, ключи_oid_id, названия_фильмов)."""
    if not os.path.exists(path):
        return "", set(), set()
    with open(path, encoding="utf-8") as f:
        text = f.read()
    known = set()
    for m in re.finditer(r'tvg-id="vk-(\d+)-(\d+)"', text):
        known.add("%s_%s" % m.groups())
    for m in re.finditer(r'oid=(-?\d+)&(?:amp;)?id=(\d+)', text):
        known.add("%s_%s" % (m.group(1).lstrip("-"), m.group(2)))
    titles = set()
    for m in re.finditer(r'^#EXTINF.*$', text, re.M):
        disp = _display_name(m.group(0))
        ym = re.search(r'\((\d{4})\)\s*$', disp)
        titles.add(norm_title(re.sub(r'\((\d{4})\)\s*$', "", disp),
                              ym.group(1) if ym else None))
    body = text
    if body.lstrip().startswith("#EXTM3U"):
        body = body.lstrip()[len("#EXTM3U"):].lstrip("\n")
    return body, known, titles


def _display_name(line):
    """Имя фильма — текст после запятой, которая НЕ внутри кавычек."""
    quoted = False
    for i, c in enumerate(line):
        if c == '"':
            quoted = not quoted
        elif c == "," and not quoted:
            return line[i + 1:].strip()
    return ""


def norm_title(name, year):
    """Ключ дедупликации по названию: «Джейсон Борн» (2016) и (2016) — одно и то же."""
    n = re.sub(r'[^\w\s]', " ", (name or "").lower(), flags=re.U)
    n = re.sub(r'\s+', " ", n).strip()
    return "%s|%s" % (n, year or "")


# ── основной сценарий ──────────────────────────────────────────────────────

def main():
    body, known, titles = load_existing(FILMS_FILE)
    print("Уже в плейлисте: %d фильмов" % len(titles))

    # собираем свежие видео со всех стен, порядок = от новых к старым
    fresh = []
    seen = set()
    for gid, title in WALLS:
        try:
            widget = fetch_widget(gid)
        except Exception as e:            # noqa: BLE001
            print("  %-16s ОШИБКА: %s" % (title, e))
            continue
        posts = parse_widget(widget)
        print("  %-16s найдено видео: %d" % (title, sum(len(p["videos"]) for p in posts)))
        for post in posts:
            for v in post["videos"]:
                key = "%s_%s" % (v["oid"].lstrip("-"), v["id"])
                if key in known or key in seen:
                    continue
                seen.add(key)
                fresh.append((v, post["text"]))

    print("Новых видео-кандидатов: %d" % len(fresh))

    added, blocks = 0, []
    for v, post_text in fresh:
        if added >= MAX_NEW_FILMS:
            break
        kind = video_kind(v, post_text)
        if kind:
            print("   пропуск (%s): %s" % (kind, v["label"]))
            continue
        name, year = parse_title_year(v["label"])
        if len(name) < 2:
            continue
        meta = tmdb_lookup(name, year)
        if not meta:
            print("   пропуск (нет в TMDB): %s" % name)
            continue
        nk = norm_title(meta["name"], meta["year"])
        if nk in titles:
            print("   уже есть: %s (%s)" % (meta["name"], meta["year"]))
            continue
        titles.add(nk)
        key, entry = build_entry(v, meta, added < FEATURED_TOP)
        blocks.append(entry)
        added += 1
        print("   + %s (%s)  [%s_%s]" % (meta["name"], meta["year"], key.split("_")[0], key.split("_")[1]))

    if not blocks:
        print("Новых фильмов нет — файл не меняется.")
        return

    out = "#EXTM3U\n" + "".join(blocks) + body
    if not out.endswith("\n"):
        out += "\n"
    with open(FILMS_FILE, "w", encoding="utf-8") as f:
        f.write(out)
    print("Добавлено %d фильмов в начало %s" % (added, FILMS_FILE))


if __name__ == "__main__":
    sys.exit(main())
