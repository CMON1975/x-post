import builtins
import fcntl
import io
import itertools
import json
import os
import re
import shlex
import struct
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
import responses
from oauthlib.common import Request
from oauthlib.oauth1.rfc5849 import signature

import x_post

API_URL = "https://api.x.com/2/tweets"
MEDIA_URL = "https://api.x.com/2/media/upload"
METADATA_URL = "https://api.x.com/2/media/metadata"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 64
GIF = b"GIF89a" + bytes(range(64))  # distinct bytes, so each chunk can be found in its request
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64  # an ISO media file's ftyp box, then its brand
MOV = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 64
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64  # the same box on an iPhone photo

CREDENTIALS = """\
api_key = "api-key"
api_secret = "api-secret"
access_token = "access-token"
access_token_secret = "access-token-secret"
"""


@pytest.fixture(autouse=True)
def config_home(tmp_path, monkeypatch):
    """Point the CLI at a scratch config dir so tests never read real credentials."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def state_home(tmp_path, monkeypatch):
    """Keep drafts in a scratch dir and the user's own editor settings out of the tests."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    return tmp_path / "state"


@pytest.fixture(autouse=True)
def systemctl(tmp_path, monkeypatch):
    """A fake systemctl on PATH that logs its arguments, so no test reaches the real user manager.

    Write a message to .fail to make it exit 1 with that on stderr."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "systemctl"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "here = pathlib.Path(__file__).parent\n"
        "with (here / 'systemctl-calls.jsonl').open('a') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if (fail := here / 'systemctl.fail').exists():\n"
        "    sys.exit(fail.read_text())\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    calls = bin_dir / "systemctl-calls.jsonl"
    return SimpleNamespace(
        calls=lambda: [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else [],
        fail=bin_dir / "systemctl.fail",
    )


@pytest.fixture
def alerts(tmp_path):
    """A fake alert command, alerts/alert.py, that logs its arguments. A test names it as the
    alert with alert_in; the scratch config.toml has none otherwise, so no test raises a real one."""
    folder = tmp_path / "alerts"
    folder.mkdir()
    script = folder / "alert.py"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "with pathlib.Path(__file__).with_suffix('.jsonl').open('a') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    script.chmod(0o755)
    log = folder / "alert.jsonl"

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return SimpleNamespace(calls=calls)


def alert_in(config_home, command):
    """Set config.toml's alert to COMMAND, given as TOML (a quoted string, or a bad value)."""
    path = config_home / "x-post" / "config.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"alert = {command}\n")
    return path


RELOAD = ["--user", "daemon-reload"]
ENABLE = ["--user", "enable", "--now", "x-post-due.timer"]


@pytest.fixture
def credentials(config_home):
    path = config_home / "x-post" / "credentials.toml"
    path.parent.mkdir()
    path.write_text(CREDENTIALS)
    return path


@pytest.fixture(autouse=True)
def api():
    """Intercept all HTTP: an unregistered request errors instead of reaching X."""
    with responses.RequestsMock() as rsps:
        yield rsps


def created(api, post_id="1850000000000000001"):
    api.post(API_URL, status=201, json={"data": {"id": post_id, "text": "..."}})


