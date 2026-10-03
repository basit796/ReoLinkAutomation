# Deploying the Reolink timelapse recorder on EC2

A control server records the camera's live stream for 4h, speeds it up 200x,
uploads the result to Google Drive, and deletes every local file. Built to stay
within a **30 GB** disk: it records in 10-minute chunks, checks free space
between chunks, never writes a full merged copy, and always cleans up temp files
(even on error/stop/reboot).

Peak disk use ≈ one recording's worth (~10 GB for 4h). If free space drops below
`DISK_MIN_FREE_GB` (default 6) it stops capturing early and processes what it has.

---

## 1. Connect

```bash
ssh -i "C:\Users\Noman traders\Downloads\cameraRecording.pem" ec2-user@3.95.195.115
```

## 2. Install system deps (Amazon Linux)

```bash
sudo yum install -y git python3 python3-pip
# ffmpeg (static build – no reliable yum package on Amazon Linux):
cd /tmp
curl -L -o ffmpeg.tar.xz https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
tar xf ffmpeg.tar.xz
sudo cp ffmpeg-*-static/ffmpeg ffmpeg-*-static/ffprobe /usr/local/bin/
ffmpeg -version   # confirm it works
```

## 3. Get the code

```bash
cd /home/ec2-user
git clone <your-repo-url> ReoLinkAutomation
cd ReoLinkAutomation/reolink_downloader
```

## 4. Python environment

```bash
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
```

## 5. Config + secrets (NOT in git)

```bash
cp config.example.py config.py
nano config.py            # set camera host/user/password, GDRIVE_FOLDER_ID
# upload your Google service account key as service_account.json (see below)
```

Copy the two secret files from your PC (run these **on your PC**, not the server):

```bash
scp -i "C:\Users\Noman traders\Downloads\cameraRecording.pem" ^
    config.py ec2-user@3.95.195.115:/home/ec2-user/ReoLinkAutomation/reolink_downloader/
scp -i "C:\Users\Noman traders\Downloads\cameraRecording.pem" ^
    service_account.json ec2-user@3.95.195.115:/home/ec2-user/ReoLinkAutomation/reolink_downloader/
```

> Note: the file **must** be named `service_account.json` (a file called
> `service_account.json.json` was found in the repo — rename it).

## 6. Quick manual test (short, no upload)

```bash
.venv/bin/python pipeline.py --duration-hours 0.05 --no-upload --keep-local
ls -lh output/           # should show one small *_200x.mp4
```

## 7. Install as a systemd service (auto-start on boot)

```bash
sudo cp deploy/reolink-recorder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now reolink-recorder
systemctl status reolink-recorder
```

The server now listens on `http://127.0.0.1:8000` (and `0.0.0.0:8000` if you open
the port). Keep it bound to localhost and drive it via SSH unless you add auth.

---

## Using it

Everything is a one-line command; each returns immediately.

```bash
# Start a 4h recording NOW:
curl -X POST localhost:8000/start

# Start a custom length:
curl -X POST "localhost:8000/start?duration_hours=2"

# Check status (state, disk free, progress, drive link):
curl localhost:8000/status

# Stop cleanly (finalizes current chunk, processes+uploads what was captured,
# then deletes all local files):
curl -X POST localhost:8000/stop
```

Logs:

```bash
tail -f logs/pipeline.log          # pipeline stages, disk usage, upload progress
journalctl -u reolink-recorder -f  # the server process itself
```

---

## Scheduled daily run (cron)

Cron (cronie) is used to auto-start a recording each day. Install it once:

```bash
sudo yum install -y cronie
sudo systemctl enable --now crond
```

Then set / change the time with the helper (server timezone = America/New_York):

```bash
./deploy/set_schedule.sh 4        # every day at 04:00 NY  <-- current setting
./deploy/set_schedule.sh 6 30     # change to 06:30
./deploy/set_schedule.sh off      # remove the schedule
crontab -l                        # see the current schedule
```

The server keeps running via systemd; cron just pokes `/start`. If a job is
already running, `/start` returns HTTP 409 and does nothing (safe).

## Google Drive auth (OAuth)

A service account has no Drive storage of its own, so uploads use OAuth as a real
Google user (`GDRIVE_AUTH = "oauth"` in config.py). Set up once:

1. Google Cloud Console -> APIs & Services -> Credentials -> Create OAuth client ID
   -> "Desktop app" -> download JSON as `oauth_client.json`.
2. OAuth consent screen -> add your account as a Test user, then **Publish app**
   (so the refresh token doesn't expire after 7 days).
3. On a machine with a browser: `python gdrive_auth.py` -> sign in -> creates
   `token.json`. Copy `token.json` (and `oauth_client.json`) next to config.py on
   the server. The server refreshes the token automatically thereafter.

Uploaded files land in `GDRIVE_FOLDER_ID` and are owned by (and use the quota of)
whichever account authorized in step 3.

---

## What happens on failure / stuck stream / reboot

- **Stuck RTSP** (no data for `WATCHDOG_STALL_SECONDS`): ffmpeg is killed and the
  capture reconnects into a new chunk automatically.
- **Low disk**: capture stops early; whatever was recorded is still sped up and
  uploaded.
- **Upload fails**: the final file is kept locally (not deleted) so nothing is
  lost; retry by re-uploading or re-running.
- **Crash / reboot**: on next start the server sweeps orphaned chunk/ts temp
  files, so the disk is left clean. (A recording interrupted by a hard reboot is
  lost — use `/stop` for a clean shutdown instead.)
