# Feed

A personal news reader. It shows headlines only (no AI summaries), with like and dislike buttons that change the ranking over time.

- `feeds.json` lists the sources. Add or remove a feed here.
- `fetch.py` collects headlines into `docs/items.json` (standard-library Python, run `python3 fetch.py`).
- `.github/workflows/fetch.yml` runs the fetcher every 2 hours on GitHub.
- `docs/` is the app, served by GitHub Pages. Open it in Safari, then Share → Add to Home Screen.

Likes are stored on the phone (Settings → Export likes to back them up).

## How ranking works
**What it learns from.** Likes and dislikes count most. It also learns, more weakly, from what you do:
opening an article, reading most of it in reader view (or bailing out within seconds), and scrolling past
something several times without opening it. All of this stays on the phone.

**The model** (in `docs/index.html`) has two parts, retrained whenever you vote or finish reading:
- a logistic regression over each article's source, category, website, title words, two-word phrases and blurb words;
- text similarity (TF-IDF) to articles you liked or read through, minus similarity to ones you disliked.

**The score** adds those to freshness (news fades in about a day, essays over a week), Hacker News points for the link
(looked up for every source, not just HN), a bonus when several sources linked the same article, and a small nudge for
sources it knows little about. Items you've scrolled past before sink. Taste counts for more once you have ~25 votes/reads.

**The list.** "For you" picks the best item, then pushes down anything too similar to what's already shown (same source,
same topic). The same story from several sources shows once, marked "also in …". Every 5th slot goes to a source you
haven't voted on much ("New to you"). GitHub repos and Show HN projects stay in the Tech tab.

**What gets fetched.** `fetch.py` drops housekeeping posts that aren't articles (open threads, link roundups, meetups,
podcast episodes) and anything older than 10 days. Feeds can skip more with a `skip` pattern in `feeds.json`.

## Reader view
`reader/` is a Cloudflare Worker (https://feed-reader.wbrooksby.workers.dev) that fetches an article when you open it
and returns just its text (Mozilla Readability). It only answers requests from the app and stores nothing.
Deploy changes with `cd reader && npx wrangler deploy`. Sources that block it are listed in `NO_READER` in `docs/index.html`.
