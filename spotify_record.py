#!/usr/bin/env python3
"""Record a Spotify track to MP3 by capturing its audio through PipeWire/PulseAudio.

Usage:
    python spotify_record.py https://open.spotify.com/track/<id> [more links...]

How it works:
  1. Track title, artist, length and cover art are read from Spotify's public embed page.
  2. A virtual sink ("SpotifySink") is created so only the browser's audio gets recorded,
     not notifications or other apps. A loopback lets you still hear it (--mute to disable).
  3. Firefox is started with its own profile (./firefox_profile) and its audio is routed
     into the virtual sink. Log in to Spotify once there; the login is kept.
  4. You press play. Recording starts automatically when sound is detected and stops once
     the track's length has been captured.
  5. The audio is encoded to MP3 with ID3 tags and cover art.

Requirements: firefox, ffmpeg (with libmp3lame), pactl and parec (pipewire-pulse / libpulse).
"""

import argparse
import array
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

SINK_NAME = "SpotifySink"
RATE = 44100
CHANNELS = 2
BYTES_PER_SECOND = RATE * CHANNELS * 2  # s16le
CHUNK_BYTES = BYTES_PER_SECOND // 10  # 100 ms
SILENCE_PEAK = 40  # out of 32767; anything above counts as sound
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
PROFILE_DIR = Path(__file__).resolve().parent / "firefox_profile"


def fetch_track_info(url):
    match = re.search(r"track[/:]([A-Za-z0-9]{22})", url)
    if not match:
        sys.exit(f"Not a Spotify track link: {url}")
    track_id = match.group(1)

    request = urllib.request.Request(
        f"https://open.spotify.com/embed/track/{track_id}",
        headers={"User-Agent": USER_AGENT},
    )
    html = urllib.request.urlopen(request, timeout=20).read().decode("utf-8")
    data = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not data:
        sys.exit("Could not read track info from Spotify (page layout may have changed).")
    entity = json.loads(data.group(1))["props"]["pageProps"]["state"]["data"]["entity"]

    images = entity.get("visualIdentity", {}).get("image", [])
    cover = max(images, key=lambda i: i.get("maxWidth", 0))["url"] if images else None
    return {
        "id": track_id,
        "url": f"https://open.spotify.com/track/{track_id}",
        "title": entity["name"],
        "artist": ", ".join(a["name"] for a in entity.get("artists", [])),
        "year": entity.get("releaseDate", {}).get("isoString", "")[:4],
        "duration": entity["duration"] / 1000,
        "cover": cover,
    }


def pactl(*args):
    return subprocess.run(["pactl", *args], check=True, capture_output=True, text=True).stdout.strip()


def setup_audio(listen):
    """Create the virtual sink (and optional loopback). Returns module ids to unload later."""
    modules = []
    sinks = pactl("list", "short", "sinks").split("\n")
    if not any(line.split("\t")[1:2] == [SINK_NAME] for line in sinks if line):
        modules.append(pactl(
            "load-module", "module-null-sink", f"sink_name={SINK_NAME}",
            f"rate={RATE}", f"channels={CHANNELS}",
            "sink_properties=device.description=SpotifyRecorder",
        ))
    if listen:
        default_sink = pactl("get-default-sink")
        modules.append(pactl(
            "load-module", "module-loopback", f"source={SINK_NAME}.monitor",
            f"sink={default_sink}", "latency_msec=50",
        ))
    pactl("set-sink-volume", SINK_NAME, "100%")
    pactl("set-sink-mute", SINK_NAME, "0")
    return modules


