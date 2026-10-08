# Rollback

Bot v1 is preserved byte-for-byte in `legacy/bot_v1.py` and the workflow can run it with its
original settings (`OPENAI_MODEL=gpt-4o-mini`, `WP_POST_STATUS=publish`, rotation, images, …).

## Option A — instant, no code change (recommended)

1. GitHub → Settings → Secrets and variables → Actions → **Variables** → New repository variable
   `NEWSBOT_ENGINE` = `legacy`.
2. The next scheduled run (or *Run workflow* with `engine = legacy`) runs bot v1.

To return to v2, delete the variable or set it to `v2`.

v1 keeps working with its own `news_cache.db` cache (same cache key prefix as before). While v2
runs it writes its decisions into that DB, so after a rollback v1 will not re-post stories v2
already published or rejected.

## Option B — pause publishing, keep v2 running

Set `NEWSBOT_MODE` = `draft` (canary drafts only) or `dry-run` (no WordPress writes).

## Option C — revert the code

Revert the merge commit of the News Bot 2.0 pull request on `main`
(`git revert -m 1 <merge-sha>`). This restores the previous `bot.py` and workflow exactly.
The v2 state cache is simply ignored afterwards.

## After a rollback

* Posts created by v2 stay published; each carries the HTML comment marker
  `poormaz-newsbot v2 nbstory-<id>`, so they can be found with a WordPress search for
  `nbstory-`.
* Drafts created by canary runs can be found the same way (status: draft).
* Nothing in WordPress settings, themes, plugins, Rank Math configuration or existing posts was
  modified by v2 apart from the posts it created.
