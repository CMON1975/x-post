"""Post a message to X from the command line."""

import argparse
import collections
import fcntl
import functools
import itertools
import json
import os
import re
import secrets
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unicodedata
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import sleep

import regex  # grapheme clusters (\X) and emoji properties, which re lacks
import requests
from requests_oauthlib import OAuth1

API_URL = "https://api.x.com/2/tweets"
MEDIA_URL = "https://api.x.com/2/media/upload"
METADATA_URL = "https://api.x.com/2/media/metadata"
# X's listed price for each billed call, in tenths of a cent, for --spent. A link post is one
# x-post's own link check flags, which errs toward links. Uploads, and posts X refuses, are free.
PRICES = {"post": 15, "link post": 200, "alt text": 5, "delete": 10}
MAX_ALT_TEXT = 1000
# X's one-shot upload takes still images only: up to 4 per post, 5 MB each.
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# A GIF or video goes up in chunks (X advises 5 MB at most) and X processes it before a post
# can carry it; a post takes one on its own. Each type's upload category and size limit: X takes
# video up to 8 GB, but x-post holds the file in memory (and copies it into the queue).
CHUNKED_MEDIA = {
    "image/gif": ("tweet_gif", 15 * 1024 * 1024),
    "video/mp4": ("tweet_video", 512 * 1024 * 1024),
    "video/quicktime": ("tweet_video", 512 * 1024 * 1024),
}
# MP4 and MOV open with an ftyp box naming a brand. HEIC and AVIF photos share the box, so only
# video brands count: ISO and MPEG-4 (phones, OBS, Windows' recorder), 3GPP, and QuickTime's.
VIDEO_BRANDS = {b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"M4V ", b"mmp4"}
VIDEO_BRANDS |= {b"3gp4", b"3gp5", b"3gp6", b"qt  "}
CHUNK_BYTES = 4 * 1024 * 1024
MAX_PROCESSING_SECONDS = 600
# X takes video from 0.5 s to 20 minutes (125 with Premium), read here from the movie header.
MIN_VIDEO_SECONDS, MAX_VIDEO_SECONDS = 0.5, 20 * 60
# A poll: 2 to 4 options of up to 25 characters (code points, as X's schema counts them), open
# 5 minutes to 7 days. --poll-for takes the m/h/d units of --at's offsets.
MIN_POLL_OPTIONS, MAX_POLL_OPTIONS = 2, 4
MAX_POLL_OPTION = 25
POLL_FOR = re.compile(r"(\d+)([mhd])", re.IGNORECASE)
POLL_UNIT_MINUTES = {"m": 1, "h": 60, "d": 24 * 60}
MIN_POLL_MINUTES, MAX_POLL_MINUTES = 5, 7 * 24 * 60
DEFAULT_POLL_FOR = "1d"
# An image given as host:path (scp's syntax) is fetched over ssh. A one-letter "host" is a
# Windows drive (C:/...), and a / before the colon (./a:b.png) keeps a path local.
REMOTE_IMAGE = re.compile(r"[\w.@-]{2,}:.+")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
KEYS = ("api_key", "api_secret", "access_token", "access_token_secret")
TIMEOUT_SECONDS = 30
# Tried in order when neither $VISUAL nor $EDITOR is set; Arch doesn't install vi by default.
FALLBACK_EDITORS = ("nvim", "vim", "vi", "nano")
# X bills a post with a link at $0.20 instead of $0.015 and doesn't say what it counts
# as one, so this is deliberately loose: a false alarm costs a keystroke, a miss $0.185.
LINK = re.compile(r"https?://\S+|www\.\S+|[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}\b", re.IGNORECASE)
# What the typo check looks up: runs of letters, apostrophes inside. It skips links, #tags,
# @mentions and anything with a digit whole, and words with capitals past the first letter
# (EEVEE, OptiX), which are names and acronyms rather than dictionary words.
WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*")
# Past either limit the app or web folds a post behind "Show more". 280 is X's own count;
# 10 lines comes from a 2025 browser test (note.com/goodcomms), not from X's docs.
MAX_WEIGHTED_LENGTH = 280
MAX_LINES = 10
# Where a post too long for X may be split: after a sentence's end (and any closing quote or
# bracket) and the spaces after it, or at a line break.
SENTENCE_END = re.compile(r"[.!?…]+[\"'”’)\]]*[ \t]+|\n")
# X's counting (twitter-text v3): these code points weigh 1, all others 2, a URL 23,
# and an emoji 2 however many code points it spans.
LIGHT_CODE_POINTS = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))
URL_WEIGHT = 23
EMOJI = regex.compile(r"[\p{Extended_Pictographic}\p{Regional_Indicator}]")
# In an -e draft, a line of just --- separates the posts of a thread, and a first line
# naming a post continues the thread from it (written when a thread stops partway).
THREAD_BREAK = re.compile(r"^[ \t]*---[ \t]*$", re.MULTILINE)
REPLY_TO = re.compile(r"reply-to: https://x\.com/\S+/status/(\d+)[ \t]*(?:\n|$)")
# A post in an -e draft can close with lines attaching images to it: image: PATH (any form -i
# takes), each optionally followed by alt: TEXT.
ATTACHMENT = re.compile(r"[ \t]*(image|alt):[ \t]*(.*?)[ \t]*")
# The --numbered mark closing a post's text: its place in the thread (2/3).
PLACE = re.compile(r"(?:^|\s)(\d+)/(\d+)$")
# A post --delete is given: its URL in any of X's forms (handle or i, query string), or its id.
STATUS_URL = re.compile(r"https://(?:www\.)?(?:x|twitter)\.com/\w+/status/(\d+)(?:[/?#]\S*)?")
# A post given as arguments that reads as one of x-post's own commands with the dashes left off
# (cancel 65a0): the command's name, then only what that command takes (a queue id is 4 hex digits).
DASHLESS_COMMAND = re.compile(
    r"undo|scheduled|spent|send-due|edit|check"
    r"|history(?: \d+)?"
    r"|(?:cancel|retry|revise)(?: [0-9a-f]{4})?"
    r"|move(?: [0-9a-f]{4}(?: .+)?)?"
    rf"|delete(?: (?:\d+|{STATUS_URL.pattern}))?",
    re.IGNORECASE,
)
# --at's forms, all local time: +90m, +2h, +1d; 09:00 and fri 18:00 (the next one); tomorrow 09:00;
# 2026-10-24 18:30. systemd's own parser can't do "tomorrow 09:00", and takes a past 09:00 as today.
AT = re.compile(
    r"\+(?P<amount>\d+)(?P<unit>[mhd])"
    r"|(?:(?P<date>\d{4}-\d\d-\d\d)\s+|(?P<day>tomorrow|mon|tue|wed|thu|fri|sat|sun)\s+)?(?P<hour>\d\d?):(?P<minute>\d\d)",
    re.IGNORECASE,
)
UNITS = {"m": "minutes", "h": "hours", "d": "days"}
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
TIMER = "x-post-due.timer"


class XPostError(Exception):
    """A failure worth one line on stderr, not a traceback."""


class TooLong(XPostError):
    """The post would be folded behind "Show more"."""


class HeldBack(XPostError):
    """The user kept the post back rather than fix it now; it becomes the draft, to TO (trim, fix)."""

    def __init__(self, to: str):
        super().__init__("not posted")
        self.to = to


class EditNow(XPostError):
    """The user would rather fix the post in the editor now, first SPLIT into a thread if asked."""

    def __init__(self, split: bool = False):
        super().__init__("edit it now")
        self.split = split


class PartlyPosted(XPostError):
    """A thread stopped partway; the unsent posts can continue from the last one sent."""

    def __init__(self, cause: XPostError, sent: int, parts: list[str], last_id: str):
        super().__init__(f"{cause}; {sent} of {len(parts)} posts went out")
        self.unsent = parts[sent:]
        self.last_id = last_id


def credentials_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config_home) / "x-post" / "credentials.toml"


def config_path() -> Path:
    return credentials_path().with_name("config.toml")


def state_dir() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(state_home) / "x-post"


def draft_path() -> Path:
    return state_dir() / "draft.txt"


def last_post_path() -> Path:
    return state_dir() / "last.json"


def log_path() -> Path:
    return state_dir() / "log.jsonl"


def words_path() -> Path:
    return credentials_path().with_name("words.txt")


def scheduled_dir() -> Path:
    return state_dir() / "scheduled"


def units_dir() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config_home) / "systemd" / "user"


def post_url(post_id: str) -> str:
    return f"https://x.com/i/status/{post_id}"


