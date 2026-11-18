"""
Weekly AI news digest: RSS feeds -> dedupe -> LLM editor -> HTML draft -> human approval.

Run:
  python digest.py --dry-run     # no API key needed, keeps the newest articles as-is
  python digest.py               # uses Claude as the editor (needs ANTHROPIC_API_KEY)
  python digest.py --approve     # marks the latest draft as approved and writes the final digest
"""

import argparse
import html
import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
SOURCES = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
MAX_ITEMS = 10
LOOKBACK_DAYS = 7


def fetch(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "news-digest/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        root = ET.fromstring(r.read())
    items = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        def get(*names):
            return next((c for c in it if c.tag.split("}")[-1] in names), None)
        title = get("title")
        link = get("link")
        date = get("pubDate", "published", "updated")
        summary = get("description", "summary")
        items.append({
            "title": (title.text or "").strip() if title is not None else "",
            "link": (link.get("href") or link.text or "").strip() if link is not None else "",
            "date": date.text if date is not None else None,
            "summary": re.sub("<[^>]+>", "", summary.text or "")[:500] if summary is not None else "",
        })
    return items


def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None


def collect() -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    seen, articles = set(), []
    for src in SOURCES:
        try:
            items = fetch(src["url"])
        except Exception as e:  # one broken feed should not stop the digest
            print(f"skip {src['name']}: {e}")
            continue
        for a in items:
            d = parse_date(a["date"])
            key = re.sub(r"\W+", " ", a["title"].lower()).strip()
            if not a["link"] or key in seen or (d and d < since):
                continue
            seen.add(key)
            articles.append({**a, "source": src["name"], "date": d.isoformat() if d else None})
    print(f"{len(articles)} new articles from {len(SOURCES)} sources")
    return articles


def llm_edit(articles: list[dict]) -> list[dict]:
    import anthropic

    numbered = "\n".join(f"[{i}] {a['source']}: {a['title']} | {a['summary'][:200]}" for i, a in enumerate(articles))
    prompt = (
        f"You are the editor of a weekly AI and technology digest for business readers.\n"
        f"Pick the {MAX_ITEMS} most important articles below. Skip duplicates, ads and minor updates.\n"
        'Reply with JSON only: [{"id": <number>, "category": "<short category>", '
        '"summary": "<two plain sentences on what happened and why it matters>"}]\n\n' + numbered
    )
    msg = anthropic.Anthropic().messages.create(
        model=os.getenv("DIGEST_MODEL", "claude-haiku-5-5"),
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text
    picks = json.loads(text[text.find("["): text.rfind("]") + 1])
    return [{**articles[p["id"]], "category": p["category"], "summary": p["summary"]}
            for p in picks if 0 <= p["id"] < len(articles)][:MAX_ITEMS]


def render(items: list[dict], status: str) -> str:
    rows = "".join(
        f"<li><b>{html.escape(a.get('category', 'News'))}</b> · "
        f"<a href=\"{html.escape(a['link'])}\">{html.escape(a['title'])}</a> "
        f"<small>({html.escape(a['source'])})</small><p>{html.escape(a['summary'])}</p></li>"
        for a in items
    )
    week = datetime.now().strftime("%G-W%V")
    return (f"<!doctype html><meta charset='utf-8'><title>AI Digest {week}</title>"
            f"<body style='font-family:system-ui;max-width:680px;margin:40px auto;line-height:1.5'>"
            f"<h1>AI Digest · {week}</h1><p><i>Status: {status}</i></p><ol>{rows}</ol></body>")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="skip the LLM step")
    ap.add_argument("--approve", action="store_true", help="approve the latest draft")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)

    if args.approve:
        items = json.loads((OUT / "draft.json").read_text(encoding="utf-8"))
        (OUT / "digest.html").write_text(render(items, "approved"), encoding="utf-8")
        print("Approved: output/digest.html is ready to send")
        return

    articles = collect()
    if args.dry_run:
        articles.sort(key=lambda a: a["date"] or "", reverse=True)
        items = articles[:MAX_ITEMS]
    else:
        items = llm_edit(articles)
    (OUT / "draft.json").write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "draft.html").write_text(render(items, "draft, waiting for review"), encoding="utf-8")
    print("Draft written to output/draft.html. Review it, then run: python digest.py --approve")


if __name__ == "__main__":
    main()