def typed(monkeypatch, keystrokes):
    """Stand in for what the user types at the prompt."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(keystrokes))


def write_editor(path, writes, exit_code):
    """A stand-in editor: records the draft it was opened on, then optionally rewrites it."""
    path.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        "draft = pathlib.Path(sys.argv[1])\n"
        "pathlib.Path(__file__).with_name('opened.txt').write_text(draft.read_text())\n"
        + (f"draft.write_text({writes!r})\n" if writes is not None else "")
        + f"sys.exit({exit_code})\n"
    )
    return path.with_name("opened.txt")


def editor(tmp_path, monkeypatch, writes=None, exit_code=0, var="VISUAL"):
    script = tmp_path / "editor.py"
    monkeypatch.setenv(var, shlex.join([sys.executable, str(script)]))
    return write_editor(script, writes, exit_code)


def draft(state_home):
    return state_home / "x-post" / "draft.txt"


def editors(tmp_path, monkeypatch, *writes):
    """A stand-in editor that writes WRITES in turn, one per opening (a further opening fails),
    and returns a function giving what each opening found."""
    script = tmp_path / "editors.py"
    found = tmp_path / "openings.json"
    script.write_text(
        "import json, pathlib, sys\n"
        "draft = pathlib.Path(sys.argv[1])\n"
        f"log = pathlib.Path({str(found)!r})\n"
        "found = json.loads(log.read_text()) if log.exists() else []\n"
        "log.write_text(json.dumps(found + [draft.read_text()]))\n"
        f"draft.write_text({list(writes)!r}[len(found)])\n"
    )
    monkeypatch.setenv("VISUAL", shlex.join([sys.executable, str(script)]))
    return lambda: json.loads(found.read_text())


class Terminal(io.StringIO):
    def isatty(self):
        return True


def typed_at_a_terminal(monkeypatch, keystrokes):
    monkeypatch.setattr(sys, "stdin", Terminal(keystrokes))


LUCIDE_ICONS = Path(os.environ.get("LUCIDE_ICONS", "/nonexistent"))


@pytest.mark.skipif(not LUCIDE_ICONS.is_dir(), reason="needs LUCIDE_ICONS naming a Lucide clone's icons/")
def test_the_app_icon_is_drawn_from_lucides_send():
    paths = lambda svg: [path.get("d") for path in ET.fromstring(svg).iter("{http://www.w3.org/2000/svg}path")]  # noqa: E731
    icon = Path(__file__).resolve().parents[1] / "assets" / "icon.svg"
    assert paths(icon.read_text()) == paths((LUCIDE_ICONS / "send.svg").read_text())


COMPLETION = Path(__file__).resolve().parents[1] / "completions" / "x-post.bash"


def complete(*words, cwd=None):
    """What TAB offers after `x-post WORDS`, the last word being the one typed so far, in a bare bash."""
    line = ["x-post", *words]
    script = (
        f"source {shlex.quote(str(COMPLETION))} || exit 3\n"
        f"COMP_WORDS=({shlex.join(line)}); COMP_CWORD={len(line) - 1}\n"
        "_x_post\n"
        "(( ${#COMPREPLY[@]} )) && printf '%s\\n' \"${COMPREPLY[@]}\"\n"
        "exit 0\n"
    )
    result = subprocess.run(["bash", "--norc", "--noprofile", "-c", script], capture_output=True, text=True, cwd=cwd)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""  # TAB must never print over the command line
    return result.stdout.splitlines()


def test_completion_offers_every_option_the_parser_has():
    options = {option for action in x_post.make_parser()._actions for option in action.option_strings}
    assert sorted(complete("-")) == sorted(options)


def test_completion_narrows_options_to_what_is_typed():
    assert complete("--re") == ["--retry", "--revise"]


@pytest.mark.parametrize("command", ["--cancel", "--retry", "--move", "--revise"])
def test_completion_offers_queue_ids_after_the_commands_that_take_one(api, credentials, clock, ids, command):
    assert x_post.main(["--at", "+1h", "one"]) == 0
    assert x_post.main(["--at", "+2h", "two"]) == 0
    assert complete(command, "") == ["0001", "0002"]
    assert complete(command, "00") == ["0001", "0002"]
    assert complete(command, "0002") == ["0002"]


def test_completion_offers_no_ids_when_nothing_is_queued():
    assert complete("--cancel", "") == []


def test_completion_offers_logged_post_ids_after_delete_newest_first_skipping_deleted(api, credentials, monkeypatch):
    assert complete("--delete", "") == []  # nothing logged yet
    posted_earlier(api, "first", "1")
    posted_earlier(api, "second", "2")
    posted_earlier(api, 'a "id": "999" joke', "3")
    deleted(api, "2")
    typed(monkeypatch, "y\n")
    assert x_post.main(["--delete", "2"]) == 0
    assert complete("--delete", "") == ["3", "1"]


def test_completion_offers_files_after_image(tmp_path):
    (tmp_path / "photo.png").write_bytes(PNG)
    (tmp_path / "notes.txt").write_text("")
    (tmp_path / "pics").mkdir()
    assert complete("-i", "ph", cwd=tmp_path) == ["photo.png"]
    assert complete("--image", "pi", cwd=tmp_path) == ["pics"]


def test_posts_message_and_prints_its_url(api, credentials, capsys):
    created(api, "1850000000000000001")
    assert x_post.main(["i'm having a great day"]) == 0
    assert json.loads(api.calls[0].request.body) == {"text": "i'm having a great day"}
    assert capsys.readouterr().out == "https://x.com/i/status/1850000000000000001\n"


def test_joins_unquoted_words_with_spaces(api, credentials):
    created(api)
    x_post.main(["having", "a", "great", "day"])
    assert json.loads(api.calls[0].request.body) == {"text": "having a great day"}


def test_signs_request_with_oauth1_user_credentials(api, credentials):
    created(api)
    x_post.main(["hello"])
    assert_signed(api.calls[0].request)


def assert_signed(request):
    header = request.headers["Authorization"].decode()
    params = dict(signature.collect_parameters(headers={"Authorization": header}, exclude_oauth_signature=False))
    assert params["oauth_consumer_key"] == "api-key"
    assert params["oauth_token"] == "access-token"
    # Recompute the signature server-side: catches the two secrets being swapped.
    oauth_request = Request(request.url, "POST")
    oauth_request.params = [(k, v) for k, v in params.items() if k != "oauth_signature"]
    oauth_request.signature = params["oauth_signature"]
    assert signature.verify_hmac_sha1(oauth_request, "api-secret", "access-token-secret")


def image(tmp_path, name="photo.png", data=PNG):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def uploaded(api, media_id):
    api.post(MEDIA_URL, status=200, json={"data": {"id": media_id, "media_key": f"3_{media_id}"}})


def test_images_are_uploaded_then_attached_in_order(api, credentials, tmp_path):
    uploaded(api, "m1")
    uploaded(api, "m2")
    created(api)
    photo, scan = image(tmp_path, "photo.jpg", JPEG), image(tmp_path, "scan.webp", WEBP)
    assert x_post.main(["-i", photo, "-i", scan, "two", "pictures"]) == 0
    first_upload = api.calls[0].request
    assert first_upload.url == MEDIA_URL
    assert b'name="media_category"\r\n\r\ntweet_image' in first_upload.body
    assert JPEG in first_upload.body
    assert WEBP in api.calls[1].request.body
    assert_signed(first_upload)
    assert json.loads(api.calls[2].request.body) == {"text": "two pictures", "media": {"media_ids": ["m1", "m2"]}}


@pytest.mark.parametrize(
    "files, error",
    [
        ([("missing.png", None)], "missing.png: no such file"),
        ([("notes.png", b"just some text")], "notes.png isn't a JPEG, PNG, WebP, GIF, MP4 or MOV"),
        ([("photo.heic", HEIC)], "photo.heic isn't a JPEG, PNG, WebP, GIF, MP4 or MOV"),
        ([("huge.png", PNG + b"\x00" * (5 * 1024 * 1024))], "huge.png is over 5 MB"),
        ([("huge.gif", GIF + b"\x00" * (15 * 1024 * 1024))], "huge.gif is over 15 MB"),
        ([(f"{n}.png", PNG) for n in range(5)], "at most 4 images per post"),
        ([("anim.gif", GIF), ("photo.png", PNG)], "a post takes up to 4 images, or one GIF or video on its own"),
        ([("one.gif", GIF), ("two.gif", GIF)], "a post takes up to 4 images, or one GIF or video on its own"),
    ],
    ids=["missing", "not-an-image", "heic", "too-big", "gif-too-big", "five", "gif-with-an-image", "two-gifs"],
)
def test_bad_images_are_refused_before_the_prompt_or_any_upload(api, credentials, tmp_path, capsys, files, error):
    argv = []
    for name, data in files:
        if data is not None:
            image(tmp_path, name, data)
        argv += ["-i", str(tmp_path / name)]
    assert x_post.main(argv) == 1
    captured = capsys.readouterr()
    assert error in captured.err
    assert captured.out == ""  # no "post: " prompt
    assert not api.calls


@pytest.fixture
def slept(monkeypatch):
    """The waits x-post would sleep, recorded instead of slept."""
    naps = []
    monkeypatch.setattr(x_post, "sleep", naps.append)
    return naps


def chunked(api, media_id, processing=(None,)):
    """Register X's chunked upload of one file: initialize, the appends, finalize (with the first
    processing_info), then a STATUS reply for each of the rest; the last one repeats."""
    api.post(f"{MEDIA_URL}/initialize", status=200, json={"data": {"id": media_id, "expires_after_secs": 86400}})
    api.post(f"{MEDIA_URL}/{media_id}/append", status=200, json={"data": {"expires_at": 1893456000}})
    first, *rest = processing
    finalized = {"id": media_id, **({"processing_info": first} if first else {})}
    api.post(f"{MEDIA_URL}/{media_id}/finalize", status=200, json={"data": finalized})
    for info in rest:
        api.get(MEDIA_URL, status=200, json={"data": {"id": media_id, "processing_info": info}})


def test_a_gif_goes_up_in_chunks_and_is_attached_once_x_has_processed_it(
    api, credentials, monkeypatch, tmp_path, slept, capsys
):
    monkeypatch.setattr(x_post, "CHUNK_BYTES", 30)  # the 70-byte GIF goes up as 30 + 30 + 10
    chunked(
        api,
        "g1",
        [
            {"state": "pending", "check_after_secs": 2},
            {"state": "in_progress", "check_after_secs": 3},
            {"state": "succeeded"},
        ],
    )
    created(api)
    assert x_post.main(["-i", image(tmp_path, "anim.gif", GIF), "look"]) == 0
    calls = list(api.calls)
    assert len(calls) == 8
    initialize, appends, finalize, statuses, posted = calls[0], calls[1:4], calls[4], calls[5:7], calls[7]
    assert json.loads(initialize.request.body) == {
        "media_type": "image/gif",
        "total_bytes": 70,
        "media_category": "tweet_gif",
    }
    for index, call in enumerate(appends):
        assert f'name="segment_index"\r\n\r\n{index}\r\n'.encode() in call.request.body
        assert GIF[index * 30 : index * 30 + 30] in call.request.body
    assert finalize.request.url == f"{MEDIA_URL}/g1/finalize"
    assert [urllib.parse.parse_qs(urllib.parse.urlsplit(call.request.url).query) for call in statuses] == [
        {"command": ["STATUS"], "media_id": ["g1"]}
    ] * 2
    assert slept == [2, 3]
    assert json.loads(posted.request.body) == {"text": "look", "media": {"media_ids": ["g1"]}}
    assert capsys.readouterr().err == "x-post: waiting for X to process anim.gif\n"


@pytest.mark.parametrize(
    "name, data, mime", [("clip.mp4", MP4, "video/mp4"), ("clip.mov", MOV, "video/quicktime")], ids=["mp4", "mov"]
)
def test_a_video_goes_up_in_chunks_as_tweet_video(api, credentials, tmp_path, slept, name, data, mime):
    chunked(api, "v1")
    created(api)
    assert x_post.main(["-i", image(tmp_path, name, data), "watch"]) == 0
    assert json.loads(api.calls[0].request.body) == {
        "media_type": mime,
        "total_bytes": len(data),
        "media_category": "tweet_video",
    }
    assert json.loads(api.calls[-1].request.body) == {"text": "watch", "media": {"media_ids": ["v1"]}}


def box(kind, payload):
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def mp4(milliseconds, version=0, media_first=False):
    """An MP4 that runs MILLISECONDS by its movie header (mvhd), in its 32- or 64-bit form, with
    the media box (a 64-bit-sized one) before or after the movie box."""
    if version == 0:
        mvhd = bytes(4) + struct.pack(">IIII", 0, 0, 1000, milliseconds)
    else:
        mvhd = bytes([1, 0, 0, 0]) + struct.pack(">QQIQ", 0, 0, 1000, milliseconds)
    moov = box(b"moov", box(b"mvhd", mvhd + bytes(80)))
    mdat = struct.pack(">I4sQ", 1, b"mdat", 32) + bytes(16)
    return box(b"ftyp", b"isom" + bytes(4) + b"isomavc1") + (mdat + moov if media_first else moov + mdat)


@pytest.mark.parametrize(
    "data, shown",
    [(mp4(1_260_000), "21:00"), (mp4(400), "0.4 s"), (mp4(1_500_000, version=1, media_first=True), "25:00")],
    ids=["over-20-minutes", "under-half-a-second", "64-bit-header-after-the-media"],
)
def test_a_video_too_long_or_short_for_x_is_refused_before_uploading(api, credentials, tmp_path, capsys, data, shown):
    clip = image(tmp_path, "clip.mp4", data)
    assert x_post.main(["-i", clip, "watch"]) == 1
    assert f"{clip} runs {shown}; X takes video from 0.5 s to 20 minutes" in capsys.readouterr().err
    assert not api.calls


@pytest.mark.parametrize("milliseconds", [500, 1_200_000, 0], ids=["half-a-second", "20-minutes", "length-unknown"])
def test_a_video_within_x_s_length_limits_goes_up(api, credentials, tmp_path, slept, milliseconds):
    chunked(api, "v1")
    created(api)
    assert x_post.main(["-i", image(tmp_path, "clip.mp4", mp4(milliseconds)), "watch"]) == 0


def test_a_video_over_512_mb_is_refused_before_anything(api, credentials, tmp_path, capsys):
    clip = Path(image(tmp_path, "long.mp4", MP4))
    os.truncate(clip, 512 * 1024 * 1024 + 1)  # sparse: nothing that big is written
    assert x_post.main(["-i", str(clip), "watch"]) == 1
    assert f"{clip} is over 512 MB" in capsys.readouterr().err
    assert not api.calls


def test_media_x_cannot_process_posts_nothing(api, credentials, tmp_path, slept, capsys):
    failed = {"state": "failed", "error": {"code": 1, "name": "InvalidMedia", "message": "Unsupported format"}}
    chunked(api, "g1", [{"state": "pending", "check_after_secs": 1}, failed])
    assert x_post.main(["-i", image(tmp_path, "anim.gif", GIF), "look"]) == 1
    assert "x-post: X couldn't process anim.gif: Unsupported format" in capsys.readouterr().err
    assert all(call.request.url != API_URL for call in api.calls)


def test_media_processing_gives_up_after_ten_minutes(api, credentials, tmp_path, slept, capsys):
    chunked(api, "g1", [{"state": "pending", "check_after_secs": 100}, {"state": "in_progress", "check_after_secs": 100}])
    assert x_post.main(["-i", image(tmp_path, "anim.gif", GIF), "look"]) == 1
    assert sum(slept) == 600
    assert "x-post: X was still processing anim.gif after 600 s" in capsys.readouterr().err
    assert all(call.request.url != API_URL for call in api.calls)


def test_image_post_needs_no_caption(api, credentials, monkeypatch, tmp_path):
    uploaded(api, "m1")
    created(api)
    typed(monkeypatch, "\n")
    assert x_post.main(["-i", image(tmp_path)]) == 0
    assert json.loads(api.calls[1].request.body) == {"media": {"media_ids": ["m1"]}}


def test_nothing_is_uploaded_before_the_link_question_is_answered(api, credentials, monkeypatch, tmp_path):
    typed(monkeypatch, "n\n")
    assert x_post.main(["-i", image(tmp_path), "see", "example.com"]) == 1
    assert not api.calls


def test_failed_upload_posts_nothing(api, credentials, tmp_path, capsys):
    api.post(MEDIA_URL, status=400, json={"title": "Invalid Request", "detail": "Unsupported media."})
    assert x_post.main(["-i", image(tmp_path), "caption"]) == 1
    assert "400 Invalid Request: Unsupported media." in capsys.readouterr().err
    assert [call.request.url for call in api.calls] == [MEDIA_URL]


PQ_WARNING = (
    "** WARNING: connection is not using a post-quantum key exchange algorithm.\n"
    '** This session may be vulnerable to "store now, decrypt later" attacks.\n'
    "** The server may need to be upgraded. See https://openssh.com/pq.html\n"
)


def fake_openssh(tmp_path, monkeypatch, tool, remote_files=None, stdout="", stderr="", exit_code=0):
    """Put a fake ssh or scp on PATH that logs its arguments.

    scp serves tmp_path/remote/<host>/<path>; ssh prints stdout. Like the real tools,
    -q hides ssh's own messages (the lines starting "ssh:" or "**")."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for spec, data in (remote_files or {}).items():
        host, _, path = spec.partition(":")
        target = tmp_path / "remote" / host / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    script = bin_dir / tool
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, shutil, sys\n"
        "here = pathlib.Path(__file__).parent\n"
        f"with (here / '{tool}-calls.jsonl').open('a') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"for line in {stderr!r}.splitlines():\n"
        "    if '-q' not in sys.argv or not line.startswith(('ssh:', '**')):\n"
        "        print(line, file=sys.stderr)\n"
        f"if {exit_code}:\n"
        f"    sys.exit({exit_code})\n"
        f"print({stdout!r}, end='')\n"
        f"if {tool == 'scp'}:\n"
        "    host, _, path = sys.argv[-2].partition(':')\n"
        "    shutil.copy(here.parent / 'remote' / host / path, sys.argv[-1])\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    calls = bin_dir / f"{tool}-calls.jsonl"
    return lambda: [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []


def test_remote_image_is_fetched_over_ssh_then_attached(api, credentials, monkeypatch, tmp_path):
    scp_calls = fake_openssh(tmp_path, monkeypatch, "scp", {"pc:Pictures/photo.png": PNG})
    uploaded(api, "m1")
    created(api)
    assert x_post.main(["-i", "pc:Pictures/photo.png", "from", "the", "other", "box"]) == 0
    assert PNG in api.calls[0].request.body
    [call] = scp_calls()
    assert "BatchMode=yes" in call and "pc:Pictures/photo.png" in call
    assert not os.path.exists(call[-1])  # the temporary copy is gone


def test_remote_fetch_failure_says_why_in_one_line(api, credentials, monkeypatch, tmp_path, capsys):
    # What real scp prints when the other machine is asleep, warning noise included.
    stderr = PQ_WARNING + "ssh: connect to host pc port 22: Connection timed out\nscp: Connection closed\n"
    fake_openssh(tmp_path, monkeypatch, "scp", stderr=stderr, exit_code=255)
    assert x_post.main(["-i", "pc:photo.png"]) == 1
    captured = capsys.readouterr()
    assert captured.err == (
        "x-post: could not fetch pc:photo.png: "
        "ssh: connect to host pc port 22: Connection timed out; scp: Connection closed\n"
    )
    assert captured.out == ""
    assert not api.calls


SCREENSHOTS = "pc:Pictures/Screenshots"


def screenshots_in(config_home, folder=SCREENSHOTS):
    """Set config.toml's screenshots folder: repr makes a str a TOML literal string, and leaves
    any other value as TOML reads it."""
    path = config_home / "x-post" / "config.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"screenshots = {folder!r}\n")
    return path


def test_screenshot_attaches_the_newest_image_in_the_configured_folder(
    api, credentials, config_home, monkeypatch, tmp_path, capsys
):
    screenshots_in(config_home)
    listing = "notes.zip\r\nScreenshot 2026-10-23 090000.png\r\nScreenshot 2026-10-23 065138.png\r\n"
    ssh_calls = fake_openssh(tmp_path, monkeypatch, "ssh", stdout=listing, stderr=PQ_WARNING)
    fake_openssh(tmp_path, monkeypatch, "scp", {f"{SCREENSHOTS}/Screenshot 2026-10-23 090000.png": PNG})
    uploaded(api, "m1")
    created(api)
    assert x_post.main(["-s", "look", "at", "this"]) == 0
    assert PNG in api.calls[0].request.body
    [call] = ssh_calls()
    assert call[-2:] == ["pc", 'dir /b /o-d /a-d-h "Pictures\\Screenshots"']
    assert "BatchMode=yes" in call
    assert capsys.readouterr().out.startswith("screenshot: Screenshot 2026-10-23 090000.png\n")


@pytest.mark.parametrize(
    "setting, error",
    [
        (None, '-s needs the folder to look in: add screenshots = "host:folder" to {path}'),
        ("Pictures/Screenshots", "screenshots in {path} isn't host:folder: 'Pictures/Screenshots'"),
        (5, "screenshots in {path} isn't host:folder: 5"),
    ],
    ids=["unset", "no-host", "not-text"],
)
def test_screenshot_needs_its_folder_set_in_the_config(
    api, credentials, config_home, monkeypatch, tmp_path, capsys, setting, error
):
    path = config_home / "x-post" / "config.toml" if setting is None else screenshots_in(config_home, setting)
    ssh_calls = fake_openssh(tmp_path, monkeypatch, "ssh")
    assert x_post.main(["-s", "look"]) == 1
    assert capsys.readouterr().err == f"x-post: {error.format(path=path)}\n"
    assert ssh_calls() == []
    assert not api.calls


@pytest.mark.parametrize(
    "stdout, stderr, exit_code, error",
    [
        ("", PQ_WARNING + "File Not Found\n", 1, "could not list screenshots on pc: File Not Found"),
        ("notes.zip\r\n", "", 0, "no screenshots on pc in Pictures/Screenshots"),
    ],
    ids=["listing-fails", "no-images"],
)
def test_screenshot_problems_are_reported_before_the_prompt(
    api, credentials, config_home, monkeypatch, tmp_path, capsys, stdout, stderr, exit_code, error
):
    screenshots_in(config_home)
    fake_openssh(tmp_path, monkeypatch, "ssh", stdout=stdout, stderr=stderr, exit_code=exit_code)
    assert x_post.main(["-s"]) == 1
    captured = capsys.readouterr()
    assert captured.err == f"x-post: {error}\n"
    assert captured.out == ""
    assert not api.calls


@pytest.mark.parametrize("spec", ["C:/photos/x.png", "./odd:name.png"], ids=["drive-letter", "dot-slash"])
def test_paths_that_only_look_remote_stay_local(api, credentials, monkeypatch, tmp_path, capsys, spec):
    scp_calls = fake_openssh(tmp_path, monkeypatch, "scp")
    assert x_post.main(["-i", spec, "caption"]) == 1
    assert f"{spec}: no such file" in capsys.readouterr().err
    assert scp_calls() == []


def described(api, media_id):
    api.post(METADATA_URL, status=200, json={"data": {"id": media_id, "associated_metadata": {}}})


def alt_body(media_id, text):
    return {"id": media_id, "metadata": {"alt_text": {"text": text}}}


def test_alt_text_is_set_on_each_image_before_posting(api, credentials, tmp_path):
    uploaded(api, "101")
    described(api, "101")
    uploaded(api, "102")
    described(api, "102")
    created(api)
    cat, dog = image(tmp_path, "cat.png"), image(tmp_path, "dog.png")
    assert x_post.main(["-i", cat, "-i", dog, "--alt", "a cat", "--alt", "a dog", "pets"]) == 0
    assert [call.request.url for call in api.calls] == [MEDIA_URL, METADATA_URL, MEDIA_URL, METADATA_URL, API_URL]
    assert json.loads(api.calls[1].request.body) == alt_body("101", "a cat")
    assert json.loads(api.calls[3].request.body) == alt_body("102", "a dog")
    assert json.loads(api.calls[4].request.body) == {"text": "pets", "media": {"media_ids": ["101", "102"]}}


def test_empty_alt_skips_that_image(api, credentials, tmp_path):
    uploaded(api, "101")
    uploaded(api, "102")
    described(api, "102")
    created(api)
    cat, dog = image(tmp_path, "cat.png"), image(tmp_path, "dog.png")
    assert x_post.main(["-i", cat, "-i", dog, "--alt", "", "--alt", "a dog", "pets"]) == 0
    assert [json.loads(call.request.body) for call in api.calls if call.request.url == METADATA_URL] == [
        alt_body("102", "a dog")
    ]


def test_more_alt_texts_than_images_is_a_usage_error(api, credentials, capsys):
    with pytest.raises(SystemExit) as exit_info:
        x_post.main(["--alt", "a cat", "caption"])
    assert exit_info.value.code == 2
    assert "more --alt (1) than images (0)" in capsys.readouterr().err
    assert not api.calls


def test_alt_text_over_1000_characters_is_refused_before_anything(api, credentials, tmp_path, capsys):
    assert x_post.main(["-i", image(tmp_path), "--alt", "x" * 1001]) == 1
    captured = capsys.readouterr()
    assert "alt text 1 is 1001/1000 characters" in captured.err
    assert captured.out == ""
    assert not api.calls


def test_failed_alt_text_posts_nothing(api, credentials, tmp_path, capsys):
    uploaded(api, "101")
    api.post(METADATA_URL, status=400, json={"title": "Invalid Request", "detail": "Bad alt text."})
    assert x_post.main(["-i", image(tmp_path), "--alt", "a cat", "caption"]) == 1
    assert "400 Invalid Request: Bad alt text." in capsys.readouterr().err
    assert API_URL not in [call.request.url for call in api.calls]


def test_check_marks_images_with_alt_text(api, tmp_path, capsys):
    cat, dog = image(tmp_path, "cat.png"), image(tmp_path, "dog.png")
    assert x_post.main(["--check", "-i", cat, "-i", dog, "--alt", "a cat", "pets"]) == 0
    assert capsys.readouterr().out.endswith("images: cat.png (alt text), dog.png\n")


def test_images_go_with_the_first_post_of_a_thread(api, credentials, monkeypatch, tmp_path):
    uploaded(api, "m1")
    created(api, "1")
    created(api, "2")
    editor(tmp_path, monkeypatch, writes="look\n---\nmore words\n")
    assert x_post.main(["-e", "-i", image(tmp_path)]) == 0
    assert [json.loads(call.request.body) for call in api.calls[1:]] == [
        {"text": "look", "media": {"media_ids": ["m1"]}},
        {"text": "more words", "reply": {"in_reply_to_tweet_id": "1"}},
    ]


def test_check_vets_and_lists_images_without_uploading(api, tmp_path, capsys):
    assert x_post.main(["--check", "-i", image(tmp_path), "caption"]) == 0
    assert capsys.readouterr().out.endswith("images: photo.png\n")
    assert x_post.main(["--check", "-i", image(tmp_path, "bad.png", b"nope"), "caption"]) == 1
    assert not api.calls


@pytest.mark.parametrize(
    "name, data, shown",
    [("anim.gif", GIF, "GIF: anim.gif (alt text)"), ("clip.mp4", MP4, "video: clip.mp4 (alt text)")],
    ids=["gif", "video"],
)
def test_check_names_a_gif_or_video_as_such(api, tmp_path, capsys, name, data, shown):
    assert x_post.main(["--check", "-i", image(tmp_path, name, data), "--alt", "moving", "look"]) == 0
    assert capsys.readouterr().out.endswith(f"{shown}\n")
    assert not api.calls


POLL =["--poll", "tea", "--poll", "coffee"]


@pytest.mark.parametrize(
    "argv, minutes",
    [([], 1440), (["--poll-for", "30m"], 30), (["--poll-for", "6H"], 360), (["--poll-for", "7d"], 10080)],
    ids=["a-day-by-default", "minutes", "hours", "a-week"],
)
def test_a_poll_goes_out_with_the_post(api, credentials, argv, minutes):
    created(api)
    assert x_post.main([*POLL, *argv, "tea or coffee?"]) == 0
    assert json.loads(api.calls[0].request.body) == {
        "text": "tea or coffee?",
        "poll": {"options": ["tea", "coffee"], "duration_minutes": minutes},
    }


def test_a_poll_at_its_limits_goes_out(api, credentials):
    created(api)
    options = [letter * 25 for letter in "abcd"]
    argv = [arg for option in options for arg in ("--poll", option)]
    assert x_post.main([*argv, "--poll-for", "5m", "pick one"]) == 0
    assert json.loads(api.calls[0].request.body)["poll"] == {"options": options, "duration_minutes": 5}


@pytest.mark.parametrize(
    "argv, error",
    [
        (["--poll", "tea"], "a poll takes 2 to 4 options, got 1"),
        ([*POLL, "--poll", "milk", "--poll", "water", "--poll", "juice"], "a poll takes 2 to 4 options, got 5"),
        (["--poll", "", "--poll", "coffee"], "a poll option can't be empty"),
        (["--poll", "t" * 26, "--poll", "coffee"], f"poll option {'t' * 26!r} is over 25 characters"),
        ([*POLL, "--poll-for", "3w"], "can't read --poll-for '3w'; try 30m, 6h or 2d"),
        ([*POLL, "--poll-for", "4m"], "--poll-for runs from 5m to 7d, not '4m'"),
        ([*POLL, "--poll-for", "8d"], "--poll-for runs from 5m to 7d, not '8d'"),
    ],
    ids=["one-option", "five-options", "empty-option", "long-option", "unreadable-time", "too-short", "too-long"],
)
def test_bad_polls_are_refused_before_the_prompt_or_anything_is_sent(api, credentials, capsys, argv, error):
    assert x_post.main(argv) == 1
    captured = capsys.readouterr()
    assert error in captured.err
    assert captured.out == ""  # no "post: " prompt
    assert not api.calls


@pytest.mark.parametrize(
    "argv, error",
    [
        (["--poll-for", "1h", "hi"], "--poll-for needs --poll"),
        ([*POLL, "-i", "photo.png", "hi"], "a poll can't go with images or video"),
        ([*POLL, "-s", "hi"], "a poll can't go with images or video"),
    ],
    ids=["poll-for-alone", "with-an-image", "with-a-screenshot"],
)
def test_poll_usage_errors(api, credentials, monkeypatch, capsys, argv, error):
    monkeypatch.setattr(x_post, "run_openssh", lambda *args, **kwargs: pytest.fail("reached ssh"))
    with pytest.raises(SystemExit) as exit_info:
        x_post.main(argv)
    assert exit_info.value.code == 2
    assert error in capsys.readouterr().err
    assert not api.calls


def test_a_poll_goes_with_the_first_post_of_a_thread(api, credentials, monkeypatch, tmp_path):
    created(api, "1")
    created(api, "2")
    editor(tmp_path, monkeypatch, writes="tea or coffee?\n---\nexplain yourself\n")
    assert x_post.main(["-e", *POLL]) == 0
    assert [json.loads(call.request.body) for call in api.calls] == [
        {"text": "tea or coffee?", "poll": {"options": ["tea", "coffee"], "duration_minutes": 1440}},
        {"text": "explain yourself", "reply": {"in_reply_to_tweet_id": "1"}},
    ]


@pytest.mark.parametrize(
    "argv, shown",
    [([], "1d"), (["--poll-for", "120m"], "2h"), (["--poll-for", "90m"], "90m")],
    ids=["day", "hours", "minutes"],
)
def test_check_shows_the_poll_without_posting(api, capsys, argv, shown):
    assert x_post.main(["--check", *POLL, *argv, "tea or coffee?"]) == 0
    assert capsys.readouterr().out.endswith(f"poll for {shown}: 'tea', 'coffee'\n")


def test_image_lines_closing_a_post_attach_to_that_post_of_a_thread(api, credentials, monkeypatch, tmp_path):
    uploaded(api, "m1")
    uploaded(api, "m2")
    for post_id in "123":
        created(api, post_id)
    shot, scan = image(tmp_path, "qzxv.png"), image(tmp_path, "scan.png")  # in the text, both would be asked about
    editor(tmp_path, monkeypatch, writes=f"first\n---\nsecond\nimage: {shot}\n---\nimage: {scan}\n")
    typed(monkeypatch, "")  # a question now would find no answer
    assert x_post.main(["-e"]) == 0
    assert [call.request.url for call in api.calls] == [MEDIA_URL, MEDIA_URL, API_URL, API_URL, API_URL]
    assert [json.loads(call.request.body) for call in api.calls[2:]] == [
        {"text": "first"},
        {"text": "second", "reply": {"in_reply_to_tweet_id": "1"}, "media": {"media_ids": ["m1"]}},
        {"reply": {"in_reply_to_tweet_id": "2"}, "media": {"media_ids": ["m2"]}},
    ]


def test_alt_lines_follow_the_dash_i_images_of_the_first_post(api, credentials, monkeypatch, tmp_path):
    uploaded(api, "m1")
    described(api, "m1")
    uploaded(api, "m2")
    uploaded(api, "m3")
    described(api, "m3")
    created(api)
    cat, dog, owl = image(tmp_path, "cat.png"), image(tmp_path, "dog.png"), image(tmp_path, "owl.png")
    editor(tmp_path, monkeypatch, writes=f"pets\nimage: {owl}\nalt: an owl\n")
    assert x_post.main(["-e", "-i", cat, "-i", dog, "--alt", "a cat"]) == 0
    assert [json.loads(call.request.body) for call in api.calls if call.request.url == METADATA_URL] == [
        alt_body("m1", "a cat"),
        alt_body("m3", "an owl"),
    ]
    assert json.loads(api.calls[-1].request.body) == {"text": "pets", "media": {"media_ids": ["m1", "m2", "m3"]}}


def test_an_image_line_takes_a_path_from_home(api, credentials, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    uploaded(api, "m1")
    created(api)
    image(tmp_path, "photo.png")
    editor(tmp_path, monkeypatch, writes="look\nimage: ~/photo.png\n")
    assert x_post.main(["-e"]) == 0
    assert json.loads(api.calls[-1].request.body) == {"text": "look", "media": {"media_ids": ["m1"]}}


def test_image_lines_before_the_end_of_a_post_are_its_text(api, credentials, monkeypatch, tmp_path):
    created(api)
    editor(tmp_path, monkeypatch, writes="image: of the day\nsunrise over the hills\n")
    assert x_post.main(["-e"]) == 0
    assert json.loads(api.calls[0].request.body) == {"text": "image: of the day\nsunrise over the hills"}


@pytest.mark.parametrize(
    "writes, argv, error",
    [
        ("first\n---\nsecond\nimage: {tmp}/missing.png\n", [], "post 2 of 2: {tmp}/missing.png: no such file"),
        ("pets\nalt: a cat\n", [], "each alt: line goes just after its own image: line"),
        (
            "pets\nimage: {tmp}/cat.png\nalt: a cat\nalt: a dog\n",
            [],
            "each alt: line goes just after its own image: line",
        ),
        ("pets\nimage:\n", [], "an image: line needs a path"),
        ("pets\nimage: {tmp}/cat.png\nalt: " + "x" * 1001 + "\n", [], "alt text 1 is 1001/1000 characters"),
        (
            "pets\nimage: {tmp}/cat.png\nimage: {tmp}/cat.png\n",
            ["-i", "{tmp}/cat.png"] * 3,
            "at most 4 images per post, got 5",
        ),
        ("tea?\nimage: {tmp}/cat.png\n", POLL, "a poll can't go with images or video; X takes one or the other"),
    ],
    ids=["missing", "alt-alone", "two-alts", "no-path", "long-alt", "five-with-dash-i", "with-a-poll"],
)
def test_bad_image_lines_are_refused_before_anything_is_sent(
    api, credentials, monkeypatch, tmp_path, state_home, capsys, writes, argv, error
):
    image(tmp_path, "cat.png")
    editor(tmp_path, monkeypatch, writes=writes.format(tmp=tmp_path))
    assert x_post.main(["-e", *(arg.format(tmp=tmp_path) for arg in argv)]) == 1
    assert error.format(tmp=tmp_path) in capsys.readouterr().err
    assert not api.calls
    assert draft(state_home).exists()


def test_check_lists_each_posts_images(api, monkeypatch, tmp_path, capsys):
    cat, dog = image(tmp_path, "cat.png"), image(tmp_path, "dog.png")
    editor(tmp_path, monkeypatch, writes=f"first\nimage: {cat}\n---\nsecond\nimage: {dog}\nalt: a dog\n")
    assert x_post.main(["--check", "-e"]) == 0
    assert capsys.readouterr().out == (
        "post 1 of 2: 5/280 characters, 1/10 lines: fits\n"
        "post 2 of 2: 6/280 characters, 1/10 lines: fits\n"
        "cost: $0.035 (2 posts, 1 alt text)\n"
        "post 1 of 2: images: cat.png\n"
        "post 2 of 2: images: dog.png (alt text)\n"
    )


def test_a_thread_that_stops_partway_keeps_the_rest_of_its_image_lines(
    api, credentials, monkeypatch, tmp_path, state_home
):
    uploaded(api, "m1")
    described(api, "m1")
    created(api, "1")
    api.post(API_URL, status=503, body="upstream unavailable")
    shot = image(tmp_path)
    editor(tmp_path, monkeypatch, writes=f"first\n---\nsecond\nimage: {shot}\nalt: a view\n---\nthird\n")
    assert x_post.main(["-e"]) == 1
    assert draft(state_home).read_text() == (
        f"reply-to: https://x.com/i/status/1\nsecond\nimage: {shot}\nalt: a view\n---\nthird\n"
    )


def test_missing_credentials_file_names_the_path(api, config_home, capsys):
    assert x_post.main(["hello"]) == 1
    expected = config_home / "x-post" / "credentials.toml"
    assert capsys.readouterr().err == f"x-post: no credentials file at {expected}\n"
    assert not api.calls


def test_incomplete_credentials_name_the_missing_keys(api, credentials, capsys):
    credentials.write_text('api_key = "api-key"\napi_secret = "api-secret"\n')
    assert x_post.main(["hello"]) == 1
    err = capsys.readouterr().err
    assert err == f"x-post: {credentials} is missing access_token, access_token_secret\n"
    assert not api.calls


def test_unquoted_secret_is_a_readable_toml_error(api, credentials, capsys):
    credentials.write_text("api_key = abc123\n")
    assert x_post.main(["hello"]) == 1
    assert capsys.readouterr().err.startswith(f"x-post: {credentials}: ")
    assert not api.calls


@pytest.mark.parametrize("argv, keystrokes", [(["  "], ""), ([], "  \n")], ids=["argument", "prompt"])
def test_blank_message_is_rejected_before_posting(api, credentials, monkeypatch, capsys, argv, keystrokes):
    typed(monkeypatch, keystrokes)
    with pytest.raises(SystemExit) as exit_info:
        x_post.main(argv)
    assert exit_info.value.code == 2
    assert "message is empty" in capsys.readouterr().err
    assert not api.calls


def test_no_message_prompts_and_posts_the_typed_line_verbatim(api, credentials, monkeypatch, capsys):
    created(api, "1850000000000000002")
    typed(monkeypatch, "it's #great! costs $5 & more\n")
    assert x_post.main([]) == 0
    assert json.loads(api.calls[0].request.body) == {"text": "it's #great! costs $5 & more"}
    assert capsys.readouterr().out == "post: https://x.com/i/status/1850000000000000002\n"


def interrupt(prompt):
    raise KeyboardInterrupt


@pytest.mark.parametrize("key", ["ctrl-d", "ctrl-c"])
def test_cancelling_the_prompt_posts_nothing(api, credentials, monkeypatch, key):
    if key == "ctrl-d":
        typed(monkeypatch, "")
    else:
        monkeypatch.setattr(builtins, "input", interrupt)
    assert x_post.main([]) == 1
    assert not api.calls


def test_credentials_are_checked_before_prompting(api, monkeypatch, capsys):
    typed(monkeypatch, "a post that would be lost\n")
    assert x_post.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no credentials file" in captured.err


@pytest.mark.parametrize(
    "text, links",
    [
        ("read https://example.com/a?b=c now", ["https://example.com/a?b=c"]),
        ("www.example.org rocks", ["www.example.org"]),
        ("new devlog at cmon1975.com", ["cmon1975.com"]),
        ("two: a.io and sub.b.dev", ["a.io", "sub.b.dev"]),
        ("pi is 3.14", []),
        ("e.g. this", []),
        ("the U.S. economy", []),
        ("wait...what", []),
        ("costs $5.99", []),
    ],
)
def test_find_links(text, links):
    assert x_post.find_links(text) == links


@pytest.mark.parametrize(
    "text, length",
    [
        ("hello", 5),
        ("日本語", 6),
        ("😷", 2),
        ("👨‍👩‍👧", 2),
        ("👍🏽", 2),
        ("🇺🇸", 2),
        ("see https://example.com/" + "a" * 100, 4 + 23),
        ("café", 4),
    ],
    ids=["latin", "cjk", "emoji", "zwj-family", "skin-tone", "flag", "url", "combining-accent"],
)
def test_weighted_length_counts_like_x(text, length):
    assert x_post.weighted_length(text) == length


@pytest.mark.parametrize("text", ["a" * 280, "\n".join(["line"] * 10)], ids=["280-chars", "10-lines"])
def test_post_at_the_fold_limits_goes_through(api, credentials, text):
    created(api)
    assert x_post.main([text]) == 0


@pytest.mark.parametrize(
    "text, error",
    [
        ("a" * 281, "281/280 characters"),
        ("日" * 141, "282/280 characters"),
        ("\n".join(["line"] * 11), "11/10 lines"),
    ],
    ids=["281-chars", "141-cjk", "11-lines"],
)
def test_post_past_the_fold_is_refused_before_sending(api, credentials, capsys, text, error):
    assert x_post.main([text]) == 1
    assert error in capsys.readouterr().err
    assert not api.calls


@pytest.mark.parametrize("argv, keystrokes", [(["a" * 281], ""), ([], "a" * 281 + "\n")], ids=["argument", "prompt"])
def test_too_long_post_becomes_the_draft_to_trim(api, credentials, monkeypatch, tmp_path, capsys, argv, keystrokes):
    typed(monkeypatch, keystrokes)
    assert x_post.main(argv) == 1
    assert "x-post -e" in capsys.readouterr().err
    opened = editor(tmp_path, monkeypatch, exit_code=1)
    x_post.main(["-e"])
    assert opened.read_text() == "a" * 281 + "\n"


def test_too_long_post_never_overwrites_an_unsent_draft(api, credentials, state_home, capsys):
    draft(state_home).parent.mkdir(parents=True)
    draft(state_home).write_text("earlier draft\n")
    assert x_post.main(["a" * 281]) == 1
    assert "unsent draft already exists" in capsys.readouterr().err
    assert draft(state_home).read_text() == "earlier draft\n"


def test_check_reports_the_count_without_credentials_or_posting(api, capsys):
    assert x_post.main(["--check", "hello", "world"]) == 0
    assert capsys.readouterr().out == "11/280 characters, 1/10 lines: fits\ncost: $0.015 (1 post)\n"
    assert not api.calls


def test_check_flags_a_post_past_the_fold_without_saving_it(api, state_home, capsys):
    assert x_post.main(["--check", "a" * 281]) == 1
    assert capsys.readouterr().out == (
        '281/280 characters, 1/10 lines: X would fold it behind "Show more"\ncost: $0.015 (1 post)\n'
    )
    assert not draft(state_home).exists()


def test_check_names_links_and_their_price_without_asking(api, capsys):
    assert x_post.main(["--check", "see", "cmon1975.com"]) == 0
    assert capsys.readouterr().out == (
        "27/280 characters, 1/10 lines: fits\n"
        "links: cmon1975.com, so it would cost $0.20, not $0.015\n"
        "cost: $0.200 (1 link post)\n"
    )


def test_check_with_edit_keeps_the_draft(api, monkeypatch, tmp_path, state_home, capsys):
    editor(tmp_path, monkeypatch, writes="line one\nline two\n")
    assert x_post.main(["--check", "-e"]) == 0
    assert capsys.readouterr().out == "17/280 characters, 2/10 lines: fits\ncost: $0.015 (1 post)\n"
    assert draft(state_home).read_text() == "line one\nline two\n"


def test_check_cannot_be_combined_with_undo(api, capsys):
    with pytest.raises(SystemExit) as exit_info:
        x_post.main(["--check", "--undo"])
    assert exit_info.value.code == 2
    assert "--check can't be combined with --undo" in capsys.readouterr().err


def test_length_is_checked_before_asking_about_link_price(api, credentials, capsys):
    assert x_post.main(["a" * 270 + " example.com"]) == 1
    captured = capsys.readouterr()
    assert "294/280 characters" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("answer", ["y\n", "YES\n"])
def test_link_post_asks_about_the_price_and_posts_on_yes(api, credentials, monkeypatch, capsys, answer):
    created(api)
    typed(monkeypatch, answer)
    assert x_post.main(["new", "post", "at", "cmon1975.com"]) == 0
    assert json.loads(api.calls[0].request.body) == {"text": "new post at cmon1975.com"}
    out = capsys.readouterr().out
    assert "cmon1975.com" in out and "$0.20" in out
    assert out.endswith("https://x.com/i/status/1850000000000000001\n")


@pytest.mark.parametrize("answer", ["n\n", "\n", ""], ids=["no", "enter", "no-terminal"])
def test_link_post_is_not_sent_without_a_yes(api, credentials, monkeypatch, answer):
    typed(monkeypatch, answer)
    assert x_post.main(["see", "example.com"]) == 1
    assert not api.calls


def test_e_at_the_link_question_takes_the_link_out_in_the_editor(
    api, credentials, monkeypatch, tmp_path, state_home, capsys
):
    openings = editors(tmp_path, monkeypatch, "see it on my site\n")
    created(api)
    typed(monkeypatch, "e\n")
    assert x_post.main(["see", "example.com"]) == 0
    assert "example.com: link posts cost $0.20, not $0.015. post anyway? e edits [y/N/e]" in capsys.readouterr().out
    assert openings() == ["see example.com\n"]
    assert json.loads(api.calls[0].request.body) == {"text": "see it on my site"}
    assert not draft(state_home).exists()


@pytest.mark.parametrize(
    "text, command",
    [
        ("cancel 65a0", "x-post --cancel 65a0"),
        ("Undo", "x-post --undo"),
        ("history 5", "x-post --history 5"),
        ("move 65a0 tomorrow 09:00", "x-post --move 65a0 'tomorrow 09:00'"),
        ("delete 2113743705123459310", "x-post --delete 2113743705123459310"),
        (
            "delete https://x.com/i/status/2113743705123459310",
            "x-post --delete https://x.com/i/status/2113743705123459310",
        ),
    ],
)
def test_a_post_that_looks_like_a_command_without_its_dashes_is_asked_about(
    api, credentials, monkeypatch, state_home, capsys, text, command
):
    typed(monkeypatch, "n\n")
    assert x_post.main(text.split()) == 1
    captured = capsys.readouterr()
    assert f"post {text!r}? it looks like {command} without its dashes; r runs that [y/N/r]" in captured.out
    assert captured.err == "x-post: not posted\n"
    assert not api.calls
    assert not draft(state_home).exists()


def test_a_command_slip_is_asked_about_before_credentials_or_images_are_read(api, monkeypatch, tmp_path, capsys):
    ssh_calls = fake_openssh(tmp_path, monkeypatch, "ssh", stdout="Screenshot 2026-10-23 090000.png\r\n")
    typed(monkeypatch, "n\n")
    assert x_post.main(["-s", "undo"]) == 1  # with no credentials file, too
    captured = capsys.readouterr()
    assert "it looks like x-post --undo without its dashes" in captured.out
    assert captured.err == "x-post: not posted\n"
    assert ssh_calls() == []


def test_check_measures_a_command_slip_without_asking(api, monkeypatch, capsys):
    typed(monkeypatch, "")  # a question now would find no answer
    assert x_post.main(["--check", "cancel", "65a0"]) == 0
    assert capsys.readouterr().out == "11/280 characters, 1/10 lines: fits\ncost: $0.015 (1 post)\n"


def test_a_command_missing_what_it_takes_isnt_offered_to_run(api, credentials, monkeypatch, capsys):
    typed(monkeypatch, "n\n")
    assert x_post.main(["cancel"]) == 1
    assert "post 'cancel'? it looks like x-post --cancel without its dashes [y/N]" in capsys.readouterr().out
    assert not api.calls


def test_a_post_that_looks_like_a_command_posts_on_yes(api, credentials, monkeypatch, capsys):
    created(api)
    typed(monkeypatch, "y\n")
    assert x_post.main(["cancel", "65a0"]) == 0
    assert "it looks like x-post --cancel 65a0 without its dashes" in capsys.readouterr().out
    assert json.loads(api.calls[0].request.body) == {"text": "cancel 65a0"}


def test_r_runs_the_command_the_post_looks_like_instead(api, credentials, clock, ids, monkeypatch, state_home, capsys):
    assert x_post.main(["--at", "+1h", "hello"]) == 0
    assert x_post.main(["--at", "+2h", "hello again"]) == 0
    typed(monkeypatch, "r\n")
    assert x_post.main(["move", "0001", "tomorrow", "09:00"]) == 0
    typed(monkeypatch, "r\n")
    assert x_post.main(["cancel", "0002"]) == 0
    assert "cancelled 0002: 'hello again'" in capsys.readouterr().out
    assert [entry["due"] for entry in queued(state_home)] == ["2026-10-24T09:00"]
    assert not api.calls


@pytest.mark.parametrize(
    "text", ["check this out", "cancel the meeting", "move fast and fix things", "delete it all", "history is long"]
)
def test_a_post_that_only_starts_with_a_command_name_posts_without_asking(api, credentials, monkeypatch, text):
    created(api)
    typed(monkeypatch, "")  # a question now would find no answer
    assert x_post.main(text.split()) == 0


def test_a_command_typed_at_the_prompt_posts_without_asking(api, credentials, monkeypatch):
    created(api)
    typed(monkeypatch, "cancel 65a0\n")
    assert x_post.main([]) == 0
    assert json.loads(api.calls[0].request.body) == {"text": "cancel 65a0"}


def test_a_command_is_asked_about_once_though_it_goes_on_to_the_editor(
    api, credentials, monkeypatch, tmp_path, capsys
):
    url = "https://x.com/i/status/2113743705123459310"
    editors(tmp_path, monkeypatch, f"delete {url}\n")
    created(api)
    typed(monkeypatch, "y\ne\ny\n")  # yes, it's a post; edit it at the link question; then yes, pay for it
    assert x_post.main(["delete", url]) == 0
    assert capsys.readouterr().out.count("without its dashes") == 1
    assert json.loads(api.calls[0].request.body) == {"text": f"delete {url}"}


def test_a_possible_typo_is_asked_about_and_posts_on_yes(api, credentials, monkeypatch, capsys):
    created(api)
    typed(monkeypatch, "y\n")
    assert x_post.main(["havign", "a", "great", "day"]) == 0
    assert "possible typos: havign (having?). post anyway?" in capsys.readouterr().out
    assert json.loads(api.calls[0].request.body) == {"text": "havign a great day"}


@pytest.mark.parametrize(
    "argv, keystrokes", [(["havign a great day"], "n\n"), ([], "havign a great day\n\n")], ids=["argument", "prompt"]
)
def test_a_typo_held_back_becomes_the_draft_to_fix(api, credentials, monkeypatch, state_home, capsys, argv, keystrokes):
    typed(monkeypatch, keystrokes)
    assert x_post.main(argv) == 1
    assert not api.calls
    assert draft(state_home).read_text() == "havign a great day\n"
    assert "x-post: saved as the draft; x-post -e opens it to fix" in capsys.readouterr().err


def test_answering_a_learns_the_words_so_they_pass_from_then_on(api, credentials, config_home, monkeypatch):
    created(api, "1")
    created(api, "2")
    typed(monkeypatch, "a\n")
    assert x_post.main(["nvim", "and", "tailnet"]) == 0
    assert (config_home / "x-post" / "words.txt").read_text() == "nvim\ntailnet\n"
    typed(monkeypatch, "")  # a question now would find no answer
    assert x_post.main(["Nvim", "over", "the", "tailnet"]) == 0


@pytest.mark.parametrize(
    "text",
    ["teh https://example.com/zzqx", "teh #zzqx @qzxv", "teh pc-2 cat9000", "teh EEVEE OptiX", "teh isn’t it"],
    ids=["link", "tag-and-mention", "digits", "capitals", "curly-apostrophe"],
)
def test_only_words_are_spell_checked(api, capsys, text):
    assert x_post.main(["--check", text]) == 0
    assert capsys.readouterr().out.endswith("possible typos: teh (the?)\n")


def test_check_lists_possible_typos_once_each_without_asking(api, capsys):
    assert x_post.main(["--check", "teh quick fox jumpd, teh zzqxwv end"]) == 0
    assert capsys.readouterr().out.endswith("possible typos: teh (the?), jumpd (jump?), zzqxwv\n")


def test_a_thread_is_asked_about_its_typos_once(api, credentials, monkeypatch, tmp_path, capsys):
    created(api, "1")
    created(api, "2")
    editor(tmp_path, monkeypatch, writes="teh first\n---\nthe jumpd second\n")
    typed(monkeypatch, "y\n")
    assert x_post.main(["-e"]) == 0
    assert "possible typos: teh (the?), jumpd (jump?). post anyway?" in capsys.readouterr().out
    assert len(api.calls) == 2


def test_e_at_the_typo_question_fixes_the_post_in_the_editor_then_posts_it(
    api, credentials, monkeypatch, tmp_path, state_home
):
    openings = editors(tmp_path, monkeypatch, "having a great day\n")
    created(api)
    typed(monkeypatch, "e\n")
    assert x_post.main(["havign a great day"]) == 0
    assert openings() == ["havign a great day\n"]
    assert json.loads(api.calls[0].request.body) == {"text": "having a great day"}
    assert not draft(state_home).exists()


def test_e_with_edit_reopens_the_draft(api, credentials, monkeypatch, tmp_path, state_home):
    openings = editors(tmp_path, monkeypatch, "havign fun\n", "having fun\n")
    created(api)
    typed(monkeypatch, "e\n")
    assert x_post.main(["-e"]) == 0
    assert openings() == ["", "havign fun\n"]
    assert json.loads(api.calls[0].request.body) == {"text": "having fun"}
    assert not draft(state_home).exists()


def test_after_e_a_post_still_not_sent_stays_the_draft(api, credentials, monkeypatch, tmp_path, state_home, capsys):
    editors(tmp_path, monkeypatch, "having fun at example.com\n")
    typed(monkeypatch, "e\nn\n")  # fix the typo, then decline the link price
    assert x_post.main(["havign fun"]) == 1
    assert capsys.readouterr().err.endswith("x-post: not posted\nx-post: draft kept; x-post -e reopens it\n")
    assert draft(state_home).read_text() == "having fun at example.com\n"


def test_e_never_overwrites_an_unsent_draft(api, credentials, monkeypatch, tmp_path, state_home, capsys):
    editors(tmp_path, monkeypatch)  # fails if it's opened
    draft(state_home).parent.mkdir(parents=True)
    draft(state_home).write_text("earlier draft\n")
    typed(monkeypatch, "e\n")
    assert x_post.main(["havign fun"]) == 1
    assert "an unsent draft already exists, so this one can't go to the editor" in capsys.readouterr().err
    assert draft(state_home).read_text() == "earlier draft\n"


def test_a_post_too_long_at_a_terminal_can_be_trimmed_in_the_editor_then_posts(
    api, credentials, monkeypatch, tmp_path, state_home, capsys
):
    trimmed = "words " * 46 + "word"
    openings = editors(tmp_path, monkeypatch, trimmed + "\n")
    created(api)
    typed_at_a_terminal(monkeypatch, "y\n")
    assert x_post.main(["word " * 60]) == 0
    question = '300/280 characters; X folds longer posts behind "Show more". edit it now? s splits it into a thread'
    assert f"{question} [y/N/s]" in capsys.readouterr().out
    assert openings() == ["word " * 60 + "\n"]
    assert json.loads(api.calls[0].request.body) == {"text": trimmed}
    assert not draft(state_home).exists()


@pytest.mark.parametrize("answer", ["n\n", ""], ids=["no", "ctrl-d"])
def test_a_post_too_long_at_a_terminal_left_as_is_becomes_the_draft(
    api, credentials, monkeypatch, state_home, capsys, answer
):
    typed_at_a_terminal(monkeypatch, answer)
    assert x_post.main(["word " * 60]) == 1
    err = capsys.readouterr().err
    assert err.endswith("x-post: not posted\nx-post: saved as the draft; x-post -e opens it to trim\n")
    assert draft(state_home).read_text() == "word " * 60 + "\n"
    assert not api.calls


FOX = ("the quick brown fox jumps over the lazy dog " * 4).strip() + "."  # 176 characters
JUGS = ("pack my box with five dozen liquor jugs " * 4).strip() + "."  # 156, too many with FOX


@pytest.mark.parametrize(
    "text, posts",
    [
        ("A" * 200 + ". " + "b " * 50 + "end.", ["A" * 200 + ".", "b " * 50 + "end."]),
        ("word " * 70, [" ".join(["word"] * 56), " ".join(["word"] * 14)]),
        ("\n".join(["word " * 19 + "end"] * 4), ["\n".join(["word " * 19 + "end"] * 2)] * 2),
        (
            "\n".join(f"line {n}" for n in range(1, 13)),
            ["\n".join(f"line {n}" for n in range(1, 11)), "line 11\nline 12"],
        ),
    ],
    ids=["at-sentence-ends", "between-words", "at-line-breaks", "ten-lines-at-most"],
)
def test_split_post(text, posts):
    assert x_post.split_post(text) == posts


def test_split_post_refuses_a_word_too_long_on_its_own():
    with pytest.raises(x_post.XPostError, match="can't split it: one word is over 280 characters on its own"):
        x_post.split_post("x" * 300)


def test_s_at_a_terminal_splits_a_post_too_long_into_a_thread_to_check_in_the_editor(
    api, credentials, monkeypatch, tmp_path, state_home
):
    opened = editor(tmp_path, monkeypatch)  # save it as split
    created(api, "1")
    created(api, "2")
    typed_at_a_terminal(monkeypatch, "s\n")
    assert x_post.main([FOX, JUGS]) == 0
    assert opened.read_text() == f"{FOX}\n---\n{JUGS}\n"
    assert [json.loads(call.request.body) for call in api.calls] == [
        {"text": FOX},
        {"text": JUGS, "reply": {"in_reply_to_tweet_id": "1"}},
    ]
    assert not draft(state_home).exists()


def test_s_splits_only_the_posts_too_long_keeping_image_lines_with_the_first_piece(
    api, credentials, monkeypatch, tmp_path
):
    cat, dog = image(tmp_path, "cat.png"), image(tmp_path, "dog.png")
    thread = f"reply-to: https://x.com/i/status/5\nimage: {dog}\n---\n"  # a post of just an image fits
    split = f"{thread}{FOX}\nimage: {cat}\n---\n{JUGS}\n"
    openings = editors(tmp_path, monkeypatch, f"{thread}{FOX} {JUGS}\nimage: {cat}\n", split)
    uploaded(api, "m1")
    uploaded(api, "m2")
    for post_id in "123":
        created(api, post_id)
    typed_at_a_terminal(monkeypatch, "s\n")
    assert x_post.main(["-e"]) == 0
    assert openings()[1] == split
    assert [json.loads(call.request.body) for call in api.calls[2:]] == [
        {"reply": {"in_reply_to_tweet_id": "5"}, "media": {"media_ids": ["m1"]}},
        {"text": FOX, "reply": {"in_reply_to_tweet_id": "1"}, "media": {"media_ids": ["m2"]}},
        {"text": JUGS, "reply": {"in_reply_to_tweet_id": "2"}},
    ]


def test_s_at_revise_splits_the_edit(api, credentials, clock, ids, monkeypatch, tmp_path, state_home):
    assert x_post.main(["--at", "+1h", "hello"]) == 0
    openings = editors(tmp_path, monkeypatch, f"{FOX} {JUGS}\n", f"{FOX}\n---\n{JUGS}\n")
    typed_at_a_terminal(monkeypatch, "s\n")
    assert x_post.main(["--revise", "0001"]) == 0
    assert openings() == ["hello\n", f"{FOX}\n---\n{JUGS}\n"]
    assert queued(state_home)[0]["parts"] == [FOX, JUGS]


def test_numbered_marks_each_post_of_a_thread_with_its_place(api, credentials, monkeypatch, tmp_path, state_home):
    for post_id in "123":
        created(api, post_id)
    editor(tmp_path, monkeypatch, writes="first\n---\nsecond\n---\nthird\n")
    assert x_post.main(["-e", "--numbered"]) == 0
    assert [json.loads(call.request.body)["text"] for call in api.calls] == ["first 1/3", "second 2/3", "third 3/3"]


def test_numbered_leaves_a_single_post_alone(api, credentials):
    created(api)
    assert x_post.main(["--numbered", "hello"]) == 0
    assert json.loads(api.calls[0].request.body) == {"text": "hello"}


def test_numbered_marks_go_before_image_lines_and_alone_on_a_post_of_just_images(
    api, credentials, monkeypatch, tmp_path
):
    uploaded(api, "m1")
    uploaded(api, "m2")
    created(api, "1")
    created(api, "2")
    editor(tmp_path, monkeypatch, writes=f"look\nimage: {image(tmp_path)}\n---\nimage: {image(tmp_path, 'b.png')}\n")
    assert x_post.main(["-e", "--numbered"]) == 0
    assert [json.loads(call.request.body) for call in api.calls[2:]] == [
        {"text": "look 1/2", "media": {"media_ids": ["m1"]}},
        {"text": "2/2", "reply": {"in_reply_to_tweet_id": "1"}, "media": {"media_ids": ["m2"]}},
    ]


def test_numbered_marks_count_toward_the_length_but_stay_out_of_the_draft(
    api, credentials, monkeypatch, tmp_path, state_home, capsys
):
    editor(tmp_path, monkeypatch, writes="a" * 278 + "\n---\nshort\n")
    assert x_post.main(["-e", "--numbered"]) == 1
    assert "post 1 of 2: 282/280 characters" in capsys.readouterr().err
    assert draft(state_home).read_text() == "a" * 278 + "\n---\nshort\n"
    assert not api.calls


def test_s_leaves_room_for_the_numbered_marks(api, credentials, monkeypatch, tmp_path):
    opened = editor(tmp_path, monkeypatch)  # save it as split
    created(api, "1")
    created(api, "2")
    typed_at_a_terminal(monkeypatch, "s\n")
    assert x_post.main(["--numbered", *["word"] * 70]) == 0
    assert opened.read_text() == " ".join(["word"] * 55) + "\n---\n" + " ".join(["word"] * 15) + "\n"
    assert [json.loads(call.request.body)["text"] for call in api.calls] == [
        " ".join(["word"] * 55) + " 1/2",
        " ".join(["word"] * 15) + " 2/2",
    ]


def test_split_leaves_room_for_two_digit_marks_once_a_thread_reaches_ten_posts():
    posts = x_post.split_long_posts(["abcdefghijk " * 250], numbered=True)
    assert len(posts) == 12
    assert all(x_post.fits(post) for post in x_post.number_posts(posts))


def test_a_numbered_thread_that_stops_partway_keeps_its_marks_in_the_draft(
    api, credentials, monkeypatch, tmp_path, state_home
):
    created(api, "1")
    api.post(API_URL, status=503, body="upstream unavailable")
    editor(tmp_path, monkeypatch, writes="first\n---\nsecond\n---\nthird\n")
    assert x_post.main(["-e", "--numbered"]) == 1
    assert draft(state_home).read_text() == "reply-to: https://x.com/i/status/1\nsecond 2/3\n---\nthird 3/3\n"


def test_numbered_carries_on_a_thread_already_marked_without_marking_it_again(api, credentials, monkeypatch, tmp_path):
    uploaded(api, "m1")
    created(api, "2")
    created(api, "3")
    rest = f"reply-to: https://x.com/i/status/1\nsecond 2/3\n---\n3/3\nimage: {image(tmp_path)}\n"
    editor(tmp_path, monkeypatch, writes=rest)
    assert x_post.main(["-e", "--numbered"]) == 0
    assert [json.loads(call.request.body)["text"] for call in api.calls[1:]] == ["second 2/3", "3/3"]


@pytest.mark.parametrize(
    "first, last",
    [("odds 50/50", "went 4/4"), ("odds 50/50", "plain"), ("step1/2", "step2/2")],
    ids=["every-post", "first-post", "inside-words"],
)
def test_numbered_still_marks_a_thread_whose_posts_just_end_in_fractions(
    api, credentials, monkeypatch, tmp_path, first, last
):
    created(api, "1")
    created(api, "2")
    editor(tmp_path, monkeypatch, writes=f"{first}\n---\n{last}\n")
    assert x_post.main(["-e", "--numbered"]) == 0
    assert [json.loads(call.request.body)["text"] for call in api.calls] == [f"{first} 1/2", f"{last} 2/2"]


def test_a_numbered_thread_is_queued_with_its_marks(api, credentials, clock, monkeypatch, tmp_path, state_home):
    editor(tmp_path, monkeypatch, writes="first\n---\nsecond\n")
    assert x_post.main(["--at", "+1h", "-e", "--numbered"]) == 0
    assert queued(state_home)[0]["parts"] == ["first 1/2", "second 2/2"]


def test_revise_opens_a_numbered_thread_without_its_marks_and_numbers_it_again(
    api, credentials, clock, ids, monkeypatch, tmp_path, state_home
):
    editor(tmp_path, monkeypatch, writes="first\n---\nsecond\n")
    assert x_post.main(["--at", "+1h", "-e", "--numbered"]) == 0
    openings = editors(tmp_path, monkeypatch, "first\n---\nsecond\n---\nthird\n")
    assert x_post.main(["--revise", "0001"]) == 0
    assert openings() == ["first\n---\nsecond\n"]
    assert queued(state_home)[0]["parts"] == ["first 1/3", "second 2/3", "third 3/3"]


def test_s_at_revise_leaves_room_for_a_numbered_threads_marks(
    api, credentials, clock, ids, monkeypatch, tmp_path, state_home
):
    editor(tmp_path, monkeypatch, writes="first\n---\nsecond\n")
    assert x_post.main(["--at", "+1h", "-e", "--numbered"]) == 0
    head, tail = " ".join(["word"] * 55), " ".join(["word"] * 15)
    openings = editors(tmp_path, monkeypatch, f"{head} {tail}\n---\nsecond\n", f"{head}\n---\n{tail}\n---\nsecond\n")
    typed_at_a_terminal(monkeypatch, "s\n")
    assert x_post.main(["--revise", "0001"]) == 0
    assert openings()[1] == f"{head}\n---\n{tail}\n---\nsecond\n"
    assert queued(state_home)[0]["parts"] == [f"{head} 1/3", f"{tail} 2/3", "second 3/3"]


def test_revise_counts_a_numbered_threads_marks_toward_its_length(
    api, credentials, clock, ids, monkeypatch, tmp_path, state_home, capsys
):
    editor(tmp_path, monkeypatch, writes="first\n---\nsecond\n")
    assert x_post.main(["--at", "+1h", "-e", "--numbered"]) == 0
    editor(tmp_path, monkeypatch, writes="x" * 278 + "\n---\nsecond\n")
    assert x_post.main(["--revise", "0001"]) == 1
    assert "post 1 of 2: 282/280 characters" in capsys.readouterr().err
    assert queued(state_home)[0]["parts"] == ["first 1/2", "second 2/2"]


def test_revise_leaves_the_marks_of_a_held_numbered_threads_rest_in_its_text(
    api, credentials, clock, ids, monkeypatch, tmp_path, state_home
):
    editor(tmp_path, monkeypatch, writes="one\n---\ntwo\n---\nthree\n")
    assert x_post.main(["--at", "+1h", "-e", "--numbered"]) == 0
    clock["now"] = datetime(2026, 10, 23, 10, 0)
    created(api, "1")
    api.post(API_URL, status=503, body="upstream unavailable")
    assert x_post.main(["--send-due"]) == 1
    openings = editors(tmp_path, monkeypatch, "reply-to: https://x.com/i/status/1\ntwo, fixed 2/3\n---\nthree 3/3\n")
    assert x_post.main(["--revise", "0001"]) == 0
    assert openings() == ["reply-to: https://x.com/i/status/1\ntwo 2/3\n---\nthree 3/3\n"]
    assert queued(state_home)[0]["parts"] == ["two, fixed 2/3", "three 3/3"]


def test_e_at_revise_reopens_the_edit(api, credentials, clock, ids, monkeypatch, tmp_path, state_home):
    assert x_post.main(["--at", "+1h", "hello"]) == 0
    openings = editors(tmp_path, monkeypatch, "havign fun\n", "having fun\n")
    typed(monkeypatch, "e\n")
    assert x_post.main(["--revise", "0001"]) == 0
    assert openings() == ["hello\n", "havign fun\n"]
    assert queued(state_home)[0]["parts"] == ["having fun"]


def test_an_at_post_is_spell_checked_when_scheduled(api, credentials, clock, state_home, monkeypatch):
    typed(monkeypatch, "n\n")
    assert x_post.main(["--at", "+1h", "havign", "fun"]) == 1
    assert queued(state_home) == []


def test_edit_posts_the_draft_with_its_line_breaks(api, credentials, monkeypatch, tmp_path, state_home, capsys):
    created(api)
    editor(tmp_path, monkeypatch, writes="line one\n\nline two\n\n")
    assert x_post.main(["-e"]) == 0
    assert json.loads(api.calls[0].request.body) == {"text": "line one\n\nline two"}
    assert capsys.readouterr().out == "https://x.com/i/status/1850000000000000001\n"
    assert not draft(state_home).exists()


def test_unsent_draft_reopens_next_time(api, credentials, monkeypatch, tmp_path, capsys):
    api.post(API_URL, status=403, json={"title": "Forbidden", "detail": "Your post is too long."})
    editor(tmp_path, monkeypatch, writes="a long draft\n")
    assert x_post.main(["-e"]) == 1
    assert "draft kept" in capsys.readouterr().err
    opened = editor(tmp_path, monkeypatch, exit_code=1)
    x_post.main(["-e"])
    assert opened.read_text() == "a long draft\n"


@pytest.mark.parametrize(
    "text, parts",
    [
        ("a\n---\nb", ["a", "b"]),
        ("a\n---\n\n---\nb\n---\n", ["a", "b"]),
        ("a\n  ---  \nb", ["a", "b"]),
        ("a --- b", ["a --- b"]),
        ("a\n----\nb", ["a\n----\nb"]),
    ],
    ids=["two", "stray-separators", "padded", "inline", "four-dashes"],
)
def test_split_thread(text, parts):
    assert x_post.split_thread(text) == parts


def test_edit_posts_a_thread_as_a_reply_chain(api, credentials, monkeypatch, tmp_path, state_home, capsys):
    for post_id in "123":
        created(api, post_id)
    editor(tmp_path, monkeypatch, writes="first\n---\nsecond\n---\nthird\n")
    assert x_post.main(["-e"]) == 0
    assert [json.loads(call.request.body) for call in api.calls] == [
        {"text": "first"},
        {"text": "second", "reply": {"in_reply_to_tweet_id": "1"}},
        {"text": "third", "reply": {"in_reply_to_tweet_id": "2"}},
    ]
    assert capsys.readouterr().out == "".join(f"https://x.com/i/status/{post_id}\n" for post_id in "123")
    assert not draft(state_home).exists()


def test_thread_is_checked_whole_before_anything_is_sent(api, credentials, monkeypatch, tmp_path, state_home, capsys):
    editor(tmp_path, monkeypatch, writes="fine\n---\n" + "a" * 281 + "\n")
    assert x_post.main(["-e"]) == 1
    assert "post 2 of 2: 281/280 characters" in capsys.readouterr().err
    assert not api.calls
    assert draft(state_home).exists()


def test_thread_asks_about_link_price_once(api, credentials, monkeypatch, tmp_path, capsys):
    editor(tmp_path, monkeypatch, writes="see a.io\n---\nsee b.dev\n")
    typed(monkeypatch, "n\n")
    assert x_post.main(["-e"]) == 1
    out = capsys.readouterr().out
    assert out.count("link posts cost") == 1
    assert "a.io, b.dev" in out
    assert not api.calls


def test_thread_that_fails_partway_continues_from_the_draft(api, credentials, monkeypatch, tmp_path, state_home, capsys):
    created(api, "1")
    api.post(API_URL, status=503, body="upstream unavailable")
    editor(tmp_path, monkeypatch, writes="first\n---\nsecond\n---\nthird\n")
    assert x_post.main(["-e"]) == 1
    assert "1 of 3 posts went out" in capsys.readouterr().err
    assert draft(state_home).read_text() == "reply-to: https://x.com/i/status/1\nsecond\n---\nthird\n"
    api.remove(responses.POST, API_URL)
    created(api, "2")
    created(api, "3")
    editor(tmp_path, monkeypatch)  # save the kept draft as it is
    assert x_post.main(["-e"]) == 0
    assert [json.loads(call.request.body) for call in api.calls[2:]] == [
        {"text": "second", "reply": {"in_reply_to_tweet_id": "1"}},
        {"text": "third", "reply": {"in_reply_to_tweet_id": "2"}},
    ]


def test_check_reports_each_post_of_a_thread(api, monkeypatch, tmp_path, capsys):
    editor(tmp_path, monkeypatch, writes="hello\n---\n" + "a" * 281 + "\n")
    assert x_post.main(["--check", "-e"]) == 1
    assert capsys.readouterr().out == (
        "post 1 of 2: 5/280 characters, 1/10 lines: fits\n"
        'post 2 of 2: 281/280 characters, 1/10 lines: X would fold it behind "Show more"\n'
        "cost: $0.030 (2 posts)\n"
    )


def test_dashes_outside_the_editor_are_just_text(api, credentials):
    created(api)
    assert x_post.main(["a\n---\nb"]) == 0
    assert json.loads(api.calls[0].request.body) == {"text": "a\n---\nb"}


def test_editor_failure_posts_nothing(api, credentials, monkeypatch, tmp_path):
    editor(tmp_path, monkeypatch, writes="half done\n", exit_code=1)
    assert x_post.main(["-e"]) == 1
    assert not api.calls


def test_quitting_with_an_empty_draft_posts_nothing(api, credentials, monkeypatch, tmp_path, capsys):
    editor(tmp_path, monkeypatch, writes="\n")
    with pytest.raises(SystemExit) as exit_info:
        x_post.main(["-e"])
    assert exit_info.value.code == 2
    assert "message is empty" in capsys.readouterr().err
    assert not api.calls


@pytest.mark.parametrize("var, others", [("VISUAL", {"EDITOR": "false"}), ("EDITOR", {})])
def test_editor_comes_from_visual_then_editor(api, credentials, monkeypatch, tmp_path, var, others):
    created(api)
    editor(tmp_path, monkeypatch, writes="hello\n", var=var)
    for name, value in others.items():
        monkeypatch.setenv(name, value)
    assert x_post.main(["-e"]) == 0


def test_without_visual_or_editor_falls_back_to_an_installed_vim(api, credentials, monkeypatch, tmp_path):
    created(api)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    write_editor(bin_dir / "vim", "hello\n", 0).parent.joinpath("vim").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert x_post.main(["-e"]) == 0


@pytest.mark.parametrize("visual", ["no-such-editor-anywhere", None], ids=["missing", "none-installed"])
def test_no_runnable_editor_is_a_readable_error(api, credentials, monkeypatch, tmp_path, capsys, visual):
    monkeypatch.setenv("PATH", str(tmp_path))
    if visual:
        monkeypatch.setenv("VISUAL", visual)
    assert x_post.main(["-e"]) == 1
    assert "editor" in capsys.readouterr().err
    assert not api.calls


def test_edit_takes_no_message(api, credentials, capsys):
    with pytest.raises(SystemExit) as exit_info:
        x_post.main(["-e", "hello"])
    assert exit_info.value.code == 2
    assert "-e takes no message" in capsys.readouterr().err
    assert not api.calls


def posted_earlier(api, text="having a great day", post_id="111"):
    created(api, post_id)
    assert x_post.main([text]) == 0


def test_undo_deletes_the_last_post_after_asking(api, credentials, monkeypatch, capsys):
    posted_earlier(api)
    capsys.readouterr()
    api.delete(f"{API_URL}/111", status=200, json={"data": {"deleted": True}})
    typed(monkeypatch, "y\n")
    assert x_post.main(["--undo"]) == 0
    assert api.calls[-1].request.method == "DELETE"
    out = capsys.readouterr().out
    assert "last post: 'having a great day'" in out
    assert "$0.010" in out
    assert out.endswith("deleted https://x.com/i/status/111\n")


def test_undo_works_once(api, credentials, monkeypatch, capsys):
    posted_earlier(api)
    api.delete(f"{API_URL}/111", status=200, json={"data": {"deleted": True}})
    typed(monkeypatch, "y\n")
    assert x_post.main(["--undo"]) == 0
    capsys.readouterr()
    assert x_post.main(["--undo"]) == 1
    assert capsys.readouterr().err == "x-post: nothing to undo\n"
    assert len(api.calls) == 2


@pytest.mark.parametrize("answer", ["n\n", ""], ids=["no", "no-terminal"])
def test_undo_without_a_yes_deletes_nothing(api, credentials, monkeypatch, answer):
    posted_earlier(api)
    typed(monkeypatch, answer)
    assert x_post.main(["--undo"]) == 1
    assert len(api.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        {"status": 404, "json": {"title": "Not Found Error", "detail": "Could not find post with id: [111]."}},
        {"status": 200, "json": {"data": {"deleted": False}}},
    ],
    ids=["error", "not-deleted"],
)
def test_failed_undo_keeps_the_post_to_retry(api, credentials, monkeypatch, response):
    posted_earlier(api)
    api.delete(f"{API_URL}/111", **response)
    typed(monkeypatch, "y\ny\n")
    assert x_post.main(["--undo"]) == 1
    assert x_post.main(["--undo"]) == 1
    assert [call.request.method for call in api.calls] == ["POST", "DELETE", "DELETE"]


def thread_posted_earlier(api, monkeypatch, tmp_path, parts=("one", "two", "three")):
    for post_id in range(1, len(parts) + 1):
        created(api, str(post_id))
    editor(tmp_path, monkeypatch, writes="\n---\n".join(parts) + "\n")
    assert x_post.main(["-e"]) == 0


def deleted(api, post_id, status=200):
    api.delete(f"{API_URL}/{post_id}", status=status, json={"data": {"deleted": True}})


def deletes(api):
    return [call.request.url.rpartition("/")[2] for call in api.calls if call.request.method == "DELETE"]


def test_undo_takes_back_a_whole_thread_last_post_first(api, credentials, monkeypatch, tmp_path, capsys):
    thread_posted_earlier(api, monkeypatch, tmp_path)
    capsys.readouterr()
    for post_id in "321":
        deleted(api, post_id)
    typed(monkeypatch, "y\n")
    assert x_post.main(["--undo"]) == 0
    assert deletes(api) == ["3", "2", "1"]
    out = capsys.readouterr().out
    assert "last post: a thread of 3, starting 'one'" in out
    assert "delete all 3, from https://x.com/i/status/1? costs $0.030" in out
    assert out.endswith("".join(f"deleted https://x.com/i/status/{post_id}\n" for post_id in "321"))
    assert x_post.main(["--undo"]) == 1  # nothing left to undo


def test_a_post_after_a_thread_is_undone_on_its_own(api, credentials, monkeypatch, tmp_path, capsys):
    thread_posted_earlier(api, monkeypatch, tmp_path, parts=("one", "two"))
    posted_earlier(api, "later", "9")
    capsys.readouterr()
    deleted(api, "9")
    typed(monkeypatch, "y\n")
    assert x_post.main(["--undo"]) == 0
    assert "last post: 'later'" in capsys.readouterr().out
    assert deletes(api) == ["9"]


def test_a_thread_carried_on_from_the_draft_is_undone_whole(api, credentials, monkeypatch, tmp_path):
    created(api, "1")
    api.post(API_URL, status=503, body="upstream unavailable")
    editor(tmp_path, monkeypatch, writes="one\n---\ntwo\n---\nthree\n")
    assert x_post.main(["-e"]) == 1
    api.remove(responses.POST, API_URL)
    created(api, "2")
    created(api, "3")
    editor(tmp_path, monkeypatch)  # send the rest, replying to 1
    assert x_post.main(["-e"]) == 0
    for post_id in "321":
        deleted(api, post_id)
    typed(monkeypatch, "y\n")
    assert x_post.main(["--undo"]) == 0
    assert deletes(api) == ["3", "2", "1"]


def test_a_thread_undo_that_fails_partway_keeps_the_rest_to_retry(api, credentials, monkeypatch, tmp_path, capsys):
    thread_posted_earlier(api, monkeypatch, tmp_path)
    deleted(api, "3")
    api.delete(f"{API_URL}/2", status=503, body="upstream unavailable")
    typed(monkeypatch, "y\n")
    assert x_post.main(["--undo"]) == 1
    assert "2 of the thread still up; x-post --undo deletes them" in capsys.readouterr().err
    deleted(api, "2")
    deleted(api, "1")
    typed(monkeypatch, "y\n")
    assert x_post.main(["--undo"]) == 0
    assert "last post: a thread of 2, starting 'one'" in capsys.readouterr().out
    assert deletes(api) == ["3", "2", "2", "1"]


def test_undo_still_reads_the_record_of_a_post_made_before_threads_were_kept(
    api, credentials, state_home, monkeypatch, capsys
):
    record = state_home / "x-post" / "last.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"id": "111", "text": "hi"}))
    deleted(api, "111")
    typed(monkeypatch, "y\n")
    assert x_post.main(["--undo"]) == 0
    assert "last post: 'hi'" in capsys.readouterr().out
    assert deletes(api) == ["111"]


@pytest.mark.parametrize(
    "argv, error",
    [
        (["--undo", "hi"], "--undo takes no message"),
        (["--undo", "-e"], "not allowed with"),
        (["--undo", "-i", "photo.png"], "--undo takes no images"),
        (["--undo", "-s"], "--undo takes no images"),
        (["--undo", "--poll", "tea"], "--undo takes no poll"),
        (["--undo", "--at", "+1h"], "--at can't be combined with --undo"),
    ],
)
def test_undo_takes_nothing_else(api, credentials, capsys, argv, error):
    with pytest.raises(SystemExit) as exit_info:
        x_post.main(argv)
    assert exit_info.value.code == 2
    assert error in capsys.readouterr().err
    assert not api.calls


FRIDAY = datetime(2026, 10, 23, 8, 30)


@pytest.fixture
def clock(monkeypatch):
    """Local wall-clock time as x-post sees it; set clock["now"] to move it."""
    clock = {"now": FRIDAY}
    monkeypatch.setattr(x_post, "local_now", lambda: clock["now"])
    return clock


def queued(state_home):
    return [json.loads(path.read_text()) for path in sorted((state_home / "x-post" / "scheduled").glob("*/post.json"))]


@pytest.fixture
def ids(monkeypatch):
    """Queue ids in order, 0001, 0002 ..., instead of random ones."""
    numbers = itertools.count(1)
    monkeypatch.setattr(x_post.secrets, "token_hex", lambda nbytes: f"{next(numbers):04x}")


@pytest.mark.parametrize(
    "text, expected",
    [
        ("+2h", datetime(2026, 10, 23, 10, 30)),
        ("+90m", datetime(2026, 10, 23, 10, 0)),
        ("+1d", datetime(2026, 10, 24, 8, 30)),
        ("09:00", datetime(2026, 10, 23, 9, 0)),
        ("08:00", datetime(2026, 10, 24, 8, 0)),
        ("tomorrow 9:05", datetime(2026, 10, 24, 9, 5)),
        ("Fri 18:00", datetime(2026, 10, 23, 18, 0)),
        ("fri 08:00", datetime(2026, 10, 30, 8, 0)),
        ("sat 09:00", datetime(2026, 10, 24, 9, 0)),
        ("2026-10-24 18:30", datetime(2026, 10, 24, 18, 30)),
    ],
    ids=[
        "hours", "minutes", "days", "later-today", "passed-today-means-tomorrow", "tomorrow",
        "weekday-today", "weekday-passed-means-next-week", "weekday-ahead", "date",
    ],
)
def test_parse_when(text, expected):
    assert x_post.parse_when(text, FRIDAY) == expected


@pytest.mark.parametrize(
    "text, error",
    [
        ("2026-10-22 09:00", "--at '2026-10-22 09:00' is in the past"),
        ("next week", "can't read --at 'next week'; try +90m, +2h, 09:00, tomorrow 09:00, fri 18:00 or 2026-10-24 18:30"),
        ("25:00", "can't read --at '25:00': no such time"),
    ],
    ids=["past", "unreadable", "no-such-time"],
)
def test_parse_when_refuses(text, error):
    with pytest.raises(x_post.XPostError) as raised:
        x_post.parse_when(text, FRIDAY)
    assert str(raised.value) == error


def test_bad_at_is_refused_before_the_prompt(api, credentials, clock, capsys):
    assert x_post.main(["--at", "next week"]) == 1
    captured = capsys.readouterr()
    assert "can't read --at 'next week'" in captured.err
    assert captured.out == ""


def test_at_queues_the_post_without_sending(api, credentials, clock, state_home, capsys):
    assert x_post.main(["--at", "tomorrow 09:00", "good", "morning"]) == 0
    assert not api.calls
    [entry] = queued(state_home)
    assert entry["due"] == "2026-10-24T09:00"
    assert entry["parts"] == ["good morning"]
    assert re.fullmatch(r"scheduled [0-9a-f]{4} for Sat 2026-10-24 09:00\n", capsys.readouterr().out)


def test_send_due_sends_what_is_due_however_late_without_asking(api, credentials, clock, monkeypatch, state_home):
    typed(monkeypatch, "y\n")  # the link question is answered when scheduling, not when sending
    assert x_post.main(["--at", "09:00", "see", "example.com"]) == 0
    assert x_post.main(["--at", "2026-10-30 09:00", "not", "yet"]) == 0
    clock["now"] = datetime(2026, 10, 26, 12, 0)  # three days late for the first
    created(api)
    assert x_post.main(["--send-due"]) == 0
    assert [json.loads(call.request.body) for call in api.calls] == [{"text": "see example.com"}]
    assert [entry["parts"] for entry in queued(state_home)] == [["not yet"]]


def test_queued_images_and_alt_text_are_sent_from_the_queue(api, credentials, clock, tmp_path):
    photo = image(tmp_path)
    assert x_post.main(["--at", "+1h", "-i", photo, "--alt", "a cat", "caption"]) == 0
    os.remove(photo)
    assert not api.calls
    clock["now"] = datetime(2026, 10, 23, 9, 30)
    uploaded(api, "101")
    described(api, "101")
    created(api)
    assert x_post.main(["--send-due"]) == 0
    assert PNG in api.calls[0].request.body
    assert json.loads(api.calls[1].request.body) == alt_body("101", "a cat")
    assert json.loads(api.calls[2].request.body) == {"text": "caption", "media": {"media_ids": ["101"]}}


def test_failed_scheduled_post_is_held_with_its_reason(api, credentials, clock, state_home):
    assert x_post.main(["--at", "+1h", "hello"]) == 0
    clock["now"] = datetime(2026, 10, 23, 10, 0)
    api.post(API_URL, status=403, json={"title": "Forbidden", "detail": "Duplicate content."})
    assert x_post.main(["--send-due"]) == 1
    [entry] = queued(state_home)
    assert entry["error"] == "403 Forbidden: Duplicate content."
    assert x_post.main(["--send-due"]) == 0  # held, not retried every minute
    assert len(api.calls) == 1


def test_scheduled_thread_that_fails_partway_is_held_then_retried_from_where_it_stopped(
    api, credentials, clock, ids, monkeypatch, tmp_path, state_home
):
    editor(tmp_path, monkeypatch, writes="one\n---\ntwo\n---\nthree\n")
    assert x_post.main(["--at", "+1h", "-e"]) == 0
    assert not draft(state_home).exists()
    clock["now"] = datetime(2026, 10, 23, 10, 0)
    created(api, "1")
    api.post(API_URL, status=503, body="upstream unavailable")
    assert x_post.main(["--send-due"]) == 1
    [entry] = queued(state_home)
    assert (entry["parts"], entry["reply_to"]) == (["two", "three"], "1")
    assert "1 of 3 posts went out" in entry["error"]
    assert x_post.main(["--retry", "0001"]) == 0
    created(api, "2")
    created(api, "3")
    assert x_post.main(["--send-due"]) == 0
    assert [json.loads(call.request.body) for call in api.calls[-2:]] == [
        {"text": "two", "reply": {"in_reply_to_tweet_id": "1"}},
        {"text": "three", "reply": {"in_reply_to_tweet_id": "2"}},
    ]
    assert queued(state_home) == []


def test_a_queued_poll_is_listed_and_goes_out_with_the_post(api, credentials, clock, ids, capsys):
    assert x_post.main(["--at", "+1h", *POLL, "tea or coffee?"]) == 0
    capsys.readouterr()
    assert x_post.main(["--scheduled"]) == 0
    assert capsys.readouterr().out == "0001  Fri 2026-10-23 09:30  $0.015  'tea or coffee?', a poll\n"
    clock["now"] = datetime(2026, 10, 23, 9, 30)
    created(api)
    assert x_post.main(["--send-due"]) == 0
    assert json.loads(api.calls[0].request.body)["poll"] == {"options": ["tea", "coffee"], "duration_minutes": 1440}


def test_a_held_threads_unsent_rest_carries_no_poll(api, credentials, clock, ids, monkeypatch, tmp_path):
    editor(tmp_path, monkeypatch, writes="tea or coffee?\n---\nexplain yourself\n")
    assert x_post.main(["--at", "+1h", "-e", *POLL]) == 0
    clock["now"] = datetime(2026, 10, 23, 10, 0)
    created(api, "1")
    api.post(API_URL, status=503, body="upstream unavailable")
    assert x_post.main(["--send-due"]) == 1
    assert x_post.main(["--retry", "0001"]) == 0
    created(api, "2")
    assert x_post.main(["--send-due"]) == 0
    assert json.loads(api.calls[-1].request.body) == {
        "text": "explain yourself",
        "reply": {"in_reply_to_tweet_id": "1"},
    }


def test_image_lines_are_queued_with_their_posts_and_sent_from_the_queue(
    api, credentials, clock, ids, monkeypatch, tmp_path, capsys
):
    cat, dog = image(tmp_path, "cat.png"), image(tmp_path, "dog.png")
    editor(tmp_path, monkeypatch, writes=f"first\nimage: {cat}\nalt: a cat\n---\nsecond\nimage: {dog}\n---\nthird\n")
    assert x_post.main(["--at", "+1h", "-e"]) == 0
    os.remove(cat)
    os.remove(dog)
    capsys.readouterr()
    assert x_post.main(["--scheduled"]) == 0
    assert capsys.readouterr().out == "0001  Fri 2026-10-23 09:30  $0.050  'first' and 2 more posts, 2 images\n"
    clock["now"] = datetime(2026, 10, 23, 9, 30)
    uploaded(api, "m1")
    described(api, "m1")
    uploaded(api, "m2")
    for post_id in "123":
        created(api, post_id)
    assert x_post.main(["--send-due"]) == 0
    assert json.loads(api.calls[1].request.body) == alt_body("m1", "a cat")
    assert [json.loads(call.request.body) for call in api.calls[3:]] == [
        {"text": "first", "media": {"media_ids": ["m1"]}},
        {"text": "second", "reply": {"in_reply_to_tweet_id": "1"}, "media": {"media_ids": ["m2"]}},
        {"text": "third", "reply": {"in_reply_to_tweet_id": "2"}},
    ]


def test_a_held_threads_unsent_rest_keeps_its_images(api, credentials, clock, ids, monkeypatch, tmp_path):
    cat, dog = image(tmp_path, "cat.png"), image(tmp_path, "dog.png")
    editor(tmp_path, monkeypatch, writes=f"one\nimage: {cat}\n---\ntwo\n---\nthree\nimage: {dog}\n")
    assert x_post.main(["--at", "+1h", "-e"]) == 0
    clock["now"] = datetime(2026, 10, 23, 10, 0)
    uploaded(api, "m1")
    uploaded(api, "m2")
    created(api, "1")
    api.post(API_URL, status=503, body="upstream unavailable")
    assert x_post.main(["--send-due"]) == 1
    assert x_post.main(["--retry", "0001"]) == 0
    api.remove(responses.POST, MEDIA_URL)
    uploaded(api, "m3")
    created(api, "2")
    created(api, "3")
    assert x_post.main(["--send-due"]) == 0
    assert [json.loads(call.request.body) for call in api.calls[-2:]] == [
        {"text": "two", "reply": {"in_reply_to_tweet_id": "1"}},
        {"text": "three", "reply": {"in_reply_to_tweet_id": "2"}, "media": {"media_ids": ["m3"]}},
    ]


def test_check_shows_when_an_at_post_would_go_out(api, clock, state_home, systemctl, capsys):
    assert x_post.main(["--check", "--at", "fri 18:00", "hello"]) == 0
    assert capsys.readouterr().out == (
        "5/280 characters, 1/10 lines: fits\ncost: $0.015 (1 post)\nwould go out Fri 2026-10-23 18:00\n"
    )
    assert queued(state_home) == []
    assert systemctl.calls() == []


def test_first_at_installs_and_starts_the_timer(api, credentials, clock, config_home, systemctl, capsys):
    assert x_post.main(["--at", "+1h", "hello"]) == 0
    units = config_home / "systemd" / "user"
    timer = (units / "x-post-due.timer").read_text()
    service = (units / "x-post-due.service").read_text()
    for line in ("OnCalendar=minutely", "Persistent=true", "WantedBy=timers.target"):
        assert f"\n{line}\n" in timer
    # The entry point beside the running interpreter: the same install that queued the post.
    assert f"\nExecStart={Path(sys.executable).with_name('x-post')} --send-due\n" in service
    assert "\nConditionPathExistsGlob=%S/x-post/scheduled/*\n" in service
    # x-post's own output at notice, systemd's per-minute Starting/Finished/skipped (info) dropped.
    for line in ("SyslogLevel=notice", "LogLevelMax=notice"):
        assert f"\n{line}\n" in service
    assert systemctl.calls() == [RELOAD, ENABLE]
    assert "x-post: installed x-post-due.timer" in capsys.readouterr().err


def test_later_at_only_makes_sure_the_timer_runs(api, credentials, clock, systemctl, capsys):
    assert x_post.main(["--at", "+1h", "one"]) == 0
    capsys.readouterr()
    assert x_post.main(["--at", "+2h", "two"]) == 0
    assert systemctl.calls() == [RELOAD, ENABLE, ENABLE]
    assert capsys.readouterr().err == ""


def test_timer_that_fails_to_start_queues_nothing_and_installs_whole_next_time(
    api, credentials, clock, state_home, systemctl, capsys
):
    systemctl.fail.write_text("Failed to connect to bus: No medium found")
    assert x_post.main(["--at", "+1h", "hello"]) == 1
    assert "could not start x-post-due.timer: Failed to connect to bus: No medium found" in capsys.readouterr().err
    assert queued(state_home) == []
    systemctl.fail.unlink()
    assert x_post.main(["--at", "+1h", "hello"]) == 0
    assert systemctl.calls() == [RELOAD, RELOAD, ENABLE]


def test_scheduled_lists_the_queue_soonest_first_without_credentials(
    api, credentials, clock, ids, monkeypatch, tmp_path, capsys
):
    editor(tmp_path, monkeypatch, writes="a thread\n---\nof two\n---\nno, three\n")
    assert x_post.main(["--at", "fri 18:00", "-e"]) == 0
    assert x_post.main(["--at", "+1h", "-i", image(tmp_path), "--alt", "a cat", "with a picture"]) == 0
    assert x_post.main(["--at", "+1d", "a long one that goes on well past where the list cuts it"]) == 0
    capsys.readouterr()
    credentials.unlink()
    assert x_post.main(["--scheduled"]) == 0
    assert capsys.readouterr().out == (
        "0002  Fri 2026-10-23 09:30  $0.020  'with a picture', 1 image\n"
        "0001  Fri 2026-10-23 18:00  $0.045  'a thread' and 2 more posts\n"
        "0003  Sat 2026-10-24 08:30  $0.015  'a long one that goes on well past where the...'\n"
    )


def test_scheduled_shows_why_a_post_is_held(api, credentials, clock, ids, capsys):
    assert x_post.main(["--at", "+1h", "hello"]) == 0
    clock["now"] = datetime(2026, 10, 23, 10, 0)
    api.post(API_URL, status=403, json={"title": "Forbidden", "detail": "Duplicate content."})
    x_post.main(["--send-due"])
    capsys.readouterr()
    assert x_post.main(["--scheduled"]) == 0
    assert capsys.readouterr().out == (
        "0001  Fri 2026-10-23 09:30  $0.015  'hello'\n"
        "      held: 403 Forbidden: Duplicate content.\n"
    )


def test_scheduled_says_when_nothing_is(api, capsys):
    assert x_post.main(["--scheduled"]) == 0
    assert capsys.readouterr().out == "nothing scheduled\n"


def test_cancel_removes_a_queued_post_without_credentials(api, credentials, clock, ids, state_home, capsys):
    assert x_post.main(["--at", "+1h", "hello"]) == 0
    assert x_post.main(["--at", "+2h", "bye"]) == 0
    capsys.readouterr()
    credentials.unlink()
    assert x_post.main(["--cancel", "0001"]) == 0
    assert capsys.readouterr().out == "cancelled 0001: 'hello'\n"
    assert [entry["parts"] for entry in queued(state_home)] == [["bye"]]


def busy(state_home, entry_id):
    """Lock a queued post the way another x-post would; closing the returned file lets go."""
    lock = (state_home / "x-post" / "scheduled" / entry_id / "lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    return lock


def test_send_due_leaves_a_post_another_x_post_holds_for_the_next_run(api, credentials, clock, ids, state_home):
    assert x_post.main(["--at", "+1h", "hello"]) == 0
    clock["now"] = datetime(2026, 10, 23, 10, 0)
    with busy(state_home, "0001"):
        assert x_post.main(["--send-due"]) == 0
    assert not api.calls
    created(api)
    assert x_post.main(["--send-due"]) == 0
    assert queued(state_home) == []


CHANGES = [["--cancel"], ["--retry"], ["--move", "+1d"], ["--revise"]]  # each command, then what follows its id


@pytest.mark.parametrize("command", CHANGES, ids=lambda command: command[0])
def test_changes_wait_for_a_post_another_x_post_holds(
    api, credentials, clock, ids, monkeypatch, tmp_path, state_home, capsys, command
):
    hold(api, clock, "hello")
    capsys.readouterr()
    editor(tmp_path, monkeypatch, writes="changed\n")  # so an unlocked --revise fails here, not hangs in nvim
    with busy(state_home, "0001"):
        assert x_post.main([command[0], "0001", *command[1:]]) == 1
    assert "scheduled post 0001 is being sent or changed right now; try again in a moment" in capsys.readouterr().err
    [entry] = queued(state_home)
    assert (entry["parts"], entry["due"]) == (["hello"], "2026-10-23T09:30")
    assert entry["error"]


@pytest.mark.parametrize("command", CHANGES, ids=lambda command: command[0])
@pytest.mark.parametrize("entry_id", ["0009", "..", "."])
def test_changes_only_reach_queued_posts(api, credentials, clock, ids, state_home, capsys, entry_id, command):
    assert x_post.main(["--at", "+1h", "hello"]) == 0
    assert x_post.main([command[0], entry_id, *command[1:]]) == 1
    assert f"no scheduled post {entry_id!r}; x-post --scheduled lists them" in capsys.readouterr().err
    assert [entry["parts"] for entry in queued(state_home)] == [["hello"]]


def test_move_changes_when_a_queued_post_goes_out_without_credentials(api, credentials, clock, ids, state_home, capsys):
    assert x_post.main(["--at", "+1h", "hello"]) == 0
    capsys.readouterr()
    credentials.unlink()
    assert x_post.main(["--move", "0001", "tomorrow 09:00"]) == 0
    assert capsys.readouterr().out == "moved 0001 to Sat 2026-10-24 09:00: 'hello'\n"
    assert queued(state_home)[0]["due"] == "2026-10-24T09:00"


@pytest.mark.parametrize(
    "text, error",
    [
        ("2026-10-22 09:00", "--move '2026-10-22 09:00' is in the past"),
        ("soon", "can't read --move 'soon'; try +90m, +2h, 09:00, tomorrow 09:00, fri 18:00 or 2026-10-24 18:30"),
    ],
    ids=["past", "unreadable"],
)
def test_move_reads_its_time_like_at(api, credentials, clock, ids, state_home, capsys, text, error):
    assert x_post.main(["--at", "+1h", "hello"]) == 0
    assert x_post.main(["--move", "0001", text]) == 1
    assert error in capsys.readouterr().err
    assert queued(state_home)[0]["due"] == "2026-10-23T09:30"


def revision(state_home, entry_id="0001"):
    return state_home / "x-post" / "scheduled" / entry_id / "revise.txt"


def test_revise_rewrites_a_queued_post_in_the_editor_without_credentials(
    api, credentials, clock, ids, monkeypatch, tmp_path, state_home, capsys
):
    assert x_post.main(["--at", "+1h", "hello there"]) == 0
    capsys.readouterr()
    opened = editor(tmp_path, monkeypatch, writes="hello\n---\nworld\n")
    credentials.unlink()
    assert x_post.main(["--revise", "0001"]) == 0
    assert opened.read_text() == "hello there\n"
    assert capsys.readouterr().out == "revised 0001, going out Fri 2026-10-23 09:30: 'hello' and 1 more post\n"
    assert queued(state_home)[0]["parts"] == ["hello", "world"]
    assert not revision(state_home).exists()


def test_revise_shows_a_held_threads_reply_to_and_keeps_it(
    api, credentials, clock, ids, monkeypatch, tmp_path, state_home, capsys
):
    editor(tmp_path, monkeypatch, writes="one\n---\ntwo\n---\nthree\n")
    assert x_post.main(["--at", "+1h", "-e"]) == 0
    clock["now"] = datetime(2026, 10, 23, 10, 0)
    created(api, "1")
    api.post(API_URL, status=503, body="upstream unavailable")
    assert x_post.main(["--send-due"]) == 1
    capsys.readouterr()
    opened = editor(tmp_path, monkeypatch, writes="reply-to: https://x.com/i/status/1\ntwo, fixed\n---\nthree\n")
    assert x_post.main(["--revise", "0001"]) == 0
    assert opened.read_text() == "reply-to: https://x.com/i/status/1\ntwo\n---\nthree\n"
    [entry] = queued(state_home)
    assert (entry["parts"], entry["reply_to"]) == (["two, fixed", "three"], "1")
    assert capsys.readouterr().out.endswith("(still held; x-post --retry 0001 releases it)\n")


def test_revise_shows_each_posts_images_and_keeps_them(
    api, credentials, clock, ids, monkeypatch, tmp_path, state_home
):
    assert x_post.main(["--at", "+1h", "-i", image(tmp_path, "cat.png"), "--alt", "a cat", "hello"]) == 0
    opened = editor(tmp_path, monkeypatch)  # save it as it is
    assert x_post.main(["--revise", "0001"]) == 0
    assert x_post.main(["--revise", "0001"]) == 0  # and again: the name doesn't grow
    folder = state_home / "x-post" / "scheduled" / "0001"
    assert opened.read_text() == f"hello\nimage: {folder}/2/cat.png\nalt: a cat\n"
    [entry] = queued(state_home)
    assert (entry["images"], entry["alts"]) == ([["3/cat.png"]], [["a cat"]])
    assert (folder / "3" / "cat.png").read_bytes() == PNG


def test_revise_moves_adds_and_drops_images(api, credentials, clock, ids, monkeypatch, tmp_path, state_home):
    assert x_post.main(["--at", "+1h", "-i", image(tmp_path, "cat.png"), "hello"]) == 0
    editor(tmp_path, monkeypatch, writes=f"hello\n---\nmore\nimage: {image(tmp_path, 'dog.png')}\nalt: a dog\n")
    assert x_post.main(["--revise", "0001"]) == 0
    [entry] = queued(state_home)
    assert (entry["images"], entry["alts"]) == ([[], ["2/dog.png"]], [[], ["a dog"]])
    assert not (state_home / "x-post" / "scheduled" / "0001" / "1").exists()


@pytest.mark.parametrize(
    "writes, argv, error",
    [
        ("hello\nimage: {tmp}/missing.png\n", [], "{tmp}/missing.png: no such file or directory"),
        ("tea?\nimage: {tmp}/cat.png\n", POLL, "a poll can't go with images or video; X takes one or the other"),
    ],
    ids=["missing", "with-a-poll"],
)
def test_revise_refuses_bad_image_lines_and_reopens_the_edit(
    api, credentials, clock, ids, monkeypatch, tmp_path, state_home, capsys, writes, argv, error
):
    image(tmp_path, "cat.png")
    assert x_post.main(["--at", "+1h", *argv, writes.split("\n")[0]]) == 0
    editor(tmp_path, monkeypatch, writes=writes.format(tmp=tmp_path))
    assert x_post.main(["--revise", "0001"]) == 1
    assert f"{error.format(tmp=tmp_path)}; x-post --revise 0001 reopens your edit" in capsys.readouterr().err
    assert queued(state_home)[0]["images"] == [[]]
    assert revision(state_home).exists()


def test_revise_refuses_a_post_that_would_fold_and_reopens_the_edit(
    api, credentials, clock, ids, monkeypatch, tmp_path, state_home, capsys
):
    assert x_post.main(["--at", "+1h", "hello"]) == 0
    editor(tmp_path, monkeypatch, writes="x" * 281)
    assert x_post.main(["--revise", "0001"]) == 1
    err = capsys.readouterr().err
    assert "281/280 characters" in err and "x-post --revise 0001 reopens your edit" in err
    assert queued(state_home)[0]["parts"] == ["hello"]
    opened = editor(tmp_path, monkeypatch)
    x_post.main(["--revise", "0001"])
    assert opened.read_text() == "x" * 281


def test_revise_asks_the_link_price_again(api, credentials, clock, ids, monkeypatch, tmp_path, state_home, capsys):
    assert x_post.main(["--at", "+1h", "hello"]) == 0
    editor(tmp_path, monkeypatch, writes="see example.com\n")
    typed(monkeypatch, "n\n")
    assert x_post.main(["--revise", "0001"]) == 1
    assert "not changed; x-post --revise 0001 reopens your edit" in capsys.readouterr().err
    assert queued(state_home)[0]["parts"] == ["hello"]
    typed(monkeypatch, "y\n")
    assert x_post.main(["--revise", "0001"]) == 0
    assert queued(state_home)[0]["parts"] == ["see example.com"]


def test_revise_asks_about_typos_again(api, credentials, clock, ids, monkeypatch, tmp_path, state_home, capsys):
    assert x_post.main(["--at", "+1h", "hello"]) == 0
    editor(tmp_path, monkeypatch, writes="havign fun\n")
    typed(monkeypatch, "n\n")
    assert x_post.main(["--revise", "0001"]) == 1
    assert "not changed; x-post --revise 0001 reopens your edit" in capsys.readouterr().err
    assert queued(state_home)[0]["parts"] == ["hello"]


def test_emptying_the_revision_changes_nothing(api, credentials, clock, ids, monkeypatch, tmp_path, state_home, capsys):
    assert x_post.main(["--at", "+1h", "hello"]) == 0
    editor(tmp_path, monkeypatch, writes="")
    assert x_post.main(["--revise", "0001"]) == 1
    assert "the revision is empty; 0001 is unchanged" in capsys.readouterr().err
    assert queued(state_home)[0]["parts"] == ["hello"]
    assert not revision(state_home).exists()


def test_revise_keeps_the_timer_off_the_post_while_the_editor_is_open(
    api, credentials, clock, ids, monkeypatch, tmp_path, state_home
):
    assert x_post.main(["--at", "+1h", "hello"]) == 0
    probe, seen = tmp_path / "probe.py", tmp_path / "seen.txt"
    probe.write_text(  # an editor that checks the lock the way the timer would, then saves
        "import fcntl, pathlib, sys\n"
        f"with open({str(state_home / 'x-post' / 'scheduled' / '0001' / 'lock')!r}, 'a') as lock:\n"
        "    try:\n"
        "        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "        state = 'free'\n"
        "    except BlockingIOError:\n"
        "        state = 'busy'\n"
        f"pathlib.Path({str(seen)!r}).write_text(state)\n"
        "pathlib.Path(sys.argv[1]).write_text('hello again\\n')\n"
    )
    monkeypatch.setenv("VISUAL", shlex.join([sys.executable, str(probe)]))
    assert x_post.main(["--revise", "0001"]) == 0
    assert seen.read_text() == "busy"


def test_a_moved_held_post_stays_held_and_retry_names_its_new_time(api, credentials, clock, ids, capsys):
    hold(api, clock, "hello")
    capsys.readouterr()
    assert x_post.main(["--move", "0001", "tomorrow 09:00"]) == 0
    assert capsys.readouterr().out == (
        "moved 0001 to Sat 2026-10-24 09:00: 'hello' (still held; x-post --retry 0001 releases it)\n"
    )
    assert x_post.main(["--retry", "0001"]) == 0
    assert capsys.readouterr().out == "0001 goes out Sat 2026-10-24 09:00: 'hello'\n"


def test_retry_sends_a_held_post_at_the_next_run_without_credentials(
    api, credentials, clock, ids, systemctl, state_home, capsys
):
    assert x_post.main(["--at", "+1h", "hello"]) == 0
    clock["now"] = datetime(2026, 10, 23, 10, 0)
    api.post(API_URL, status=503, body="upstream unavailable")
    assert x_post.main(["--send-due"]) == 1
    capsys.readouterr()
    credentials.unlink()
    assert x_post.main(["--retry", "0001"]) == 0
    assert capsys.readouterr().out == "0001 goes out within a minute: 'hello'\n"
    assert systemctl.calls() == [RELOAD, ENABLE, ENABLE]  # the timer is made sure of, as by --at
    credentials.write_text(CREDENTIALS)
    created(api)
    assert x_post.main(["--send-due"]) == 0
    assert json.loads(api.calls[-1].request.body) == {"text": "hello"}
    assert queued(state_home) == []


def hold(api, clock, text):
    """Queue a post an hour out, move the clock past it, and have X refuse it."""
    assert x_post.main(["--at", "+1h", text]) == 0
    clock["now"] += timedelta(hours=2)
    api.post(API_URL, status=403, json={"title": "Forbidden", "detail": "Duplicate content."})
    assert x_post.main(["--send-due"]) == 1


HELD_ALERT = ["x-post: scheduled post held", "0001 'hello': 403 Forbidden: Duplicate content."]


def test_a_held_post_runs_the_alert_command_set_in_the_config(
    api, credentials, config_home, clock, ids, monkeypatch, tmp_path, alerts
):
    monkeypatch.setenv("HOME", str(tmp_path))
    alert_in(config_home, "'~/alerts/alert.py --urgent'")
    hold(api, clock, "hello")
    assert alerts.calls() == [["--urgent", *HELD_ALERT]]


def test_a_held_post_raises_no_alert_without_one_set(api, credentials, clock, ids, state_home, capsys, alerts):
    hold(api, clock, "hello")
    assert alerts.calls() == []
    assert "alert" not in capsys.readouterr().err
    assert queued(state_home)[0]["error"] == "403 Forbidden: Duplicate content."


@pytest.mark.parametrize(
    "command, error",
    [("'/missing/alert'", "No such file or directory"), ("5", "alert in {path} isn't a command: 5")],
    ids=["missing", "not-text"],
)
def test_an_alert_that_cannot_go_out_is_logged_and_the_post_still_held(
    api, credentials, config_home, clock, ids, state_home, capsys, command, error
):
    path = alert_in(config_home, command)
    hold(api, clock, "hello")
    assert "x-post: could not raise an alert: " in (err := capsys.readouterr().err)
    assert error.format(path=path) in err
    assert queued(state_home)[0]["error"] == "403 Forbidden: Duplicate content."


def test_other_runs_warn_while_a_scheduled_post_is_held(api, credentials, clock, capsys):
    hold(api, clock, "one")
    capsys.readouterr()
    assert x_post.main(["--check", "hi"]) == 0
    assert capsys.readouterr().err == "x-post: 1 scheduled post is held; x-post --scheduled shows why\n"
    hold(api, clock, "two")
    capsys.readouterr()
    assert x_post.main(["--check", "hi"]) == 0
    assert capsys.readouterr().err == "x-post: 2 scheduled posts are held; x-post --scheduled shows why\n"


@pytest.mark.parametrize(
    "argv", [["--scheduled"], ["--cancel", "0001"], ["--retry", "0001"], ["--send-due"], ["--spent"], ["--history"]]
)
def test_queue_commands_do_not_warn_about_held_posts(api, credentials, clock, ids, capsys, argv):
    hold(api, clock, "hello")
    capsys.readouterr()
    x_post.main(argv)
    assert "held;" not in capsys.readouterr().err


def test_retry_refuses_a_post_that_is_not_held(api, credentials, clock, ids, capsys):
    assert x_post.main(["--at", "+1h", "hello"]) == 0
    assert x_post.main(["--retry", "0001"]) == 1
    assert "scheduled post 0001 isn't held; it goes out Fri 2026-10-23 09:30" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--send-due", "hello"],
        ["--scheduled", "-e"],
        ["--cancel", "0001", "--at", "+1h"],
        ["--retry", "0001", "-s"],
        ["--move", "0001", "+1h", "hello"],
        ["--revise", "0001", "-i", "x.png"],
        ["--spent", "hello"],
        ["--history", "5", "hello"],
        ["--delete", "1", "hello"],
    ],
)
def test_queue_commands_take_nothing_else(api, capsys, argv):
    with pytest.raises(SystemExit) as exit_info:
        x_post.main(argv)
    assert exit_info.value.code == 2
    assert f"{argv[0]} takes no other options" in capsys.readouterr().err


