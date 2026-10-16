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
