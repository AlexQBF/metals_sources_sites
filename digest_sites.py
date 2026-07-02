#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ТЕСТОВЫЙ бот-дайджест по золоту и серебру из RSS-САЙТОВ (дубль основного бота).
Отличия от основного: источник — RSS-ленты (feeds_sites.json), котировок НЕТ,
свои файлы памяти/архива, свой Telegram-канал (свои секреты с суффиксом _SITES).

Поток:
  1. Читает ленты из feeds_sites.json
  2. Берёт записи за окно (будни 36ч, понедельник 72ч)
  3. Отсеивает уже отправленные (sent_sites.json)
  4. Gemini: отбор золото/серебро, склейка дублей, важность, дайджест абзацами
  5. Шлёт в тестовый Telegram-канал
  6. Сохраняет архив digests_sites/ и журналы

Секреты (GitHub Actions Secrets):
  TELEGRAM_BOT_TOKEN_SITES, TELEGRAM_CHAT_ID_SITES  — токен и id ТЕСТОВОГО канала
  AI_API_KEY, AI_BASE_URL, AI_MODEL                 — те же, что у основного (Gemini)
"""

import os
import re
import json
import time
import html
from datetime import datetime, timezone, timedelta

import requests
import feedparser

FEEDS_FILE = "feeds_sites.json"
SENT_FILE = "sent_sites.json"
RECENT_DIGESTS_FILE = "recent_digests_sites.json"
DIGESTS_DIR = "digests_sites"
MAX_ITEMS_TO_AI = 120
RECENT_DIGESTS_KEEP = 5
REQUEST_TIMEOUT = 25

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN_SITES", "").strip()
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID_SITES", "").strip()

AI_KEY = os.environ.get("AI_API_KEY", "").strip()
AI_BASE = os.environ.get("AI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai").strip().rstrip("/")
AI_MODEL = os.environ.get("AI_MODEL", "gemini-2.5-flash").strip()

MSK = timezone(timedelta(hours=3))


def get_hours_window():
    return 72 if datetime.now(MSK).weekday() == 0 else 36


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_feeds():
    data = load_json(FEEDS_FILE, {"feeds": []})
    out = []
    for it in data.get("feeds", []):
        if it.get("enabled", True) and it.get("url"):
            out.append({"name": it.get("name", it["url"]), "url": it["url"]})
    return out


def entry_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def entry_id(entry, feed_name):
    # уникальный ключ записи: ссылка или guid, иначе имя+заголовок
    return entry.get("link") or entry.get("id") or f"{feed_name}:{entry.get('title','')}"


def collect_all(feeds, sent_ids):
    hours = get_hours_window()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    items = []
    for feed in feeds:
        try:
            resp = requests.get(feed["url"], timeout=REQUEST_TIMEOUT,
                                headers={"User-Agent": "Mozilla/5.0 (DigestSitesBot/1.0)"})
            parsed = feedparser.parse(resp.content)
        except Exception as e:
            print(f"[!] {feed['name']}: ОШИБКА — {e}")
            continue

        got = len(parsed.entries)
        if getattr(parsed, "bozo", 0) and got == 0:
            print(f"[!] {feed['name']}: ответ не похож на RSS (HTTP {resp.status_code}) — записей 0")
            continue

        fresh = 0
        for entry in parsed.entries:
            eid = entry_id(entry, feed["name"])
            if eid in sent_ids:
                continue
            ts = entry_time(entry)
            if ts and ts < cutoff:
                continue
            title = html.unescape(entry.get("title", "").strip())
            summary = html.unescape(re.sub(r"<[^>]+>", "", entry.get("summary", ""))).strip()
            if len(summary) > 700:
                summary = summary[:700] + "…"
            items.append({
                "id": eid,
                "source": feed["name"],
                "title": title,
                "summary": summary,
                "link": entry.get("link", ""),
            })
            fresh += 1
        print(f"[i] {feed['name']}: получено {got}, новых за {hours}ч {fresh}")
        time.sleep(0.3)

    print(f"[i] Итого новых записей: {len(items)}")
    return items[:MAX_ITEMS_TO_AI]


AI_SYSTEM = (
    "Ты — редактор ежедневного дайджеста «AU и AG. Главное за день» по рынку ЗОЛОТА и СЕРЕБРА. "
    "Тебе дают материалы с отраслевых сайтов за последние ~1,5 суток и список тем прошлых дайджестов.\n\n"
    "ЖЁСТКИЙ ФИЛЬТР ТЕМЫ: оставляй ТОЛЬКО золото и серебро (цены, спрос/предложение, добыча, запасы, "
    "прогнозы, отчёты, решения регуляторов/ЦБ, сделки, проекты). СТРОГО ВЫБРАСЫВАЙ платину, палладий, МПГ, "
    "медь, никель, сталь, уголь, нефть, алмазы и прочее. Если материал в основном про них — выбрасывай целиком.\n\n"
    "ТОЛЬКО НОВОСТИ И СОБЫТИЯ: что-то произошло/изменилось/заявлено/куплено/выросло/упало. "
    "Образовательные и технические материалы без новостного повода — отсекай.\n\n"
    "ОЦЕНКА ВАЖНОСТИ: по каждой новости оцени, насколько это значимо для рынка золота/серебра и отрасли в целом "
    "(масштаб события, влияние на цены, размер компании/сделки, значимость для рынка РФ и мира). "
    "Сначала ставь самое важное, затем менее важное — строго по убыванию значимости. "
    "Проходное и мелкое убирай.\n\n"
    "Склеивай дубли (одно событие из разных источников = один пункт). Не повторяй темы прошлых дайджестов.\n"
    "Пунктов: до 10, но НЕ добивай до 10 искусственно — если действительно значимого меньше, дай меньше "
    "(лучше 5 важных, чем 10 с мусором).\n\n"
    "ФОРМАТ: каждый пункт —\n"
    "▪️ <b>Заголовок</b>\n"
    "и 1-2 ёмких предложения по сути.\n"
    "ЗАГОЛОВОК должен быть КОРОТКИМ и хлёстким — 3-6 слов, как в новостной ленте, а не целое предложение. "
    "Не пересказывай в заголовке всю суть, только цепляющая суть темы. Детали — в описании.\n"
    "В КОНЦЕ текста каждого пункта добавь ссылку на источник в виде названия источника: "
    "<a href=\"URL\">Название источника</a>. Название и URL бери ТОЛЬКО из полей «Источник» и «Ссылка» "
    "соответствующего материала — НИЧЕГО НЕ ВЫДУМЫВАЙ. Если у материала нет ссылки — не добавляй ссылку вообще. "
    "При склейке дублей укажи ссылку одного, самого содержательного источника.\n"
    "Между пунктами пустая строка. Используй только HTML-теги <b> и <a>. Без Markdown, без вступления/заключения.\n"
    "Если значимого нет — верни ровно: НЕТ_НОВОСТЕЙ"
)


def make_digest_ai(items, recent_topics):
    blocks = []
    for i, p in enumerate(items, 1):
        blocks.append(f"[{i}] Источник: {p['source']}\nСсылка: {p['link']}\nЗаголовок: {p['title']}\nТекст: {p['summary']}")
    user = "МАТЕРИАЛЫ:\n\n" + "\n\n".join(blocks)
    if recent_topics:
        user += "\n\nТЕМЫ ПРОШЛЫХ ДАЙДЖЕСТОВ (не повторять):\n" + "\n".join(f"- {t}" for t in recent_topics)

    payload = {"model": AI_MODEL, "messages": [
        {"role": "system", "content": AI_SYSTEM}, {"role": "user", "content": user}], "temperature": 0.4}
    backoff = [30, 60, 90]
    last_err = None
    total = len(backoff) + 1
    for attempt in range(1, total + 1):
        try:
            resp = requests.post(f"{AI_BASE}/chat/completions",
                                 headers={"Authorization": f"Bearer {AI_KEY}", "Content-Type": "application/json"},
                                 json=payload, timeout=120)
            if resp.status_code >= 500:
                last_err = f"HTTP {resp.status_code}"
                if attempt <= len(backoff):
                    print(f"[i] Gemini {resp.status_code}, попытка {attempt}/{total}, жду {backoff[attempt-1]}с…")
                    time.sleep(backoff[attempt-1])
                continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except requests.exceptions.RequestException as e:
            last_err = str(e)
            if attempt <= len(backoff):
                print(f"[i] Сбой Gemini (попытка {attempt}/{total}): {e}, жду {backoff[attempt-1]}с…")
                time.sleep(backoff[attempt-1])
    raise RuntimeError(f"Gemini недоступен после {total} попыток: {last_err}")


def clean_html_for_telegram(text):
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    allowed = {"b", "i", "u", "s", "a", "code", "pre"}
    def strip_tag(m):
        tag = m.group(1).lower().lstrip("/")
        return m.group(0) if tag in allowed else ""
    return re.sub(r"</?\s*([a-zA-Z0-9]+)[^>]*>", strip_tag, text)


def send_to_telegram(text):
    text = clean_html_for_telegram(text)
    if not TG_TOKEN or not TG_CHAT:
        print("[!] Нет TELEGRAM_BOT_TOKEN_SITES / TELEGRAM_CHAT_ID_SITES — вывод в лог:")
        print(text)
        return
    LIMIT = 3800
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > LIMIT:
            if cur: chunks.append(cur); cur = ""
            chunks.append(line[:LIMIT]); line = line[LIMIT:]
        if len(cur) + len(line) + 1 > LIMIT:
            chunks.append(cur); cur = line
        else:
            cur = cur + "\n" + line if cur else line
    if cur: chunks.append(cur)
    for chunk in chunks:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          data={"chat_id": TG_CHAT, "text": chunk, "parse_mode": "HTML",
                                "disable_web_page_preview": "true"}, timeout=30)
        if not r.ok:
            print(f"[!] Ошибка отправки: {r.status_code} {r.text}")
        time.sleep(1)


def make_stub(items):
    if not items:
        return "За период новых материалов не собрано."
    lines = ["<b>⚠️ Тестовый режим (ИИ не подключён)</b>", "Сырые материалы с сайтов:\n"]
    for p in items[:40]:
        lines.append(f"• <b>{html.escape(p['source'])}</b>: {html.escape(p['title'])}")
    return "\n".join(lines)


def main():
    now_msk = datetime.now(MSK)
    months = ["января","февраля","марта","апреля","мая","июня",
              "июля","августа","сентября","октября","ноября","декабря"]
    date_ru = f"{now_msk.day} {months[now_msk.month-1]} {now_msk.year}"
    header = f"<b>🌐 AU &amp; AG (сайты) · {date_ru}</b>\n\n"

    feeds = load_feeds()
    sent = load_json(SENT_FILE, {"ids": []})
    sent_ids = set(sent.get("ids", []))
    recent = load_json(RECENT_DIGESTS_FILE, {"topics": []})

    print(f"[i] Лент: {len(feeds)}, в памяти: {len(sent_ids)}")
    items = collect_all(feeds, sent_ids)

    ai_failed = False
    if AI_KEY:
        try:
            body = make_digest_ai(items, recent.get("topics", []))
        except Exception as e:
            print(f"[!] Ошибка Gemini: {e}")
            ai_failed = True
            body = ("⚠️ Дайджест (сайты) временно недоступен: сервис ИИ перегружен. "
                    "Следующая попытка — в очередном запуске.")
    else:
        body = make_stub(items)

    if body.strip() == "НЕТ_НОВОСТЕЙ":
        body = "За период существенных новостей по золоту и серебру не найдено."

    send_to_telegram(header + body)

    new_ids = [] if ai_failed else [p["id"] for p in items if p["id"]]
    sent_ids.update(new_ids)
    save_json(SENT_FILE, {"ids": list(sent_ids)[-3000:]})

    if AI_KEY and not ai_failed and body and "не найдено" not in body and "не собрано" not in body:
        os.makedirs(DIGESTS_DIR, exist_ok=True)
        with open(os.path.join(DIGESTS_DIR, now_msk.strftime("%Y-%m-%d") + ".md"), "w", encoding="utf-8") as f:
            f.write(f"# Дайджест (сайты) {date_ru}\n\n" + body)
        heads = re.findall(r"<b>(.*?)</b>", body)
        topics = (recent.get("topics", []) + heads)[-RECENT_DIGESTS_KEEP * 5:]
        save_json(RECENT_DIGESTS_FILE, {"topics": topics})

    print("[i] Готово.")


if __name__ == "__main__":
    main()
