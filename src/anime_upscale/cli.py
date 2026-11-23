#!/usr/bin/env python3
import argparse
from datetime import datetime
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

try:
  from tqdm import tqdm

  HAS_TQDM = True
except ImportError:
  HAS_TQDM = False


VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".webm",
    ".flv",
    ".wmv",
    ".m4v",
    ".ts",
}

REQUIRED_BINARIES = ("ffmpeg", "ffprobe", "realesrgan-ncnn-vulkan")
DEFAULT_CACHE_PENALTY_VIDEOS = 3


def check_required_binaries(binaries=REQUIRED_BINARIES, verbose=False):
  """Verify that all required external CLI tools are available in PATH."""
  resolved = {name: shutil.which(name) for name in binaries}
  missing = [name for name, path in resolved.items() if path is None]
  if missing:
    missing_list = ", ".join(f"'{name}'" for name in missing)
    raise RuntimeError(
        f"Missing required external tool(s) in PATH: {missing_list}. "
        "Please install them and ensure they are accessible from your PATH."
    )
  if verbose:
    print("All required external dependencies are available in PATH:")
    for name, path in resolved.items():
      print(f"  - {name}: {path}")
  return resolved


def format_duration(seconds):
  """Format seconds into a readable duration string (e.g., 12.4s, 02:15, 01:04:20)."""
  seconds = max(0.0, float(seconds))
  if seconds < 60:
    return f"{seconds:.1f}s"
  total_sec = int(round(seconds))
  hours, rem = divmod(total_sec, 3600)
  mins, secs = divmod(rem, 60)
  if hours > 0:
    return f"{hours:02d}:{mins:02d}:{secs:02d}"
  return f"{mins:02d}:{secs:02d}"


def build_video_log_path(output_file, log_base_arg=None):
  """Build an individual log file path for a specific output video."""
  out_abs = os.path.abspath(output_file)
  out_dir = os.path.dirname(out_abs)
  stem = os.path.splitext(os.path.basename(out_abs))[0]

  if not log_base_arg:
    return os.path.join(out_dir, f"{stem}.log")

  base_abs = os.path.abspath(log_base_arg)
  if log_base_arg.endswith(("/", "\\")) or os.path.isdir(base_abs):
    return os.path.join(base_abs, f"{stem}.log")

  base_dir = os.path.dirname(base_abs)
  base_name, base_ext = os.path.splitext(os.path.basename(base_abs))
  ext = base_ext if base_ext else ".log"
  return os.path.join(base_dir, f"{base_name}_{stem}{ext}")


