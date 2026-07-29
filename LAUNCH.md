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
- [x] (Optional, security) Verify `cafeci.to` on the org — done 2026-07-08
      (`_github-pages-challenge-cafecitohq` TXT at Namecheap, `protected_domain_state:
      verified` on the web repo's Pages API; covers immediate subdomains too). Note: this is
      separate from the org-profile "Verified domains" system (`_gh-Cafecitohq-o` TXT, also
      done) — GitHub runs two independent domain verifications.
- [x] Add a SECURITY.md (`security@cafeci.to` already forwards) — done 2026-07-07.

## Launch day (in order)

1. ✅ ~~Flip the code repo public:~~ done — `gh repo edit cafecitohq/cafecito --visibility public --accept-visibility-change-consequences`
2. ✅ ~~Swap teaser → full site:~~ done — copied `docs/index.html` into the `web` repo as
   `index.html` (its `github.com/cafecitohq/cafecito` links now resolve), commit, push. Pages
   redeploys `cafeci.to` in ~1 min. *(2026-07-28: `docs/index.html` has since been deleted —
   it was an unserved duplicate that drifted out of sync with the live site. The site's only
   source is the `web` repo. Recover the original with `git show bf10714:docs/index.html`.)*
3. ✅ ~~Verify:~~ done (site 200, all links 200, stranger-install works) — `curl -sI https://cafeci.to` → 200; click through GitHub / story / spec links.
4. ⏳ **Publish the post (Victor):** the story is [docs/launch-post.md](docs/launch-post.md). Post to HN
   with the title *"Your AI agents write code faster than they can merge it."* Link `cafeci.to`.
   *(2026-07-28: the original title — "97% of concurrent code changes don't conflict. Your merge
   queue serializes 100% of them." — was retired after real clients reported they could not tell
   what it meant. It was used for the 2026-07-08 HN submission, which is historical and cannot be
   edited. Do not reuse it in any future post, deck, or video.)*
5. **(Optional) PyPI:** `python3 -m build && twine upload dist/*` — name `cafecito` was free
   on 2026-07-06.

## Rollback

Everything is reversible except making the code public (one-way). The teaser stays up
regardless; if anything on launch day misfires, the `web` repo can be reverted to the teaser
commit in one `git revert` + push.