def load_credentials(path: Path) -> dict:
    try:
        creds = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise XPostError(f"no credentials file at {path}")
    except tomllib.TOMLDecodeError as e:
        raise XPostError(f"{path}: {e}")
    missing = [key for key in KEYS if not creds.get(key)]
    if missing:
        raise XPostError(f"{path} is missing {', '.join(missing)}")
    return creds


def describe_failure(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if isinstance(body, dict) and body.get("detail"):
        return f"{response.status_code} {body.get('title', response.reason)}: {body['detail']}"
    return f"{response.status_code} {response.reason}: {response.text.strip()}"


def call_x(method: str, url: str, creds: dict, expected_status: int, **kwargs) -> requests.Response:
    """Send a request signed as the credentials' user; anything but expected_status is an error."""
    auth = OAuth1(
        creds["api_key"],
        creds["api_secret"],
        creds["access_token"],
        creds["access_token_secret"],
    )
    try:
        response = requests.request(method, url, auth=auth, timeout=TIMEOUT_SECONDS, **kwargs)
    except requests.RequestException as e:
        raise XPostError(f"could not reach X: {e}")
    if response.status_code != expected_status:
        raise XPostError(describe_failure(response))
    return response


def image_type(data: bytes) -> str | None:
    """The MIME type from the file's first bytes, for the formats x-post can upload."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[4:8] == b"ftyp" and data[8:12] in VIDEO_BRANDS:
        return "video/quicktime" if data[8:12] == b"qt  " else "video/mp4"
    return None


def find_box(data: bytes, kind: bytes) -> bytes | None:
    """The contents of the first MP4/MOV box of KIND among those DATA holds, or None."""
    offset = 0
    while offset + 8 <= len(data):
        size, found = struct.unpack(">I4s", data[offset : offset + 8])
        header = 8
        if size == 1:  # a 64-bit size follows
            size, header = struct.unpack(">Q", data[offset + 8 : offset + 16])[0], 16
        if size < header:  # broken, or 0: the last box, running to the end of the file
            return None
        if found == kind:
            return data[offset + header : offset + size]
        offset += size
    return None


def video_seconds(data: bytes) -> float | None:
    """How long a video runs by its movie header (moov/mvhd), or None if that doesn't say: a
    fragmented MP4 leaves its length at 0."""
    if not (mvhd := find_box(find_box(data, b"moov") or b"", b"mvhd")):
        return None
    if mvhd[0] == 1:  # version 1: 64-bit times
        timescale, duration = struct.unpack(">IQ", mvhd[20:32])
    else:
        timescale, duration = struct.unpack(">II", mvhd[12:20])
    return duration / timescale if timescale and duration else None


def video_length(seconds: float) -> str:
    return f"{seconds:.1f} s" if seconds < 60 else f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def load_images(specs: list[str], before: list[tuple[str, bytes, str]] = ()) -> list[tuple[str, bytes, str]]:
    """Read and vet every image up front, so a bad one stops things before anything is sent.
    BEFORE are images already read for the same post, which go first."""
    if (count := len(before) + len(specs)) > MAX_IMAGES:
        raise XPostError(f"at most {MAX_IMAGES} images per post, got {count}")
    media = [*before, *map(read_image, specs)]
    if len(media) > 1 and any(mime in CHUNKED_MEDIA for _, _, mime in media):
        raise XPostError("a post takes up to 4 images, or one GIF or video on its own")
    return media


def read_image(spec: str) -> tuple[str, bytes, str]:
    if REMOTE_IMAGE.fullmatch(spec):
        with tempfile.TemporaryDirectory(prefix="x-post-") as folder:
            return read_local_image(fetch(spec, Path(folder)), spec)
    return read_local_image(Path(spec), spec)


def run_openssh(tool: str, *args: str, doing: str) -> str:
    """Run ssh or scp without ever prompting, returning stdout; on failure say why in one line."""
    result = subprocess.run(
        [tool, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Not -q, which would also hide "connect to host ... timed out"; drop the ** warnings instead.
        reasons = [line.strip() for line in result.stderr.splitlines() if line.strip() and not line.startswith("**")]
        raise XPostError(f"could not {doing}: {'; '.join(reasons)}")
    return result.stdout


def screenshots_folder() -> tuple[str, str]:
    """The host and folder -s looks in, from config.toml's screenshots = "host:folder"."""
    path, config = load_config()
    if (setting := config.get("screenshots")) is None:
        raise XPostError(f'-s needs the folder to look in: add screenshots = "host:folder" to {path}')
    if not isinstance(setting, str) or not REMOTE_IMAGE.fullmatch(setting):
        raise XPostError(f"screenshots in {path} isn't host:folder: {setting!r}")
    host, _, folder = setting.partition(":")
    return host, folder


def newest_screenshot() -> str:
    """host:path of the newest image in the screenshots folder, skipping anything else there.
    It's listed with dir, so the host's ssh shell is Windows' cmd.exe."""
    host, folder = screenshots_folder()
    listing = run_openssh(
        "ssh", host, f'dir /b /o-d /a-d-h "{folder.replace("/", "\\")}"', doing=f"list screenshots on {host}"
    )
    for name in map(str.strip, listing.splitlines()):
        if name.lower().endswith(IMAGE_SUFFIXES):
            return f"{host}:{folder}/{name}"
    raise XPostError(f"no screenshots on {host} in {folder}")


def fetch(spec: str, folder: Path) -> Path:
    """Copy host:path into the folder over ssh."""
    run_openssh("scp", spec, str(folder), doing=f"fetch {spec}")
    return next(folder.iterdir())


def read_local_image(path: Path, spec: str) -> tuple[str, bytes, str]:
    """The file's name, bytes and type, judged by its first bytes and held to that type's limit."""
    try:
        with path.open("rb") as file:
            mime = image_type(file.read(12))
        if not mime:
            raise XPostError(f"{spec} isn't a JPEG, PNG, WebP, GIF, MP4 or MOV")
        limit = CHUNKED_MEDIA[mime][1] if mime in CHUNKED_MEDIA else MAX_IMAGE_BYTES
        if path.stat().st_size > limit:
            raise XPostError(f"{spec} is over {limit // (1024 * 1024)} MB")
        data = path.read_bytes()
    except OSError as e:
        raise XPostError(f"{spec}: {e.strerror.lower()}")
    if mime.startswith("video/") and (seconds := video_seconds(data)) is not None:
        if not MIN_VIDEO_SECONDS <= seconds <= MAX_VIDEO_SECONDS:
            raise XPostError(f"{spec} runs {video_length(seconds)}; X takes video from 0.5 s to 20 minutes")
    return path.name, data, mime


def upload_chunked(image: tuple[str, bytes, str], category: str, creds: dict) -> str:
    """X's chunked upload: initialize, append in pieces, finalize, then wait while X processes it."""
    name, data, mime = image
    body = {"media_type": mime, "total_bytes": len(data), "media_category": category}
    media_id = call_x("POST", f"{MEDIA_URL}/initialize", creds, 200, json=body).json()["data"]["id"]
    for index, start in enumerate(range(0, len(data), CHUNK_BYTES)):
        chunk = (name, data[start : start + CHUNK_BYTES], mime)
        call_x("POST", f"{MEDIA_URL}/{media_id}/append", creds, 200, files={"media": chunk}, data={"segment_index": index})
    info = call_x("POST", f"{MEDIA_URL}/{media_id}/finalize", creds, 200).json()["data"].get("processing_info")
    if info and info["state"] != "succeeded":
        print(f"x-post: waiting for X to process {name}", file=sys.stderr)
    waited = 0
    while info and info["state"] != "succeeded":
        if info["state"] == "failed":
            raise XPostError(f"X couldn't process {name}: {info.get('error', {}).get('message', 'no reason given')}")
        if waited >= MAX_PROCESSING_SECONDS:
            raise XPostError(f"X was still processing {name} after {waited} s")
        sleep(pause := info.get("check_after_secs", 1))
        waited += pause
        status = call_x("GET", MEDIA_URL, creds, 200, params={"command": "STATUS", "media_id": media_id})
        info = status.json()["data"]["processing_info"]
    return media_id


def upload_image(image: tuple[str, bytes, str], creds: dict) -> str:
    """Upload one image (or GIF or video, in chunks) and return the media id a post can attach."""
    if image[2] in CHUNKED_MEDIA:
        return upload_chunked(image, CHUNKED_MEDIA[image[2]][0], creds)
    response = call_x(
        "POST", MEDIA_URL, creds, 200, files={"media": image}, data={"media_category": "tweet_image"}
    )
    return response.json()["data"]["id"]


def check_alt_texts(alts: list[str]) -> None:
    for number, alt in enumerate(alts, 1):
        if len(alt) > MAX_ALT_TEXT:
            raise XPostError(f"alt text {number} is {len(alt)}/{MAX_ALT_TEXT} characters")


def make_poll(options: list[str], duration: str | None) -> dict | None:
    """The poll a post carries, held to X's limits, or None without options."""
    if not options:
        return None
    if not MIN_POLL_OPTIONS <= len(options) <= MAX_POLL_OPTIONS:
        raise XPostError(f"a poll takes {MIN_POLL_OPTIONS} to {MAX_POLL_OPTIONS} options, got {len(options)}")
    for option in options:
        if not option:
            raise XPostError("a poll option can't be empty")
        if len(option) > MAX_POLL_OPTION:
            raise XPostError(f"poll option {option!r} is over {MAX_POLL_OPTION} characters")
    duration = duration or DEFAULT_POLL_FOR
    if not (match := POLL_FOR.fullmatch(duration.strip())):
        raise XPostError(f"can't read --poll-for {duration!r}; try 30m, 6h or 2d")
    minutes = int(match[1]) * POLL_UNIT_MINUTES[match[2].lower()]
    if not MIN_POLL_MINUTES <= minutes <= MAX_POLL_MINUTES:
        raise XPostError(f"--poll-for runs from 5m to 7d, not {duration!r}")
    return {"options": options, "duration_minutes": minutes}


def poll_duration(minutes: int) -> str:
    """Minutes in the largest --poll-for unit that fits evenly: 1440 is 1d, 90 is 90m."""
    for unit, size in reversed(POLL_UNIT_MINUTES.items()):
        if minutes % size == 0:
            return f"{minutes // size}{unit}"


def upload_images(images: list[tuple[str, bytes, str]], alts: list[str], creds: dict) -> list[str]:
    """Upload each image and give it its alt text ($0.005 each; '' means none), returning media ids."""
    media_ids = []
    for image, alt in itertools.zip_longest(images, alts, fillvalue=""):
        media_ids.append(upload_image(image, creds))
        if alt:
            body = {"id": media_ids[-1], "metadata": {"alt_text": {"text": alt}}}
            call_x("POST", METADATA_URL, creds, 200, json=body)
            log_call("alt text")
    return media_ids


def post(
    text: str,
    creds: dict,
    reply_to: str | None = None,
    media_ids: list[str] | None = None,
    poll: dict | None = None,
) -> str:
    """Create a post as the credentials' user and return its id."""
    body = {"text": text} if text else {}
    if reply_to:
        body["reply"] = {"in_reply_to_tweet_id": reply_to}
    if media_ids:
        body["media"] = {"media_ids": media_ids}
    if poll:
        body["poll"] = poll
    post_id = call_x("POST", API_URL, creds, 201, json=body).json()["data"]["id"]
    # What it replies to, so --history can tell a thread (however it was carried on) from single posts.
    replying = {"reply_to": reply_to} if reply_to else {}
    log_call("link post" if find_links(text) else "post", id=post_id, text=text, **replying)
    return post_id


def post_thread(
    parts: list[str],
    creds: dict,
    reply_to: str | None = None,
    media_ids: list[list[str]] | None = None,
    poll: dict | None = None,
) -> None:
    """Post each part as a reply to the one before, printing each URL as it lands.

    Each part carries its own media ids (none past the end of MEDIA_IDS); the poll goes with the
    first post only."""
    for sent, (part, ids) in enumerate(itertools.zip_longest(parts, media_ids or [])):
        try:
            post_id = post(part, creds, reply_to, ids, poll if sent == 0 else None)
        except XPostError as e:
            if sent == 0:
                raise
            raise PartlyPosted(e, sent, parts, reply_to) from None
        remember(post_id, part, reply_to)
        reply_to = post_id
        print(post_url(post_id))


def local_now() -> datetime:
    return datetime.now()


def parse_when(text: str, now: datetime, flag: str = "--at") -> datetime:
    """The local time TEXT names, to the minute; it must be in the future. Errors name the flag."""
    now = now.replace(second=0, microsecond=0)
    if not (match := AT.fullmatch(text.strip())):
        raise XPostError(
            f"can't read {flag} {text!r}; try +90m, +2h, 09:00, tomorrow 09:00, fri 18:00 or 2026-10-24 18:30"
        )
    if match["amount"]:
        due = now + timedelta(**{UNITS[match["unit"].lower()]: int(match["amount"])})
    else:
        try:
            due = datetime.combine(
                date.fromisoformat(match["date"]) if match["date"] else now.date(),
                time(int(match["hour"]), int(match["minute"])),
            )
        except ValueError:
            raise XPostError(f"can't read {flag} {text!r}: no such time") from None
        day = (match["day"] or "").lower()
        if day == "tomorrow":
            due += timedelta(days=1)
        elif day:
            due += timedelta(days=(WEEKDAYS.index(day) - now.weekday()) % 7)
        if due <= now and not match["date"]:
            due += timedelta(days=7 if day else 1)
    if due <= now:
        raise XPostError(f"{flag} {text!r} is in the past")
    return due


def when(due: datetime) -> str:
    return f"{due:%a %Y-%m-%d %H:%M}"


def timer_units() -> dict[str, str]:
    """The user units that run --send-due every minute while anything is queued."""
    # The entry point beside this interpreter, so the timer runs the install that queued the post.
    x_post = Path(sys.executable).with_name("x-post")
    return {
        TIMER: (
            "[Unit]\n"
            "Description=Send x-post's scheduled posts when they're due\n"
            "\n"
            "[Timer]\n"
            "OnCalendar=minutely\n"
            "Persistent=true\n"
            "\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        ),
        "x-post-due.service": (
            "[Unit]\n"
            "Description=Send x-post's scheduled posts that are due\n"
            "# Skipped, without starting x-post, while nothing is queued.\n"
            "ConditionPathExistsGlob=%S/x-post/scheduled/*\n"
            "\n"
            "[Service]\n"
            "Type=oneshot\n"
            f"ExecStart={x_post} --send-due\n"
            "# x-post's own output (posts sent, failures) goes to the journal at notice, and the\n"
            "# Starting, Finished and skipped lines systemd logs every minute (info) are dropped.\n"
            "SyslogLevel=notice\n"
            "LogLevelMax=notice\n"
        ),
    }


def systemctl(*args: str) -> None:
    try:
        result = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    except FileNotFoundError:
        raise XPostError(f"could not start {TIMER}: no systemctl")
    if result.returncode != 0:
        raise XPostError(f"could not start {TIMER}: {result.stderr.strip()}")


def start_timer() -> None:
    """Install the timer the first time (or when these units change), and make sure it's running."""
    units = units_dir()
    wanted = timer_units()
    changed = any(not (units / name).exists() or (units / name).read_text() != text for name, text in wanted.items())
    try:
        if changed:
            units.mkdir(parents=True, exist_ok=True)
            for name, text in wanted.items():
                (units / name).write_text(text)
            systemctl("daemon-reload")
        systemctl("enable", "--now", TIMER)
    except XPostError:
        if changed:  # so the next --at installs it whole, reload and all
            for name in wanted:
                (units / name).unlink(missing_ok=True)
        raise
    if changed:
        print(f"x-post: installed {TIMER}, which sends queued posts when they're due", file=sys.stderr)


def queue(
    due: datetime,
    parts: list[str],
    reply_to: str | None,
    media: list[list[tuple[str, bytes, str]]],
    alts: list[list[str]],
    poll: dict | None,
) -> str:
    """Hold a post for --send-due, with copies of each part's images, and return its id."""
    while (folder := scheduled_dir() / secrets.token_hex(2)).exists():
        pass
    folder.mkdir(parents=True)
    # Written last, so --send-due never finds an entry without its images.
    entry = {
        "due": due.isoformat(timespec="minutes"),
        "parts": parts,
        "reply_to": reply_to,
        "images": store_images(folder, media),
        "alts": alts,
        "poll": poll,
    }
    save_entry(folder, entry)
    return folder.name


def store_images(folder: Path, media: list[list[tuple[str, bytes, str]]]) -> list[list[str]]:
    """Copy each part's images into a queued post's folder, returning their paths there per part.
    Each goes in a numbered folder of its own, numbered on from any already there, so it keeps
    its name, and a revision never overwrites what it replaces."""
    taken = [int(path.name) for path in folder.iterdir() if path.name.isdecimal()]
    numbers = itertools.count(max(taken, default=0) + 1)
    names = []
    for images in media:
        names.append([])
        for name, data, _ in images:
            (number := folder / str(next(numbers))).mkdir()
            (number / name).write_bytes(data)
            names[-1].append(f"{number.name}/{name}")
    return names


def save_entry(folder: Path, entry: dict) -> None:
    """Write post.json whole, renamed into place, so a send running alongside never reads half of it."""
    (folder / "post.tmp").write_text(json.dumps(entry))
    (folder / "post.tmp").rename(folder / "post.json")


def load_entry(folder: Path) -> dict:
    return json.loads((folder / "post.json").read_text())


def scheduled_posts() -> list[tuple[Path, dict]]:
    """Every queued post with its folder, soonest first."""
    entries = [(path.parent, load_entry(path.parent)) for path in scheduled_dir().glob("*/post.json")]
    return sorted(entries, key=lambda item: item[1]["due"])


def lock_entry(folder: Path):
    """Hold a queued post against every other x-post until the returned file is closed. The
    kernel lets go if this x-post dies, so a post can't stay stuck."""
    try:
        lock = open(folder / "lock", "a")
    except FileNotFoundError:  # sent or cancelled since it was listed
        raise XPostError(f"no scheduled post {folder.name!r}; x-post --scheduled lists them") from None
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise XPostError(
            f"scheduled post {folder.name} is being sent or changed right now; try again in a moment"
        ) from None
    return lock


def describe_entry(entry: dict) -> str:
    text = repr(textwrap.shorten(entry["parts"][0], 50, placeholder="..."))
    if more := len(entry["parts"]) - 1:
        text += f" and {more} more post{'s' * (more > 1)}"
    if images := sum(map(len, entry["images"])):
        text += f", {images} image{'s' * (images > 1)}"
    if entry["poll"]:
        text += ", a poll"
    return text


def list_scheduled() -> None:
    """Print the queue, soonest first, with what each post will bill and X's reason for any
    post that's held."""
    if not (entries := scheduled_posts()):
        print("nothing scheduled")
    for folder, entry in entries:
        cost = f"${sum(call['cost'] for call in billed_calls(entry['parts'], entry['alts'])) / 1000:.3f}"
        print(f"{folder.name}  {when(datetime.fromisoformat(entry['due']))}  {cost}  {describe_entry(entry)}")
        if error := entry.get("error"):
            print(f"      held: {error}")


def warn_if_held() -> None:
    """Holds happen unattended, so any other run mentions them."""
    if held := sum(1 for _, entry in scheduled_posts() if entry.get("error")):
        are = "s are" if held > 1 else " is"
        print(f"x-post: {held} scheduled post{are} held; x-post --scheduled shows why", file=sys.stderr)


def find_entry(entry_id: str) -> tuple[Path, dict]:
    # Looked up among the queued folders, never joined onto a path, so an id like .. can't
    # reach outside the queue.
    for folder, entry in scheduled_posts():
        if folder.name == entry_id:
            return folder, entry
    raise XPostError(f"no scheduled post {entry_id!r}; x-post --scheduled lists them")


def cancel(entry_id: str) -> None:
    folder, _ = find_entry(entry_id)
    with lock_entry(folder):
        entry = load_entry(folder)
        shutil.rmtree(folder)
    print(f"cancelled {entry_id}: {describe_entry(entry)}")


def retry(entry_id: str) -> None:
    """Release a held post to the next timer run; a broken thread carries on from its last post."""
    folder, _ = find_entry(entry_id)
    with lock_entry(folder):
        entry = load_entry(folder)
        if not entry.pop("error", None):
            raise XPostError(
                f"scheduled post {entry_id} isn't held; it goes out {when(datetime.fromisoformat(entry['due']))}"
            )
        start_timer()
        save_entry(folder, entry)
    print(f"{entry_id} goes out {going_out(entry)}: {describe_entry(entry)}")


def going_out(entry: dict) -> str:
    due = datetime.fromisoformat(entry["due"])
    return when(due) if due > local_now() else "within a minute"


def still_held(entry_id: str, entry: dict) -> str:
    return f" (still held; x-post --retry {entry_id} releases it)" if entry.get("error") else ""


def move(entry_id: str, text: str) -> None:
    """Change when a queued post goes out. A held one stays held: only --retry releases it.
    Moved into another month, it's checked against that month's budget as --at checks it."""
    budget = load_budget()
    due = parse_when(text, local_now(), "--move")
    folder, _ = find_entry(entry_id)
    with lock_entry(folder):
        entry = load_entry(folder)
        before = sending_month(entry)
        entry["due"] = due.isoformat(timespec="minutes")
        if f"{(month := sending_month(entry)):%Y-%m}" != f"{before:%Y-%m}":
            check_budget(entry["parts"], entry["alts"], budget, month, refused="not moved")
        save_entry(folder, entry)
    print(f"moved {entry_id} to {when(due)}: {describe_entry(entry)}{still_held(entry_id, entry)}")


def revise(entry_id: str) -> None:
    """Rewrite a queued post in the editor, with the checks, link question and budget --at asks.

    The post stays locked while the editor is open, so the timer can't send it mid-edit. An
    edit that isn't accepted stays in revise.txt, and the next --revise reopens it."""
    budget = load_budget()  # a broken config.toml is refused before the editor opens
    folder, _ = find_entry(entry_id)
    with lock_entry(folder):
        entry = load_entry(folder)
        # A whole thread queued with --numbered is edited unmarked and marked again, as -e does;
        # a held rest (2/3, 3/3) keeps its marks in its text, since it carries on the posted 1/3.
        numbered = first_place(entry["parts"]) == 1
        edit = folder / "revise.txt"
        if not edit.exists():
            texts = [PLACE.sub("", part) for part in entry["parts"]] if numbered else entry["parts"]
            drafted = map(with_image_lines, texts, entry["images"], entry["alts"], itertools.repeat(folder))
            edit.write_text(draft_text(entry["reply_to"], drafted))
        while True:
            reply_to, text = split_reply_to(edit_draft(edit))
            if not (drafted := split_thread(text)):
                edit.unlink()
                raise XPostError(f"the revision is empty; {entry_id} is unchanged")
            posts = number_posts(drafted) if numbered else drafted
            # Plain errors, not TooLong or HeldBack: main would save those as the draft.
            try:
                parts, media, alts = attach_images(posts, [], [], entry["poll"])
            except XPostError as e:
                raise XPostError(f"{e}; x-post --revise {entry_id} reopens your edit") from None
            try:
                vet(parts)
                break
            except EditNow as e:
                if e.split:
                    edit.write_text(draft_text(reply_to, split_long_posts(drafted, numbered)))
                continue
            except TooLong as e:
                raise XPostError(f"{e}; x-post --revise {entry_id} reopens your edit") from None
            except XPostError:  # held back, or the link price declined
                raise XPostError(f"not changed; x-post --revise {entry_id} reopens your edit") from None
        # Counted in place of what it replaces, last as in main.
        reopens = f"not changed; x-post --revise {entry_id} reopens your edit"
        check_budget(parts, alts, budget, sending_month(entry), entry_id, reopens)
        replaced = entry["images"]
        entry.update(parts=parts, reply_to=reply_to, images=store_images(folder, media), alts=alts)
        save_entry(folder, entry)
        for name in itertools.chain.from_iterable(replaced):
            shutil.rmtree((folder / name).parent)
        edit.unlink()
    print(f"revised {entry_id}, going out {going_out(entry)}: {describe_entry(entry)}{still_held(entry_id, entry)}")


def with_image_lines(text: str, names: list[str], alts: list[str], folder: Path) -> str:
    """A queued post as an -e draft would have it: its text, then an image: line per image
    (its copy in FOLDER), each followed by an alt: line if it has alt text."""
    lines = [text] if text else []
    for name, alt in itertools.zip_longest(names, alts, fillvalue=""):
        lines += [f"image: {folder / name}", f"alt: {alt}"] if alt else [f"image: {folder / name}"]
    return "\n".join(lines)


def alert(title: str, body: str) -> None:
    """Run config.toml's alert command (notify-send, say, or a script of your own) with TITLE and
    BODY after its own words. Without one there's no alert: the journal has the hold, and every
    other run mentions it."""
    path, config = load_config()
    if (command := config.get("alert")) is None:
        return
    try:
        if not isinstance(command, str) or not (words := shlex.split(command)):
            raise ValueError(f"alert in {path} isn't a command: {command!r}")
        run = [os.path.expanduser(words[0]), *words[1:], title, body]
        subprocess.run(run, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        print(f"x-post: could not raise an alert: {e}", file=sys.stderr)


def send_due(creds: dict) -> int:
    """Send every queued post that's due, however late. A failure is held with its reason, not retried."""
    status = 0
    for folder, _ in scheduled_posts():
        try:
            lock = lock_entry(folder)
        except XPostError:  # being changed (it goes at the next run) or just removed
            continue
        with lock:
            entry = load_entry(folder)  # as any change before the lock left it
            if entry.get("error") or datetime.fromisoformat(entry["due"]) > local_now():
                continue
            try:
                media_ids = [
                    upload_images([read_local_image(folder / name, name) for name in names], alts, creds)
                    for names, alts in zip(entry["images"], entry["alts"])
                ]
                post_thread(entry["parts"], creds, entry["reply_to"], media_ids, entry["poll"])
            except XPostError as e:
                print(f"x-post: scheduled post {folder.name}: {e}", file=sys.stderr)
                if isinstance(e, PartlyPosted):
                    unsent = len(e.unsent)
                    entry.update(
                        parts=e.unsent,
                        reply_to=e.last_id,
                        images=entry["images"][-unsent:],
                        alts=entry["alts"][-unsent:],
                        poll=None,
                    )
                entry["error"] = str(e)
                save_entry(folder, entry)
                alert("x-post: scheduled post held", f"{folder.name} {describe_entry(entry)}: {e}")
                status = 1
                continue
            shutil.rmtree(folder)
    return status


def recorded_posts() -> list[dict]:
    """What --undo would take back, oldest first: the last post x-post made, or its whole thread."""
    path = last_post_path()
    if not path.exists():
        return []
    record = json.loads(path.read_text())
    return record.get("posts") or [record]  # a lone {"id", "text"}, recorded before threads were kept


def save_record(posts: list[dict]) -> None:
    path = last_post_path()
    if not posts:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"posts": posts}))


