#!/usr/bin/env python3
"""
Automatic Reolink timelapse pipeline.

You pass ONE thing: the START time. Everything else comes from config.py.

Flow:
  1. Record the camera's LIVE stream in original quality for
     RECORD_DURATION_HOURS (end = start + duration). If the start is in the
     future, the script waits for it.
  2. Speed the footage up SPEED_FACTOR x and strip the audio.
  3. Upload the final video to Google Drive.
  4. Delete the local files (unless the upload failed or --keep-local is set).

Why live RTSP instead of downloading stored recordings? This camera's firmware
file-download service is broken (returns 0 bytes and drops the connection for
every recording, even for a brand-new admin user, and a reboot doesn't fix it).
Live RTSP capture is lossless and reliable, so that's what we use.

Usage:
  python record_timelapse.py --start "2026-07-15 06:00:00"
  python record_timelapse.py --start "2026-07-15 06:00:00" --duration-hours 2
  python record_timelapse.py --start "2026-07-15 06:00:00" --no-upload --keep-local
"""
import os
import sys
import glob
import time
import argparse
import subprocess
from datetime import datetime, timedelta
from urllib.parse import quote

import config
from reolink import speed_up_video


def rtsp_url():
    """Build the RTSP URL, URL-encoding the credentials (handles !, @, etc.)."""
    user = quote(config.CAMERA_USER, safe="")
    pw = quote(config.CAMERA_PASSWORD, safe="")
    return (f"rtsp://{user}:{pw}@{config.CAMERA_HOST}:{config.RTSP_PORT}/"
            f"{config.RTSP_STREAM_PATH}")


def redacted_url():
    """Same URL but with the password hidden, for printing."""
    return (f"rtsp://{config.CAMERA_USER}:****@{config.CAMERA_HOST}:"
            f"{config.RTSP_PORT}/{config.RTSP_STREAM_PATH}")


def probe_duration(path):
    """Return a media file's duration in seconds, or 0 on failure."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except (ValueError, TypeError):
        return 0.0


def record_segment(url, seconds, out_path):
    """Record up to `seconds` of live stream losslessly (-c copy).

    ffmpeg may exit early if the network drops; we return whether any usable
    footage landed on disk and let the caller decide about reconnecting.
    """
    cmd = [
        "ffmpeg", "-y",
        "-rtsp_transport", "tcp",     # TCP is far more stable than UDP over WAN
        "-timeout", "15000000",       # 15s socket timeout (microseconds)
        "-i", url,
        "-t", str(int(seconds)),
        "-c", "copy",                 # no re-encode -> exact original quality
        "-movflags", "+faststart",
        out_path,
    ]
    print(f"  recording up to {int(seconds)}s -> {os.path.basename(out_path)}")
    subprocess.run(cmd, capture_output=True, text=True)
    return os.path.exists(out_path) and os.path.getsize(out_path) > 100_000


def concat_segments(segments, output_path):
    """Losslessly join HEVC segments via MPEG-TS.

    Live RTSP captures do NOT concatenate cleanly with the mp4 concat demuxer
    (-c copy): the HEVC reference frames break at each segment boundary, so the
    joined file becomes almost undecodable (only a handful of frames survive,
    which is what made an earlier build produce a 9-frame, unwatchable clip).

    Converting each segment to an MPEG-TS elementary stream with the
    hevc_mp4toannexb bitstream filter and then joining with ffmpeg's concat
    PROTOCOL produces a clean, fully decodable file. We run ffmpeg from the
    output directory and use relative .ts names so paths with spaces (e.g.
    "...\\Noman traders\\...") don't trip up the concat: protocol parser.
    """
    if not segments:
        return None
    if len(segments) == 1:
        return segments[0]

    work_dir = os.path.dirname(os.path.abspath(output_path))
    ts_names = []
    for i, seg in enumerate(segments):
        ts_name = f"_concat_{i:03d}.ts"
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", os.path.abspath(seg),
             "-c", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "mpegts", ts_name],
            cwd=work_dir, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(os.path.join(work_dir, ts_name)):
            print(f"  TS convert failed for {os.path.basename(seg)}: "
                  f"{r.stderr.strip()[-200:]}")
            return None
        ts_names.append(ts_name)

    concat_arg = "concat:" + "|".join(ts_names)
    print(f"\nMerging {len(segments)} segments -> {os.path.basename(output_path)}")
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", concat_arg,
         "-c", "copy", "-movflags", "+faststart", os.path.basename(output_path)],
        cwd=work_dir, capture_output=True, text=True)

    for ts_name in ts_names:                       # clean up intermediates
        ts_path = os.path.join(work_dir, ts_name)
        if os.path.exists(ts_path):
            os.remove(ts_path)

    if r.returncode == 0 and os.path.exists(output_path):
        print(f"  Merged OK - {os.path.getsize(output_path) / (1024 ** 3):.2f} GB")
        return output_path
    print(f"  Merge failed: {r.stderr.strip()[-200:]}")
    return None


def record_live(url, total_seconds, work_dir, tag):
    """Record the live stream for total_seconds, stitching across disconnects.

    If the stream drops mid-recording, we reconnect and keep going into a new
    segment, then losslessly concatenate all segments at the end. Returns the
    raw merged file path (or a single segment), or None if nothing was captured.
    """
    segments = []
    start = time.time()
    remaining = total_seconds
    seg_idx = 0

    while remaining > 5:
        seg_idx += 1
        seg_path = os.path.join(work_dir, f"{tag}_seg{seg_idx:03d}.mp4")
        ok = record_segment(url, remaining, seg_path)
        if ok:
            segments.append(seg_path)
            print(f"    got {probe_duration(seg_path):.0f}s")
        else:
            print("    segment failed (stream drop); reconnecting in 5s...")
            if os.path.exists(seg_path):
                os.remove(seg_path)
            time.sleep(5)

        elapsed = time.time() - start
        remaining = total_seconds - elapsed

    if not segments:
        return None
    if len(segments) == 1:
        return segments[0]

    merged = os.path.join(work_dir, f"{tag}_raw.mp4")
    result = concat_segments(segments, merged)
    if result:
        for seg in segments:               # free the per-segment files
            if os.path.exists(seg):
                os.remove(seg)
    return result


def upload_to_drive(local_path):
    """Upload a file to Google Drive using the configured service account."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    if not os.path.exists(config.GDRIVE_CREDENTIALS_FILE):
        raise FileNotFoundError(
            f"Service account file not found: {config.GDRIVE_CREDENTIALS_FILE}. "
            "See config.example.py for the Google Drive setup steps.")

    creds = service_account.Credentials.from_service_account_file(
        config.GDRIVE_CREDENTIALS_FILE,
        scopes=["https://www.googleapis.com/auth/drive.file"])
    service = build("drive", "v3", credentials=creds)

    meta = {"name": os.path.basename(local_path)}
    if config.GDRIVE_FOLDER_ID:
        meta["parents"] = [config.GDRIVE_FOLDER_ID]

    media = MediaFileUpload(local_path, mimetype="video/mp4", resumable=True)
    print(f"\nUploading to Google Drive: {os.path.basename(local_path)}")
    request = service.files().create(
        body=meta, media_body=media,
        fields="id,name,webViewLink", supportsAllDrives=True)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"  {int(status.progress() * 100)}%")
    link = response.get("webViewLink") or response.get("id")
    print(f"  Uploaded: {link}")
    return response.get("id")


