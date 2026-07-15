# Copy this file to `config.py` and fill in your real values.
# config.py is git-ignored so your passwords never get committed.

# ===================== Camera =====================
CAMERA_HOST = "sunrisesuatomation.ddns.net"
CAMERA_USER = "admin"
CAMERA_PASSWORD = "your-password-here"
RTSP_PORT = 554

# RTSP stream path for the lens/quality you want to record.
#   Duo 3 main lens, full quality (HEVC 7680x2160): "h265Preview_01_main"
#   Lower-res sub stream (smaller files):            "h265Preview_01_sub"
RTSP_STREAM_PATH = "h265Preview_01_main"

# ===================== Recording =====================
# Default length of one recording job. Starting a job records from "now" for
# this many hours (override per-job via the API / CLI).
RECORD_DURATION_HOURS = 4

# Record in fixed-length chunks so free disk can be checked between them and a
# stop doesn't wait hours. 600s = 10 min chunks -> a 4h job is ~24 chunks.
SEGMENT_SECONDS = 600

# ===================== Storage guards (small disk, e.g. 30 GB EC2) =====================
DISK_MIN_FREE_GB = 6          # stop capturing early if free disk drops below this
WATCHDOG_STALL_SECONDS = 30   # kill+reconnect if recording produces no new bytes this long

# ===================== Speed-up / output =====================
SPEED_FACTOR = 200        # 4h of footage -> 72s at 200x
SPEED_FPS = 30            # output frame rate
SPEED_WIDTH = 3840        # output width (keeps 32:9). 0 = keep native 7680.
SPEED_CRF = 18            # encode quality, lower = better (18 ~ visually lossless)

# ===================== Google Drive =====================
# Uses a Google service account (headless, no browser needed).
# One-time setup:
#   1. Go to https://console.cloud.google.com/ -> create a project.
#   2. APIs & Services -> Enable APIs -> enable "Google Drive API".
#   3. APIs & Services -> Credentials -> Create Credentials -> Service account.
#   4. Open the service account -> Keys -> Add key -> JSON. Save it next to this
#      file as service_account.json (this file is also git-ignored).
#   5. In Google Drive, create a folder, right-click -> Share, and share it with
#      the service account's email (looks like name@project.iam.gserviceaccount.com)
#      as "Editor".
#   6. Open that folder in the browser; the URL ends with the folder id:
#      https://drive.google.com/drive/folders/<THIS_IS_THE_FOLDER_ID>
#      Paste it below.
# Auth mode:
#   "oauth"           -> upload as a real Google user (uses THAT user's Drive
#                        quota; works with a normal Gmail). Create token.json once
#                        with gdrive_auth.py (needs an OAuth "Desktop app" client).
#   "service_account" -> upload as the service account. This ONLY works into a
#                        Shared Drive (a service account has no personal quota).
GDRIVE_AUTH = "oauth"
GDRIVE_OAUTH_CLIENT_FILE = "oauth_client.json"    # OAuth Desktop client secret JSON
GDRIVE_OAUTH_TOKEN_FILE = "token.json"            # produced by gdrive_auth.py
GDRIVE_CREDENTIALS_FILE = "service_account.json"  # used only for GDRIVE_AUTH="service_account"
GDRIVE_FOLDER_ID = ""     # target Drive folder id (you must have write access to it)

# ===================== Local files =====================
WORK_DIR = "./output"
LOG_DIR = "./logs"
DELETE_LOCAL_AFTER_UPLOAD = True   # remove local files once the upload succeeds

# ===================== FastAPI control server =====================
SERVER_HOST = "0.0.0.0"   # bind address (use 127.0.0.1 to keep it local-only)
SERVER_PORT = 8000