def remember(post_id: str, text: str, reply_to: str | None) -> None:
    """Record a post for --undo. One replying to the last recorded post joins its thread, so a
    thread carried on from the draft or the queue comes back whole; any other starts afresh."""
    posts = recorded_posts()
    if not (reply_to and posts and posts[-1]["id"] == reply_to):
        posts = []
    save_record([*posts, {"id": post_id, "text": text}])


def undo(creds: dict) -> None:
    """Delete the last post x-post made, or the whole thread it ended, last post first, after
    showing it and asking. Whatever isn't deleted stays on record for the next --undo."""
    if not (posts := recorded_posts()):
        raise XPostError("nothing to undo")
    total = len(posts)
    if total == 1:
        print(f"last post: {posts[0]['text']!r}")
        question = f"delete {post_url(posts[0]['id'])}? costs $0.010"
    else:
        print(f"last post: a thread of {total}, starting {posts[0]['text']!r}")
        question = f"delete all {total}, from {post_url(posts[0]['id'])}? costs ${total / 100:.3f}"
    if not ask_yes(question):
        raise XPostError("not deleted")
    while posts:
        try:
            delete_post(posts[-1]["id"], creds)
        except XPostError as e:
            if total > 1:
                raise XPostError(f"{e}; {len(posts)} of the thread still up; x-post --undo deletes them") from None
            raise
        url = post_url(posts.pop()["id"])
        save_record(posts)
        print(f"deleted {url}")


