# Launch checklist

**LAUNCHED 2026-07-07.** Code repo public, full landing page live at https://cafeci.to,
install verified from a clean environment (`pip install git+…` → working binary). Remaining
below: HN submission (Victor's account) and optional post-launch items.

## Architecture

- **`cafecitohq/web`** (public) — owns `cafeci.to` permanently. Currently the launch teaser.
- **`cafecitohq/cafecito`** (private → public on launch) — the code, this repo.

The domain never moves; on launch we just swap the teaser for the full page and flip the code
repo public.

## Before launch

- [x] Record the demo — done 2026-07-07: [examples/demo.cast](examples/demo.cast) (34s,
      asciicast v3, live reconciler call) + [examples/demo.gif](examples/demo.gif) (agg,
      coffee theme, 983×694, ~1 MB), embedded in README and the landing page (raw URL
      resolves when the repo goes public). Optional: upload the cast to asciinema.org for a
      scrubbable player. Re-record: `PATH=<venv-with-cafecito+pytest>/bin:$PATH asciinema rec
      --window-size 100x30 -c "DEMO_DELAY=1.4 ./examples/demo.sh" demo.cast`.
- [ ] (Optional, security) Verify `cafeci.to` on the org: GitHub → org `Cafecitohq` settings →
      Pages / Verified domains → add the `_github-pages-challenge-cafecitohq` TXT at Namecheap.
      Prevents any other account from ever claiming the domain on Pages.
- [x] Add a SECURITY.md (`security@cafeci.to` already forwards) — done 2026-07-07.

## Launch day (in order)

1. ✅ ~~Flip the code repo public:~~ done — `gh repo edit cafecitohq/cafecito --visibility public --accept-visibility-change-consequences`
2. ✅ ~~Swap teaser → full site:~~ done — copy [docs/index.html](docs/index.html) into the `web` repo as
   `index.html` (its `github.com/cafecitohq/cafecito` links now resolve), commit, push. Pages
   redeploys `cafeci.to` in ~1 min.
3. ✅ ~~Verify:~~ done (site 200, all links 200, stranger-install works) — `curl -sI https://cafeci.to` → 200; click through GitHub / story / spec links.
4. ⏳ **Publish the post (Victor):** the story is [docs/launch-post.md](docs/launch-post.md). Post to HN
   with the title *"97% of concurrent code changes don't conflict. Your merge queue serializes
   100% of them."* Link `cafeci.to`.
5. **(Optional) PyPI:** `python3 -m build && twine upload dist/*` — name `cafecito` was free
   on 2026-07-06.

## Rollback

Everything is reversible except making the code public (one-way). The teaser stays up
regardless; if anything on launch day misfires, the `web` repo can be reverted to the teaser
commit in one `git revert` + push.
