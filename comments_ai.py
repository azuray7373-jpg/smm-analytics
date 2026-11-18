# -*- coding: utf-8 -*-
"""AI-парсинг комментариев: сбор + классификация GLM.

Сбор:
- YouTube — автоматически по API (видео обоих каналов из базы, ключ уже работает);
- Instagram/TikTok/Дзен/VK — площадки не отдают комментарии сторонним сервисам:
  импорт CSV на экране «Комментарии» (инструкция там же), вся классификация — AI.

Классификация: GLM-5.3 (строгий JSON) с фолбэком на словарную эвристику.
Каждый комментарий получает main_type (вопрос/боль/возражение/страх/идея/
покупка/регистрация/позитив/негатив/прочее), sentiment (хороший/плохой/нейтральный)
и теги. Плохие = боли, возражения, страхи, негатив — то, что чинить.
"""
import json
import re
from datetime import date, datetime

import requests as _rq
from db import db, get_setting, Comment, Channel, ContentItem

GOOD = {"позитив", "идея", "покупка", "регистрация"}
BAD = {"боль", "возражение", "страх", "негатив"}

_SYS = (
    "Ты — аналитик аудитории школы сыроделия. Классифицируй комментарии зрителей. "
    "Для каждого верни JSON-объект: {\"main\": один из [вопрос, боль, возражение, "
    "страх, идея, покупка, регистрация, позитив, негатив, прочее], "
    "\"sentiment\": один из [хороший, плохой, нейтральный], "
    "\"tags\": [до 3 уточнений своими словами, по-русски]}. "
    "Определяй по смыслу, а не по наличию слов: «опять не получилось» — боль; "
    "«сколько стоит полный курс» — покупка; «боюсь, что не получится» — страх; "
    "«дорого» — возражение; «снимите про козий сыр» — идея. "
    "Рекламные/заводные комментарии ботов — негатив. Отвечай ТОЛЬКО валидным "
    "JSON-массивом той же длины, что и вход, без пояснений."
)


def _glml_key():
    return get_setting("ai_api_key")


def classify_batch(texts, batch=40):
    """GLM-классификация списка текстов → [{main, sentiment, tags}].
    При сбое — словарная эвристика (фолбэк, классификация не останавливается)."""
    import comments as cm
    out = []
    for i in range(0, len(texts), batch):
        chunk = [str(t or "")[:600] for t in texts[i:i + batch]]
        res = None
        key = _glml_key()
        if key:
            try:
                base = (get_setting("ai_base_url") or "https://api.z.ai/api/paas/v4").rstrip("/")
                model = get_setting("ai_model") or "glm-4.6"
                payload = json.dumps(
                    [{"i": n, "text": t} for n, t in enumerate(chunk)], ensure_ascii=False)
                r = _rq.post(f"{base}/chat/completions",
                             headers={"Authorization": f"Bearer {key}"},
                             json={"model": model, "temperature": 0.1,
                                   "thinking": {"type": "disabled"},  # классификация — не рассуждение
                                   "messages": [{"role": "system", "content": _SYS},
                                                {"role": "user", "content": payload}]},
                             timeout=(5, 90))
                if r.status_code == 400:
                    body = {"model": model, "temperature": 0.1,
                            "messages": [{"role": "system", "content": _SYS},
                                         {"role": "user", "content": payload}]}
                    r = _rq.post(f"{base}/chat/completions",
                                 headers={"Authorization": f"Bearer {key}"},
                                 json=body, timeout=(5, 90))
                d = r.json()
                if r.status_code == 200 and d.get("choices"):
                    raw = (d["choices"][0]["message"].get("content") or "").strip()
                    m = re.search(r"\[.*\]", raw, re.S)
                    if m:
                        parsed = json.loads(m.group(0))
                        if isinstance(parsed, list) and len(parsed) == len(chunk):
                            res = parsed
            except Exception:
                res = None
        if res is None:
            res = []
            for t in chunk:
                main, tags = cm.classify(t)
                sent = "хороший" if main in GOOD else ("плохой" if main in BAD else "нейтральный")
                res.append({"main": main, "sentiment": sent, "tags": tags})
        for item in res:
            main = str(item.get("main", "прочее")).lower()
            if main not in {"вопрос", "боль", "возражение", "страх", "идея", "покупка",
                            "регистрация", "позитив", "негатив", "прочее"}:
                main = "прочее"
            sent = str(item.get("sentiment", "нейтральный"))
            if sent not in ("хороший", "плохой", "нейтральный"):
                sent = "плохой" if main in BAD else ("хороший" if main in GOOD else "нейтральный")
            tags = item.get("tags") or []
            if not isinstance(tags, list):
                tags = [str(tags)]
            out.append({"main": main, "sentiment": sent,
                        "tags": [str(x)[:64] for x in tags[:3]]})
    return out


