#!/usr/bin/env python3
"""
Storage-lean, stoppable Reolink timelapse pipeline (built for a 30 GB EC2 box).

One job at a time. The job:
  1. Records the camera's LIVE RTSP stream in original quality, in fixed-length
     chunks (config.SEGMENT_SECONDS). Between chunks it checks free disk and the
     stop flag, so it never fills the disk and can be stopped within one chunk.
  2. Joins the chunks losslessly (MPEG-TS + hevc_mp4toannexb + concat protocol)
     WITHOUT ever writing a full merged copy -- the speed-up reads the chunks
     directly. This keeps peak disk usage at ~one recording's worth (~10 GB),
     never double.
  3. Speeds the footage up SPEED_FACTOR x, strips audio, re-encodes small.
  4. Uploads the small final file to Google Drive.
  5. Deletes every local file. On ANY exit path (success, error, stop) the temp
     files are cleaned up in a finally block, so storage is always left clean.

The whole thing is designed to be driven by server.py (FastAPI), but it also has
a __main__ CLI so you can run one job straight from the shell.

Why live RTSP and not file download? The camera's firmware download service is
broken (returns 0 bytes). Live capture is lossless and reliable.
"""
import os
import glob
import time
import shutil
import logging
import threading
import subprocess
from enum import Enum
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from urllib.parse import quote

import config


# --------------------------------------------------------------------------- #
# Config helpers (getattr so an older config.py without the new keys still runs)
# --------------------------------------------------------------------------- #
def _cfg(name, default):
    return getattr(config, name, default)


GB = 1024 ** 3


