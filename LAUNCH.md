# Launch checklist

Current state (2026-07-07): **teaser is live at https://cafeci.to** (HTTPS), served from the
public repo `cafecitohq/web`. This code repo (`cafecitohq/cafecito`) is **private**. The full
landing page is ready at [docs/index.html](docs/index.html); its GitHub links resolve the
moment this repo goes public.

## Architecture

- **`cafecitohq/web`** (public) — owns `cafeci.to` permanently. Currently the launch teaser.
- **`cafecitohq/cafecito`** (private → public on launch) — the code, this repo.

The domain never moves; on launch we just swap the teaser for the full page and flip the code
repo public.

## Before launch

- [ ] Record the demo — `asciinema rec demo.cast -c "DEMO_DELAY=1.4 ./examples/demo.sh"`
      (checklist in [examples/README.md](examples/README.md)); host the cast (asciinema.org)
      or export a GIF for the site + README.
- [ ] (Optional, security) Verify `cafeci.to` on the org: GitHub → org `Cafecitohq` settings →
      Pages / Verified domains → add the `_github-pages-challenge-cafecitohq` TXT at Namecheap.
      Prevents any other account from ever claiming the domain on Pages.
- [ ] Add a SECURITY.md (`security@cafeci.to` already forwards).

## Launch day (in order)

1. **Flip the code repo public:** `gh repo edit cafecitohq/cafecito --visibility public --accept-visibility-change-consequences`
2. **Swap teaser → full site:** copy [docs/index.html](docs/index.html) into the `web` repo as
   `index.html` (its `github.com/cafecitohq/cafecito` links now resolve), commit, push. Pages
   redeploys `cafeci.to` in ~1 min.
3. **Verify:** `curl -sI https://cafeci.to` → 200; click through GitHub / story / spec links.
4. **Publish the post:** the story is [docs/launch-post.md](docs/launch-post.md). Post to HN
   with the title *"97% of concurrent code changes don't conflict. Your merge queue serializes
   100% of them."* Link `cafeci.to`.
5. **(Optional) PyPI:** `python3 -m build && twine upload dist/*` — name `cafecito` was free
   on 2026-07-06.

## Rollback

Everything is reversible except making the code public (one-way). The teaser stays up
regardless; if anything on launch day misfires, the `web` repo can be reverted to the teaser
commit in one `git revert` + push.
