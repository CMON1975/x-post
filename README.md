# x-post

Post to X from the terminal, without opening the site or app.

```
x-post 'having a great day'
https://x.com/i/status/1850000000000000001
```

x-post only writes. Nothing in it needs you to have read X first, so it doesn't quote posts, reply to other people's, or show timelines. It calls X's pay-per-use API with your own keys.

## Features

- **Posts and threads** from the command line, a prompt or your editor, with `---` lines between a thread's posts and optional `1/3` numbering. [Posting](docs/posting.md)
- **Images, GIFs and video**, from local files or another machine over ssh, the newest screenshot with `-s`, and alt text. [Media](docs/media.md)
- **Polls** of 2 to 4 options. [Polls](docs/posting.md#polls)
- **Scheduling** with `--at`, sent by a systemd user timer, and a queue you can list, move, revise and cancel. [Scheduling](docs/scheduling.md)
- **Undo, history and delete** for the posts x-post made, without looking them up on X. [Your posts](docs/your-posts.md)
- **Checks before anything is sent**: posts X would fold behind "Show more", likely typos, links (which cost more), and commands typed without their dashes. [Checks](docs/checks.md)
- **Cost tracking**: price a post without sending it, add up the month, and set a monthly budget. [Costs](docs/costs.md)

## Quick start

| Command | Does |
| --- | --- |
| `x-post 'text'` | posts it and prints the URL |
| `x-post` | asks for the post at a prompt, where nothing needs quoting |
| `x-post -e` | opens your editor; `---` lines make a thread |
| `x-post -i photo.jpg 'caption'` | attaches an image (up to 4) |
| `x-post --at 'fri 18:00' 'text'` | schedules it |
| `x-post --check 'text'` | measures and prices it without posting |
| `x-post --undo` | deletes the last post or thread |
| `x-post --history` | lists what you've posted, with URLs |
| `x-post --spent` | adds up what you've spent |

## Costs

X bills each call from credits you prepay:

| Post | Post with a link | Alt text | Delete |
| --- | --- | --- | --- |
| $0.015 | $0.20 | $0.005 | $0.010 |

These are the October 2026 rates; check the Developer Console for current ones. x-post asks before sending a link post. [More on costs](docs/costs.md)

## Install

You need Python 3.14 and [uv](https://docs.astral.sh/uv/), an X developer account with credits, and, for scheduling, Linux with systemd.

1. In the X Developer Console, create an App, set its permissions to **Read and write**, and *then* regenerate its Consumer Key and Access Token. [Step by step](docs/setup.md#keys)
2. Put the four values in `~/.config/x-post/credentials.toml`, readable only by you:

   ```toml
   api_key = "..."              # Consumer Key
   api_secret = "..."           # Consumer Secret
   access_token = "..."
   access_token_secret = "..."
   ```

3. Install from a clone:

   ```
   git clone https://github.com/CMON1975/x-post
   cd x-post
   uv tool install .
   ```

[Setup](docs/setup.md) also covers the optional settings, tab completion and where x-post keeps its files.

## Documentation

- [Posting](docs/posting.md): the command line, the prompt, the editor, threads, numbering, polls, quoting
- [Media](docs/media.md): images, GIFs and video, remote files, screenshots, alt text
- [Scheduling](docs/scheduling.md): `--at`, the queue, held posts, alerts, the timer
- [Your posts](docs/your-posts.md): undo, history, delete
- [Checks](docs/checks.md): length and `--check`, splitting into a thread, typos, links, missing dashes
- [Costs](docs/costs.md): prices, `--spent`, the monthly budget
- [Setup](docs/setup.md): keys, install, tab completion, settings, files

## Development

```
uv run pytest
```

The tests intercept all HTTP, so they never reach X, and run in scratch config and state folders, so they never touch your credentials, draft or queue. A test holds the tab completion's option list to the parser's, so a new flag has to be added there too.

The App icon for the Developer Console is `assets/icon.png`, rendered from `assets/icon.svg` (Lucide's `send`, white on slate) with `rsvg-convert -w 1024 -h 1024 assets/icon.svg -o assets/icon.png`. A test holds its paths to the ones in a Lucide clone's `icons/` folder, named by `LUCIDE_ICONS`, and skips without it.

## License

MIT, in `LICENSE`. The icon in `assets/` is Lucide's `send`, under Lucide's ISC license, kept in `assets/LICENSE-lucide`.

Written by CMON1975 with Claude (Anthropic), through Claude Code.
