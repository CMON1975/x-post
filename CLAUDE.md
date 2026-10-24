# x-post

CLI that posts to X via `POST https://api.x.com/2/tweets`, signed with OAuth 1.0a user-context keys. All code is in `src/x_post/__init__.py`.

- Design rule: x-post is write-only, so the user never has to open X's site or app. A feature that needs something found by reading X first (quoting a post, replying to someone else's, timelines, search, mentions) doesn't fit; don't propose it (decided 2026-10-23). Acting on x-post's own posts (threads, `--undo`) is fine.
- The account has no Premium and won't get one (decided 2026-10-23), so don't propose Premium-only features: editing a posted post (`--amend`), posts past 280 characters, video past 20 minutes.

- Tests: `uv run pytest`. Every test runs inside a `responses` mock and a scratch `XDG_CONFIG_HOME`, so nothing reaches X or the real credentials. Keep it that way.
- Never run `x-post` for real without asking: a post is public and costs money ($0.015, or $0.20 with a URL).
- Credentials: `~/.config/x-post/credentials.toml` (mode 600). Never read them into chat, never copy them into the repo.
- Installed with `uv tool install --editable .`; code edits are live, but dependency changes need `--reinstall`.

- The repo is meant to go public, so nothing of one machine's setup goes in it: hostnames, home paths and notification routing belong in the untracked `~/.config/x-post/config.toml`, with no default in the code (decided 2026-10-23).
- PROCESS.md is a local journal, excluded in `.git/info/exclude`; never commit it. The history before the fresh start is kept locally as `refs/archive/full-history`; never push it.