# ═══════════════ СБОР: YouTube (API-ключ уже в настройках) ═══════════════

def fetch_youtube_comments(max_videos=8, max_per_video=100):
    """Комментарии к свежим видео обоих YouTube-каналов (по видео из базы)."""
    key = get_setting("youtube_api_key")
    if not key:
        return {"error": "нет youtube_api_key"}
    videos = (ContentItem.query
              .filter(ContentItem.link.like("https://www.youtube.com/watch?v=%"))
              .order_by(ContentItem.published_at.desc().nullslast())
              .limit(60).all())
    rows, seen_ch = [], {}
    for v in videos:
        m = re.search(r"[?&]v=([\w-]{6,})", v.link or "")
        if not m:
            continue
        ch = v.channel
        if seen_ch.get(ch.id, 0) >= max_videos:
            continue
        try:
            r = _rq.get("https://www.googleapis.com/youtube/v3/commentThreads",
                        params={"part": "snippet", "videoId": m.group(1),
                                "maxResults": max_per_video,
                                "textFormat": "plainText", "key": key}, timeout=30)
            if r.status_code == 403:   # комментарии отключены на видео
                continue
            r.raise_for_status()
        except Exception:
            continue
        seen_ch[ch.id] = seen_ch.get(ch.id, 0) + 1
        for it in r.json().get("items", []):
            s = it.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            txt = (s.get("textDisplay") or "").strip()
            if not txt:
                continue
            dt = s.get("publishedAt", "")[:10]
            rows.append({
                "channel_id": ch.id, "content_id": v.id,
                "author": (s.get("authorDisplayName") or "")[:255],
                "text": txt[:4000],
                "likes": s.get("likeCount") or 0,
                "date": dt or date.today().isoformat(),
                "video": (v.title or "")[:80],
            })
    return {"rows": rows, "videos": sum(seen_ch.values())}


def save_classified(rows, source="youtube_api"):
    """Дедупликация + GLM-классификация + сохранение. Возвращает сводку."""
    if not rows:
        return {"new": 0, "skipped": 0, "good": 0, "bad": 0, "questions": 0}
    existing = {(c.channel_id, (c.author or ""), (c.text or "")[:300], c.date)
                for c in Comment.query.filter(Comment.date >= date(2026, 1, 1)).all()}
    fresh = [r for r in rows
             if (r["channel_id"], r["author"], r["text"][:300],
                 datetime.strptime(r["date"][:10], "%Y-%m-%d").date() if isinstance(r["date"], str) else r["date"]) not in existing]
    skipped = len(rows) - len(fresh)
    cls = classify_batch([r["text"] for r in fresh]) if fresh else []
    good = bad = questions = 0
    for r, c in zip(fresh, cls):
        d = r["date"]
        if isinstance(d, str):
            try:
                d = datetime.strptime(d[:10], "%Y-%m-%d").date()
            except ValueError:
                d = date.today()
        db.session.add(Comment(channel_id=r["channel_id"], content_id=r.get("content_id"),
                               date=d, author=r["author"], text=r["text"],
                               likes=r.get("likes") or 0,
                               main_type=c["main"], tags=json.dumps(c["tags"], ensure_ascii=False),
                               source=source))
        if c["sentiment"] == "хороший":
            good += 1
        elif c["sentiment"] == "плохой":
            bad += 1
        if c["main"] == "вопрос":
            questions += 1
    db.session.commit()
    return {"new": len(fresh), "skipped": skipped, "good": good,
            "bad": bad, "questions": questions}


def collect_youtube():
    """Собрать комментарии YouTube и классифицировать GLM. Для /collect и кнопки."""
    got = fetch_youtube_comments()
    if got.get("error"):
        return got
    summary = save_classified(got["rows"])
    summary["videos"] = got["videos"]
    return summary


def reclassify_recent(days=30):
    """Переклассифицировать существующие комментарии GLM (например, после импорта CSV)."""
    from datetime import timedelta
    since = date.today() - timedelta(days=days)
    items = Comment.query.filter(Comment.date >= since).all()
    if not items:
        return {"new": 0, "good": 0, "bad": 0, "questions": 0, "skipped": 0}
    cls = classify_batch([c.text for c in items])
    good = bad = questions = 0
    for c, x in zip(items, cls):
        c.main_type = x["main"]
        c.tags = json.dumps(x["tags"], ensure_ascii=False)
        if x["sentiment"] == "хороший":
            good += 1
        elif x["sentiment"] == "плохой":
            bad += 1
        if x["main"] == "вопрос":
            questions += 1
    db.session.commit()
    return {"new": len(items), "skipped": 0, "good": good, "bad": bad,
            "questions": questions}