def test_each_billed_call_is_tallied_at_its_listed_price(api, credentials, clock, monkeypatch, tmp_path, capsys):
    posted_earlier(api, "plain", "1")
    typed(monkeypatch, "y\n")
    posted_earlier(api, "see example.com", "2")
    uploaded(api, "m1")
    described(api, "m1")
    created(api, "3")
    assert x_post.main(["-i", image(tmp_path), "--alt", "a cat", "pets"]) == 0
    deleted(api, "3")
    typed(monkeypatch, "y\n")
    assert x_post.main(["--undo"]) == 0
    calls = len(api.calls)
    credentials.unlink()
    capsys.readouterr()
    assert x_post.main(["--spent"]) == 0
    assert capsys.readouterr().out.startswith("October 2026: $0.245 (2 posts, 1 link post, 1 alt text, 1 delete)\n")
    assert len(api.calls) == calls  # read from x-post's own record, not X


def test_spent_splits_this_month_from_all_time(api, credentials, clock, capsys):
    clock["now"] = datetime(2026, 9, 30, 23, 50)
    posted_earlier(api, "september", "1")
    clock["now"] = FRIDAY
    posted_earlier(api, "october", "2")
    capsys.readouterr()
    assert x_post.main(["--spent"]) == 0
    assert capsys.readouterr().out == "October 2026: $0.015 (1 post)\nsince 2026-09-30: $0.030 (2 posts)\n"
    clock["now"] = datetime(2026, 11, 2, 9, 0)
    assert x_post.main(["--spent"]) == 0
    assert capsys.readouterr().out.startswith("November 2026: $0.000\n")


