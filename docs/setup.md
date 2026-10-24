# Setup

## Requirements

- Python 3.14 and [uv](https://docs.astral.sh/uv/). uv fetches Python if need be.
- An X developer account with pay-per-use credits.
- For [scheduling](scheduling.md): Linux with a systemd user session.
- For `-s`, optionally: a Windows machine you reach over ssh (see [the newest screenshot](media.md#the-newest-screenshot)).

## Keys

x-post signs its calls with OAuth 1.0a user keys, four values from the X Developer Console.

1. Sign in to the Developer Console (console.x.com) with the account you want to post as, and create an App.
2. In the App's **User authentication settings** (click **Set up**), set App permissions to **Read and write**. If it insists on a callback or website URL, any placeholder (`http://127.0.0.1/`) works, since x-post never uses it. You don't need the OAuth 2.0 Client ID and Secret it may show afterwards.
3. Under **OAuth 1.0 Keys**, click **Regenerate** on **Consumer Key** (older docs call it the API Key) and copy the key and its secret; they're shown only once. Then **Regenerate** the **Access Token** and copy it and its secret.
4. Buy some credits.
5. Put the four values in `~/.config/x-post/credentials.toml`, readable only by you (`chmod 600`):

   ```toml
   api_key = "..."              # Consumer Key
   api_secret = "..."           # Consumer Secret
   access_token = "..."
   access_token_secret = "..."
   ```

Do step 3 *after* step 2. A token made while the App was read-only stays read-only (the console labels it "Read"), and posting fails with a 403 about "oauth1 app permissions". The regenerated token should say "Read and write".

## Install

Install from a clone, which puts `x-post` in `~/.local/bin`:

```
git clone https://github.com/CMON1975/x-post
cd x-post
uv tool install .
```

Add `--editable` to have code changes take effect without reinstalling.

## Tab completion

`completions/x-post.bash` completes the options, queued post ids after `--cancel`, `--retry`, `--move` and `--revise`, your logged post ids after `--delete` (newest first, deleted ones left out), and file names after `-i`. It needs only bash's own `complete`, not the bash-completion package, so source it from `~/.bashrc`, wherever you cloned x-post:

```bash
[[ -r ~/x-post/completions/x-post.bash ]] && . ~/x-post/completions/x-post.bash
```

## Settings

`~/.config/x-post/config.toml` is optional, and every setting in it is off until you add it:

| Setting | For | Example |
| --- | --- | --- |
| `screenshots` | the folder `-s` takes the newest image from ([more](media.md#the-newest-screenshot)) | `screenshots = "pc:Pictures/Screenshots"` |
| `alert` | a command to run when a queued post is held ([more](scheduling.md#alerts)) | `alert = "notify-send --urgency=critical"` |
| `budget` | a monthly spending cap in dollars ([more](costs.md#monthly-budget)) | `budget = 1.00` |

## Files

| Path | Holds |
| --- | --- |
| `~/.config/x-post/credentials.toml` | your four keys |
| `~/.config/x-post/config.toml` | the settings above |
| `~/.config/x-post/words.txt` | words the typo check accepts, one per line |
| `~/.local/state/x-post/draft.txt` | the `-e` draft |
| `~/.local/state/x-post/last.json` | the last post or thread, for `--undo` |
| `~/.local/state/x-post/log.jsonl` | every billed call, for `--spent` and `--history` |
| `~/.local/state/x-post/scheduled/` | the queue, one folder per post with copies of its images |
| `~/.config/systemd/user/x-post-due.timer` and `.service` | the scheduler, installed by the first `--at` |