def process_tree(root_pid):
    """PIDs of root_pid and all of its descendants."""
    children = {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        children.setdefault(int(fields[1]), []).append(int(stat.parent.name))
    pids, stack = set(), [root_pid]
    while stack:
        pid = stack.pop()
        pids.add(pid)
        stack.extend(children.get(pid, []))
    return pids


def route_browser_audio(browser_pid):
    """Move any audio stream from our Firefox into the virtual sink (in case PULSE_SINK was ignored)."""
    pids = process_tree(browser_pid)
    for stream in json.loads(pactl("-f", "json", "list", "sink-inputs") or "[]"):
        pid = stream.get("properties", {}).get("application.process.id")
        if pid and int(pid) in pids:
            subprocess.run(["pactl", "move-sink-input", str(stream["index"]), SINK_NAME],
                           capture_output=True)


def start_browser(url):
    PROFILE_DIR.mkdir(exist_ok=True)
    user_js = PROFILE_DIR / "user.js"
    if not user_js.exists():
        user_js.write_text(
            'user_pref("media.eme.enabled", true);\n'  # DRM, needed by Spotify
            'user_pref("media.gmp-widevinecdm.enabled", true);\n'
            'user_pref("media.gmp-widevinecdm.visible", true);\n'
            'user_pref("media.autoplay.default", 0);\n'
            'user_pref("browser.shell.checkDefaultBrowser", false);\n'
        )
    env = dict(os.environ, PULSE_SINK=SINK_NAME)
    return subprocess.Popen(
        ["firefox", "--new-instance", "--profile", str(PROFILE_DIR), url],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def open_in_browser(url):
    # Hands the URL to the already running Firefox that uses this profile.
    subprocess.run(["firefox", "--profile", str(PROFILE_DIR), url],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def pause_browser(browser_pid):
    subprocess.run(["playerctl", "-p", f"firefox.instance_{browser_pid}", "pause"], capture_output=True)


def peak(chunk):
    samples = array.array("h", chunk[: len(chunk) - len(chunk) % 2])
    return max(map(abs, samples), default=0)


def record(info, browser_pid, raw_path, wait_timeout):
    """Wait for sound on the virtual sink, then capture the track's length of audio."""
    parec = subprocess.Popen(
        ["parec", "-d", f"{SINK_NAME}.monitor", "--format=s16le", f"--rate={RATE}",
         f"--channels={CHANNELS}", "--latency-msec=100"],
        stdout=subprocess.PIPE,
    )
    target_bytes = int(info["duration"] * BYTES_PER_SECOND) // 4 * 4
    written = 0
    try:
        print(f"Press play in the Firefox window. Waiting for audio (timeout {wait_timeout}s)...")
        deadline = time.monotonic() + wait_timeout
        last_route = 0
        while True:
            chunk = parec.stdout.read(CHUNK_BYTES)
            if not chunk:
                raise RuntimeError("parec stopped unexpectedly")
            if peak(chunk) > SILENCE_PEAK:
                break
            if time.monotonic() - last_route > 1:
                route_browser_audio(browser_pid)
                last_route = time.monotonic()
            if time.monotonic() > deadline:
                raise TimeoutError("No audio detected. Did playback start?")

        print("Recording...")
        with open(raw_path, "wb") as out:
            while written < target_bytes:
                chunk = chunk[: target_bytes - written]
                out.write(chunk)
                written += len(chunk)
                done = written / BYTES_PER_SECOND
                print(f"\r  {fmt_time(done)} / {fmt_time(info['duration'])}", end="", flush=True)
                if written < target_bytes:
                    chunk = parec.stdout.read(CHUNK_BYTES)
                    if not chunk:
                        raise RuntimeError("parec stopped unexpectedly")
        print()
    finally:
        parec.terminate()
        parec.wait()


def encode_mp3(info, raw_path, out_path, bitrate):
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "s16le", "-ar", str(RATE), "-ac", str(CHANNELS), "-i", str(raw_path)]
    cover_path = None
    if info["cover"]:
        try:
            cover_path = raw_path.with_suffix(".jpg")
            request = urllib.request.Request(info["cover"], headers={"User-Agent": USER_AGENT})
            cover_path.write_bytes(urllib.request.urlopen(request, timeout=20).read())
            cmd += ["-i", str(cover_path), "-map", "0:a", "-map", "1:v", "-c:v", "copy",
                    "-disposition:v", "attached_pic",
                    "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
        except OSError:
            cover_path = None
            print("Could not download cover art, continuing without it.")
    cmd += [
        # Trim silence left at the end (e.g. songs that fade out before their listed length).
        "-af", "areverse,silenceremove=start_periods=1:start_threshold=-60dB,areverse",
        "-c:a", "libmp3lame", "-b:a", bitrate, "-id3v2_version", "3",
        "-metadata", f"title={info['title']}",
        "-metadata", f"artist={info['artist']}",
        "-metadata", f"date={info['year']}",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)


def fmt_time(seconds):
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def safe_filename(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "track"


def main():
    parser = argparse.ArgumentParser(description="Record Spotify tracks to MP3 via system audio capture.")
    parser.add_argument("links", nargs="+", help="Spotify track link(s)")
    parser.add_argument("-o", "--output-dir", default="songs", help="where to save MP3s (default: ./songs)")
    parser.add_argument("-b", "--bitrate", default="320k", help="MP3 bitrate (default: 320k)")
    parser.add_argument("--mute", action="store_true", help="don't play the audio through your speakers")
    parser.add_argument("--wait", type=int, default=300, help="seconds to wait for you to press play")
    args = parser.parse_args()

    for tool in ("firefox", "ffmpeg", "pactl", "parec"):
        if not shutil.which(tool):
            sys.exit(f"Missing required program: {tool}")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tracks = [fetch_track_info(link) for link in args.links]

    modules = setup_audio(listen=not args.mute)
    browser = None
    # Make Ctrl+C still run the cleanup below.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for n, info in enumerate(tracks, 1):
                print(f"\n[{n}/{len(tracks)}] {info['artist']} - {info['title']} ({fmt_time(info['duration'])})")
                if browser is None:
                    browser = start_browser(info["url"])
                else:
                    open_in_browser(info["url"])

                raw_path = Path(tmp) / f"{info['id']}.raw"
                record(info, browser.pid, raw_path, args.wait)
                pause_browser(browser.pid)

                out_path = out_dir / f"{safe_filename(info['artist'] + ' - ' + info['title'])}.mp3"
                encode_mp3(info, raw_path, out_path, args.bitrate)
                print(f"Saved {out_path}")
    except (TimeoutError, RuntimeError) as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
    finally:
        if browser and browser.poll() is None:
            browser.terminate()
        for module in reversed(modules):
            subprocess.run(["pactl", "unload-module", module], capture_output=True)


if __name__ == "__main__":
    main()
