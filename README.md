## Prerequisites

This project requires certain system-level audio and video processing tools. If you are on an Ubuntu/Debian-based system, install them by running:

```bash
sudo apt update
sudo apt install -y pulseaudio pulseaudio-utils ffmpeg python3-pip
```


## Setup
```
pulseaudio --start
pactl load-module module-null-sink sink_name=virtual_speaker sink_properties=device.description="Virtual_Speaker"
pactl set-default-sink virtual_speaker
```

```
# Install librespot (or cargo install librespot) and run:
librespot --name "PiRecorder" --backend pulseaudio &
```



