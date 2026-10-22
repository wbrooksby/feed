#!/usr/bin/env python3
"""Fetch every feed in feeds.json and write docs/items.json for the app.

Standard library only, so it runs anywhere (locally or in GitHub Actions).
Items are merged with the previous items.json so the time we first saw an
item is kept, and anything older than KEEP_DAYS is dropped.

Each item also gets signals the app's ranking uses:
  d       a short plain-text blurb (from the feed or the TLDR issue), used only for matching, never shown
  points  Hacker News points for the link, looked up for every source, plus a link to the HN thread
  also    other sources that linked the same article
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
BLURB = 300      # characters of description kept per item
HN_API = "https://hn.algolia.com/api/v1/search?restrictSearchableAttributes=url&hitsPerPage=5&query="
HN_RECHECK_H = 6   # re-check HN points this often while an item is young...
HN_YOUNG_H = 72    # ...which is this many hours
UA = "Mozilla/5.0 (personal news reader; +https://github.com)"
TRACKING = re.compile(r"^(utm_|ref$|reflink$|st$|source$)")
# Housekeeping posts that aren't articles: open threads, link roundups, meetups, etc.
JUNK_TITLE = re.compile(r"""(?ix)
    \b(hidden\ )?open\ thread\b | \bdiscussion\ (post|thread)\b | \bassorted\ links\b
  | ^reading\ list\b | ^roundup\b | ^links?\ for\b | ^weekly\ (links|thread)\b | \btop\ \d+\ longreads\b
  | \bmeetups?\b | ^\[?subscriber\ exclusive | \beverywhere\ \d{4}: | \btimes\ (&|and)\ places\b
  | ^what\ are\ you\ (doing|working\ on)\ this\ week | \bpodcast\b.*\bad[- ]free\b
""")
JUNK_URL = re.compile(r"(youtube\.com|youtu\.be)/")


def is_junk(feed, title, link):
    if JUNK_TITLE.search(title) or JUNK_URL.search(link):
        return True
    return bool(feed.get("skip") and re.search(feed["skip"], link))


def get(url, tries=3):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    for n in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read()
        except Exception:
            if n == tries - 1:
                raise
            time.sleep(3 * (n + 1))


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
        try:
            page = get(issue["link"]).decode("utf-8", "replace")
        except Exception as ex:  # an issue page can 404 briefly while it's being published
            print(f"  ! skipped {issue['link']}: {ex}", file=sys.stderr)
            continue
        pat = r'<a class="font-bold" href="([^"]+)"[^>]*><h3>(.*?)</h3></a>\s*(?:<div class="newsletter-html">(.*?)</div>)?'
        for m in re.finditer(pat, page, re.S):
            url, title = html.unescape(m.group(1)), text(m.group(2))
            if "(Sponsor)" in title or "tldr.tech" in url or is_junk(feed, title, url):
                continue
            repo = re.search(r"\((GitHub Repo|Website)\)\s*$", title)
            mins = re.search(r"\((\d+) minute read\)\s*$", title)
            title = re.sub(r"\s*\((\d+ minute read|GitHub Repo|Website)\)\s*$", "", title)
            yield {"title": title, "link": url, "date": issue["date"], "desc": m.group(3) or "",
                   "extra": {"mins": int(mins.group(1))} if mins else {"kind": "repo"} if repo else {}}


def fetch_rss(feed):
    raw = get(feed["url"])
    entries = sorted(parse_feed(raw), key=lambda e: e["date"] or 0, reverse=True)
    for e in entries[: feed.get("max", DEFAULT_MAX)]:
        title = e["title"] or fallback_title(e["desc"])
        if not title:
            continue
        link = e["link"]
        if is_junk(feed, title, link or ""):
            continue
        if not link:
            if "linkFallback" not in feed:
                continue
            link = feed["linkFallback"].replace("{title}", urllib.parse.quote_plus(title))
        yield {"title": title, "link": link, "date": e["date"], "desc": e["desc"], "extra": extras(feed, e)}


def blurb(desc, title):
    t = text(desc)
    if t.lower().startswith(title.lower()):
        t = t[len(title):].strip(" :-–—")
    t = re.sub(r"\b(Article URL|Comments URL|Points|# Comments):.*", "", t).strip()  # hnrss boilerplate
    return t[:BLURB]


def hn_lookup(url):
    """Best HN submission for this exact link: (points, comments, thread url) or None."""
    if not url.startswith("http"):
        return None
    hits = json.loads(get(HN_API + urllib.parse.quote(url, safe=""), tries=2))["hits"]
    norm = lambda u: item_id(clean_url(u or ""))
    hits = [h for h in hits if h.get("url") and norm(h["url"]) == norm(url)]
    if not hits:
        return None
    h = max(hits, key=lambda h: h.get("points") or 0)
    return h.get("points") or 0, h.get("num_comments") or 0, f"https://news.ycombinator.com/item?id={h['objectID']}"


def add_hn(items, now):
    """Look up HN points for new items, and again every few hours while they're young."""
    todo = [i for i in items if not i["src"].startswith("hn") and i["url"].startswith("http")
            and ("hnAt" not in i or (now - i["t"] < HN_YOUNG_H * 3600 and now - i["hnAt"] > HN_RECHECK_H * 3600))]

    def one(i):
        try:
            return i, hn_lookup(i["url"])
        except Exception:
            return i, False  # try again next run

    found = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i, r in pool.map(one, todo):
            if r is False:
                continue
            i["hnAt"] = now
            if r:
                found += 1
                i["points"], i["hnComments"] = r[0], r[1]
                i.setdefault("discuss", r[2])
    print(f"  hn lookups: {len(todo)}, on HN: {found}")


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
                if iid in items:  # same link already came from another source
                    first = items[iid]
                    if feed["id"] != first["src"] and feed["id"] not in first.setdefault("also", []):
                        first["also"].append(feed["id"])
                    continue
                prev = old.get(iid, {})
                seen = prev.get("seen", now)
                items[iid] = {
                    "id": iid, "src": feed["id"], "cat": feed["cat"],
                    "title": e["title"][:220], "url": url,
                    "t": e["date"] or seen, "seen": seen,
                    **({"d": blurb(e["desc"], e["title"])} if blurb(e["desc"], e["title"]) else {}),
                    **{k: prev[k] for k in ("hnAt", "points", "hnComments", "discuss") if k in prev},
                    **({"kind": feed["kind"]} if feed.get("kind") else {}),
                    **e["extra"],
                }

    cutoff = now - KEEP_DAYS * 86400
    # t is the published date (or first-seen when a feed has none), so old posts
    # in a slow feed's backlog don't linger just because we only just saw them.
    kept = sorted((i for i in items.values() if i["t"] >= cutoff),
                  key=lambda i: i["t"], reverse=True)
    add_hn(kept, now)
    sources = [{k: f[k] for k in ("id", "name", "cat") if k in f} | ({"discover": True} if f.get("discover") else {})
               for f in feeds]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"updated": now, "sources": sources, "errors": errors, "items": kept},
                              ensure_ascii=False, separators=(",", ":")))
    print(f"wrote {len(kept)} items, {len(errors)} feed errors")


if __name__ == "__main__":
    main()