def delete(target: str, creds: dict) -> None:
    """Delete one of x-post's posts, given its URL or id, after showing it and asking. It leaves
    the --undo record too, so --undo never tries it again."""
    if target.isdecimal():
        post_id = target
    elif match := STATUS_URL.fullmatch(target):
        post_id = match[1]
    else:
        raise XPostError(f"can't read {target!r} as a post; give its URL or id (x-post --history lists them)")
    url = post_url(post_id)
    entries = [entry for entry in logged() if entry.get("id") == post_id]
    if any(entry["kind"] == "delete" for entry in entries):
        raise XPostError(f"{url} is already deleted")
    print(f"post: {entries[-1]['text']!r}" if entries else "post: not in x-post's log, so its text can't be shown")
    if not ask_yes(f"delete {url}? costs $0.010"):
        raise XPostError("not deleted")
    delete_post(post_id, creds)
    save_record([post for post in recorded_posts() if post["id"] != post_id])
    print(f"deleted {url}")


def delete_post(post_id: str, creds: dict) -> None:
    """Have X delete one of the user's posts ($0.010), and log it."""
    response = call_x("DELETE", f"{API_URL}/{post_id}", creds, 200)
    if not response.json()["data"]["deleted"]:
        raise XPostError("X did not delete it")
    log_call("delete", id=post_id)