class JobState(str, Enum):
    IDLE = "idle"
    WAITING = "waiting"        # start time is in the future
    RECORDING = "recording"
    PROCESSING = "processing"  # joining chunks
    SPEEDING = "speeding"      # ffmpeg speed-up encode
    UPLOADING = "uploading"
    CLEANING = "cleaning"
    DONE = "done"
    ERROR = "error"
    STOPPED = "stopped"


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
def setup_logging():
    log_dir = _cfg("LOG_DIR", "./logs")
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger("reolink")
    if logger.handlers:              # already configured (server reload etc.)
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(os.path.join(log_dir, "pipeline.log"),
                             maxBytes=5 * 1024 * 1024, backupCount=5,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
def rtsp_url():
    user = quote(config.CAMERA_USER, safe="")
    pw = quote(config.CAMERA_PASSWORD, safe="")
    return (f"rtsp://{user}:{pw}@{config.CAMERA_HOST}:{config.RTSP_PORT}/"
            f"{config.RTSP_STREAM_PATH}")


def redacted_url():
    return (f"rtsp://{config.CAMERA_USER}:****@{config.CAMERA_HOST}:"
            f"{config.RTSP_PORT}/{config.RTSP_STREAM_PATH}")


def free_gb(path):
    try:
        return shutil.disk_usage(path).free / GB
    except OSError:
        return 0.0


def probe_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except (ValueError, TypeError):
        return 0.0


# --------------------------------------------------------------------------- #
# The job
# --------------------------------------------------------------------------- #
class RecordingJob:
    """A single record -> speed-up -> upload -> cleanup run, on a worker thread."""

    def __init__(self, duration_hours=None, upload=True, keep_local=False,
                 start_at=None):
        self.log = setup_logging()
        self.duration_hours = (duration_hours
                               if duration_hours is not None
                               else _cfg("RECORD_DURATION_HOURS", 4))
        self.upload = upload
        self.keep_local = keep_local
        self.start_at = start_at                    # datetime or None (== now)

        self.work_dir = os.path.abspath(_cfg("WORK_DIR", "./output"))
        self.segment_seconds = int(_cfg("SEGMENT_SECONDS", 600))
        self.min_free_gb = float(_cfg("DISK_MIN_FREE_GB", 6))
        self.stall_seconds = int(_cfg("WATCHDOG_STALL_SECONDS", 30))

        self.state = JobState.IDLE
        self.error = None
        self.tag = None
        self.created_at = datetime.now()
        self.record_started_at = None
        self.record_ends_at = None
        self.final_path = None
        self.drive_link = None
        self.segments_captured = 0
        self.captured_seconds = 0.0

        self._stop_event = threading.Event()
        self._current_proc = None
        self._proc_lock = threading.Lock()
        self._thread = None

    # ----- public control ------------------------------------------------- #
    def start(self):
        os.makedirs(self.work_dir, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="recording-job",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        """Signal the job to stop. It finalizes the current chunk, then still
        processes+uploads whatever was captured, then cleans up."""
        self.log.info("STOP requested.")
        self._stop_event.set()
        self._kill_current("stop requested")

    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def status(self):
        remaining = None
        if self.record_ends_at and self.state == JobState.RECORDING:
            remaining = max(0, (self.record_ends_at - datetime.now()).total_seconds())
        return {
            "state": self.state.value,
            "error": self.error,
            "tag": self.tag,
            "duration_hours": self.duration_hours,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "record_started_at": self.record_started_at.isoformat(timespec="seconds")
                if self.record_started_at else None,
            "record_ends_at": self.record_ends_at.isoformat(timespec="seconds")
                if self.record_ends_at else None,
            "record_seconds_remaining": int(remaining) if remaining is not None else None,
            "segments_captured": self.segments_captured,
            "captured_seconds": round(self.captured_seconds, 1),
            "disk_free_gb": round(free_gb(self.work_dir), 2),
            "final_file": os.path.basename(self.final_path) if self.final_path else None,
            "drive_link": self.drive_link,
            "stopping": self._stop_event.is_set(),
            "alive": self.is_alive(),
        }

    # ----- ffmpeg process management -------------------------------------- #
    def _kill_current(self, why):
        """Gracefully finalize the running ffmpeg (send 'q'), then hard-kill."""
        with self._proc_lock:
            proc = self._current_proc
        if proc is None or proc.poll() is not None:
            return
        self.log.info("Signaling ffmpeg to finish (%s).", why)
        try:
            if proc.stdin:
                proc.stdin.write(b"q")
                proc.stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()
            except OSError:
                pass

    def _run_ffmpeg(self, cmd, log_path, cwd=None, watch_path=None):
        """Run ffmpeg, streaming its stderr to log_path (avoids pipe deadlock on
        long runs). Polls for the stop flag and, if watch_path is given, a stall.

        Returns one of: "ok", "error", "stopped", "stalled".
        """
        with open(log_path, "ab") as errlog:
            proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE,
                                    stdout=errlog, stderr=errlog)
            with self._proc_lock:
                self._current_proc = proc

            last_size, last_change = -1, time.time()
            try:
                while proc.poll() is None:
                    if self._stop_event.is_set():
                        self._kill_current("stop flag")
                        break
                    if watch_path:
                        sz = os.path.getsize(watch_path) if os.path.exists(watch_path) else 0
                        if sz != last_size:
                            last_size, last_change = sz, time.time()
                        elif time.time() - last_change > self.stall_seconds:
                            self.log.warning("Watchdog: ffmpeg stalled (%ss no data). Killing.",
                                             self.stall_seconds)
                            self._kill_current("watchdog stall")
                            proc.wait()
                            return "stalled"
                    time.sleep(1)
            finally:
                with self._proc_lock:
                    self._current_proc = None

        if self._stop_event.is_set():
            return "stopped"
        return "ok" if proc.returncode == 0 else "error"

    # ----- pipeline stages ------------------------------------------------- #
    def _record(self):
        """Capture the live stream into chunk files. Returns the chunk paths."""
        url = rtsp_url()
        total_seconds = int(self.duration_hours * 3600)
        self.record_started_at = datetime.now()
        self.record_ends_at = self.record_started_at + timedelta(seconds=total_seconds)
        self.log.info("Recording %.2fh from %s (chunks of %ss).",
                      self.duration_hours, redacted_url(), self.segment_seconds)

        segments = []
        start = time.time()
        seg_idx = 0
        log_path = os.path.join(self.work_dir, f"{self.tag}_ffmpeg.log")

        while not self._stop_event.is_set():
            elapsed = time.time() - start
            remaining = total_seconds - elapsed
            if remaining <= 2:
                break

            free = free_gb(self.work_dir)
            if free < self.min_free_gb:
                self.log.warning("Low disk (%.1f GB free < %.1f GB). Stopping capture early.",
                                 free, self.min_free_gb)
                break

            this_len = int(min(self.segment_seconds, remaining))
            seg_idx += 1
            seg_path = os.path.join(self.work_dir, f"{self.tag}_seg{seg_idx:03d}.mp4")
            cmd = [
                "ffmpeg", "-y", "-loglevel", "warning",
                "-rtsp_transport", "tcp",
                "-timeout", "15000000",
                "-i", url,
                "-t", str(this_len),
                "-c", "copy",
                "-movflags", "+faststart",
                seg_path,
            ]
            self.log.info("Chunk %d: up to %ss (%.0f min left, %.1f GB free)...",
                          seg_idx, this_len, remaining / 60, free)
            result = self._run_ffmpeg(cmd, log_path, watch_path=seg_path)

            good = os.path.exists(seg_path) and os.path.getsize(seg_path) > 100_000
            if good:
                dur = probe_duration(seg_path)
                segments.append(seg_path)
                self.segments_captured = len(segments)
                self.captured_seconds += dur
                self.log.info("  chunk %d OK (%.0fs, %.2f GB total captured).",
                              seg_idx, dur, sum(os.path.getsize(s) for s in segments) / GB)
            else:
                if os.path.exists(seg_path):
                    os.remove(seg_path)
                if result == "stopped":
                    break
                self.log.warning("  chunk %d failed (%s); reconnecting in 5s...",
                                 seg_idx, result)
                if self._stop_event.wait(5):
                    break

        return segments

    def _to_ts(self, segments):
        """Convert each mp4 chunk to an MPEG-TS elementary stream and DELETE the
        mp4 immediately, so disk usage stays flat (never holds mp4 + ts together).
        Returns the relative .ts names (ffmpeg is run with cwd=work_dir)."""
        log_path = os.path.join(self.work_dir, f"{self.tag}_ffmpeg.log")
        ts_names = []
        for i, seg in enumerate(segments):
            ts_name = f"{self.tag}_ts{i:03d}.ts"
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", os.path.abspath(seg),
                   "-c", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "mpegts", ts_name]
            result = self._run_ffmpeg(cmd, log_path, cwd=self.work_dir)
            if result != "ok" or not os.path.exists(os.path.join(self.work_dir, ts_name)):
                self.log.error("TS convert failed for %s (%s).", os.path.basename(seg), result)
                return None
            ts_names.append(ts_name)
            os.remove(seg)                    # free the mp4 right away
        return ts_names

    def _speed_up(self, input_arg, cwd, out_name):
        """Speed up SPEED_FACTOR x, strip audio, small re-encode. input_arg is
        either a relative .ts filename, a 'concat:a.ts|b.ts' string, or an
        absolute mp4 path. Runs with cwd so relative/concat inputs resolve."""
        log_path = os.path.join(self.work_dir, f"{self.tag}_ffmpeg.log")
        factor = config.SPEED_FACTOR
        fps = _cfg("SPEED_FPS", 30)
        width = _cfg("SPEED_WIDTH", 3840)
        crf = _cfg("SPEED_CRF", 18)
        vf = f"setpts=PTS/{factor},fps={fps}"
        if width:
            vf += f",scale={width}:-2"
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-skip_frame", "nokey",
            "-i", input_arg,
            "-filter:v", vf,
            "-an",
            "-c:v", "libx264", "-preset", "slow", "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            out_name,
        ]
        self.log.info("Speeding up %sx -> %s", factor, out_name)
        result = self._run_ffmpeg(cmd, log_path, cwd=cwd)
        out_path = os.path.join(cwd, out_name)
        if result == "ok" and os.path.exists(out_path):
            self.log.info("  speed-up OK (%.1f MB).", os.path.getsize(out_path) / (1024 ** 2))
            return out_path
        self.log.error("Speed-up failed (%s).", result)
        return None

    def _drive_credentials(self):
        """Build Drive API credentials for the configured auth mode.

        GDRIVE_AUTH = "oauth"           -> upload as a real Google user (files are
                                          owned by that user, use their quota).
        GDRIVE_AUTH = "service_account" -> upload as the service account (ONLY works
                                          into a Shared Drive; a service account has
                                          no personal Drive quota of its own).
        """
        scopes = ["https://www.googleapis.com/auth/drive.file"]
        mode = _cfg("GDRIVE_AUTH", "service_account")

        if mode == "oauth":
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            token_file = _cfg("GDRIVE_OAUTH_TOKEN_FILE", "token.json")
            if not os.path.exists(token_file):
                raise FileNotFoundError(
                    f"OAuth token not found: {token_file}. Run gdrive_auth.py once "
                    "on a machine with a browser to create it.")
            creds = Credentials.from_authorized_user_file(token_file, scopes)
            if not creds.valid:
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    with open(token_file, "w", encoding="utf-8") as f:
                        f.write(creds.to_json())   # persist the refreshed token
                else:
                    raise RuntimeError(
                        "OAuth token is invalid and cannot be refreshed. "
                        "Re-run gdrive_auth.py to create a fresh token.json.")
            return creds

        # default: service account
        from google.oauth2 import service_account
        cred_file = config.GDRIVE_CREDENTIALS_FILE
        if not os.path.exists(cred_file):
            raise FileNotFoundError(
                f"Service account file not found: {cred_file}. See config.example.py.")
        return service_account.Credentials.from_service_account_file(cred_file, scopes=scopes)

    def _upload(self, local_path):
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

        creds = self._drive_credentials()
        service = build("drive", "v3", credentials=creds)
        meta = {"name": os.path.basename(local_path)}
        if config.GDRIVE_FOLDER_ID:
            meta["parents"] = [config.GDRIVE_FOLDER_ID]

        media = MediaFileUpload(local_path, mimetype="video/mp4", resumable=True)
        request = service.files().create(body=meta, media_body=media,
                                         fields="id,name,webViewLink",
                                         supportsAllDrives=True)
        self.log.info("Uploading %s to Google Drive...", os.path.basename(local_path))
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                self.log.info("  upload %d%%", int(status.progress() * 100))
        link = response.get("webViewLink") or response.get("id")
        self.log.info("  uploaded: %s", link)
        return link

    def _cleanup_temp(self):
        """Remove every chunk/ts/log/raw scratch file for this tag. Keep the
        final only if the caller asked to. Always safe to call."""
        if not self.tag:
            return
        patterns = [f"{self.tag}_seg*.mp4", f"{self.tag}_ts*.ts", f"{self.tag}_ffmpeg.log"]
        removed = 0
        for pat in patterns:
            for p in glob.glob(os.path.join(self.work_dir, pat)):
                try:
                    os.remove(p)
                    removed += 1
                except OSError:
                    pass
        if removed:
            self.log.info("Cleaned %d temp file(s). Disk free: %.1f GB.",
                          removed, free_gb(self.work_dir))

    # ----- the worker ----------------------------------------------------- #
    def _run(self):
        try:
            # optional wait for a future start time
            if self.start_at and self.start_at > datetime.now():
                self.state = JobState.WAITING
                wait = (self.start_at - datetime.now()).total_seconds()
                self.log.info("Waiting %.2fh until start %s.", wait / 3600, self.start_at)
                if self._stop_event.wait(wait):
                    self.state = JobState.STOPPED
                    return
            self.tag = datetime.now().strftime("%Y%m%d_%H%M%S")

            # 1) record
            self.state = JobState.RECORDING
            segments = self._record()
            if not segments:
                if self._stop_event.is_set():
                    self.state = JobState.STOPPED
                    self.log.info("Stopped before any footage was captured.")
                else:
                    self.state = JobState.ERROR
                    self.error = "No footage captured."
                    self.log.error(self.error)
                return
            self.log.info("Captured %d chunk(s), %.0fs total.",
                          len(segments), self.captured_seconds)

            # 2) prepare speed-up input WITHOUT a merged copy
            self.state = JobState.PROCESSING
            if len(segments) == 1:
                speed_input = os.path.abspath(segments[0])   # feed the mp4 directly
                cwd = self.work_dir
            else:
                ts_names = self._to_ts(segments)             # deletes the mp4s
                if not ts_names:
                    self.state = JobState.ERROR
                    self.error = "Failed to join chunks."
                    return
                speed_input = "concat:" + "|".join(ts_names)
                cwd = self.work_dir

            # 3) speed up
            self.state = JobState.SPEEDING
            factor = config.SPEED_FACTOR
            factor = int(factor) if factor == int(factor) else factor
            out_name = f"{self.tag}_{factor}x.mp4"
            self.final_path = self._speed_up(speed_input, cwd, out_name)
            self._cleanup_temp()                             # free chunks/ts now
            if not self.final_path:
                self.state = JobState.ERROR
                self.error = "Speed-up failed."
                return

            # 4) upload
            uploaded = False
            if self.upload:
                self.state = JobState.UPLOADING
                try:
                    self.drive_link = self._upload(self.final_path)
                    uploaded = self.drive_link is not None
                except Exception as e:                       # noqa: BLE001
                    self.error = f"Upload failed: {e}"
                    self.log.error(self.error)
                    self.log.info("Keeping final file locally so nothing is lost.")
            else:
                self.log.info("Upload disabled; keeping final file locally.")

            # 5) cleanup
            self.state = JobState.CLEANING
            if uploaded and _cfg("DELETE_LOCAL_AFTER_UPLOAD", True) and not self.keep_local:
                if os.path.exists(self.final_path):
                    os.remove(self.final_path)
                self.log.info("Deleted local final after upload. Disk free: %.1f GB.",
                              free_gb(self.work_dir))
                self.final_path = None
            else:
                self.log.info("Final kept locally: %s", self.final_path)

            self.state = JobState.STOPPED if self._stop_event.is_set() else JobState.DONE
            self.log.info("Job finished: %s", self.state.value)

        except Exception as e:                               # noqa: BLE001
            self.state = JobState.ERROR
            self.error = str(e)
            self.log.exception("Job crashed: %s", e)
        finally:
            # storage is ALWAYS left clean of scratch files, on every exit path
            self._cleanup_temp()


# --------------------------------------------------------------------------- #
# CLI (single job, no server)
# --------------------------------------------------------------------------- #
def _main():
    import argparse
    ap = argparse.ArgumentParser(description="Record live -> 200x -> Google Drive.")
    ap.add_argument("--start", help='Optional start "YYYY-MM-DD HH:MM:SS". '
                                     "Omit to start now.")
    ap.add_argument("--duration-hours", type=float,
                    default=_cfg("RECORD_DURATION_HOURS", 4))
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--keep-local", action="store_true")
    args = ap.parse_args()

    start_at = None
    if args.start:
        start_at = datetime.strptime(args.start, "%Y-%m-%d %H:%M:%S")

    job = RecordingJob(duration_hours=args.duration_hours,
                       upload=not args.no_upload,
                       keep_local=args.keep_local,
                       start_at=start_at)
    job.start()
    try:
        while job.is_alive():
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nCtrl+C -> stopping cleanly...")
        job.stop()
        job._thread.join()
    print("Final state:", job.state.value)


if __name__ == "__main__":
    _main()
