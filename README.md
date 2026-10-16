# Feed

A personal news reader. It shows headlines only (no AI summaries), with like and dislike buttons that change the ranking over time.

- `feeds.json` lists the sources. Add or remove a feed here.
- `fetch.py` collects headlines into `docs/items.json` (standard-library Python, run `python3 fetch.py`).
- `.github/workflows/fetch.yml` runs the fetcher every 2 hours on GitHub.
- `docs/` is the app, served by GitHub Pages. Open it in Safari, then Share → Add to Home Screen.

Likes are stored on the phone (Settings → Export likes to back them up).

## How ranking works
Each item's score combines how recent it is, your likes and dislikes for its source and category,
and the words in titles you've voted on. Hacker News points add a small boost. In "For you", no single
source can take over the list, and every 5th slot goes to a source you haven't voted on much yet (marked "New to you").

## Reader view
`reader/` is a Cloudflare Worker (https://feed-reader.wbrooksby.workers.dev) that fetches an article when you open it
and returns just its text (Mozilla Readability). It only answers requests from the app and stores nothing.
Deploy changes with `cd reader && npx wrangler deploy`. Sources that block it are listed in `NO_READER` in `docs/index.html`.
