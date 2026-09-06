# -*- coding: utf-8 -*-
"""AI-релей: генерирует LLM-контент на GitHub Actions (где Gemini доступен)
и отправляет результат в приложение. Запускается 4 раза в сутки вместе с релеем данных."""
import json, os, sys, requests
from datetime import date, timedelta

PA_URL = os.environ.get("PA_URL", "").rstrip("/")
INGEST = os.environ.get("PA_INGEST_TOKEN")
AI_KEY = os.environ.get("AI_API_KEY", "")
AI_BASE = os.environ.get("AI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
AI_MODEL = os.environ.get("AI_MODEL", "gemini-2.5-flash")

SYSTEM = (
    "Ты — AI SMM-специалист школы сыроделия. Тебе даны РЕАЛЬНЫЕ данные из аналитики. "
    "Правила: используй ТОЛЬКО числа из данных; нет данных — «недостаточно данных»; "
    "давай КОНКРЕТНЫЕ рекомендации; 3-7 предложений по делу; отвечай по-русски."
)


def call_llm(user_prompt):
    for model in (AI_MODEL, "gemini-2.5-flash"):
        try:
            r = requests.post(
                f"{AI_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {AI_KEY}"},
                json={"model": model, "temperature": 0.2,
                      "messages": [{"role": "system", "content": SYSTEM},
                                   {"role": "user", "content": user_prompt}]},
                timeout=120)
            d = r.json()
            if r.status_code == 200 and d.get("choices"):
                text = d["choices"][0]["message"].get("content", "")
                if text.strip():
                    return text
        except Exception as e:
            print(f"  model {model}: {e}")
    return None


def get_context():
    """Получаем контекст данных из приложения."""
    r = requests.get(f"{PA_URL}/api/ai_context",
                     params={"token": INGEST}, timeout=60)
    r.raise_for_status()
    return r.json()


def push_result(kind, text, metadata=None):
    """Отправляем AI-результат в приложение."""
    r = requests.post(f"{PA_URL}/api/ai_result",
                      json={"kind": kind, "text": text, "meta": metadata or {}},
                      headers={"X-Ingest-Token": INGEST}, timeout=60)
    print(f"  push {kind}: {r.status_code}")
    return r.status_code == 200


def main():
    if not all([PA_URL, INGEST, AI_KEY]):
        sys.exit("Нужны PA_URL, PA_INGEST_TOKEN, AI_API_KEY")
    print("=== AI Relay ===")
    ctx = get_context()
    print(f"context получен: {len(json.dumps(ctx))} байт")

    # 1. Еженедельный отчёт (если понедельник или нет свежего)
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    prompt = (
        f"Составь недельный отчёт для SMM-команды школы сыроделия.\n"
        f"Данные за неделю:\n{json.dumps(ctx.get('week', {}), ensure_ascii=False, default=str)}\n"
        f"По каналам:\n{json.dumps(ctx.get('channels', []), ensure_ascii=False, default=str)}\n"
        f"Структура: Общая картина; Что дало рост; Что ухудшилось; "
        f"Рекомендации: УСИЛИТЬ / ИЗМЕНИТЬ / УБРАТЬ.")
    text = call_llm(prompt)
    if text:
        push_result("weekly_report", text, {"start": str(week_start), "end": str(week_start + timedelta(days=6))})
        print(f"  weekly_report: {len(text)} символов")

    # 2. Контент-план на следующую неделю
    trends = ctx.get("trends", [])
    comments = ctx.get("comments_summary", {})
    prompt = (
        f"Составь контент-план на следующую неделю: РОВНО 7 идей.\n"
        f"Для каждой: тема, формат (reels/post/story), день+час, CTA, UTM-метка.\n"
        f"Тренды рубрик:\n{json.dumps(trends[:5], ensure_ascii=False)}\n"
        f"Боли аудитории:\n{json.dumps(comments.get('top_pains', []), ensure_ascii=False)}\n"
        f"В конце: «Чего избегать» (2 пункта).")
    text = call_llm(prompt)
    if text:
        push_result("content_plan", text, {"start": str(week_start), "end": str(week_start + timedelta(days=6))})
        print(f"  content_plan: {len(text)} символов")

    # 3. Анализ комментариев
    if comments.get("total", 0) > 0:
        prompt = (
            f"Проанализируй комментарии аудитории за неделю.\n"
            f"Всего: {comments['total']}\n"
            f"Топ боли: {json.dumps(comments.get('top_pains', []), ensure_ascii=False)}\n"
            f"Топ вопросы: {json.dumps(comments.get('top_questions', []), ensure_ascii=False)}\n"
            f"Напиши: 3 главных боли, 2 идеи для контента, 1 предупреждение.")
        text = call_llm(prompt)
        if text:
            push_result("comments_summary", text)
            print(f"  comments_summary: {len(text)} символов")

    print("=== AI Relay завершён ===")


if __name__ == "__main__":
    main()
