#!/usr/bin/env python3
"""Fetch every feed in feeds.json and write docs/items.json for the app.

Standard library only, so it runs anywhere (locally or in GitHub Actions).
Items are merged with the previous items.json so the time we first saw an
item is kept, and anything older than KEEP_DAYS is dropped.
"""
import hashlib
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs" / "items.json"
KEEP_DAYS = 10
DEFAULT_MAX = 20
TLDR_ISSUES = 2  # newest issues to split per TLDR feed
UA = "Mozilla/5.0 (personal news reader; +https://github.com)"
TRACKING = re.compile(r"^(utm_|ref$|reflink$|st$|source$)")


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def clean_url(url):
    p = urllib.parse.urlsplit(url.strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not TRACKING.match(k)]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), ""))


def item_id(url):
    p = urllib.parse.urlsplit(url)
    key = (p.netloc.removeprefix("www.") + p.path.rstrip("/") + ("?" + p.query if p.query else "")).lower()
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def text(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def fallback_title(desc):
    words = text(desc).split()
    if not words:
        return None
    t = " ".join(words[:14])
    return t + ("…" if len(words) > 14 else "")


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp())


def local(tag):
    return tag.rsplit("}", 1)[-1]


def child(el, *names):
    for c in el:
        if local(c.tag) in names:
            return c
    return None


def child_text(el, *names):
    c = child(el, *names)
    return (c.text or "").strip() if c is not None else ""


def parse_feed(raw):
    """Yield dicts of title/link/desc/date/comments from RSS or Atom."""
    root = ET.fromstring(raw)
    entries = [e for e in root.iter() if local(e.tag) in ("item", "entry")]
    for e in entries:
        link = ""
        l = child(e, "link")
        if l is not None:
            link = (l.text or "").strip() or l.get("href", "")
            if local(e.tag) == "entry":  # Atom: prefer rel=alternate
                for c in e:
                    if local(c.tag) == "link" and c.get("rel", "alternate") == "alternate":
                        link = c.get("href", link)
                        break
        yield {
            "title": text(child_text(e, "title")),
            "link": link,
            "desc": child_text(e, "description", "summary", "content"),
            "date": parse_date(child_text(e, "pubDate", "published", "updated", "date")),
            "comments": child_text(e, "comments"),
        }


def extras(feed, entry):
    """Source-specific bits worth showing next to a title (never summaries)."""
    out = {}
    desc = entry["desc"]
    if feed["id"].startswith("hn"):
        m = re.search(r"Points:\s*(\d+)", desc)
        if m:
            out["points"] = int(m.group(1))
        if entry["comments"]:
            out["discuss"] = entry["comments"]
    elif feed["id"].startswith("lobsters") and entry["comments"]:
        out["discuss"] = entry["comments"]
    elif feed["id"] == "gh-trending":
        # Repo names alone mean little, so keep the repo's own one-line tagline.
        m = re.search(r"<p>(.*?)</p>", html.unescape(desc), re.S)
        if m and text(m.group(1)):
            out["sub"] = text(m.group(1))[:160]
    return out


def fetch_tldr(feed):
    raw = get(feed["url"])
    issues = list(parse_feed(raw))[:TLDR_ISSUES]
    for issue in issues:
        page = get(issue["link"]).decode("utf-8", "replace")
        for m in re.finditer(r'<a class="font-bold" href="([^"]+)"[^>]*><h3>(.*?)</h3></a>', page, re.S):
            url, title = html.unescape(m.group(1)), text(m.group(2))
            if "(Sponsor)" in title or "tldr.tech" in url:
                continue
            mins = re.search(r"\((\d+) minute read\)\s*$", title)
            title = re.sub(r"\s*\((\d+ minute read|GitHub Repo|Website)\)\s*$", "", title)
            yield {"title": title, "link": url, "date": issue["date"],
                   "extra": {"mins": int(mins.group(1))} if mins else {}}


def fetch_rss(feed):
    raw = get(feed["url"])
    entries = sorted(parse_feed(raw), key=lambda e: e["date"] or 0, reverse=True)
    for e in entries[: feed.get("max", DEFAULT_MAX)]:
        title = e["title"] or fallback_title(e["desc"])
        if not title:
            continue
        link = e["link"]
        if not link or link.rstrip("/") in ("https://freakonomics.com",):
            if "linkFallback" not in feed:
                continue
            link = feed["linkFallback"].replace("{title}", urllib.parse.quote_plus(title))
        yield {"title": title, "link": link, "date": e["date"], "extra": extras(feed, e)}


def fetch(feed):
    try:
        fn = fetch_tldr if feed.get("type") == "tldr" else fetch_rss
        return feed, list(fn(feed)), None
    except Exception as ex:  # one broken feed must not stop the rest
        return feed, [], f"{type(ex).__name__}: {ex}"


def main():
    feeds = json.loads((ROOT / "feeds.json").read_text())["feeds"]
    now = int(time.time())
    old = {}
    if OUT.exists():
        try:
            old = {i["id"]: i for i in json.loads(OUT.read_text())["items"]}
        except (ValueError, KeyError):
            pass

    items, errors = {}, {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for feed, entries, err in pool.map(fetch, feeds):
            if err:
                errors[feed["id"]] = err
                print(f"  ! {feed['id']}: {err}", file=sys.stderr)
                # keep what we already had from this source
                for i in old.values():
                    if i["src"] == feed["id"]:
                        items.setdefault(i["id"], i)
                continue
            print(f"  {feed['id']}: {len(entries)}")
            for e in entries:
                url = clean_url(e["link"]) if e["link"].startswith("http") else e["link"]
                iid = item_id(url)
                if iid in items:
                    continue  # same link already came from another source
                prev = old.get(iid, {})
                seen = prev.get("seen", now)
                items[iid] = {
                    "id": iid, "src": feed["id"], "cat": feed["cat"],
                    "title": e["title"][:220], "url": url,
                    "t": e["date"] or seen, "seen": seen,
                    **({"kind": feed["kind"]} if feed.get("kind") else {}),
                    **e["extra"],
                }

    cutoff = now - KEEP_DAYS * 86400
    kept = sorted((i for i in items.values() if max(i["t"], i["seen"]) >= cutoff),
                  key=lambda i: i["t"], reverse=True)
    sources = [{k: f[k] for k in ("id", "name", "cat") if k in f} | ({"discover": True} if f.get("discover") else {})
               for f in feeds]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"updated": now, "sources": sources, "errors": errors, "items": kept},
                              ensure_ascii=False, separators=(",", ":")))
    print(f"wrote {len(kept)} items, {len(errors)} feed errors")


if __name__ == "__main__":
    main()