def budget(config_home, value):
    path = config_home / "x-post" / "config.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(f"budget = {value}\n")
    return path


@pytest.mark.parametrize("answer, sent", [("n\n", 1), ("y\n", 2)], ids=["no", "yes"])
def test_a_post_past_the_monthly_budget_is_asked_about_at_a_terminal(
    api, credentials, config_home, clock, monkeypatch, capsys, answer, sent
):
    budget(config_home, "0.02")
    posted_earlier(api, "first", "1")
    created(api, "2")
    typed_at_a_terminal(monkeypatch, answer)
    assert x_post.main(["second"]) == (0 if sent == 2 else 1)
    question = "this would bring October 2026 to $0.030, past your $0.02 budget. post anyway? [y/N]"
    assert question in capsys.readouterr().out
    assert len(api.calls) == sent
    if sent == 1:
        api.remove(responses.POST, API_URL)  # the unused registration


@pytest.mark.parametrize(
    "cap, month",
    [("0.02", "$0.015 (1 post) of your $0.02 budget"), ("0.01", "$0.015 (1 post), past your $0.01 budget")],
    ids=["within", "past"],
)
def test_spent_shows_the_month_against_the_budget(api, credentials, config_home, clock, capsys, cap, month):
    clock["now"] = datetime(2026, 9, 30, 23, 50)
    posted_earlier(api, "september", "1")  # past the 0.02 budget all told, but not this month
    clock["now"] = FRIDAY
    posted_earlier(api, "october", "2")
    budget(config_home, cap)
    capsys.readouterr()
    assert x_post.main(["--spent"]) == 0
    assert capsys.readouterr().out == f"October 2026: {month}\nsince 2026-09-30: $0.030 (2 posts)\n"