def log_call(kind: str, **details: str) -> None:
    """Log a billed call at its listed price, for --spent, with the post's id (and text) for
    --history. One appended line, so a send from the timer running alongside can't tangle with it."""
    entry = {"at": local_now().isoformat(timespec="seconds"), "kind": kind, "cost": PRICES[kind], **details}
    line = json.dumps(entry)
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as log:
        log.write(line + "\n")


def logged() -> list[dict]:
    """Every billed call x-post has logged, oldest first."""
    path = log_path()
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def tally(entries: list[dict]) -> str:
    counts = collections.Counter(entry["kind"] for entry in entries)
    kinds = ", ".join(f"{counts[kind]} {kind}{'s' * (counts[kind] > 1)}" for kind in PRICES if counts[kind])
    total = f"${sum(entry['cost'] for entry in entries) / 1000:.3f}"
    return f"{total} ({kinds})" if kinds else total


def logged_in(month: datetime) -> list[dict]:
    return [entry for entry in logged() if entry["at"].startswith(f"{month:%Y-%m}")]


def billed_calls(parts: list[str], alts: list[list[str]]) -> list[dict]:
    """What sending these posts would bill, shaped like the log's entries: each post at its
    link-or-not price, then each alt text."""
    kinds = ["link post" if find_links(part) else "post" for part in parts]
    kinds += ["alt text"] * sum(1 for post_alts in alts for alt in post_alts if alt)
    return [{"kind": kind, "cost": PRICES[kind]} for kind in kinds]


def load_config() -> tuple[Path, dict]:
    """config.toml's settings (none without the file), and its path for messages about them."""
    path = config_path()
    try:
        return path, tomllib.loads(path.read_text())
    except FileNotFoundError:
        return path, {}
    except tomllib.TOMLDecodeError as e:
        raise XPostError(f"{path}: {e}")


def load_budget() -> int | None:
    """The monthly budget set in config.toml, in tenths of a cent like PRICES, or None."""
    path, config = load_config()
    if (budget := config.get("budget")) is None:
        return None
    if not isinstance(budget, (int, float)):
        raise XPostError(f"budget in {path} isn't a number of dollars: {budget!r}")
    return round(budget * 1000)


def against_budget(total: int, budget: int) -> str:
    """How TOTAL stands against the BUDGET (both in tenths of a cent), worded to follow it."""
    return f"{' of' if total <= budget else ', past'} your ${budget / 1000:.2f} budget"


def sending_month(entry: dict) -> datetime:
    """When a queued post will bill: when it's due, or now if that's passed (it's held, or the
    timer couldn't run), since it goes out at the next run."""
    return max(datetime.fromisoformat(entry["due"]), local_now())


def queued_in(month: datetime, leaving_out: str | None = None) -> list[dict]:
    """What the queued posts going out in MONTH will bill, shaped like the log's entries; all
    but the one LEAVING_OUT names (being revised)."""
    return [
        call
        for folder, entry in scheduled_posts()
        if folder.name != leaving_out and f"{sending_month(entry):%Y-%m}" == f"{month:%Y-%m}"
        for call in billed_calls(entry["parts"], entry["alts"])
    ]


def month_with(calls: list[dict], month: datetime, budget: int, leaving_out: str | None = None) -> tuple[int, str]:
    """What MONTH would come to with CALLS on top of what's logged and queued for it (but the
    one LEAVING_OUT names), and that total worded against the BUDGET."""
    queued = sum(entry["cost"] for entry in queued_in(month, leaving_out))
    total = sum(entry["cost"] for entry in logged_in(month) + calls) + queued
    of_it = f" (${queued / 1000:.3f} of it queued)" if queued else ""
    return total, f"${total / 1000:.3f}{of_it}{against_budget(total, budget)}"


def check_budget(
    parts: list[str],
    alts: list[list[str]],
    budget: int | None,
    month: datetime,
    leaving_out: str | None = None,
    refused: str = "not posted",
) -> None:
    """Ask before posting what would take MONTH's spend, as logged and queued, past the BUDGET;
    refuse off a terminal, saying REFUSED."""
    if budget is None:
        return
    total, standing = month_with(billed_calls(parts, alts), month, budget, leaving_out)
    if total <= budget:
        return
    over = f"this would bring {month:%B %Y} to {standing}"
    if not sys.stdin.isatty():
        raise XPostError(f"{over}; {refused}")
    if not ask_yes(f"{over}. post anyway?"):
        raise XPostError(refused)