class Logger:
  """Write detailed debug and process messages to the current video's log file."""

  def __init__(self, log_path):
    self.log_path = os.path.abspath(log_path)
    log_dir = os.path.dirname(self.log_path)
    if log_dir:
      os.makedirs(log_dir, exist_ok=True)
    self._lock = threading.Lock()
    self._fp = open(self.log_path, "a", encoding="utf-8", buffering=1)
    self.info(f"=== New session started ({datetime.now().isoformat()}) ===")

  def _write(self, level, msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    line = f"[{ts}] [{level}] {msg}\n"
    with self._lock:
      self._fp.write(line)
      self._fp.flush()

  def info(self, msg):
    self._write("INFO", msg)

  def warn(self, msg):
    self._write("WARN", msg)

  def error(self, msg):
    self._write("ERROR", msg)

  def raw(self, source, line):
    clean = line.rstrip("\r\n")
    if clean:
      self._write(source, clean)

  def close(self):
    with self._lock:
      if not self._fp.closed:
        self._write("INFO", "=== Session finished ===")
        self._fp.close()


def _drain_stream_to_log(stream, logger, source, collector=None):
  """Read a stream line by line in a thread to avoid PIPE deadlocks and log output."""
  try:
    for line in iter(stream.readline, ""):
      logger.raw(source, line)
      if collector is not None:
        collector.append(line.rstrip("\r\n"))
  finally:
    try:
      stream.close()
    except OSError:
      pass


class TimedTqdmWrapper:
  """tqdm wrapper with elapsed time tracking and total duration measurement."""

  def __init__(self, total, desc, unit="it"):
    self.start_time = time.time()
    self._bar = tqdm(
        total=max(total, 1),
        desc=desc,
        unit=unit,
        dynamic_ncols=True,
        bar_format=(
            "{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]"
        ),
    )

  def update(self, amount=1):
    self._bar.update(amount)

  def set_total(self, new_total):
    if new_total > 0:
      self._bar.total = new_total
      self._bar.refresh()

  def elapsed_seconds(self):
    return time.time() - self.start_time

  def close(self):
    self._bar.close()


class SimpleProgressBar:
  """Fallback progress bar with elapsed time counter when tqdm is not installed."""

  def __init__(self, total=100, desc="", unit="it"):
    self.total = max(total, 1)
    self.desc = desc
    self.unit = unit
    self.n = 0
    self.start_time = time.time()
    self._render()

  def update(self, amount=1):
    self.n = min(self.total, self.n + amount)
    self._render()

  def set_total(self, new_total):
    if new_total > 0:
      self.total = new_total
      self._render()

  def elapsed_seconds(self):
    return time.time() - self.start_time

  def _render(self):
    pct = (self.n / self.total) * 100
    bar_len = 25
    filled = int(bar_len * self.n / self.total)
    bar = "#" * filled + "-" * (bar_len - filled)
    elapsed_str = format_duration(self.elapsed_seconds())
    sys.stdout.write(
        f"\r{self.desc}: [{bar}] {pct:5.1f}% ({self.n}/{self.total} {self.unit}) [elapsed: {elapsed_str}]"
    )
    sys.stdout.flush()

  def close(self):
    self.n = self.total
    self._render()
    print()


def create_progress(total, desc, unit="it"):
  if HAS_TQDM:
    return TimedTqdmWrapper(total=total, desc=desc, unit=unit)
  return SimpleProgressBar(total=total, desc=desc, unit=unit)


def get_file_md5(file_path):
  """Compute the MD5 hash of a file to detect exact duplicates."""
  hash_md5 = hashlib.md5()
  with open(file_path, "rb") as f:
    for chunk in iter(lambda: f.read(65536), b""):
      hash_md5.update(chunk)
  return hash_md5.hexdigest()


def make_cache_key(md5_hash, model, scale, img_ext):
  """Build a unique cache key per frame content, AI model, scale, and format."""
  safe_model = re.sub(r"[^A-Za-z0-9_.-]", "_", model)
  return f"{md5_hash}_{safe_model}_s{scale}.{img_ext}"


def evict_stale_cache_entries(
    global_cache, current_video_idx, penalty_videos, logger
):
  """Remove global cache entries not seen within the last N videos."""
  stale_keys = [
      key
      for key, entry in global_cache.items()
      if (current_video_idx - entry["last_seen"]) >= penalty_videos
  ]
  for key in stale_keys:
    entry = global_cache.pop(key)
    cached_path = entry["path"]
    if os.path.exists(cached_path):
      try:
        os.remove(cached_path)
      except OSError as e:
        logger.warn(f"Failed to delete cache entry '{cached_path}': {e}")
  logger.info(
      f"Cache eviction on video #{current_video_idx} (penalty={penalty_videos}): "
      f"evicted={len(stale_keys)}, active={len(global_cache)}"
  )
  return len(stale_keys)


def parse_aspect_ratio(aspect_str):
  """Parse '16:9', '4:3', '21:9', or a decimal into (rel_width, rel_height)."""
  aspect_str = aspect_str.strip().lower()
  if aspect_str in ("keep", "original", "none", "auto"):
    return None
  if ":" in aspect_str:
    parts = aspect_str.split(":")
    if len(parts) != 2:
      raise ValueError(f"Invalid aspect ratio: '{aspect_str}'")
    w, h = float(parts[0]), float(parts[1])
    if w <= 0 or h <= 0:
      raise ValueError(f"Invalid aspect ratio: '{aspect_str}'")
    return (w, h)
  val = float(aspect_str)
  if val <= 0:
    raise ValueError(f"Invalid aspect ratio: '{aspect_str}'")
  return (val, 1.0)


def get_video_info(input_path, logger):
  """Retrieve fps, duration in seconds, and estimated frame count."""
  fps = "24"
  duration = 0.0
  nb_frames = 0

  cmd = [
      "ffprobe",
      "-v",
      "error",
      "-select_streams",
      "v:0",
      "-show_entries",
      "stream=r_frame_rate,nb_frames:format=duration",
      "-of",
      "default=noprint_wrappers=1",
      input_path,
  ]
  logger.info(f"Querying metadata with ffprobe: {' '.join(cmd)}")
  res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
  if res.returncode == 0:
    for line in res.stdout.splitlines():
      if line.startswith("r_frame_rate="):
        val = line.split("=", 1)[1].strip()
        if val and val != "0/0":
          fps = val
      elif line.startswith("duration="):
        try:
          duration = float(line.split("=", 1)[1].strip())
        except ValueError:
          pass
      elif line.startswith("nb_frames="):
        try:
          nb_frames = int(line.split("=", 1)[1].strip())
        except ValueError:
          pass
  else:
    logger.warn(f"ffprobe exited with code {res.returncode}: {res.stderr.strip()}")

  if nb_frames <= 0 and duration > 0:
    try:
      if "/" in fps:
        num, den = fps.split("/")
        fps_float = float(num) / float(den)
      else:
        fps_float = float(fps)
      nb_frames = int(duration * fps_float)
    except (ValueError, ZeroDivisionError):
      nb_frames = 0

  logger.info(
      f"Metadata for '{input_path}': fps={fps}, duration={duration:.2f}s, est_frames={nb_frames}"
  )
  return fps, duration, nb_frames


def build_vf_filter(target_height, aspect_ratio, ar_mode):
  """Build the FFmpeg -vf filter chain for target height and aspect ratio."""
  filters = []
  if aspect_ratio is None:
    filters.append(f"scale=-2:{target_height}:flags=lanczos")
  else:
    w_ratio, h_ratio = aspect_ratio
    target_w = int(round((target_height * w_ratio) / h_ratio))
    if target_w % 2 != 0:
      target_w += 1
    target_h = target_height if target_height % 2 == 0 else target_height + 1

    if ar_mode == "stretch":
      filters.append(f"scale={target_w}:{target_h}:flags=lanczos")
    elif ar_mode == "crop":
      filters.append(
          f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase:flags=lanczos,"
          f"crop={target_w}:{target_h}"
      )
    elif ar_mode == "pad":
      filters.append(
          f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black"
      )

  filters.append("setsar=1")
  return ",".join(filters)


def build_encoder_args(encoder, crf):
  """Build video encoder arguments for CPU (libx264) or GPU (h264_nvenc)."""
  if encoder == "nvenc":
    return [
        "-c:v",
        "h264_nvenc",
        "-rc",
        "vbr",
        "-cq",
        str(crf),
        "-b:v",
        "0",
        "-preset",
        "p4",
        "-pix_fmt",
        "yuv420p",
    ]
  return [
      "-c:v",
      "libx264",
      "-preset",
      "fast",
      "-crf",
      str(crf),
      "-pix_fmt",
      "yuv420p",
  ]


def run_ffmpeg_with_progress(cmd, total_frames, desc, logger):
  """Run FFmpeg sending logs to the log file and showing a progress bar with elapsed time."""
  progress_cmd = (
      cmd[:-1] + ["-progress", "pipe:1", "-nostats", "-loglevel", "info", cmd[-1]]
  )
  logger.info(f"Running FFmpeg ({desc}): {' '.join(progress_cmd)}")
  pbar = create_progress(total_frames if total_frames > 0 else 100, desc=desc, unit="fr")
  last_frame = 0
  stderr_lines = []

  proc = subprocess.Popen(
      progress_cmd,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
      bufsize=1,
  )

  err_thread = threading.Thread(
      target=_drain_stream_to_log,
      args=(proc.stderr, logger, "FFMPEG_STDERR", stderr_lines),
      daemon=True,
  )
  err_thread.start()

  for line in proc.stdout:
    line = line.strip()
    if line.startswith("frame="):
      try:
        cur_frame = int(line.split("=", 1)[1].strip())
        if total_frames > 0 and cur_frame > last_frame:
          pbar.update(cur_frame - last_frame)
          last_frame = cur_frame
      except ValueError:
        pass
    elif line.startswith("progress=") and line.endswith("end"):
      if total_frames <= 0:
        pbar.n = pbar.total

  err_thread.join(timeout=5)
  proc.wait()
  elapsed = pbar.elapsed_seconds()
  pbar.close()
  logger.info(
      f"FFmpeg ({desc}) finished with exit code {proc.returncode} in {format_duration(elapsed)}"
  )

  if proc.returncode != 0:
    raise RuntimeError(
        f"FFmpeg failed during '{desc}' (exit code {proc.returncode}). "
        f"Check log '{logger.log_path}': {' | '.join(stderr_lines[-5:])}"
    )


def run_realesrgan_quiet_with_progress(cmd, unique_dir, expected_count, img_ext, desc, logger):
  """Run realesrgan-ncnn-vulkan sending output to log and progress with elapsed time to console."""
  logger.info(f"Running Real-ESRGAN ({desc}, expected={expected_count}): {' '.join(cmd)}")
  pbar = create_progress(max(expected_count, 1), desc=desc, unit="img")
  stdout_lines = []
  stderr_lines = []
  suffix = f".{img_ext.lower()}"

  proc = subprocess.Popen(
      cmd,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
  )

  out_thread = threading.Thread(
      target=_drain_stream_to_log,
      args=(proc.stdout, logger, "REALESRGAN_OUT", stdout_lines),
      daemon=True,
  )
  err_thread = threading.Thread(
      target=_drain_stream_to_log,
      args=(proc.stderr, logger, "REALESRGAN_ERR", stderr_lines),
      daemon=True,
  )
  out_thread.start()
  err_thread.start()

  done_count = 0
  while proc.poll() is None:
    if os.path.exists(unique_dir):
      current_files = sum(1 for f in os.listdir(unique_dir) if f.lower().endswith(suffix))
      if current_files > done_count:
        diff = min(current_files - done_count, max(expected_count - done_count, 0))
        pbar.update(diff)
        done_count += diff
    time.sleep(0.25)

  out_thread.join(timeout=5)
  err_thread.join(timeout=5)

  final_files = (
      sum(1 for f in os.listdir(unique_dir) if f.lower().endswith(suffix))
      if os.path.exists(unique_dir)
      else 0
  )
  if final_files > done_count:
    pbar.update(min(final_files - done_count, max(expected_count - done_count, 0)))

  elapsed = pbar.elapsed_seconds()
  pbar.close()
  logger.info(
      f"Real-ESRGAN finished with exit code {proc.returncode} "
      f"(processed={final_files}/{expected_count}) in {format_duration(elapsed)}"
  )

  if proc.returncode != 0:
    combined = stderr_lines + stdout_lines
    raise RuntimeError(
        f"realesrgan-ncnn-vulkan failed (exit code {proc.returncode}). "
        f"Check log '{logger.log_path}': {' | '.join(combined[-8:])}"
    )


def assemble_video_with_fallback(
    frames_out,
    img_ext,
    fps,
    audio_file,
    has_audio,
    vf_chain,
    encoder,
    crf,
    total_frames,
    output_file,
    logger,
):
  """Assemble the final video, automatically falling back from NVENC to CPU if NVENC fails."""
  base_cmd = [
      "ffmpeg",
      "-y",
      "-framerate",
      fps,
      "-i",
      os.path.join(frames_out, f"frame_%08d.{img_ext}"),
  ]
  if has_audio:
    base_cmd.extend(["-i", audio_file])

  base_cmd.extend(["-vf", vf_chain])

  def _build_full_cmd(enc_name):
    cmd = list(base_cmd)
    cmd.extend(build_encoder_args(enc_name, crf))
    if has_audio:
      cmd.extend(["-c:a", "aac", "-b:a", "192k", "-map", "0:v:0", "-map", "1:a:0"])
    cmd.append(output_file)
    return cmd

  try:
    run_ffmpeg_with_progress(
        _build_full_cmd(encoder),
        total_frames,
        desc=f"4/4 Muxing video ({encoder})",
        logger=logger,
    )
  except RuntimeError as exc:
    if encoder == "nvenc":
      warn_msg = (
          "NVENC encoding failed; falling back automatically to CPU (libx264, preset=fast)."
      )
      logger.warn(f"{warn_msg} Reason: {exc}")
      print(f"\n[Warning] {warn_msg}")
      run_ffmpeg_with_progress(
          _build_full_cmd("cpu"),
          total_frames,
          desc="4/4 Muxing video (cpu fallback)",
          logger=logger,
      )
    else:
      raise


def process_single_video(
    input_file,
    output_file,
    args,
    aspect_ratio,
    global_cache,
    cache_dir,
    work_tmp_base,
    video_idx,
    logger,
):
  """Process a single video using the global temporary cache and apply cache eviction."""
  logger.info(
      f"Start video #{video_idx}: '{input_file}' -> '{output_file}' "
      f"(max_frames={args.max_frames}, encoder={args.encoder}, tile_size={args.tile_size}, ia_format={args.ia_format})"
  )
  os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
  fps, _, est_frames = get_video_info(input_file, logger)

  target_extract_frames = est_frames
  if args.max_frames and args.max_frames > 0:
    target_extract_frames = (
        min(est_frames, args.max_frames) if est_frames > 0 else args.max_frames
    )

  img_ext = args.ia_format.lower()

  with tempfile.TemporaryDirectory(prefix="anime_vid_", dir=work_tmp_base) as tmp_dir:
    frames_in = os.path.join(tmp_dir, "frames_in")
    frames_to_ia = os.path.join(tmp_dir, "frames_to_ia")
    frames_ia_out = os.path.join(tmp_dir, "frames_ia_out")
    frames_out = os.path.join(tmp_dir, "frames_out")
    audio_file = os.path.join(tmp_dir, "audio.mka")

    for d in (frames_in, frames_to_ia, frames_ia_out, frames_out):
      os.makedirs(d, exist_ok=True)

    # 1. Extract frames (with optional limit for quick tests)
    extract_cmd = ["ffmpeg", "-y", "-i", input_file]
    if args.max_frames and args.max_frames > 0:
      extract_cmd.extend(["-frames:v", str(args.max_frames)])
    extract_cmd.extend([
        "-qscale:v",
        "1",
        os.path.join(frames_in, "frame_%08d.png"),
    ])
    run_ffmpeg_with_progress(
        extract_cmd, target_extract_frames, desc="1/4 Extracting frames", logger=logger
    )

    # Extract audio if present
    has_audio = (
        subprocess.run(
            ["ffmpeg", "-y", "-i", input_file, "-vn", "-c:a", "copy", audio_file],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
        and os.path.exists(audio_file)
        and os.path.getsize(audio_file) > 0
    )
    logger.info(f"Audio extraction: has_audio={has_audio}")

    # 2. Local deduplication + global cache lookup
    all_frames = sorted(
        f for f in os.listdir(frames_in) if f.lower().endswith(".png")
    )
    if not all_frames:
      raise RuntimeError(f"No frames could be extracted from '{input_file}'.")

    frame_to_key = {}
    missing_keys_sample = {}
    last_hash = None
    last_key = None
    local_dups = 0
    cache_hits = 0

    pbar_dedup = create_progress(len(all_frames), desc="2/4 Cache & dedup", unit="fr")
    for frame_name in all_frames:
      src_path = os.path.join(frames_in, frame_name)
      current_hash = get_file_md5(src_path)

      if current_hash == last_hash and last_key is not None:
        cache_key = last_key
        local_dups += 1
      else:
        cache_key = make_cache_key(current_hash, args.model, args.scale, img_ext)
        last_hash = current_hash
        last_key = cache_key

        if cache_key in global_cache and os.path.exists(global_cache[cache_key]["path"]):
          global_cache[cache_key]["last_seen"] = video_idx
          cache_hits += 1
        elif cache_key not in missing_keys_sample:
          missing_keys_sample[cache_key] = src_path

      frame_to_key[frame_name] = cache_key
      pbar_dedup.update(1)
    dedup_elapsed = pbar_dedup.elapsed_seconds()
    pbar_dedup.close()

    total_saved_frames = local_dups + cache_hits
    new_for_ia_count = len(missing_keys_sample)

    cache_summary = (
        f"Cache lookup complete in {format_duration(dedup_elapsed)}: "
        f"{cache_hits} cache matches found, {local_dups} consecutive duplicate frames found, "
        f"{total_saved_frames}/{len(all_frames)} AI frame renders saved ({new_for_ia_count} new frames to upscale)."
    )
    logger.info(f"2/4 {cache_summary}")
    print(f"    -> {cache_summary}")

    for cache_key, sample_src in missing_keys_sample.items():
      shutil.copy2(sample_src, os.path.join(frames_to_ia, f"{cache_key}.png"))

    # 3. Upscale only new frames missing from the global cache
    if new_for_ia_count > 0:
      realesrgan_cmd = [
          "realesrgan-ncnn-vulkan",
          "-i",
          frames_to_ia,
          "-o",
          frames_ia_out,
          "-s",
          str(args.scale),
          "-n",
          args.model,
          "-f",
          img_ext,
      ]
      if args.tile_size and args.tile_size > 0:
        realesrgan_cmd.extend(["-t", str(args.tile_size)])

      run_realesrgan_quiet_with_progress(
          realesrgan_cmd,
          frames_ia_out,
          new_for_ia_count,
          img_ext=img_ext,
          desc="3/4 AI Real-ESRGAN",
          logger=logger,
      )

      for cache_key in missing_keys_sample:
        ia_result_path = os.path.join(frames_ia_out, f"{cache_key}.{img_ext}")
        cached_file_path = os.path.join(cache_dir, f"{cache_key}.{img_ext}")
        if os.path.exists(ia_result_path):
          shutil.copy2(ia_result_path, cached_file_path)
          global_cache[cache_key] = {
              "path": cached_file_path,
              "last_seen": video_idx,
          }
        else:
          logger.warn(f"AI output not found for cache key '{cache_key}'")
    else:
      logger.info("3/4 AI Real-ESRGAN skipped (100% resolved from global cache).")

    # Rebuild full frame sequence using the actual AI output extension
    for idx, frame_name in enumerate(all_frames, start=1):
      cache_key = frame_to_key[frame_name]
      cached_src = global_cache[cache_key]["path"]
      shutil.copy2(cached_src, os.path.join(frames_out, f"frame_{idx:08d}.{img_ext}"))

    # 4. Assemble final video with target height and aspect ratio
    vf_chain = build_vf_filter(args.height, aspect_ratio, args.ar_mode)
    assemble_video_with_fallback(
        frames_out=frames_out,
        img_ext=img_ext,
        fps=fps,
        audio_file=audio_file,
        has_audio=has_audio,
        vf_chain=vf_chain,
        encoder=args.encoder,
        crf=args.crf,
        total_frames=len(all_frames),
        output_file=output_file,
        logger=logger,
    )

  # Evict cache entries not seen within the last N videos
  evict_stale_cache_entries(global_cache, video_idx, args.cache_penalty, logger)
  logger.info(f"End video #{video_idx} completed: '{output_file}'")


def list_root_videos(directory):
  """Return only video files in the root of the directory (non-recursive)."""
  videos = []
  with os.scandir(directory) as entries:
    for entry in entries:
      if entry.is_file():
        ext = os.path.splitext(entry.name)[1].lower()
        if ext in VIDEO_EXTENSIONS:
          videos.append(entry.path)
  return sorted(videos)


def build_parser():
  parser = argparse.ArgumentParser(
      description=(
          "Anime upscaler with global frame cache, configurable eviction penalty, "
          "aspect ratio control, NVENC-to-CPU fallback, tile-size tuning, and elapsed timers."
      )
  )
  parser.add_argument(
      "-i",
      "--input",
      required=False,
      help="Input video file or root directory with videos (required unless --check-deps is used).",
  )
  parser.add_argument(
      "-o",
      "--output",
      required=False,
      help="Output file path (if -i is a file) or output directory (if -i is a directory).",
  )
  parser.add_argument(
      "--check-deps",
      action="store_true",
      help="Check required external CLI dependencies in PATH and exit without processing videos.",
  )
  parser.add_argument(
      "-s", "--scale", type=int, default=2, help="AI upscaling factor (default: 2)."
  )
  parser.add_argument(
      "--height",
      type=int,
      default=1080,
      help="Target output height in pixels (default: 1080).",
  )
  parser.add_argument(
      "--aspect",
      default="16:9",
      help="Target aspect ratio, e.g., '16:9', '4:3', '21:9', or 'keep' (default: '16:9').",
  )
  parser.add_argument(
      "--ar-mode",
      choices=["stretch", "crop", "pad"],
      default="stretch",
      help="How to apply target aspect ratio: stretch, crop, or pad.",
  )
  parser.add_argument(
      "--model",
      default="realesr-animevideov3",
      help="Real-ESRGAN model name (default: realesr-animevideov3).",
  )
  parser.add_argument(
      "--tile-size",
      type=int,
      default=0,
      help="Tile size (-t) for realesrgan-ncnn-vulkan; 0 = auto (default: 0).",
  )
  parser.add_argument(
      "--ia-format",
      choices=["png", "jpg"],
      default="png",
      help="Intermediate image format for Real-ESRGAN: png or jpg (default: png).",
  )
  parser.add_argument(
      "--encoder",
      choices=["cpu", "nvenc"],
      default="cpu",
      help="Final video encoder: 'cpu' (libx264) or 'nvenc' (h264_nvenc with CPU fallback).",
  )
  parser.add_argument(
      "--crf",
      type=int,
      default=18,
      help="CRF quality (for cpu) or CQ quality (for nvenc) (lower = higher quality/size, default: 18).",
  )
  parser.add_argument(
      "--cache-penalty",
      type=int,
      default=DEFAULT_CACHE_PENALTY_VIDEOS,
      help=(
          "Number of videos without appearing after which a frame is evicted from the "
          f"global cache (default: {DEFAULT_CACHE_PENALTY_VIDEOS})."
      ),
  )
  parser.add_argument(
      "--max-frames",
      type=int,
      default=None,
      help="Maximum number of frames to process per video (useful for quick tests).",
  )
  parser.add_argument(
      "--temp-dir",
      default=None,
      help="Base directory for temporary files and cache (default: system temp dir).",
  )
  parser.add_argument(
      "--log-file",
      default=None,
      help=(
          "Base log file path or directory for per-video logs "
          "(default: <output_video_name>.log next to the output video)."
      ),
  )
  return parser


def main():
  parser = build_parser()
  args = parser.parse_args()
  session_start = time.time()
  active_logger = None

  try:
    if args.check_deps:
      check_required_binaries(verbose=True)
      return

    check_required_binaries(verbose=False)

    if not args.input or not args.output:
      parser.error("the following arguments are required: -i/--input, -o/--output")

    if args.cache_penalty < 0:
      raise ValueError("'--cache-penalty' cannot be negative.")

    if args.max_frames is not None and args.max_frames < 0:
      raise ValueError("'--max-frames' cannot be negative.")

    if args.tile_size < 0:
      raise ValueError("'--tile-size' cannot be negative.")

    aspect_ratio = parse_aspect_ratio(args.aspect)

    if not os.path.exists(args.input):
      raise FileNotFoundError(f"Input path '{args.input}' does not exist.")

    if args.temp_dir:
      os.makedirs(args.temp_dir, exist_ok=True)

    global_cache = {}
    with tempfile.TemporaryDirectory(prefix="anime_global_cache_", dir=args.temp_dir) as cache_dir:
      if os.path.isdir(args.input):
        if os.path.exists(args.output) and not os.path.isdir(args.output):
          raise ValueError(
              f"Since '-i' is a directory, '-o' ('{args.output}') must also be a directory."
          )

        os.makedirs(args.output, exist_ok=True)
        videos = list_root_videos(args.input)

        if not videos:
          print(f"[Warning] No videos found in the root of '{args.input}'.")
          return

        for idx, vid_path in enumerate(videos, start=1):
          vid_start = time.time()
          filename = os.path.basename(vid_path)
          out_path = os.path.join(args.output, filename)
          log_path = build_video_log_path(out_path, args.log_file)
          logger = Logger(log_path)
          active_logger = logger
          try:
            if args.temp_dir:
              logger.info(f"Using custom temp base directory: '{args.temp_dir}'")
            logger.info(f"Global temporary cache directory: '{cache_dir}'")
            logger.info(f"Found {len(videos)} videos in '{args.input}' (root only).")
            print(f"\n=== Video [{idx}/{len(videos)}]: {filename} ===")
            print(f"    -> Log: {log_path}")
            process_single_video(
                vid_path,
                out_path,
                args,
                aspect_ratio,
                global_cache,
                cache_dir,
                args.temp_dir,
                idx,
                logger,
            )
            vid_elapsed = format_duration(time.time() - vid_start)
            print(f"    -> Total video time: {vid_elapsed}")
            logger.info(f"Total time for video #{idx} ({filename}): {vid_elapsed}")
          finally:
            logger.close()
            active_logger = None

        total_elapsed = format_duration(time.time() - session_start)
        print(f"\n[Completed] All videos saved to '{args.output}' in {total_elapsed}.")

      else:
        out_parent = os.path.dirname(os.path.abspath(args.output))
        os.makedirs(out_parent, exist_ok=True)
        vid_start = time.time()
        log_path = build_video_log_path(args.output, args.log_file)
        logger = Logger(log_path)
        active_logger = logger
        try:
          if args.temp_dir:
            logger.info(f"Using custom temp base directory: '{args.temp_dir}'")
          logger.info(f"Global temporary cache directory: '{cache_dir}'")
          logger.info(f"Single-file mode: '{args.input}' -> '{args.output}'")
          print(f"\n=== Processing file: {os.path.basename(args.input)} ===")
          print(f"    -> Log: {log_path}")
          process_single_video(
              args.input,
              args.output,
              args,
              aspect_ratio,
              global_cache,
              cache_dir,
              args.temp_dir,
              1,
              logger,
          )
          vid_elapsed = format_duration(time.time() - vid_start)
          print(f"\n[Completed] Video saved to '{args.output}' in {vid_elapsed}.")
          logger.info(f"Single file completed in {vid_elapsed}")
        finally:
          logger.close()
          active_logger = None

  except Exception as exc:
    if active_logger is not None:
      active_logger.error(f"Fatal error: {exc}")
      active_logger.close()
    log_hint = f" '{args.log_file}'" if args.log_file else " for the corresponding video"
    print(f"\n[Error] {exc} (check the log{log_hint} for details)", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
  main()