@pytest.mark.parametrize(
    "cap, standing, october, november",
    [
        (None, "", "", ""),
        (
            "0.03",
            " of your $0.03 budget",
            ", making $0.030 of your $0.03 budget",
            ", making $0.035, past your $0.03 budget",
        ),
    ],
    ids=["no-budget", "budget"],
)
def test_spent_shows_what_the_queue_will_bill_by_month(
    api, credentials, config_home, clock, ids, tmp_path, capsys, cap, standing, october, november
):
    posted_earlier(api, "first", "1")
    assert x_post.main(["--at", "2026-11-03 09:00", "-i", image(tmp_path), "--alt", "a cat", "third"]) == 0
    assert x_post.main(["--at", "2026-10-30 09:00", "second"]) == 0
    assert x_post.main(["--at", "2026-11-02 09:00", "fourth"]) == 0
    if cap:
        budget(config_home, cap)
    capsys.readouterr()
    assert x_post.main(["--spent"]) == 0
    assert capsys.readouterr().out == (
        f"October 2026: $0.015 (1 post){standing}\n"
        "since 2026-10-23: $0.015 (1 post)\n"
        f"queued for October 2026: $0.015 (1 post){october}\n"
        f"queued for November 2026: $0.035 (2 posts, 1 alt text){november}\n"
    )


