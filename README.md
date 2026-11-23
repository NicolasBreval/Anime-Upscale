# Anime Upscale

A Python CLI pipeline for upscaling anime and video files using `realesrgan-ncnn-vulkan` and `FFmpeg`[cite: 4]. It combines consecutive-frame deduplication, a global cross-video frame cache with configurable eviction penalties, per-video log files, aspect-ratio/resolution control, optional NVENC encoding with automatic CPU fallback, and live progress/elapsed time tracking[cite: 4].

---

## How It Works

For each input video (either a single file or root-level video files inside an input directory), the script runs a four-stage pipeline:

1. **Frame & Audio Extraction (`1/4 Extracting frames`):** Uses `FFmpeg` to extract frames into a temporary working directory (optionally capped at `--max-frames`) and extracts the audio stream if present.
2. **Cache Lookup & Deduplication (`2/4 Cache & dedup`):** Computes an MD5 hash for each frame. Consecutive identical frames are deduplicated locally, while unique frames are checked against a shared temporary global cache keyed by hash, model, scale, and intermediate format (`--ia-format`). It reports how many cache hits, consecutive duplicates, and total AI renders were saved.
3. **AI Upscaling (`3/4 AI Real-ESRGAN`):** Sends only uncached frames to `realesrgan-ncnn-vulkan` using the selected `--model`, `--scale`, `--ia-format`, and `--tile-size`, then stores new results in the global cache.
4. **Video Assembly (`4/4 Muxing video`):** Reconstructs the full frame sequence using the actual AI output extension (`.png` or `.jpg`), applies target height and aspect-ratio filters (`--height`, `--aspect`, `--ar-mode`), muxes audio if available, and encodes the output video using `--encoder` (`cpu` with `libx264` `-preset fast`, or `nvenc` with automatic fallback to `cpu` if NVENC fails).

After each video finishes, cache entries not seen in the last `--cache-penalty` videos are evicted to keep temporary disk usage under control[cite: 4].

---

## Installing Anime Upscale from GitHub

You can install `anime-upscale` directly from the repository (`https://github.com/NicolasBreval/Anime-Upscale`) using `pipx`, `pip`, or by cloning the repository locally[cite: 4].

### Recommended: Install with `pipx`
```bash
pipx install "git+[https://github.com/NicolasBreval/Anime-Upscale.git](https://github.com/NicolasBreval/Anime-Upscale.git)"
```

### Install with `pip`
```bash
pip install "git+[https://github.com/NicolasBreval/Anime-Upscale.git](https://github.com/NicolasBreval/Anime-Upscale.git)"
```

### Clone and Install Locally (Editable Mode for Development)
```bash
git clone [https://github.com/NicolasBreval/Anime-Upscale.git](https://github.com/NicolasBreval/Anime-Upscale.git)
cd Anime-Upscale
pip install --upgrade pip setuptools wheel
pip install --no-build-isolation --editable .
```

---

## External Dependencies & Installation

### System / CLI Tools Required in `PATH`
* **FFmpeg (`ffmpeg` and `ffprobe`):** Used for metadata inspection, frame/audio extraction, scaling/aspect-ratio filtering, and final video encoding[cite: 4]
* **`realesrgan-ncnn-vulkan`:** Vulkan-based Real-ESRGAN binary used for AI frame upscaling (defaults to `realesr-animevideov3`)[cite: 4]

### Python Dependencies
* **Python 3.9+** (standard library plus `tqdm` declared in package dependencies)[cite: 4]
* **`tqdm`:** Enhances progress bars in the terminal; if unavailable, the CLI falls back to a built-in text progress bar[cite: 4]

---

### Installation on Linux

#### Arch / Manjaro
```bash
sudo pacman -Syu ffmpeg vulkan-icd-loader
# Optional for progress bars:
sudo pacman -S python-tqdm
# Optional for Vulkan diagnostics:
sudo pacman -S vulkan-tools
```
Install `realesrgan-ncnn-vulkan` from the AUR (for example with `yay` or `paru`) or download a release binary from the official `realesrgan-ncnn-vulkan` repository and place it in `/usr/local/bin` (or another directory in your `PATH`)[cite: 4]:
```bash
yay -S realesrgan-ncnn-vulkan-bin
```

#### Debian / Ubuntu
```bash
sudo apt update
sudo apt install -y ffmpeg libvulkan1 python3-tqdm
```
Download the Linux release archive of `realesrgan-ncnn-vulkan`, extract it, ensure `realesrgan-ncnn-vulkan` and its `models/` directory are accessible (or put the binary in `/usr/local/bin`), and mark it executable[cite: 4]:
```bash
chmod +x /usr/local/bin/realesrgan-ncnn-vulkan
```

---

### Installation on Windows

