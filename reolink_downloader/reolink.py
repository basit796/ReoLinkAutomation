import requests
import json
import os
from datetime import datetime
import urllib3
import argparse
import time
import subprocess

# Disable SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class CameraRefusingDownloads(Exception):
    """The camera's file-serving service is wedged and needs a reboot."""

def get_token(camera_ip, username, password, port=443):
    """Get authentication token from the camera"""
    url = f"https://{camera_ip}:{port}/api.cgi?cmd=Login"
    headers = {'Content-Type': 'application/json'}

    data = [{
        "cmd": "Login",
        "param": {
            "User": {
                "Version": "0",
                "userName": username,
                "password": password
            }
        }
    }]

    response = requests.post(url, headers=headers, json=data, verify=False)
    if response.status_code == 200:
        result = response.json()
        if result[0]["code"] == 0 and "value" in result[0]:
            token = result[0]["value"]["Token"]["name"]
            print(f"Successfully authenticated. Token: {token}")
            return token
    print(f"Authentication failed: {response.text}")
    return None

def logout(camera_ip, token, port=443):
    """Release the session on the camera.

    The camera allows only a few concurrent sessions, and abandoned ones can
    wedge its download service. Always call this, including on Ctrl+C.
    """
    if not token:
        return
    url = f"https://{camera_ip}:{port}/api.cgi?cmd=Logout&token={token}"
    try:
        requests.post(url, json=[{"cmd": "Logout", "param": {}}], verify=False, timeout=10)
        print("Logged out (session released).")
    except Exception as e:
        print(f"Logout failed (session may linger): {e}")


def search_recordings(camera_ip, token, channel, start_time, end_time, port=443):
    """Search for recordings using the Search API"""
    url = f"https://{camera_ip}:{port}/api.cgi?cmd=Search&token={token}"
    headers = {'Content-Type': 'application/json'}

    data = [{
        "cmd": "Search",
        "action": 0,
        "param": {
            "Search": {
                "channel": channel,
                "onlyStatus": 0,
                "streamType": "main",
                "StartTime": {
                    "year": int(start_time[0:4]),
                    "mon": int(start_time[4:6]),
                    "day": int(start_time[6:8]),
                    "hour": int(start_time[8:10]),
                    "min": int(start_time[10:12]),
                    "sec": int(start_time[12:14])
                },
                "EndTime": {
                    "year": int(end_time[0:4]),
                    "mon": int(end_time[4:6]),
                    "day": int(end_time[6:8]),
                    "hour": int(end_time[8:10]),
                    "min": int(end_time[10:12]),
                    "sec": int(end_time[12:14])
                }
            }
        }
    }]

    response = requests.post(url, headers=headers, json=data, verify=False)

    if response.status_code == 200:
        result = response.json()
        if result[0]["code"] == 0 and "value" in result[0]:
            if "SearchResult" in result[0]["value"] and "File" in result[0]["value"]["SearchResult"]:
                return result[0]["value"]["SearchResult"]["File"]

    return []