def spent() -> None:
    """Print what x-post's billed calls came to this month and since the record began, then
    what the queue will bill, by the month each post goes out."""
    budget = load_budget()
    if entries := logged():
        now = local_now()
        month = logged_in(now)
        standing = "" if budget is None else against_budget(sum(entry["cost"] for entry in month), budget)
        print(f"{now:%B %Y}: {tally(month)}{standing}")
        print(f"since {entries[0]['at'][:10]}: {tally(entries)}")
    else:
        print("nothing recorded yet")
    months = {f"{sending_month(entry):%Y-%m}": sending_month(entry) for _, entry in scheduled_posts()}
    for month in months.values():
        line = f"queued for {month:%B %Y}: {tally(queued_in(month))}"
        if budget is not None:
            total, _ = month_with([], month, budget)
            line += f", making ${total / 1000:.3f}{against_budget(total, budget)}"
        print(line)


def history(count: int) -> None:
    """Print the last COUNT posts or threads x-post made, newest first, with their URLs. A thread
    is one line, under its first post; posts logged before replies were are each their own."""
    entries = logged()
    gone = {entry["id"] for entry in entries if entry["kind"] == "delete"}
    threads, by_last_id = [], {}
    for entry in entries:
        if entry["kind"] not in ("post", "link post"):
            continue
        if (thread := by_last_id.pop(entry.get("reply_to"), None)) is None:
            threads.append(thread := [])
        thread.append(entry)
        by_last_id[entry["id"]] = thread
    if not threads:
        print("no posts recorded yet")
    for first, *rest in reversed(threads[-count:]):
        text = repr(textwrap.shorten(first["text"], 50, placeholder="..."))
        line = f"{when(datetime.fromisoformat(first['at']))}  {post_url(first['id'])}  {text}"
        if rest:
            line += f" and {len(rest)} more post{'s' * (len(rest) > 1)}"
        if deleted := sum(entry["id"] in gone for entry in [first, *rest]):
            line += " (deleted)" if deleted == 1 + len(rest) else f" ({deleted} of {1 + len(rest)} deleted)"
        print(line)


def post_count(text: str) -> int:
    if not text.isdecimal() or int(text) < 1:
        raise argparse.ArgumentTypeError(f"not a number of posts: {text!r}")
    return int(text)


def prompt_for_message() -> str:
    """Read one line from the user; the shell never sees it, so nothing needs quoting."""
    import readline  # noqa: F401  (imported for its side effect: line editing in input())

    return input("post: ")


def find_editor() -> list[str]:
    if editor := os.environ.get("VISUAL") or os.environ.get("EDITOR"):
        return shlex.split(editor)
    for name in FALLBACK_EDITORS:
        if shutil.which(name):
            return [name]
    raise XPostError("no editor found; set $VISUAL or $EDITOR")


def edit_draft(path: Path) -> str:
    """Open the draft in an editor. It stays on disk until posted, so nothing written is lost."""
    command = find_editor()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    try:
        result = subprocess.run([*command, str(path)])
    except FileNotFoundError:
        raise XPostError(f"could not start editor {command[0]!r}")
    if result.returncode != 0:
        raise XPostError(f"editor exited with status {result.returncode}, not posted")
    return path.read_text().strip()


def find_links(text: str) -> list[str]:
    return LINK.findall(text)


def code_point_weight(char: str) -> int:
    return 1 if any(low <= ord(char) <= high for low, high in LIGHT_CODE_POINTS) else 2


def weighted_length(text: str) -> int:
    """The length X counts against 280 (loose link matches can only overcount)."""
    text = unicodedata.normalize("NFC", text)
    length = URL_WEIGHT * len(find_links(text))
    for cluster in regex.findall(r"\X", LINK.sub("", text)):
        length += 2 if EMOJI.search(cluster) else sum(map(code_point_weight, cluster))
    return length


def line_count(text: str) -> int:
    return text.count("\n") + 1


def check_fits_without_unrolling(text: str) -> None:
    if (length := weighted_length(text)) > MAX_WEIGHTED_LENGTH:
        raise TooLong(f'{length}/{MAX_WEIGHTED_LENGTH} characters; X folds longer posts behind "Show more"')
    if (lines := line_count(text)) > MAX_LINES:
        raise TooLong(f'{lines}/{MAX_LINES} lines; X on the web folds longer posts behind "Show more"')


def fits(text: str, room: int = 0) -> bool:
    """Whether TEXT shows unfolded, with ROOM characters to spare (for a 2/3 mark)."""
    text = text.strip()
    return weighted_length(text) + room <= MAX_WEIGHTED_LENGTH and line_count(text) <= MAX_LINES


def pieces(text: str, boundary: re.Pattern) -> list[str]:
    """TEXT cut after each BOUNDARY, every piece keeping the boundary that ends it."""
    ends = [match.end() for match in boundary.finditer(text)]
    return [text[start:end] for start, end in zip([0, *ends], [*ends, len(text)]) if start < end]


def split_post(text: str, room: int = 0) -> list[str]:
    """TEXT as a thread of posts that each fit (with ROOM to spare), as full as they'll go: split
    at sentence ends and line breaks, and between words only where a sentence is too long on its own."""
    posts, current = [], ""
    for sentence in pieces(text, SENTENCE_END):
        for piece in [sentence] if fits(sentence, room) else pieces(sentence, re.compile(r"\s+")):
            if fits(current + piece, room):
                current += piece
                continue
            if not fits(piece, room):
                raise XPostError(f"can't split it: one word is over {MAX_WEIGHTED_LENGTH} characters on its own")
            posts.append(current.strip())
            current = piece
    return [post for post in [*posts, current.strip()] if post]


def split_long_posts(drafted: list[str], numbered: bool = False) -> list[str]:
    """A draft's posts with each one X would fold split into as many as it takes, leaving room
    for --numbered marks if NUMBERED. Its image: lines go with the first of them."""
    room = 0
    while True:
        posts = []
        for part in drafted:
            lines = part.splitlines()
            text = "\n".join(lines[: (start := attachments_start(lines))])
            if fits(text, room):
                posts.append(part)
                continue
            first, *rest = split_post(text, room)
            posts += ["\n".join([first, *lines[start:]]), *rest]
        # A mark's width depends on how many posts there are, which depends on the room left.
        if not numbered or len(mark := f" {len(posts)}/{len(posts)}") <= room:
            return posts
        room = len(mark)


def number_posts(drafted: list[str]) -> list[str]:
    """A thread's posts each marked with its place (2/3) at the end of its text, before any
    image: lines; a single post isn't marked, nor a thread already marked (the rest of one that
    stopped partway: 2/3, 3/3)."""
    if len(drafted) < 2:
        return drafted
    texts, posts = [], []
    for number, part in enumerate(drafted, 1):
        lines = part.splitlines()
        texts.append(text := "\n".join(lines[: (start := attachments_start(lines))]))
        posts.append("\n".join([f"{text} {number}/{len(drafted)}", *lines[start:]]))
    return posts if first_place(texts) is None else drafted


def first_place(texts: list[str]) -> int | None:
    """Where a thread already marked by --numbered starts, from its posts' TEXTS: each ends in
    its place, consecutive out of one total and finishing on the last (1 for a whole thread, 2
    for the rest of one that stopped partway at 2/3, 3/3). None if not, or for a single post."""
    places = [(int(match[1]), int(match[2])) if (match := PLACE.search(text)) else None for text in texts]
    if len(texts) < 2 or not all(places):
        return None
    if places != [(number, places[-1][1]) for number in range(places[0][0], places[-1][1] + 1)]:
        return None
    return places[0][0]


@functools.cache
def dictionary():
    from spellchecker import SpellChecker  # 160,000 words to load, so only once there's text to check

    return SpellChecker()


def possible_typos(parts: list[str]) -> dict[str, str | None]:
    """Words neither the dictionary nor the user's own list knows, in order, each with its
    likeliest correction (None when there's none)."""
    path = words_path()
    learned = set(path.read_text().lower().split()) if path.exists() else set()
    typos = {}
    for part in parts:
        for token in LINK.sub(" ", part.replace("’", "'")).split():
            if token[0] in "#@" or any(char.isdigit() for char in token):
                continue
            for word in WORD.findall(token):
                if word[1:] != word[1:].lower():
                    continue
                word = word.lower()
                if word not in learned and word not in typos and dictionary().unknown([word]):
                    typos[word] = dictionary().correction(word)
    return typos


def describe_typos(typos: dict[str, str | None]) -> str:
    return "possible typos: " + ", ".join(f"{word} ({fix}?)" if fix else word for word, fix in typos.items())