1. **Install Python:** Download and install Python 3 from [python.org](https://www.python.org/) (check **Add python.exe to PATH** during installation)[cite: 4].
2. **Install `tqdm` (optional):**
   ```powershell
   pip install tqdm
   ```
3. **Install FFmpeg:** Install via `winget` or Chocolatey, or download a release build and add its `bin` folder (containing `ffmpeg.exe` and `ffprobe.exe`) to your Windows `PATH`[cite: 4]:
   ```powershell
   winget install Gyan.FFmpeg
   ```
4. **Install `realesrgan-ncnn-vulkan`:** Download the Windows `.zip` release of `realesrgan-ncnn-vulkan`, extract it, and add the folder containing `realesrgan-ncnn-vulkan.exe` to your Windows `PATH`[cite: 4].

---

### Installation on macOS

Using [Homebrew](https://brew.sh/):
```bash
brew install ffmpeg tqdm
```
Download the macOS release of `realesrgan-ncnn-vulkan`, extract the binary, move it to a directory in your `PATH` (such as `/usr/local/bin`), and allow execution[cite: 4]:
```bash
chmod +x /usr/local/bin/realesrgan-ncnn-vulkan
```

---

## Usage

### Check External Dependencies Only
```bash
anime-upscale --check-deps
```

### Upscale a Single Video File
```bash
anime-upscale \
  -i "/path/to/episode_01.mkv" \
  -o "/path/to/output/episode_01_1080p.mp4" \
  -s 2 \
  --height 1080 \
  --aspect 16:9 \
  --ar-mode stretch \
  --tile-size 256 \
  --ia-format jpg \
  --temp-dir "/fast/nvme/tmp"
```

### Batch Process a Directory of Videos (Non-Recursive Root)
```bash
anime-upscale \
  -i "/path/to/input_videos" \
  -o "/path/to/output_videos" \
  -s 2 \
  --height 1080 \
  --tile-size 256 \
  --ia-format jpg \
  --cache-penalty 3 \
  --temp-dir "/fast/nvme/tmp"
```

### Quick Test Run on the First 300 Frames
```bash
anime-upscale \
  -i "/path/to/episode_01.mkv" \
  -o "/path/to/test_out.mp4" \
  --max-frames 300 \
  --tile-size 256 \
  --ia-format jpg
```

---

## CLI Arguments Reference

| Argument | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `-i`, `--input` | `str` | *Required* (unless `--check-deps`) | Path to an input video file or a root directory containing videos (non-recursive when a directory is passed) |
| `-o`, `--output` | `str` | *Required* (unless `--check-deps`) | Output video file path (when `-i` is a file) or output directory path (when `-i` is a directory) |
| `--check-deps` | `bool` | `False` | Verify that `ffmpeg`, `ffprobe`, and `realesrgan-ncnn-vulkan` are available in `PATH`, print their resolved paths, and exit without processing videos |
| `-s`, `--scale` | `int` | `2` | Upscaling factor passed to `realesrgan-ncnn-vulkan` (`-s`) |
| `--height` | `int` | `1080` | Target output height in pixels applied during final assembly |
| `--aspect` | `str` | `"16:9"` | Target aspect ratio, such as `"16:9"`, `"4:3"`, `"21:9"`, a decimal width ratio (e.g., `"1.78"`), or `"keep"` / `"original"` / `"none"` / `"auto"` to preserve source proportions |
| `--ar-mode` | `str` | `"stretch"` | How target aspect ratio is applied when `--aspect` is not `"keep"`: `stretch`, `crop`, or `pad` (black bars) |
| `--model` | `str` | `"realesr-animevideov3"` | Real-ESRGAN model name (`-n`) passed to `realesrgan-ncnn-vulkan` |
| `--tile-size` | `int` | `0` | Tile size (`-t`) for `realesrgan-ncnn-vulkan`. `0` lets the binary choose automatically; positive values control VRAM usage vs. overlap overhead |
| `--ia-format` | `str` | `"png"` | Intermediate image format produced by Real-ESRGAN and stored in cache: `png` or `jpg`. `jpg` is faster and smaller on disk; `png` is lossless |
| `--encoder` | `str` | `"cpu"` | Final video encoder: `cpu` (`libx264` with `-preset fast`) or `nvenc` (`h264_nvenc` VBR/CQ with automatic fallback to `cpu` if NVENC fails) |
| `--crf` | `int` | `18` | Quality factor: `CRF` for `cpu` (`libx264`) or `CQ` for `nvenc` (`h264_nvenc`). Lower values mean higher quality and larger files |
| `--cache-penalty` | `int` | `3` | Number of subsequent videos a cached frame can go unseen before being evicted from the temporary global cache (`0` or higher) |
| `--max-frames` | `int` | `None` | Maximum number of frames to extract and process per video (useful for quick benchmarks and testing) |
| `--temp-dir` | `str` | `None` | Base directory for temporary working folders and the global cache. Defaults to the system temporary directory; pointing this to a fast SSD/NVMe is recommended |
| `--log-file` | `str` | `None` | Optional base path or directory for per-video `.log` files. By default, each video writes `<output_video_stem>.log` next to the output video file[cite: 4] |