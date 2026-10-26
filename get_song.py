import subprocess
from playwright.sync_api import sync_playwright

def record_track(track_url, duration_seconds, output_file):
    with sync_playwright() as p:
        # Launch Chromium with a persistent session to bypass login screens
        browser = p.chromium.launch_persistent_context(
            user_data_dir="./spotify_session",
            headless=True
        )
        page = browser.new_page()
        
        # Navigate to the track URL
        page.goto(track_url)
        
        # Wait for the play button to load and click it
        play_button = page.locator('button[data-testid="play-button"]').first
        play_button.wait_for()
        play_button.click()
        
        print(f"Recording started: {output_file}")
        
        # Capture the virtual sink output using FFmpeg
        # -f pulse: uses the PulseAudio/PipeWire subsystem
        # -i SpotifySink.monitor: targets the output of our virtual sink
        # -t: stops recording automatically after the specified duration
        ffmpeg_cmd = [
            "ffmpeg", 
            "-y",  # Overwrite output file if it exists
            "-f", "pulse", 
            "-i", "SpotifySink.monitor", 
            "-t", str(duration_seconds), 
            output_file
        ]
        
        # Run FFmpeg silently
        process = subprocess.Popen(
            ffmpeg_cmd, 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL
        )
        
        # The script pauses here while FFmpeg records in real-time
        process.wait()
        
        print("Recording complete.")
        browser.close()

# Example usage: 
# record_track("https://open.spotify.com/track/...", 215, "recorded_track.wav")