def main():
    ap = argparse.ArgumentParser(
        description="Record the camera live, speed it up, and push to Google Drive.")
    ap.add_argument("--start", required=True,
                    help='Start time "YYYY-MM-DD HH:MM:SS" (local). '
                         'If in the future, the script waits until then.')
    ap.add_argument("--duration-hours", type=float,
                    default=config.RECORD_DURATION_HOURS,
                    help=f"Override recording length (default: {config.RECORD_DURATION_HOURS}h)")
    ap.add_argument("--no-upload", action="store_true",
                    help="Skip the Google Drive upload (keeps the file locally)")
    ap.add_argument("--keep-local", action="store_true",
                    help="Keep local files even after a successful upload")
    args = ap.parse_args()

    try:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        print('ERROR: --start must be "YYYY-MM-DD HH:MM:SS", e.g. "2026-07-15 06:00:00"')
        sys.exit(1)

    duration_s = int(args.duration_hours * 60)
    end_dt = start_dt + timedelta(seconds=duration_s)
    os.makedirs(config.WORK_DIR, exist_ok=True)

    print(f"Camera : {redacted_url()}")
    print(f"Window : {start_dt}  ->  {end_dt}  ({args.duration_hours}h)")

    now = datetime.now()
    if start_dt > now:
        wait = (start_dt - now).total_seconds()
        print(f"\nWaiting {wait / 3600:.2f}h until start ({start_dt})... "
              f"leave this running.")
        time.sleep(wait)
    elif start_dt < now:
        print("\nNote: the start time is in the past, and a LIVE stream can't be "
              "rewound. Recording NOW for the full duration instead.")

    tag = start_dt.strftime("%Y%m%d_%H%M%S")

    print(f"\n===== RECORDING LIVE ({args.duration_hours}h) =====")
    raw = record_live(rtsp_url(), duration_s, config.WORK_DIR, tag)
    if not raw:
        print("Recording failed entirely (no footage captured). Aborting.")
        sys.exit(1)
    print(f"Raw footage: {raw}  ({probe_duration(raw):.0f}s, "
          f"{os.path.getsize(raw) / (1024 ** 3):.2f} GB)")

    factor = int(config.SPEED_FACTOR) if config.SPEED_FACTOR == int(config.SPEED_FACTOR) \
        else config.SPEED_FACTOR
    final = os.path.join(config.WORK_DIR, f"{tag}_{factor}x.mp4")
    out = speed_up_video(raw, final, config.SPEED_FACTOR,
                         fps=config.SPEED_FPS, width=config.SPEED_WIDTH,
                         crf=config.SPEED_CRF)
    if not out:
        print(f"Speed-up failed. Keeping raw file: {raw}")
        sys.exit(1)

    uploaded = False
    if args.no_upload:
        print("\n--no-upload set; skipping Google Drive.")
    else:
        try:
            uploaded = upload_to_drive(out) is not None
        except Exception as e:
            print(f"\nDrive upload failed: {e}")
            print("Keeping local files so nothing is lost.")

    if uploaded and config.DELETE_LOCAL_AFTER_UPLOAD and not args.keep_local:
        removed = 0
        for p in [raw, out] + glob.glob(os.path.join(config.WORK_DIR, f"{tag}_seg*.mp4")):
            if os.path.exists(p):
                os.remove(p)
                removed += 1
        print(f"\nDeleted {removed} local file(s) after successful upload.")
    else:
        print(f"\nFinal video kept locally: {out}")

    print("\nDone.")


if __name__ == "__main__":
    main()