def confirm_typos(typos: dict[str, str | None]) -> str:
    """Ask what to do about possible typos: "post" anyway ('a' also adds the words to the user's
    list, so they pass from then on), "edit" the post now, or "hold" it back."""
    question = f"{describe_typos(typos)}. post anyway? a adds them to your words, e edits [y/N/a/e] "
    answer = input(question).strip().lower()
    if answer in ("a", "add"):
        path = words_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as words:
            words.write("".join(f"{word}\n" for word in typos))
        return "post"
    if answer in ("e", "edit"):
        return "edit"
    return "post" if answer in ("y", "yes") else "hold"


def vet(parts: list[str]) -> None:
    """The checks a post passes before it's sent or queued: the fold limits, typos, then the
    link price. Raises EditNow if the user would rather fix it in the editor, which a post past
    the fold is offered only at a terminal; elsewhere it's refused as TooLong."""
    try:
        check_thread_fits(parts)
    except TooLong as e:
        if not sys.stdin.isatty():
            raise
        try:
            answer = input(f"{e}. edit it now? s splits it into a thread [y/N/s] ").strip().lower()
        except EOFError:
            print(file=sys.stderr)  # end the line ^D left the cursor on
            answer = ""
        if answer in ("s", "split"):
            raise EditNow(split=True) from None
        raise (EditNow() if answer in ("y", "yes") else HeldBack("trim")) from None
    if typos := possible_typos(parts):
        if (answer := confirm_typos(typos)) == "edit":
            raise EditNow()
        if answer == "hold":
            raise HeldBack("fix")
    if links := [link for part in parts for link in find_links(part)]:
        if (answer := confirm_link_price(links)) == "edit":
            raise EditNow()
        if answer == "no":
            raise XPostError("not posted")


def split_thread(text: str) -> list[str]:
    return [part.strip() for part in THREAD_BREAK.split(text) if part.strip()]


def draft_text(reply_to: str | None, posts: Iterable[str]) -> str:
    """Posts laid out as an -e draft has them: a reply-to: line if they carry on a thread, then
    the posts with a line of --- between them."""
    header = f"reply-to: {post_url(reply_to)}\n" if reply_to else ""
    return header + "\n---\n".join(posts) + "\n"


def split_reply_to(text: str) -> tuple[str | None, str]:
    """Split off a leading reply-to: line, returning the post id it names and the rest."""
    if match := REPLY_TO.match(text):
        return match[1], text[match.end() :]
    return None, text


def split_attachments(part: str) -> tuple[str, list[tuple[str, str]]]:
    """Split the image: and alt: lines closing a post off its text, returning the text and each
    image's path (~ expanded) with its alt text ('' for none)."""
    lines = part.splitlines()
    end = attachments_start(lines)
    attached = []
    for kind, value in (ATTACHMENT.fullmatch(line).groups() for line in lines[end:]):
        if kind == "image":
            if not value:
                raise XPostError("an image: line needs a path")
            attached.append([os.path.expanduser(value), None])
        elif attached and attached[-1][1] is None:
            attached[-1][1] = value
        else:
            raise XPostError("each alt: line goes just after its own image: line")
    return "\n".join(lines[:end]).strip(), [(spec, alt or "") for spec, alt in attached]


def attachments_start(lines: list[str]) -> int:
    """Where the image: and alt: lines closing a post begin, among its LINES."""
    start = len(lines)
    while start and ATTACHMENT.fullmatch(lines[start - 1]):
        start -= 1
    return start


def attach_images(
    parts: list[str], images: list[tuple[str, bytes, str]], alts: list[str], poll: dict | None
) -> tuple[list[str], list[list[tuple[str, bytes, str]]], list[list[str]]]:
    """Each post of a draft without its image: lines, then the images those lines name and their
    alt texts, read and vetted. The -i and -s IMAGES (with ALTS) go first in the first post, which
    can't have any if it carries the POLL."""
    texts, media, alt_texts = [], [], []
    for number, part in enumerate(parts, 1):
        before, before_alts = (images, alts + [""] * (len(images) - len(alts))) if number == 1 else ([], [])
        try:
            text, attached = split_attachments(part)
            alt_texts.append(before_alts + [alt for _, alt in attached])
            check_alt_texts(alt_texts[-1])
            media.append(load_images([spec for spec, _ in attached], before))
        except XPostError as e:
            raise XPostError(f"{part_label(number, len(parts))}{e}") from None
        texts.append(text)
    if poll and media[0]:
        raise XPostError("a poll can't go with images or video; X takes one or the other")
    return texts, media, alt_texts


def part_label(number: int, total: int) -> str:
    return f"post {number} of {total}: " if total > 1 else ""


def check_thread_fits(parts: list[str]) -> None:
    for number, part in enumerate(parts, 1):
        try:
            check_fits_without_unrolling(part)
        except TooLong as e:
            raise TooLong(f"{part_label(number, len(parts))}{e}") from None


def report(
    parts: list[str],
    media: list[list[tuple[str, bytes, str]]],
    alt_texts: list[list[str]],
    poll: dict | None,
    budget: int | None,
    month: datetime,
) -> int:
    """Print how each post measures up, what it would cost (and where MONTH would come to
    against the BUDGET), then each one's images, without posting; 0 if all would show unfolded."""
    status = 0
    for number, part in enumerate(parts, 1):
        try:
            check_fits_without_unrolling(part)
            verdict = "fits"
        except TooLong:
            verdict, status = 'X would fold it behind "Show more"', 1
        print(
            f"{part_label(number, len(parts))}{weighted_length(part)}/{MAX_WEIGHTED_LENGTH} characters, "
            f"{line_count(part)}/{MAX_LINES} lines: {verdict}"
        )
    if links := [link for part in parts for link in find_links(part)]:
        print(f"links: {', '.join(links)}, so it would cost $0.20, not $0.015")
    print(f"cost: {tally(calls := billed_calls(parts, alt_texts))}")
    if budget is not None:
        print(f"{month:%B %Y} would come to {month_with(calls, month, budget)[1]}")
    for number, (images, alts) in enumerate(zip(media, alt_texts), 1):
        if not images:
            continue
        names = [
            f"{name} (alt text)" if alt else name
            for (name, _, _), alt in itertools.zip_longest(images, alts, fillvalue="")
        ]
        mime = images[0][2]  # a GIF or video comes alone
        label = "images" if mime not in CHUNKED_MEDIA else "GIF" if mime == "image/gif" else "video"
        print(f"{part_label(number, len(parts))}{label}: {', '.join(names)}")
    if poll:
        print(f"poll for {poll_duration(poll['duration_minutes'])}: {', '.join(map(repr, poll['options']))}")
    if typos := possible_typos(parts):
        print(describe_typos(typos))
    return status


def save_as_draft(draft: Path, text: str, to: str) -> None:
    """Keep a refused post to TO (trim, fix) in the editor, unless that would overwrite an unsent draft."""
    if draft.exists() and draft.read_text().strip():
        print("x-post: an unsent draft already exists, so this one wasn't saved", file=sys.stderr)
        return
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text(text + "\n")
    print(f"x-post: saved as the draft; x-post -e opens it to {to}", file=sys.stderr)


def confirm_not_a_command(text: str) -> list[str] | None:
    """Refuse, unless the user says to post it, a post that looks like an x-post command whose
    dashes were left off, which would otherwise go out in public. Returns the command's
    arguments if the user would rather run it (offered once it has all it takes)."""
    words = text.split()
    if not DASHLESS_COMMAND.fullmatch(" ".join(words)):
        return None
    command = command_meant(words)
    shown = shlex.join(["x-post", *command]) if command else f"x-post --{words[0].lower()}"
    choices = "; r runs that [y/N/r]" if command else " [y/N]"
    answer = input(f"post {text!r}? it looks like {shown} without its dashes{choices} ").strip().lower()
    if command and answer in ("r", "run"):
        return command
    if answer not in ("y", "yes"):
        raise XPostError("not posted")
    return None


def command_meant(words: list[str]) -> list[str] | None:
    """The arguments that run the x-post command WORDS spell without its dashes, or None if
    it's missing what it takes (--move's WHEN may run over several words)."""
    flag, rest = f"--{words[0].lower()}", words[1:]
    if flag == "--move" and len(rest) > 1:
        rest = [rest[0], " ".join(rest[1:])]
    takes = {"--cancel": 1, "--retry": 1, "--revise": 1, "--delete": 1, "--move": 2}.get(flag, 0)
    return [flag, *rest] if len(rest) >= takes else None