def download_recording(camera_ip, token, filename, output_path, expected_size=0, port=443, max_retries=4):
    """Download a recording via curl, which tolerates the camera's chunked stream.

    Reolink firmware corrupts HTTP chunked framing mid-transfer, which makes
    Python's requests/urllib3 raise InvalidChunkLength every time. curl handles
    the broken stream far better, and on stubborn failures we force HTTP/1.0 so
    the camera close-delimits the body instead of chunking it at all. We verify
    the downloaded byte count against the size the Search API reported.
    """
    output = os.path.basename(filename)
    url = (f"https://{camera_ip}:{port}/cgi-bin/api.cgi?cmd=Download"
           f"&source={filename}&output={output}&token={token}")

    # Consider the download good if we got at least ~99% of the expected size
    # (or any non-zero bytes when the search didn't report a size).
    min_ok = int(expected_size * 0.99) if expected_size else 1

    for attempt in range(1, max_retries + 1):
        # Later attempts force HTTP/1.0 to bypass the chunked-framing bug.
        force_http10 = attempt >= 3
        cmd = [
            "curl", "-k", "-sS", "-o", output_path,
            "--connect-timeout", "15", "--max-time", "600",
            "--retry", "2", "--retry-delay", "2", "--retry-all-errors",
        ]
        if force_http10:
            cmd.append("--http1.0")
        cmd.append(url)

        print(f"  attempt {attempt}/{max_retries}: {output}"
              f"{' (http/1.0)' if force_http10 else ''}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            print("  ERROR: curl not found on PATH. Install curl or add it to PATH.")
            return False

        got = os.path.getsize(output_path) if os.path.exists(output_path) else 0

        # Trust the byte count over curl's exit code. The camera closes TLS without
        # close_notify, so curl reports exit 56 even on a fully-received file.
        if got >= min_ok:
            note = " (ignoring benign TLS close)" if result.returncode else ""
            print(f"  OK - {got / (1024 * 1024):.2f} MB "
                  f"(expected ~{expected_size / (1024 * 1024):.2f} MB){note}")
            return True

        # Zero bytes + connection dropped = the camera's download service is refusing
        # us outright. Hammering it makes things worse, so bail out early.
        if got == 0 and result.returncode == 56:
            print("  camera closed the connection with 0 bytes")
            if attempt >= 2:
                # Signal the caller to stop the entire run - continuing would just
                # hammer a camera that is refusing every download.
                raise CameraRefusingDownloads(
                    "The camera accepted the request then closed the connection with 0 bytes. "
                    "Its file-serving service is wedged. Reboot the camera (a reboot keeps all "
                    "recordings and settings) and run this again."
                )
        else:
            print(f"  failed: short read {got / (1024 * 1024):.2f} MB "
                  f"/ ~{expected_size / (1024 * 1024):.2f} MB - retrying")
        time.sleep(2)

    print(f"  FAILED after {max_retries} attempts")
    return False

# def download_recording(camera_ip, token, filename, output_path, expected_size=0, port=443, max_retries=15):
#     """Download using resumable curl requests. Each attempt continues from
#     where the last one left off via HTTP Range (-C -), instead of restarting
#     the whole file when the camera drops the connection.
#     """
#     output = os.path.basename(filename)
#     url = (f"https://{camera_ip}:{port}/cgi-bin/api.cgi?cmd=Download"
#            f"&source={filename}&output={output}&token={token}")

#     min_ok = int(expected_size * 0.99) if expected_size else 1

#     for attempt in range(1, max_retries + 1):
#         got_before = os.path.getsize(output_path) if os.path.exists(output_path) else 0

#         cmd = [
#             "curl", "-k", "-sS", "-o", output_path,
#             "-C", "-",  # resume from wherever the partial file left off
#             "--connect-timeout", "15", "--max-time", "120",
#         ]
#         cmd.append(url)

#         print(f"  attempt {attempt}/{max_retries}: {output} (resuming from {got_before/(1024*1024):.2f} MB)")
#         result = subprocess.run(cmd, capture_output=True, text=True)

#         got = os.path.getsize(output_path) if os.path.exists(output_path) else 0

#         if got >= min_ok:
#             print(f"  OK - {got / (1024 * 1024):.2f} MB "
#                   f"(expected ~{expected_size / (1024 * 1024):.2f} MB)")
#             return True

#         if got == got_before:
#             # No progress at all this attempt - short pause before retrying
#             print(f"  no progress ({got/(1024*1024):.2f} MB) - retrying")
#             time.sleep(3)
#         else:
#             print(f"  progress: {got/(1024*1024):.2f} / ~{expected_size/(1024*1024):.2f} MB - continuing")
#             time.sleep(1)

#     print(f"  FAILED after {max_retries} attempts - got {got/(1024*1024):.2f} MB of ~{expected_size/(1024*1024):.2f} MB")
#     return False


def merge_videos(video_files, output_path):
    """Losslessly concatenate clips into one file using ffmpeg's concat demuxer.

    No re-encoding, so this is near-instant. Requires the inputs to share the
    same codec/resolution, which Reolink clips from one camera always do.
    """
    if not video_files:
        print("Nothing to merge.")
        return None

    # ffmpeg concat demuxer needs a list file of the inputs.
    list_path = os.path.join(os.path.dirname(output_path) or ".", "_concat_list.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for path in video_files:
            safe = os.path.abspath(path).replace("'", "'\\''")
            f.write(f"file '{safe}'\n")

    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_path, "-c", "copy", output_path,
    ]
    print(f"\nMerging {len(video_files)} clips -> {output_path}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    os.remove(list_path)

    if result.returncode == 0 and os.path.exists(output_path):
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"  Merged OK - {size_mb:.2f} MB")
        return output_path
    print(f"  Merge failed: {result.stderr.strip()[-300:]}")
    return None


def speed_up_video(input_path, output_path, factor, fps=30, width=3840, crf=18):
    """Create a sped-up version at the given factor, e.g. 200x.

    setpts=PTS/factor compresses the timeline; capping to `fps` drops the excess
    frames. Speeding up ALWAYS requires re-encoding (you cannot stream-copy a
    timestamp change), so we keep per-frame quality high with crf=18.

    -skip_frame nokey decodes ONLY keyframes. The source has a keyframe every 2s
    but a 200x timelapse only needs a frame every ~6.7s, so the other ~96% of
    frames never need decoding. On multi-hour sources this saves a lot of time.

    width=0 keeps the camera's native 7680x2160, but that produces a multi-GB
    file most players cannot decode, so we default to 3840x1080 (same 32:9 view).
    """
    vf = f"setpts=PTS/{factor},fps={fps}"
    if width:
        vf += f",scale={width}:-2"

    cmd = [
        "ffmpeg", "-y",
        "-skip_frame", "nokey",
        "-i", input_path,
        "-filter:v", vf,
        "-an",
        "-c:v", "libx264", "-preset", "slow", "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        output_path,
    ]
    print(f"\nSpeeding up {factor}x -> {output_path}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode == 0 and os.path.exists(output_path):
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"  Sped-up video OK - {size_mb:.2f} MB")
        return output_path
    print(f"  Speed-up failed: {result.stderr.strip()[-300:]}")
    return None


def main():
    parser = argparse.ArgumentParser(description='Download Reolink camera recordings within a time range.')
    parser.add_argument('--ip', required=True, help='Camera IP address')
    parser.add_argument('--username', required=True, help='Camera username')
    parser.add_argument('--password', required=True, help='Camera password')
    parser.add_argument('--port', type=int, default=443, help='Camera port (default: 443)')
    parser.add_argument('--start', required=True, help='Start date/time (format: YYYY-MM-DD HH:MM:SS)')
    parser.add_argument('--end', required=True, help='End date/time (format: YYYY-MM-DD HH:MM:SS)')
    parser.add_argument('--output', default='./recordings', help='Output directory (default: ./recordings)')
    parser.add_argument('--channel', type=int, default=0, help='Camera channel (default: 0)')
    parser.add_argument('--list-only', action='store_true', help='Only list recordings, do not download')
    parser.add_argument('--merge', action='store_true',
                        help='Merge all downloaded clips into one merged.mp4')
    parser.add_argument('--speed', type=float, default=0,
                        help='Also produce a sped-up video by this factor (e.g. 100). Implies --merge.')
    parser.add_argument('--speed-fps', type=int, default=30,
                        help='Output frame rate for the sped-up video (default: 30)')
    parser.add_argument('--speed-width', type=int, default=3840,
                        help='Output width for the sped-up video, keeping aspect ratio. '
                             'Use 7680 for full native resolution, or 0 to not rescale at all '
                             '(default: 3840, i.e. 3840x1080)')
    parser.add_argument('--speed-crf', type=int, default=18,
                        help='Quality of the sped-up encode, lower = better. 18 is near-lossless (default: 18)')
    parser.add_argument('--keep-clips', action='store_true',
                        help='Keep the individual downloaded clips after a successful merge. '
                             'By default they are deleted to save disk space (the merge is lossless, '
                             'so the merged file contains identical footage).')

    args = parser.parse_args()
    if args.speed:
        args.merge = True

    # Parse date strings
    start_dt = datetime.strptime(args.start, "%Y-%m-%d %H:%M:%S")
    end_dt = datetime.strptime(args.end, "%Y-%m-%d %H:%M:%S")

    start_time = start_dt.strftime("%Y%m%d%H%M%S")
    end_time = end_dt.strftime("%Y%m%d%H%M%S")

    # Create output directory
    os.makedirs(args.output, exist_ok=True)

    # Get authentication token
    token = get_token(args.ip, args.username, args.password, args.port)
    if not token:
        print("Failed to authenticate with camera")
        return

    # Always release the session, even on Ctrl+C. Abandoned sessions are what
    # wedge the camera's download service.
    try:
        run(args, token, start_time, end_time)
    except CameraRefusingDownloads as e:
        print(f"\n*** ABORTED ***\n{e}")
    except KeyboardInterrupt:
        print("\nInterrupted - releasing camera session before exit...")
    finally:
        logout(args.ip, token, args.port)


def run(args, token, start_time, end_time):

    # Search for recordings
    recordings = search_recordings(args.ip, token, args.channel, start_time, end_time, args.port)

    if not recordings:
        print("No recordings found")
        return

    print(f"Found {len(recordings)} recordings")

    # Display recordings
    print("\n===== RECORDINGS FOUND =====")
    for i, rec in enumerate(recordings):
        name = rec.get("name", "unknown")
        size_bytes = int(rec.get("size", 0))
        size_mb = size_bytes / (1024 * 1024)

        # Format the start and end times for better readability
        if "StartTime" in rec and "EndTime" in rec:
            st = rec["StartTime"]
            et = rec["EndTime"]
            start_str = f"{st['year']}-{st['mon']:02d}-{st['day']:02d} {st['hour']:02d}:{st['min']:02d}:{st['sec']:02d}"
            end_str = f"{et['year']}-{et['mon']:02d}-{et['day']:02d} {et['hour']:02d}:{et['min']:02d}:{et['sec']:02d}"
            print(f"{i+1}. {name} - Size: {size_mb:.2f}MB, Time: {start_str} to {end_str}")
        else:
            print(f"{i+1}. {name} - Size: {size_mb:.2f}MB")

    # Save recording info to JSON for reference
    with open(os.path.join(args.output, "recordings_info.json"), "w") as f:
        json.dump(recordings, f, indent=2)

    # Download recordings if not list-only mode
    if not args.list_only:
        print("\n===== DOWNLOADING RECORDINGS =====")
        downloaded_files = []

        for i, rec in enumerate(recordings):
            name = rec.get("name", "")
            if not name:
                print(f"Recording {i+1} has no filename, skipping")
                continue

            # Create output file path
            output_file = os.path.join(args.output, os.path.basename(name))
            expected_size = int(rec.get("size", 0))

            print(f"\nDownloading recording {i+1}/{len(recordings)}: {name}")

            # Add delay between downloads to avoid overwhelming the camera
            if i > 0:
                time.sleep(1)

            success = download_recording(args.ip, token, name, output_file,
                                         expected_size=expected_size, port=args.port)

            if success:
                downloaded_files.append(output_file)

        print(f"\nDownload complete. Successfully downloaded "
              f"{len(downloaded_files)} of {len(recordings)} recordings.")

        # ----- Post-processing: merge and/or speed up -----
        if args.merge and downloaded_files:
            print("\n===== POST-PROCESSING =====")
            # Merge in chronological order (filenames carry the timestamp).
            downloaded_files.sort()
            merged_path = os.path.join(args.output, "merged.mp4")
            merged = merge_videos(downloaded_files, merged_path)

            # Free the ~8GB of individual clips now that the lossless merge holds
            # the same footage. Only do this if every clip made it into the merge.
            if merged and not args.keep_clips:
                if len(downloaded_files) == len(recordings):
                    freed = sum(os.path.getsize(p) for p in downloaded_files)
                    for path in downloaded_files:
                        os.remove(path)
                    print(f"  Deleted {len(downloaded_files)} source clips "
                          f"(freed {freed / (1024 ** 3):.2f} GB). Use --keep-clips to retain them.")
                else:
                    print("  Some clips failed to download; keeping clips so nothing is lost.")

            if merged and args.speed:
                factor = int(args.speed) if args.speed == int(args.speed) else args.speed
                speed_path = os.path.join(args.output, f"merged_{factor}x.mp4")
                speed_up_video(merged, speed_path, factor,
                               fps=args.speed_fps, width=args.speed_width,
                               crf=args.speed_crf)

if __name__ == "__main__":
    main()