def test_spent_shows_an_overdue_queued_post_under_this_month_even_with_nothing_recorded(
    api, credentials, clock, ids, capsys
):
    assert x_post.main(["--at", "2026-10-31 23:30", "late"]) == 0
    clock["now"] = datetime(2026, 11, 1, 10, 0)  # the machine was off, so it goes out at the next run
    capsys.readouterr()
    assert x_post.main(["--spent"]) == 0
    assert capsys.readouterr().out == "nothing recorded yet\nqueued for November 2026: $0.015 (1 post)\n"


def test_check_counts_link_posts_and_alt_text_in_the_cost(api, monkeypatch, tmp_path, capsys):
    editor(tmp_path, monkeypatch, writes=f"see example.com\nimage: {image(tmp_path)}\nalt: a cat\n---\nmore\n")
    assert x_post.main(["--check", "-e"]) == 0
    assert "cost: $0.220 (1 post, 1 link post, 1 alt text)\n" in capsys.readouterr().out


@pytest.mark.parametrize(
    "cap, argv, line",
    [
        ("1.00", [], "October 2026 would come to $0.030 of your $1.00 budget"),
        ("0.03", [], "October 2026 would come to $0.030 of your $0.03 budget"),
        ("0.02", [], "October 2026 would come to $0.030, past your $0.02 budget"),
        ("1.00", ["--at", "2026-11-02 09:00"], "November 2026 would come to $0.015 of your $1.00 budget"),
    ],
    ids=["within", "exactly", "past", "month-it-goes-out"],
)
def test_check_shows_where_the_month_would_come_to_against_the_budget(
    api, credentials, config_home, clock, capsys, cap, argv, line
):
    budget(config_home, cap)
    posted_earlier(api, "first", "1")
    capsys.readouterr()
    assert x_post.main(["--check", *argv, "second"]) == 0
    assert f"cost: $0.015 (1 post)\n{line}\n" in capsys.readouterr().out


