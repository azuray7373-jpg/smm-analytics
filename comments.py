# -*- coding: utf-8 -*-
"""Анализ комментариев по ТЗ: вопросы, боли, страхи, возражения, позитив, негатив,
просьбы о новых темах, намерение купить/зарегистрироваться. Классификация —
словарная эвристика (без выдумывания: только разметка текста), LLM не обязательна.
Ключевые фразы со временем расширяются в словарях ниже."""
import csv, io, json, re
from datetime import date
from collections import Counter
from db import db, Comment, Channel, ContentItem

RULES = [
    ("покупка", ["купить", "сколько стоит", "цена", "как оплатить", "где купить", "прайс",
                 "стоимость", "заказать", "оформить"]),
    ("регистрация", ["записат", "зарегистрир", "как попасть", "ссылку на", "вебинар",
                     "регистрация", "оставить заявку", "попасть на"]),
    ("боль", ["не получается", "не могу", "устал", "болит", "проблема", "сложно", "тяжело",
              "не работает", "ошибк", "не выходит", "срываюсь", "не хватает", "мешает",
              "разочарован", "нет результата", "не помогает"]),
    ("возражение", ["дорого", "подорожает", "не поможет", "сомневаюсь", "развод", "скам",
                    "а вдруг", "а если не", "бессмысленно", "не верю", "прореклам"]),
    ("страх", ["боюсь", "страшно", "опасаюсь", "страх", "вдруг не смогу"]),
    ("идея", ["сделайте про", "было бы круто", "хочу увидеть", "пожалуйста сделайте",
              "можно видео про", "напишите про", "снимите про", "разберите"]),
    ("негатив", ["фигн", "бред", "ужас", "плохо", "не нравится", "достал", "бесит",
                 "отстой", "хуйн", "чушь", "ерунда"]),
    ("позитив", ["спасибо", "класс", "супер", "огонь", "полезно", "нравится", "топ",
                 "здорово", "отлично", "шедевр", "благодар"]),
]

QUESTION_WORDS = ("как", "что", "почему", "сколько", "где", "когда", "можно", "нужно",
                  "какой", "какая", "будет", "а если")


def classify(text):
    """Возвращает (main_type, [все метки]). Вопрос определяется синтаксически."""
    t = (text or "").lower()
    tags = []
    is_question = ("?" in t) or any(t.strip().startswith(w + " ") or t.strip().startswith(w + "?")
                                    for w in QUESTION_WORDS)
    for name, words in RULES:
        if any(w in t for w in words):
            tags.append(name)
    if is_question:
        tags.insert(0, "вопрос")
    priority = ["покупка", "регистрация", "боль", "возражение", "страх", "вопрос",
                "идея", "негатив", "позитив"]
    main = next((p for p in priority if p in tags), "прочее")
    return main, tags or ["прочее"]


def import_csv(fileobj):
    """CSV: date,channel_id,content_link(необяз.),author,text[,likes]"""
    text = fileobj.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    n = 0
    for row in reader:
        row = {k.strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
        txt = row.get("text") or row.get("текст") or row.get("comment")
        if not txt:
            continue
        d = row.get("date")
        try:
            d = date.fromisoformat((d or "")[:10])
        except ValueError:
            d = date.today()
        ch_id = None
        if row.get("channel_id"):
            try:
                ch_id = int(row["channel_id"])
            except ValueError:
                pass
        content_id = None
        link = row.get("content_link") or row.get("link")
        if link:
            ci = ContentItem.query.filter(ContentItem.link.like(f"%{link[:80]}%")).first()
            if ci:
                content_id = ci.id
        main, tags = classify(txt)
        try:
            likes = float(row.get("likes") or 0)
        except (TypeError, ValueError):
            likes = 0
        db.session.add(Comment(
            channel_id=ch_id, content_id=content_id, date=d,
            author=(row.get("author") or "")[:255], text=txt,
            likes=likes, main_type=main,
            tags=json.dumps(tags, ensure_ascii=False)))
        n += 1
    db.session.commit()
    return n


def digest(start: date, end: date, channel_id=None, limit=5):
    """Сводка по комментариям за период: счётчики типов + топ-списки."""
    q = Comment.query.filter(Comment.date >= start, Comment.date <= end)
    if channel_id:
        q = q.filter(Comment.channel_id == channel_id)
    comments = q.order_by(Comment.likes.desc()).all()
    counts = Counter(c.main_type for c in comments)
    def top(t, n=limit):
        items = [c for c in comments if t in (json.loads(c.tags or "[]"))]
        seen, out = set(), []
        for c in items:
            key = re.sub(r"\W+", "", c.text.lower())[:40]
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
            if len(out) >= n:
                break
        return out
    return {
        "total": len(comments),
        "counts": dict(counts),
        "questions": top("вопрос"),
        "pains": top("боль"),
        "objections": top("возражение"),
        "ideas": top("идея"),
        "intentions": top("покупка") + top("регистрация"),
        "negatives": top("негатив", 3),
    }


def digest_text(d):
    """Текстовая версия для AI-отчёта (только факты из данных)."""
    if not d["total"]:
        return ""
    L = [f"Комментариев за период: {d['total']}. Распределение: "
         + ", ".join(f"{k} — {v}" for k, v in sorted(d["counts"].items(), key=lambda x: -x[1])) + "."]
    for title, key in (("ТОП вопросы", "questions"), ("ТОП боли", "pains"),
                       ("ТОП возражения", "objections"), ("Идеи тем", "ideas"),
                       ("Намерения купить/зарегистрироваться", "intentions")):
        items = d[key]
        if items:
            L.append(f"{title}:")
            L.extend(f"- «{c.text[:120]}» (лайков {int(c.likes or 0)})" for c in items[:5])
    return "\n".join(L)
