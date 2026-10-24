# Images, GIFs and video

Add `-i PATH` once for each file:

```
x-post -i ~/photo.jpg -i ~/scan.png 'the view from here'
```

| Kind | Formats | Per post | Limit |
| --- | --- | --- | --- |
| Images | JPEG, PNG, WebP | up to 4 | 5 MB each |
| GIF | GIF | one, on its own | 15 MB |
| Video | MP4, MOV | one, on its own | 512 MB, 0.5 seconds to 20 minutes |

X doesn't mix kinds in one post. With an image, the post's text can be empty.

Every file is checked before the prompt and before anything is sent: it must exist, be under its limit, and really be that format, judged by its bytes, not its name. Nothing uploads until any [link question](checks.md#links) is answered, and `--check` lists the files too.

Uploading is free, and an image post bills as a plain post ($0.015). X doesn't document that; it's what the balance has shown.

## Files on another machine

A `host:path`, scp style, is copied over ssh into a temporary folder that's deleted as soon as it's read:

```
x-post -i pc:Pictures/photo.jpg 'caption'
x-post -i "pc:C:/Users/you/Desktop/screen shot.png" 'caption'
```

- Paths are relative to the remote home unless they start with a drive letter. Forward slashes work on Windows.
- Any host you can ssh to without a password works. The copy never prompts, and gives up after 10 seconds if the host doesn't answer.
- A one-letter "host" (`C:/x.png`) or a `/` before the colon (`./odd:name.png`) keeps the path local.

## The newest screenshot

`-s` attaches the newest image (PNG, JPEG or WebP) in a screenshots folder on a Windows machine you reach over ssh. It prints the file's name first, so you can see which one it picked:

```
x-post -s 'look at this'
screenshot: Screenshot 2026-10-23 065138.png
```

Name the machine and folder in `~/.config/x-post/config.toml`, in `host:path` form:

```toml
screenshots = "pc:Pictures/Screenshots"
```

Without the setting, `-s` says what to add before doing anything else. It lists the folder with `dir`, so the machine's ssh shell must be `cmd.exe`.

## Alt text

`--alt TEXT` gives alt text, which screen readers read out, to each image in order: the `-i` images first, then the `-s` screenshot.

```
x-post -i cat.png -i dog.png --alt '' --alt 'a beagle asleep on a keyboard' 'pets'
x-post -s --alt 'the X developer console, keys tab' 'found it'
```

- `--alt ''` skips an image, and leaving images off the end is fine.
- Each may be up to 1000 characters, checked before anything is sent, and costs $0.005.
- It works on GIFs and video too. X doesn't document that, but it took it in live tests, and if it ever refuses, nothing is posted.

## Images on any post of a thread

`-i` and `-s` images go with a thread's first post. In `x-post -e`, any post can end with `image:` lines attaching images to it, each optionally followed by an `alt:` line:

```
the mesh, first pass
image: ~/renders/mesh.png
alt: a low-poly boat in wireframe
---
textured
image: pc:Pictures/Screenshots/boat.png
```

- Any path `-i` takes works, `~/…` and `host:…` included.
- Each post keeps to the limits above, and a post can be just images. `-i` and `-s` images come first in the first post.
- Only lines at the end of a post count, so a line of text that happens to start with "image:" stays text. The lines aren't counted toward length, spell-checked or priced as links.
- A poll's post can't have `image:` lines.
- Every image is read before any question and uploaded before the first post goes out. If the thread stops partway, the unsent posts keep their `image:` lines in the draft, so those images upload again, and their alt text bills again, when you carry on.
- With `--at`, each post's images are copied into the queue with it.

## GIF and video details

- A GIF or video uploads in 4 MB chunks, and then X processes it. x-post prints `x-post: waiting for X to process clip.mp4` and waits up to 10 minutes. If X fails to process it, or is still at it after that, nothing is posted. H.264 video with AAC audio is the safe choice.
- A video's length is read from its header before anything uploads, so one outside 0.5 seconds to 20 minutes is refused at once: `clip.mp4 runs 21:04; X takes video from 0.5 s to 20 minutes`. A fragmented MP4, whose header gives no length, is left for X to judge.
- X allows video up to 8 GB. x-post stops at 512 MB because it holds the file in memory, and `--at` copies it into the queue.
- An iPhone HEIC photo looks like an MP4 from its header, so it's refused rather than sent as video.
- Dimensions aren't checked. X's docs list 1280×1024 as the largest without Premium, but a 1920×1080 clip posted fine from an account without it.
- Premium accounts may post video up to 125 minutes, which the length check doesn't know about.