def test_off_a_terminal_a_post_past_the_budget_is_refused(api, credentials, config_home, clock, monkeypatch, capsys):
    budget(config_home, "0.01")
    typed(monkeypatch, "y\n")  # what would be read if it asked
    assert x_post.main(["hello"]) == 1
    expected = "x-post: this would bring October 2026 to $0.015, past your $0.01 budget; not posted\n"
    assert capsys.readouterr().err == expected
    assert not api.calls


def test_the_budget_counts_only_the_month_the_post_goes_out(api, credentials, config_home, clock, monkeypatch):
    budget(config_home, "0.015")
    typed(monkeypatch, "")  # a question now would find no answer
    clock["now"] = datetime(2026, 9, 30, 23, 50)
    posted_earlier(api, "september", "1")
    clock["now"] = datetime(2026, 10, 31, 12, 0)
    posted_earlier(api, "october", "2")
    assert x_post.main(["--at", "2026-11-02 09:00", "november"]) == 0


def test_link_posts_and_alt_text_count_toward_the_budget(
    api, credentials, config_home, clock, monkeypatch, tmp_path, capsys
):
    budget(config_home, "0.20")
    typed_at_a_terminal(monkeypatch, "y\nn\n")  # yes to the link price, then no
    assert x_post.main(["-i", image(tmp_path), "--alt", "a cat", "see", "example.com"]) == 1
    assert "this would bring October 2026 to $0.205, past your $0.20 budget" in capsys.readouterr().out
    assert not api.calls


