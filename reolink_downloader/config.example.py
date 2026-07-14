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
# You pass only the START time on the command line. The recording ends this
# many hours later. (End time = start + RECORD_DURATION_HOURS.)
RECORD_DURATION_HOURS = 4

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
GDRIVE_CREDENTIALS_FILE = "service_account.json"
GDRIVE_FOLDER_ID = ""     # target Drive folder id (shared with the service account)

# ===================== Local files =====================
WORK_DIR = "./output"
DELETE_LOCAL_AFTER_UPLOAD = True   # remove local files once the upload succeeds