def ask_yes(question: str) -> bool:
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def confirm_link_price(links: list[str]) -> str:
    """Ask whether to pay for the links: "post", "edit" the post now (to take one out), or "no"."""
    question = f"{', '.join(links)}: link posts cost $0.20, not $0.015. post anyway? e edits [y/N/e] "
    answer = input(question).strip().lower()
    if answer in ("e", "edit"):
        return "edit"
    return "post" if answer in ("y", "yes") else "no"


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="x-post", description="Post a message to X.")
    parser.add_argument(
        "message",
        nargs="*",
        help="text to post (words are joined with spaces); omit it to be prompted instead",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "-e",
        "--edit",
        action="store_true",
        help="write the post in your editor, keeping line breaks; an unsent draft reopens next time",
    )
    mode.add_argument(
        "--undo",
        action="store_true",
        help="delete the last post x-post made, after asking ($0.010)",
    )
    parser.add_argument(
        "--numbered",
        action="store_true",
        help="mark each post of a thread with its place at the end (1/3, 2/3, 3/3), counted in its length",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="show the count and any links without posting (free, needs no credentials)",
    )
    parser.add_argument(
        "-i",
        "--image",
        action="append",
        metavar="PATH",
        help="attach a JPEG, PNG or WebP image (repeat for up to 4), or one GIF or video (MP4, MOV); "
        "the caption may be empty",
    )
    parser.add_argument(
        "-s",
        "--screenshot",
        action="store_true",
        help="attach the newest image in the screenshots folder set in config.toml",
    )
    parser.add_argument(
        "--alt",
        action="append",
        default=[],
        metavar="TEXT",
        help="alt text for the images in order, -i ones first, then -s ('' skips one; $0.005 each)",
    )
    parser.add_argument(
        "--poll",
        action="append",
        default=[],
        metavar="OPTION",
        help="add a poll option (repeat for 2 to 4, 25 characters each); the text is the question",
    )
    parser.add_argument(
        "--poll-for",
        metavar="DURATION",
        help="how long the poll stays open: 30m, 6h, 2d (5m to 7d; default 1d)",
    )
    parser.add_argument(
        "--at",
        metavar="WHEN",
        help="queue the post to go out later: +90m, +2h, 09:00, tomorrow 09:00, fri 18:00, 2026-10-24 18:30",
    )
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="list the queued posts, soonest first, and why any is held",
    )
    parser.add_argument(
        "--cancel",
        metavar="ID",
        help="remove a queued post; --scheduled shows the ids",
    )
    parser.add_argument(
        "--retry",
        metavar="ID",
        help="send a held post at the timer's next run; a broken thread carries on where it stopped",
    )
    parser.add_argument(
        "--move",
        nargs=2,
        metavar=("ID", "WHEN"),
        help="change when a queued post goes out (WHEN as for --at); a held one stays held",
    )
    parser.add_argument(
        "--revise",
        metavar="ID",
        help="rewrite a queued post in your editor, checked as --at checks it",
    )
    parser.add_argument(
        "--send-due",
        action="store_true",
        help="send the queued posts that are due, however late (the timer runs this every minute)",
    )
    parser.add_argument(
        "--spent",
        action="store_true",
        help="what x-post's posts, alt texts and deletes have cost, this month and in all, at X's listed "
        "prices (from its own record; the Developer Console has the real balance)",
    )
    parser.add_argument(
        "--history",
        nargs="?",
        const=10,
        type=post_count,
        metavar="N",
        help="the last N posts x-post made (default 10), newest first, with their URLs (from its own log)",
    )
    parser.add_argument(
        "--delete",
        metavar="POST",
        help="delete one of your posts, given its URL or id (--history lists them), after asking ($0.010)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    defaults = vars(parser.parse_args([]))
    for flag in (
        "--send-due", "--scheduled", "--cancel", "--retry", "--move", "--revise", "--spent", "--history", "--delete"
    ):
        name = flag[2:].replace("-", "_")
        if getattr(args, name) and {**vars(args), name: defaults[name]} != defaults:
            parser.error(f"{flag} takes no other options")
    if len(args.alt) > (image_count := len(args.image or []) + args.screenshot):
        parser.error(f"more --alt ({len(args.alt)}) than images ({image_count})")
    if args.message and (args.edit or args.undo):
        parser.error(f"{'-e' if args.edit else '--undo'} takes no message")
    if args.undo and (args.image or args.screenshot):
        parser.error("--undo takes no images")
    if args.undo and args.poll:
        parser.error("--undo takes no poll")
    if args.poll_for and not args.poll:
        parser.error("--poll-for needs --poll")
    if args.poll and (args.image or args.screenshot):
        parser.error("a poll can't go with images or video; X takes one or the other")
    if args.check and args.undo:
        parser.error("--check can't be combined with --undo")
    if args.at and args.undo:
        parser.error("--at can't be combined with --undo")
    draft = draft_path()
    editing = args.edit  # becomes True once a post is sent to the editor to fix
    try:
        if args.scheduled:
            list_scheduled()
            return 0
        if args.spent:
            spent()
            return 0
        if args.history:
            history(args.history)
            return 0
        if args.cancel:
            cancel(args.cancel)
            return 0
        if args.retry:
            retry(args.retry)
            return 0
        if args.move:
            move(*args.move)
            return 0
        if args.revise:
            revise(args.revise)
            return 0
        # First of all, so a slip reads no credentials and fetches no images; --check never posts.
        if args.message and not args.check and (command := confirm_not_a_command(" ".join(args.message))):
            return main(command)
        # Before prompting, so a broken credentials file can't eat a typed message.
        creds = None if args.check else load_credentials(credentials_path())
        budget = load_budget()
        if args.send_due:
            return send_due(creds)
        warn_if_held()
        if args.undo:
            undo(creds)
            return 0
        if args.delete:
            delete(args.delete, creds)
            return 0
        due = parse_when(args.at, local_now()) if args.at else None
        check_alt_texts(args.alt)
        poll = make_poll(args.poll, args.poll_for)
        specs = args.image or []
        if args.screenshot:
            screenshot = newest_screenshot()
            print(f"screenshot: {screenshot.rpartition('/')[2]}")
            specs = specs + [screenshot]
        images = load_images(specs)
        while True:
            reply_to = None
            if editing:
                reply_to, text = split_reply_to(edit_draft(draft))
                drafted = split_thread(text)
            else:
                text = " ".join(args.message) if args.message else prompt_for_message()
                drafted = [text] if text.strip() or images else []
            if not drafted:
                parser.error("message is empty")
            posts = number_posts(drafted) if args.numbered else drafted  # the draft itself stays unmarked
            if editing:
                parts, media, alts = attach_images(posts, images, args.alt, poll)
            else:
                parts, media, alts = posts, [images], [args.alt]
            if args.check:
                status = report(parts, media, alts, poll, budget, due or local_now())
                if due:
                    print(f"would go out {when(due)}")
                return status
            try:
                vet(parts)
                break
            except EditNow as e:  # carry on as -e would, from the draft
                if not editing:
                    if draft.exists() and draft.read_text().strip():
                        raise XPostError(
                            "an unsent draft already exists, so this one can't go to the editor"
                        ) from None
                    draft.parent.mkdir(parents=True, exist_ok=True)
                    draft.write_text(text + "\n")
                    editing = True
                if e.split:  # rewritten split, reply-to: and image: lines and all
                    draft.write_text(draft_text(reply_to, split_long_posts(drafted, args.numbered)))
        check_budget(parts, alts, budget, due or local_now())  # last, once every other answer is in
        if due:
            start_timer()
            print(f"scheduled {queue(due, parts, reply_to, media, alts, poll)} for {when(due)}")
        else:
            media_ids = [upload_images(post_media, post_alts, creds) for post_media, post_alts in zip(media, alts)]
            post_thread(parts, creds, reply_to, media_ids, poll)
    except XPostError as e:
        print(f"x-post: {e}", file=sys.stderr)
        if isinstance(e, PartlyPosted):  # the unsent posts as drafted, image: lines and all
            draft.write_text(draft_text(e.last_id, posts[-len(e.unsent) :]))  # marks and all
            print("x-post: the rest is in the draft, replying to the last one sent; x-post -e sends it", file=sys.stderr)
        elif editing:
            if draft.exists() and draft.read_text().strip():
                print("x-post: draft kept; x-post -e reopens it", file=sys.stderr)
        elif isinstance(e, TooLong):
            save_as_draft(draft, text, "trim")
        elif isinstance(e, HeldBack):
            save_as_draft(draft, text, e.to)
        return 1
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)  # end the line ^C or ^D left the cursor on
        return 1
    if editing:
        draft.unlink()
    return 0
