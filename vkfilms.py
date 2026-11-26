#!/usr/bin/env python3
"""
VK Film Harvester — собирает свежие фильмы из галерей VK Видео.

Источник — анонимный API catalog.getVideo, который использует страница
vkvideo.ru/@group/all. Метаданные (постер, фон, год, жанры, описание, трейлер)
подтягиваются из TMDB. Новинки добавляются в НАЧАЛО плейлиста по дате загрузки.

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
FEATURED_TOP = 3        # сколько первых новинок помечать tvg-featured="1"
SEASONAL_TOP = 7
SEASONAL_TITLE_PRIORITY = {
    "один дома": 100,
    "один дома 2 затерянный в нью йорке": 99,
    "крепкий орешек": 98,
}
SEASONAL_TITLE_MARKERS = (
    "рождеств", "новый год", "новогод", "один дома", "санта",
    "дед мороз", "christmas", "xmas", "santa claus", "new year",
    "scrooge", "скрудж", "гринч", "полярный экспресс",
)
SEASONAL_DESCRIPTION_MARKERS = (
    "рождеств", "новый год", "новогод", "дед мороз", "санта-клаус",
    "санта клаус", "ёлка", "елка", "снеговик", "christmas",
    "new year", "santa claus",
)
MIN_FILM_SECONDS = 45 * 60
MAX_FILM_SECONDS = 4 * 3600 + 30 * 60
TITLE_ALIASES = {
    "чемпион с мадон сок": ("Чемпион", 2018),
    "гильдия убийств": ("Гильдия убийц", 2024),
    "хартленд": ("В сердце Нашвилла", 2026),
}
VK_API = "https://api.vkvideo.ru/method/"
VK_CLIENT_ID = "52461373"

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


def _post_json(url, params, token=None, tries=3, method="POST"):
    headers = {
        "User-Agent": UA,
        "Origin": "https://vkvideo.ru",
        "Referer": "https://vkvideo.ru/",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    query = urllib.parse.urlencode(params)
    if method == "POST":
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = query.encode()
    else:
        body = None
        url += ("&" if "?" in url else "?") + query
    last = None
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:            # noqa: BLE001
            last = e
            time.sleep(2)
    raise last


def anonymous_token():
    payload = _post_json("https://login.vk.ru/", {
        "act": "get_anonym_token",
        "client_id": VK_CLIENT_ID,
        "scopes": "",
        "isApiOauthAnonymEnabled": "false",
    }, method="GET")
    token = (payload.get("data") or {}).get("access_token")
    if not token:
        raise RuntimeError("VK не вернул анонимный токен")
    return token


def fetch_gallery(screen_name, token):
    payload = _post_json(VK_API + "catalog.getVideo", {
        "v": "5.289",
        "client_id": VK_CLIENT_ID,
        "vk_platform": "vkvideo",
        "url": "https://vkvideo.ru/@%s/all" % screen_name,
        "need_blocks": "1",
        "applied_toggles": "[]",
        "enabled_features": "[]",
    }, token)
    response = payload.get("response")
    if not isinstance(response, dict):
        raise RuntimeError("VK не вернул видеогалерею")
    videos = []
    for item in response.get("videos") or []:
        videos.append({
            "oid": str(item.get("owner_id") or ""),
            "id": str(item.get("id") or ""),
            "label": (item.get("title") or "").strip(),
            "hash": item.get("access_key") or None,
            "date": int(item.get("date") or 0),
            "duration": int(item.get("duration") or 0),
            "description": (item.get("description") or "").strip(),
        })
    return videos


def tmdb_json(path):
    sep = "&" if "?" in path else "?"
    url = "%s%s%sapi_key=%s" % (TMDB_API, path, sep, TMDB_KEY)
    return json.loads(_get(url, encoding="utf-8"))


# ── фильтрация видео ────────────────────────────────────────────────────────

def duration_seconds(label, seconds=None):
    if seconds is not None:
        return int(seconds or 0)
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


def video_kind(video, post_text=""):
    text = ((video.get("label") or "") + " " + (post_text or "")).lower()
    if any(marker in text for marker in (
            "сериал", "все серии", "сезон", "эпизод", "серия",
            "series", "season", "episode")):
        return "сериал"
    if any(marker in text for marker in (
            "коллекция", "сборник", "трейлер", "trailer",
            "клип", "clip", "анонс", "teaser")):
        return "подборка или клип"
    seconds = duration_seconds(video.get("label"), video.get("duration"))
    if seconds and seconds < MIN_FILM_SECONDS:
        return "короткое видео"
    if seconds and seconds > MAX_FILM_SECONDS:
        return "многочасовая подборка"
    return None


# ── название и год из подписи видео ────────────────────────────────────────

def parse_title_year(label):
    """«Одиссея | The Odyssey (2026) длительностью 2 часа…» -> («Одиссея», 2026)."""
    s = html.unescape(label or "").strip()
    s = re.sub(r'^(Видео|Video|Фильм)\s+', "", s, flags=re.I)
    # отрезаем служебный хвост VK (длительность / возрастное ограничение)
    s = re.split(r'\s+(?:длительностью|продолжительностью|Duration|Видео с возрастн)',
                 s, flags=re.I)[0]
    s = re.split(r',\s*(?:смотреть|скачать|онлайн)\b', s, flags=re.I)[0]

    year = None
    my = re.search(r'\(((?:19|20)\d{2})', s)
    if not my:
        my = re.search(r'(?:,|\s)((?:19|20)\d{2})\s*$', s)
    if my:
        year = int(my.group(1))

    # всё после первой скобки — служебный хвост (4K, Дубляж, сезон, актёры …)
    title = s.split("(")[0]
    if not title.strip():
        title = s
    # «Русское название | Original (year)» -> берём русскую часть
    title = title.split("|")[0]
    title = re.split(r'\s+(?:HD|4K|UHD|Дубляж|Локализ)', title, flags=re.I)[0]
    title = re.sub(r',\s*(?:19|20)\d{2}\s*$', "", title)
    if year:
        title = re.sub(r'\b(?:19|20)\d{2}\b', " ", title)
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
    alias_key = re.sub(r'\s+', " ", title.strip().casefold())
    query_title, year = TITLE_ALIASES.get(alias_key, (title, year))
    query = urllib.parse.quote(query_title)
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
        "runtime": int(det.get("runtime") or 0),
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
    """Ключ дедупликации по названию без языковой пометки в скобках."""
    n = re.sub(r'\(\s*укр\.?\s*\)', " ", (name or "").lower(), flags=re.U)
    n = re.sub(r'[^\w\s]', " ", n, flags=re.U)
    n = re.sub(r'\s+', " ", n).strip()
    return "%s|%s" % (n, year or "")


def _entry_key(entry):
    match = re.search(r'tvg-id="vk-(\d+)-(\d+)"', entry)
    return "%s_%s" % match.groups() if match else None


def seasonal_score(name, description=""):
    key = norm_title(name, None).split("|", 1)[0]
    priority = SEASONAL_TITLE_PRIORITY.get(key)
    if priority:
        return priority
    title = (name or "").casefold()
    text = (description or "").casefold()
    score = 0
    if any(marker in title for marker in SEASONAL_TITLE_MARKERS):
        score += 3
    if any(marker in text for marker in SEASONAL_DESCRIPTION_MARKERS):
        score += 1
    return score


def mark_seasonal_entries(text):
    parts = re.split(r'(?=^#EXTINF)', text, flags=re.M)
    header, entries = parts[0], parts[1:]
    cleaned, candidates = [], []
    for index, entry in enumerate(entries):
        entry = re.sub(r'\s+tvg-seasonal="[^"]*"', "", entry)
        cleaned.append(entry)
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', entry))
        name = html.unescape(attrs.get("tvg-name") or _display_name(entry.splitlines()[0]))
        score = seasonal_score(name, html.unescape(attrs.get("tvg-description", "")))
        if score:
            candidates.append((-score, index, name))
    selected = sorted(candidates)[:SEASONAL_TOP]
    for _, index, _ in selected:
        entry = cleaned[index]
        if ' group-title=' in entry:
            entry = entry.replace(' group-title=', ' tvg-seasonal="1" group-title=', 1)
        else:
            entry = entry.replace('\n', ' tvg-seasonal="1"\n', 1)
        cleaned[index] = entry
    return header + "".join(cleaned), [name for _, _, name in selected]


def merge_recent_entries(body, new_entries, dates):
    parts = re.split(r'(?=^#EXTINF)', body, flags=re.M)
    header, entries = parts[0], parts[1:]
    recent, rest = [], []
    for entry in entries:
        key = _entry_key(entry)
        if key in dates:
            recent.append((dates[key], entry))
        else:
            rest.append(entry)
    for entry in new_entries:
        recent.append((dates[_entry_key(entry)], entry))
    recent.sort(key=lambda item: item[0], reverse=True)

    ordered = []
    for index, (_, entry) in enumerate(recent):
        entry = re.sub(r'\s+tvg-featured="1"', "", entry)
        if index < FEATURED_TOP:
            entry = entry.replace(' group-title=', ' tvg-featured="1" group-title=', 1)
        ordered.append(entry)
    rest = [re.sub(r'\s+tvg-featured="1"', "", entry) for entry in rest]
    return header + "".join(ordered + rest)


# ── основной сценарий ──────────────────────────────────────────────────────

def main():
    body, known, titles = load_existing(FILMS_FILE)
    print("Уже в плейлисте: %d фильмов" % len(titles))

    try:
        token = anonymous_token()
    except Exception as e:            # noqa: BLE001
        print("ОШИБКА получения анонимного доступа VK: %s" % e)
        return 1

    fresh = []
    seen = set()
    gallery_dates = {}
    for gid, title in WALLS:
        try:
            videos = fetch_gallery(title, token)
        except Exception as e:        # noqa: BLE001
            print("  %-16s ОШИБКА: %s" % (title, e))
            continue
        print("  %-16s найдено видео: %d" % (title, len(videos)))
        for v in videos:
            if not v["oid"] or not v["id"]:
                continue
            key = "%s_%s" % (v["oid"].lstrip("-"), v["id"])
            gallery_dates[key] = v.get("date", 0)
            if key in known or key in seen:
                continue
            seen.add(key)
            fresh.append(v)

    fresh.sort(key=lambda v: v.get("date", 0), reverse=True)
    print("Новых видео-кандидатов: %d" % len(fresh))

    added, blocks = 0, []
    for v in fresh:
        kind = video_kind(v, v.get("description"))
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
        if meta["runtime"] and meta["runtime"] * 60 < MIN_FILM_SECONDS:
            print("   пропуск (короткометражка): %s (%s мин)" % (
                meta["name"], meta["runtime"]))
            continue
        nk = norm_title(meta["name"], meta["year"])
        if nk in titles:
            print("   уже есть: %s (%s)" % (meta["name"], meta["year"]))
            continue
        titles.add(nk)
        key, entry = build_entry(v, meta, added < FEATURED_TOP)
        blocks.append(entry)
        added += 1
        print("   + %s (%s)  [%s_%s]" % (
            meta["name"], meta["year"], key.split("_")[0], key.split("_")[1]))

    if blocks:
        out = "#EXTM3U\n" + merge_recent_entries(body, blocks, gallery_dates)
    else:
        out = "#EXTM3U\n" + body
    out, seasonal_names = mark_seasonal_entries(out)
    if not out.endswith("\n"):
        out += "\n"

    current = ""
    if os.path.exists(FILMS_FILE):
        with open(FILMS_FILE, encoding="utf-8") as f:
            current = f.read()
    if out == current:
        print("Новых фильмов нет — файл не меняется.")
        print("Сезонные баннеры (%d): %s" % (
            len(seasonal_names), ", ".join(seasonal_names) or "нет"))
        return

    with open(FILMS_FILE, "w", encoding="utf-8") as f:
        f.write(out)
    if added:
        print("Добавлено %d фильмов в начало %s" % (added, FILMS_FILE))
    print("Сезонные баннеры (%d): %s" % (
        len(seasonal_names), ", ".join(seasonal_names) or "нет"))


if __name__ == "__main__":
    sys.exit(main())