def test_the_budget_counts_posts_queued_for_the_same_month(
    api, credentials, config_home, clock, ids, monkeypatch, state_home, capsys
):
    budget(config_home, "0.02")
    typed(monkeypatch, "")
    assert x_post.main(["--at", "2026-12-01 09:00", "december"]) == 0
    assert x_post.main(["--at", "2026-11-02 09:00", "first"]) == 0
    capsys.readouterr()
    assert x_post.main(["--at", "2026-11-03 09:00", "second"]) == 1
    assert capsys.readouterr().err == (
        "x-post: this would bring November 2026 to $0.030 ($0.015 of it queued), past your $0.02 budget; not posted\n"
    )
    assert len(queued(state_home)) == 2


def test_an_overdue_queued_post_counts_toward_the_month_it_goes_out_in(
    api, credentials, config_home, clock, ids, monkeypatch, capsys
):
    budget(config_home, "0.02")
    typed(monkeypatch, "")
    assert x_post.main(["--at", "2026-10-31 23:30", "late"]) == 0
    clock["now"] = datetime(2026, 11, 1, 10, 0)  # the machine was off, so it goes out at the next run
    capsys.readouterr()
    assert x_post.main(["hello"]) == 1
    assert "this would bring November 2026 to $0.030 ($0.015 of it queued)" in capsys.readouterr().err
    assert not api.calls


def test_check_counts_the_queue_in_where_the_month_would_come_to(api, credentials, config_home, clock, ids, capsys):
    budget(config_home, "1.00")
    assert x_post.main(["--at", "2026-11-02 09:00", "first"]) == 0
    capsys.readouterr()
    assert x_post.main(["--check", "--at", "2026-11-03 09:00", "second"]) == 0
    assert "November 2026 would come to $0.030 ($0.015 of it queued) of your $1.00 budget\n" in capsys.readouterr().out


def test_revise_checks_the_budget_counting_the_revision_in_place_of_what_it_replaces(
    api, credentials, config_home, clock, ids, monkeypatch, tmp_path, state_home, capsys
):
    budget(config_home, "0.03")
    assert x_post.main(["--at", "2026-11-02 09:00", "first"]) == 0
    assert x_post.main(["--at", "2026-11-03 09:00", "second"]) == 0  # exactly at the cap
    editor(tmp_path, monkeypatch, writes="second\n---\nthird\n")
    capsys.readouterr()
    assert x_post.main(["--revise", "0002"]) == 1
    assert capsys.readouterr().err == (
        "x-post: this would bring November 2026 to $0.045 ($0.015 of it queued), past your $0.03 budget;"
        " not changed; x-post --revise 0002 reopens your edit\n"
    )
    assert queued(state_home)[1]["parts"] == ["second"]
    assert revision(state_home, "0002").read_text() == "second\n---\nthird\n"


def test_revise_past_the_budget_is_asked_about_at_a_terminal(
    api, credentials, config_home, clock, ids, monkeypatch, tmp_path, state_home, capsys
):
    budget(config_home, "0.02")
    assert x_post.main(["--at", "2026-11-02 09:00", "first"]) == 0
    editor(tmp_path, monkeypatch, writes="first\n---\nsecond\n")
    typed_at_a_terminal(monkeypatch, "y\n")
    assert x_post.main(["--revise", "0001"]) == 0
    question = "this would bring November 2026 to $0.030, past your $0.02 budget. post anyway? [y/N]"
    assert question in capsys.readouterr().out
    assert queued(state_home)[0]["parts"] == ["first", "second"]


def test_moving_a_post_into_a_month_it_would_take_past_the_budget_is_refused_off_a_terminal(
    api, credentials, config_home, clock, ids, monkeypatch, state_home, capsys
):
    budget(config_home, "0.02")
    posted_earlier(api, "first", "1")
    assert x_post.main(["--at", "2026-11-02 09:00", "november"]) == 0
    typed(monkeypatch, "")
    capsys.readouterr()
    assert x_post.main(["--move", "0001", "2026-10-30 09:00"]) == 1
    expected = "x-post: this would bring October 2026 to $0.030, past your $0.02 budget; not moved\n"
    assert capsys.readouterr().err == expected
    assert queued(state_home)[0]["due"] == "2026-11-02T09:00"


def test_moving_a_post_within_its_month_isnt_asked_about_the_budget(
    api, credentials, config_home, clock, ids, monkeypatch, state_home
):
    budget(config_home, "0.01")
    typed_at_a_terminal(monkeypatch, "y\n")  # past it already, by a yes
    assert x_post.main(["--at", "2026-11-02 09:00", "november"]) == 0
    typed(monkeypatch, "")
    assert x_post.main(["--move", "0001", "2026-11-05 09:00"]) == 0
    assert queued(state_home)[0]["due"] == "2026-11-05T09:00"


def test_revise_refuses_a_budget_that_isnt_a_number_before_the_editor(
    api, credentials, config_home, clock, ids, monkeypatch, tmp_path, capsys
):
    assert x_post.main(["--at", "+1h", "hello"]) == 0
    path = budget(config_home, '"lots"')
    opened = editor(tmp_path, monkeypatch, writes="hello there\n")
    capsys.readouterr()
    assert x_post.main(["--revise", "0001"]) == 1
    assert capsys.readouterr().err == f"x-post: budget in {path} isn't a number of dollars: 'lots'\n"
    assert not opened.exists()


def test_a_budget_that_isnt_a_number_is_refused_before_anything(api, credentials, config_home, monkeypatch, capsys):
    path = budget(config_home, '"lots"')
    typed(monkeypatch, "")
    assert x_post.main(["hello"]) == 1
    assert capsys.readouterr().err == f"x-post: budget in {path} isn't a number of dollars: 'lots'\n"
    assert not api.calls


def test_refused_posts_and_uploads_cost_nothing(api, credentials, tmp_path, capsys):
    uploaded(api, "m1")
    api.post(API_URL, status=403, json={"title": "Forbidden", "detail": "Duplicate content."})
    assert x_post.main(["-i", image(tmp_path), "again"]) == 1
    capsys.readouterr()
    assert x_post.main(["--spent"]) == 0
    assert capsys.readouterr().out == "nothing recorded yet\n"


def test_history_lists_posts_newest_first_with_their_urls_without_credentials(
    api, credentials, clock, monkeypatch, capsys
):
    typed(monkeypatch, "y\n")
    posted_earlier(api, "see example.com", "1")
    clock["now"] = datetime(2026, 10, 23, 9, 15)
    posted_earlier(api, "a long one that goes on well past where the list cuts it", "2")
    calls = len(api.calls)
    credentials.unlink()
    capsys.readouterr()
    assert x_post.main(["--history"]) == 0
    assert capsys.readouterr().out == (
        "Fri 2026-10-23 09:15  https://x.com/i/status/2  'a long one that goes on well past where the...'\n"
        "Fri 2026-10-23 08:30  https://x.com/i/status/1  'see example.com'\n"
    )
    assert len(api.calls) == calls


def test_history_marks_deleted_posts(api, credentials, clock, monkeypatch, capsys):
    posted_earlier(api, "first", "1")
    posted_earlier(api, "typo", "2")
    deleted(api, "2")
    typed(monkeypatch, "y\n")
    assert x_post.main(["--undo"]) == 0
    capsys.readouterr()
    assert x_post.main(["--history"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Fri 2026-10-23 08:30  https://x.com/i/status/2  'typo' (deleted)",
        "Fri 2026-10-23 08:30  https://x.com/i/status/1  'first'",
    ]


def test_history_shows_a_thread_as_one_line_and_counts_it_as_one(
    api, credentials, clock, monkeypatch, tmp_path, capsys
):
    posted_earlier(api, "before", "1")
    for post_id in "234":
        created(api, post_id)
    editor(tmp_path, monkeypatch, writes="first\n---\nsecond\n---\nthird\n")
    assert x_post.main(["-e"]) == 0
    clock["now"] = datetime(2026, 10, 23, 9, 15)
    posted_earlier(api, "after", "5")
    capsys.readouterr()
    assert x_post.main(["--history", "2"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Fri 2026-10-23 09:15  https://x.com/i/status/5  'after'",
        "Fri 2026-10-23 08:30  https://x.com/i/status/2  'first' and 2 more posts",
    ]


def test_history_marks_a_thread_partly_then_wholly_deleted(api, credentials, clock, monkeypatch, tmp_path, capsys):
    created(api, "1")
    created(api, "2")
    editor(tmp_path, monkeypatch, writes="first\n---\nsecond\n")
    assert x_post.main(["-e"]) == 0
    deleted(api, "2")
    typed(monkeypatch, "y\n")
    assert x_post.main(["--delete", "2"]) == 0
    capsys.readouterr()
    assert x_post.main(["--history"]) == 0
    assert capsys.readouterr().out.endswith("'first' and 1 more post (1 of 2 deleted)\n")
    deleted(api, "1")
    typed(monkeypatch, "y\n")
    assert x_post.main(["--undo"]) == 0
    capsys.readouterr()
    assert x_post.main(["--history"]) == 0
    assert capsys.readouterr().out.endswith("'first' and 1 more post (deleted)\n")


def test_history_keeps_a_thread_carried_on_from_the_draft_as_one(
    api, credentials, clock, monkeypatch, tmp_path, capsys
):
    created(api, "1")
    api.post(API_URL, status=503, body="upstream unavailable")
    editor(tmp_path, monkeypatch, writes="first\n---\nsecond\n---\nthird\n")
    assert x_post.main(["-e"]) == 1
    api.remove(responses.POST, API_URL)
    created(api, "2")
    created(api, "3")
    editor(tmp_path, monkeypatch)  # save the kept draft as it is
    assert x_post.main(["-e"]) == 0
    capsys.readouterr()
    assert x_post.main(["--history"]) == 0
    assert capsys.readouterr().out == "Fri 2026-10-23 08:30  https://x.com/i/status/1  'first' and 2 more posts\n"


def test_history_shows_the_last_10_unless_told(api, credentials, clock, capsys):
    for number in range(1, 13):
        posted_earlier(api, f"post {number}", str(number))
    capsys.readouterr()
    assert x_post.main(["--history"]) == 0
    shown = capsys.readouterr().out.splitlines()
    assert (len(shown), shown[0][-9:], shown[-1][-9:]) == (10, "'post 12'", " 'post 3'")
    assert x_post.main(["--history", "3"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 3


@pytest.mark.parametrize("count", ["0", "-2", "ten"])
def test_history_takes_only_a_positive_count(api, capsys, count):
    with pytest.raises(SystemExit) as exit_info:
        x_post.main(["--history", count])
    assert exit_info.value.code == 2
    assert f"not a number of posts: {count!r}" in capsys.readouterr().err


def test_history_says_when_there_is_none(api, capsys):
    assert x_post.main(["--history"]) == 0
    assert capsys.readouterr().out == "no posts recorded yet\n"


@pytest.mark.parametrize(
    "target",
    ["https://x.com/i/status/1", "https://x.com/someone/status/1?s=20", "1"],
    ids=["url", "url-with-handle", "id"],
)
def test_delete_takes_down_an_earlier_post_after_asking(api, credentials, clock, monkeypatch, capsys, target):
    posted_earlier(api, "first", "1")
    posted_earlier(api, "second", "2")
    capsys.readouterr()
    deleted(api, "1")
    typed(monkeypatch, "y\n")
    assert x_post.main(["--delete", target]) == 0
    out = capsys.readouterr().out
    assert "post: 'first'" in out and "delete https://x.com/i/status/1? costs $0.010" in out
    assert out.endswith("deleted https://x.com/i/status/1\n")
    assert deletes(api) == ["1"]
    assert x_post.main(["--history"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Fri 2026-10-23 08:30  https://x.com/i/status/2  'second'",
        "Fri 2026-10-23 08:30  https://x.com/i/status/1  'first' (deleted)",
    ]


@pytest.mark.parametrize("answer", ["n\n", ""], ids=["no", "no-terminal"])
def test_delete_without_a_yes_deletes_nothing(api, credentials, monkeypatch, answer):
    posted_earlier(api, "first", "1")
    typed(monkeypatch, answer)
    assert x_post.main(["--delete", "1"]) == 1
    assert deletes(api) == []


def test_delete_refuses_a_post_already_deleted(api, credentials, monkeypatch, capsys):
    posted_earlier(api, "first", "1")
    deleted(api, "1")
    typed(monkeypatch, "y\n")
    assert x_post.main(["--delete", "1"]) == 0
    assert x_post.main(["--delete", "1"]) == 1
    assert "x-post: https://x.com/i/status/1 is already deleted" in capsys.readouterr().err
    assert deletes(api) == ["1"]


def test_delete_of_a_post_from_before_the_log_says_so_and_asks(api, credentials, monkeypatch, capsys):
    deleted(api, "999")
    typed(monkeypatch, "y\n")
    assert x_post.main(["--delete", "https://x.com/i/status/999"]) == 0
    assert "post: not in x-post's log, so its text can't be shown" in capsys.readouterr().out
    assert deletes(api) == ["999"]


def test_a_deleted_post_leaves_the_undo_record(api, credentials, monkeypatch, tmp_path, capsys):
    thread_posted_earlier(api, monkeypatch, tmp_path)
    deleted(api, "3")
    typed(monkeypatch, "y\n")
    assert x_post.main(["--delete", "3"]) == 0
    capsys.readouterr()
    deleted(api, "2")
    deleted(api, "1")
    typed(monkeypatch, "y\n")
    assert x_post.main(["--undo"]) == 0
    assert "last post: a thread of 2, starting 'one'" in capsys.readouterr().out
    assert deletes(api) == ["3", "2", "1"]


@pytest.mark.parametrize("target", ["example.com/status/1", "https://x.com/i/status/", "12ab"])
def test_delete_needs_a_post_url_or_id(api, credentials, capsys, target):
    assert x_post.main(["--delete", target]) == 1
    assert f"can't read {target!r} as a post; give its URL or id (x-post --history lists them)" in capsys.readouterr().err
    assert not api.calls


def test_prompt_has_line_editing(api, credentials, monkeypatch):
    # input() only gets arrow keys and history once readline is imported.
    monkeypatch.delitem(sys.modules, "readline")
    created(api)
    typed(monkeypatch, "hello\n")
    x_post.main([])
    assert "readline" in sys.modules


def test_api_error_reports_status_and_detail(api, credentials, capsys):
    api.post(
        API_URL,
        status=403,
        json={
            "title": "Forbidden",
            "status": 403,
            "type": "about:blank",
            "detail": "You are not allowed to create a Tweet with duplicate content.",
        },
    )
    assert x_post.main(["hello"]) == 1
    assert capsys.readouterr().err == (
        "x-post: 403 Forbidden: You are not allowed to create a Tweet with duplicate content.\n"
    )


def test_non_json_error_reports_raw_body(api, credentials, capsys):
    api.post(API_URL, status=503, body="upstream unavailable")
    assert x_post.main(["hello"]) == 1
    assert capsys.readouterr().err == "x-post: 503 Service Unavailable: upstream unavailable\n"


def test_network_failure_is_reported_without_traceback(api, credentials, capsys):
    api.post(API_URL, body=requests.ConnectionError("connection refused"))
    assert x_post.main(["hello"]) == 1
    assert capsys.readouterr().err == "x-post: could not reach X: connection refused\n"


def test_request_has_a_timeout_so_it_cannot_hang(api, credentials):
    created(api)
    x_post.main(["hello"])
    assert api.calls[0].request.req_kwargs["timeout"] is not None
