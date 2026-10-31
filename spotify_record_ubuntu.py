import os
import re
import time
import subprocess
import spotipy
from spotipy.oauth2 import SpotifyOAuth

# 1. Configure Spotify API Credentials
os.environ["SPOTIPY_CLIENT_ID"] = "YOUR_CLIENT_ID"
os.environ["SPOTIPY_CLIENT_SECRET"] = "YOUR_CLIENT_SECRET"
os.environ["SPOTIPY_REDIRECT_URI"] = "http://localhost:8888/callback"

SCOPE = "user-read-playback-state user-modify-playback-state"
DEVICE_NAME = "PiRecorder"
PULSE_MONITOR = "virtual_speaker.monitor"


def get_device_id(sp, target_name):
    """Find the headless Raspberry Pi Spotify Connect device ID."""
    devices = sp.devices().get("devices", [])
    for d in devices:
        if d["name"] == target_name:
            return d["id"]
    raise RuntimeError(f"Device '{target_name}' not found. Ensure librespot is running.")


def record_spotify_track(track_url, output_dir="."):
    # Authenticate (use open_browser=False on headless Pi; copy-paste the auth URL once)
    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(scope=SCOPE, open_browser=False))

    # Fetch track metadata & duration
    track = sp.track(track_url)
    track_uri = track["uri"]
    track_name = track["name"]
    artist_name = track["artists"][0]["name"]
    duration_sec = (track["duration_ms"] / 1000.0) + 1.5  # Add 1.5s buffer

    safe_filename = re.sub(r'[\\/*?:"<>|]', "", f"{artist_name} - {track_name}.mp3")
    output_path = os.path.join(output_dir, safe_filename)

    device_id = get_device_id(sp, DEVICE_NAME)

    # Prepare FFmpeg system-level capture command
    ffmpeg_cmd = [
        "ffmpeg",
        "-y",                          # Overwrite output file if it exists
        "-f", "pulse",                 # Use PulseAudio capture
        "-i", PULSE_MONITOR,           # Record from virtual sink monitor
        "-t", f"{duration_sec:.2f}",   # Stop after track duration
        "-acodec", "libmp3lame",       # Encode to MP3
        "-b:a", "320k",                # 320 kbps bitrate
        "-metadata", f"title={track_name}",
        "-metadata", f"artist={artist_name}",
        output_path
    ]

    print(f"Starting playback and recording: {artist_name} - {track_name} ({duration_sec:.1f}s)")

    # Start FFmpeg recording process
    recorder = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    # Brief pause to let FFmpeg initialize before audio starts
    time.sleep(0.5)
    sp.start_playback(device_id=device_id, uris=[track_uri])

    # Wait for FFmpeg to finish recording the track duration
    _, stderr = recorder.communicate()

    if recorder.returncode == 0:
        print(f"Saved successfully to: {output_path}")
    else:
        print(f"FFmpeg error:\n{stderr.decode('utf-8', errors='ignore')}")


if __name__ == "__main__":
    song_link = input("Enter Spotify Track URL: ").strip()
    record_spotify_track(song_link)


