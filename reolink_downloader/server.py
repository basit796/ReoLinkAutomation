#!/usr/bin/env python3
"""
FastAPI control server for the Reolink timelapse pipeline.

Runs one recording job at a time and hosts a web control panel so the client
can change the daily start/end time, speed-up factor, crop, quality and Drive
folder from a form -- no code edits, no OS crontab.

Endpoints:
  GET  /          -> HTML control panel (status + editable settings form)
  POST /settings  -> save settings from the form (applies live, next job uses them)
  GET  /health    -> {"ok": true}
  GET  /status    -> full job status (JSON)
  POST /start     -> start a recording NOW (?duration_hours=, ?start=, ...)
  POST /stop      -> stop the running job cleanly (finalize -> process -> upload)

The daily recording is fired by a built-in scheduler thread (see settings.py),
so there is no crontab to maintain. Times use the timezone in settings.

Start it with:  python server.py     (systemd runs this on boot)
"""
import json
import os
import threading
import time
from datetime import datetime
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import config
import settings as settings_store
from pipeline import RecordingJob, JobState, setup_logging, free_gb

app = FastAPI(title="Reolink Timelapse Recorder", version="2.0")
log = setup_logging()

# Apply any saved settings.json onto the live config before anything runs.
settings_store.apply_to_config()

# The single active/last job. Guarded by the GIL for these simple reads/writes.
_job: Optional[RecordingJob] = None

_SCHED_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "scheduler_state.json")


def _job_running() -> bool:
    return _job is not None and _job.is_alive() and _job.state not in (
        JobState.DONE, JobState.ERROR, JobState.STOPPED)


def _start_job(duration_hours=None, upload=True, keep_local=False, start_at=None):
    global _job
    _job = RecordingJob(duration_hours=duration_hours, upload=upload,
                        keep_local=keep_local, start_at=start_at)
    _job.start()
    return _job


# --------------------------------------------------------------------------- #
# Built-in daily scheduler (replaces the OS crontab)
# --------------------------------------------------------------------------- #
def _load_sched_state():
    try:
        with open(_SCHED_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_sched_state(state):
    try:
        with open(_SCHED_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


def _tz(name):
    if ZoneInfo is None:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def _scheduler_loop():
    log.info("Scheduler thread started.")
    while True:
        try:
            s = settings_store.load()
            if s.get("enabled"):
                now = datetime.now(_tz(s.get("timezone", "America/New_York")))
                today = now.date().isoformat()
                hh, mm = [int(x) for x in str(s["start_time"]).split(":")]
                state = _load_sched_state()
                if (now.hour == hh and now.minute == mm
                        and state.get("last_fired") != today
                        and not _job_running()):
                    state["last_fired"] = today
                    _save_sched_state(state)
                    dur = settings_store.duration_hours(s)
                    _start_job(duration_hours=dur)
                    log.info("Scheduler fired daily job: %.3fh, start %s %s.",
                             dur, s["start_time"], s.get("timezone"))
        except Exception as e:  # never let the scheduler die
            log.exception("Scheduler loop error: %s", e)
        time.sleep(20)


@app.on_event("startup")
def _cleanup_orphans():
    """After a crash or reboot sweep leftover chunk/ts scratch files so the
    30 GB disk starts clean. Finished *_NNNx.mp4 outputs are left alone."""
    import glob
    work = config.WORK_DIR
    if os.path.isdir(work):
        removed = 0
        for pat in ("*_seg*.mp4", "*_ts*.ts", "_concat_*.ts", "*_ffmpeg.log", "*_raw.mp4"):
            for p in glob.glob(os.path.join(work, pat)):
                try:
                    os.remove(p)
                    removed += 1
                except OSError:
                    pass
        if removed:
            log.info("Startup cleanup: removed %d orphaned temp file(s).", removed)


@app.on_event("startup")
def _start_scheduler():
    threading.Thread(target=_scheduler_loop, name="scheduler", daemon=True).start()


# --------------------------------------------------------------------------- #
# Web control panel
# --------------------------------------------------------------------------- #
def _drive_service():
    """A Drive v3 service via the pipeline's own auth (unstarted job, no camera)."""
    return RecordingJob()._drive_service()


def _current_audio_name():
    """Best-effort: the latest audio filename in the Drive folder, or None."""
    folder = getattr(config, "GDRIVE_AUDIO_FOLDER_ID", "")
    if not folder:
        return None
    try:
        job = RecordingJob()
        latest = job._latest_audio_file(job._drive_service(), folder)
        return latest[1] if latest else None
    except Exception:  # never break the panel over a Drive hiccup
        return None


def _next_run_text(s):
    if not s.get("enabled"):
        return "Daily schedule is OFF"
    tz = _tz(s.get("timezone", "America/New_York"))
    now = datetime.now(tz)
    hh, mm = [int(x) for x in str(s["start_time"]).split(":")]
    from datetime import timedelta
    nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return nxt.strftime("%a %d %b, %H:%M ") + s.get("timezone", "")


def _render_panel(msg="", errors=None):
    s = settings_store.load()
    dur = settings_store.duration_hours(s)
    st = _job.status() if _job is not None else {"state": "idle"}
    state = st.get("state", "idle")
    disk = round(free_gb(config.WORK_DIR), 2)
    est = (dur * 3600) / max(int(s.get("SPEED_FACTOR") or 1), 1) + float(s.get("INTRO_SECONDS") or 0)
    audio_name = _current_audio_name() or "— none uploaded yet —"
    audio_on = "ON" if s.get("AUDIO_ENABLED") else "OFF"
    err_html = ""
    if errors:
        err_html = "<div class='err'>" + "<br>".join(errors) + "</div>"
    if msg:
        err_html += f"<div class='ok'>{msg}</div>"

    running = _job_running()
    badge_color = {"recording": "#e0245e", "speeding": "#f39c12",
                   "uploading": "#3498db", "done": "#2ecc71",
                   "error": "#e74c3c"}.get(state, "#888")

    # status detail lines
    detail = ""
    if _job is not None:
        for k in ("tag", "captured_seconds", "record_ends_at", "drive_link"):
            if st.get(k):
                v = st[k]
                if k == "drive_link":
                    v = f"<a href='{v}' target='_blank'>{v}</a>"
                detail += f"<tr><td>{k}</td><td>{v}</td></tr>"

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sunrise Timelapse Control</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         max-width: 720px; margin: 0 auto; padding: 20px; line-height: 1.5;
         background: #f6f7f9; color: #1a1a1a; }}
  @media (prefers-color-scheme: dark) {{ body {{ background:#15171a; color:#e8e8e8; }}
    .card {{ background:#1f2226 !important; }} input,select {{ background:#2a2e33; color:#e8e8e8; border-color:#3a3f45 !important; }} }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .sub {{ color:#888; margin:0 0 18px; font-size:13px; }}
  .card {{ background:#fff; border-radius:12px; padding:18px 20px; margin-bottom:16px;
          box-shadow:0 1px 3px rgba(0,0,0,.08); }}
  .badge {{ display:inline-block; padding:3px 12px; border-radius:20px; color:#fff;
           font-weight:600; font-size:13px; background:{badge_color}; }}
  label {{ display:block; font-weight:600; font-size:13px; margin:12px 0 4px; }}
  .hint {{ font-weight:400; color:#999; font-size:12px; }}
  input, select {{ width:100%; box-sizing:border-box; padding:9px 11px; font-size:15px;
          border:1px solid #d5d8dc; border-radius:8px; }}
  .row {{ display:flex; gap:14px; }} .row > div {{ flex:1; }}
  button {{ font-size:15px; font-weight:600; padding:11px 18px; border:0; border-radius:8px;
           cursor:pointer; }}
  .primary {{ background:#2563eb; color:#fff; }}
  .go {{ background:#16a34a; color:#fff; }} .stop {{ background:#dc2626; color:#fff; }}
  .est {{ font-size:15px; background:#eef4ff; border-radius:8px; padding:10px 12px; margin-top:12px; }}
  @media (prefers-color-scheme: dark) {{ .est {{ background:#1c2733; }} }}
  table {{ width:100%; font-size:13px; border-collapse:collapse; }}
  td {{ padding:3px 6px; border-bottom:1px solid rgba(128,128,128,.15); vertical-align:top; word-break:break-all; }}
  td:first-child {{ color:#888; width:150px; }}
  .err {{ background:#fdecea; color:#b71c1c; padding:10px 12px; border-radius:8px; margin-bottom:12px; }}
  .ok {{ background:#e8f5e9; color:#1b5e20; padding:10px 12px; border-radius:8px; margin-bottom:12px; }}
  details summary {{ cursor:pointer; font-weight:600; margin-top:6px; }}
  .muted {{ color:#888; font-size:12px; }}
</style></head><body>
<h1>🌅 Sunrise Timelapse Control</h1>
<p class="sub">Change the recording window, speed and look here — the daily video updates automatically. No code needed.</p>
{err_html}

<div class="card">
  <span class="badge">{state.upper()}</span>
  <span class="muted"> &nbsp; Disk free: {disk} GB &nbsp;•&nbsp; Next run: {_next_run_text(s)}</span>
  <table style="margin-top:10px">{detail}</table>
</div>

<form method="post" action="/settings">
<div class="card">
  <div class="row">
    <div><label>Start time <span class="hint">(24h)</span>
      <input name="start_time" id="start" value="{s['start_time']}" placeholder="06:00"></label></div>
    <div><label>End time <span class="hint">(24h)</span>
      <input name="end_time" id="end" value="{s['end_time']}" placeholder="08:00"></label></div>
  </div>
  <label>Speed factor <span class="hint">(higher = shorter video)</span>
    <input name="SPEED_FACTOR" id="speed" type="number" min="1" value="{s['SPEED_FACTOR']}"></label>
  <label>Daily schedule
    <select name="enabled">
      <option value="true" {"selected" if s.get("enabled") else ""}>ON — record every day</option>
      <option value="false" {"" if s.get("enabled") else "selected"}>OFF — manual only</option>
    </select></label>
  <label>Background audio
    <select name="AUDIO_ENABLED">
      <option value="true" {"selected" if s.get("AUDIO_ENABLED") else ""}>ON — add latest audio to video</option>
      <option value="false" {"" if s.get("AUDIO_ENABLED") else "selected"}>OFF — silent video</option>
    </select></label>
  <div class="est" id="est">Estimated video length: <b id="len">…</b>
    <span class="muted">(recording window ÷ speed + intro)</span></div>

  <details>
    <summary>Advanced (crop, quality, folder, timezone)</summary>
    <div class="row">
      <div><label>Crop left <input name="CROP_LEFT" type="number" step="0.01" value="{s['CROP_LEFT']}"></label></div>
      <div><label>Crop right <input name="CROP_RIGHT" type="number" step="0.01" value="{s['CROP_RIGHT']}"></label></div>
    </div>
    <div class="row">
      <div><label>Crop top <input name="CROP_TOP" type="number" step="0.01" value="{s['CROP_TOP']}"></label></div>
      <div><label>Crop bottom <input name="CROP_BOTTOM" type="number" step="0.01" value="{s['CROP_BOTTOM']}"></label></div>
    </div>
    <div class="row">
      <div><label>Output width px <input name="SPEED_WIDTH" type="number" value="{s['SPEED_WIDTH']}"></label></div>
      <div><label>Intro seconds <input name="INTRO_SECONDS" type="number" step="0.5" value="{s['INTRO_SECONDS']}"></label></div>
    </div>
    <label>Timezone <input name="timezone" value="{s['timezone']}"></label>
    <label>Google Drive folder ID <input name="GDRIVE_FOLDER_ID" value="{s['GDRIVE_FOLDER_ID']}"></label>
  </details>

  <div style="margin-top:16px"><button class="primary" type="submit">💾 Save settings</button></div>
</div>
</form>

<div class="card">
  <form method="post" action="/start" style="display:inline">
    <button class="go" type="submit" {"disabled" if running else ""}>▶ Record now (test)</button>
  </form>
  <form method="post" action="/stop" style="display:inline; margin-left:8px">
    <button class="stop" type="submit" {"" if running else "disabled"}>■ Stop current job</button>
  </form>
  <p class="muted">“Record now” starts a recording immediately using the current window length ({dur:.2f} h).</p>
</div>

<div class="card">
  <h3 style="margin:0 0 6px">🎵 Background audio <span class="muted" style="font-weight:400">({audio_on})</span></h3>
  <p style="margin:0 0 10px">Current track on the video: <b>{audio_name}</b></p>
  <form method="post" action="/upload-audio" enctype="multipart/form-data">
    <input type="file" name="audio" accept="audio/*" required>
    <div style="margin-top:10px"><button class="primary" type="submit">⬆ Upload audio</button></div>
  </form>
  <p class="muted">Upload an audio file and it becomes the newest track — every new video from then on uses it.
    Shorter audio is looped, longer is trimmed to the video length. Turn it on/off with “Background audio” above.</p>
</div>

<script>
function fmt(sec){{ sec=Math.round(sec); var m=Math.floor(sec/60), s=sec%60;
  return m>0 ? (m+"m "+s+"s") : (s+"s"); }}
function calc(){{
  var a=document.getElementById('start').value.split(':');
  var b=document.getElementById('end').value.split(':');
  var sp=parseFloat(document.getElementById('speed').value)||1;
  var intro={float(s.get('INTRO_SECONDS') or 0)};
  if(a.length<2||b.length<2){{document.getElementById('len').textContent='—';return;}}
  var mins=(b[0]*60+ +b[1])-(a[0]*60+ +a[1]); if(mins<=0) mins+=1440;
  var out=(mins*60)/sp + intro;
  document.getElementById('len').textContent=fmt(out)+"  (recording "+(mins/60).toFixed(2)+" h)";
}}
['start','end','speed'].forEach(function(id){{document.getElementById(id).addEventListener('input',calc);}});
calc();
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def root():
    return _render_panel()


@app.post("/settings", response_class=HTMLResponse)
async def save_settings(request: Request):
    form = await request.form()
    data = {k: form[k] for k in form}
    merged, errors = settings_store.save(data)
    if errors:
        return HTMLResponse(_render_panel(errors=errors), status_code=400)
    log.info("Settings updated via form: start=%s end=%s speed=%s enabled=%s",
             merged["start_time"], merged["end_time"],
             merged["SPEED_FACTOR"], merged["enabled"])
    return HTMLResponse(_render_panel(msg="Saved. The next recording will use these settings."))


@app.post("/upload-audio", response_class=HTMLResponse)
async def upload_audio(request: Request):
    """Receive an audio file from the panel and upload it into the Drive audio
    folder, where it becomes the newest track used by the next video."""
    from googleapiclient.http import MediaFileUpload
    form = await request.form()
    up = form.get("audio")
    if up is None or not getattr(up, "filename", ""):
        return HTMLResponse(_render_panel(errors=["Choose an audio file first."]), status_code=400)
    folder = getattr(config, "GDRIVE_AUDIO_FOLDER_ID", "")
    if not folder:
        return HTMLResponse(_render_panel(errors=["No audio folder configured (GDRIVE_AUDIO_FOLDER_ID)."]),
                            status_code=400)
    tmp = os.path.join(config.WORK_DIR, "_audio_upload_" + os.path.basename(up.filename))
    try:
        os.makedirs(config.WORK_DIR, exist_ok=True)
        data = await up.read()
        with open(tmp, "wb") as f:
            f.write(data)
        service = _drive_service()
        media = MediaFileUpload(tmp, mimetype=(up.content_type or "audio/mpeg"), resumable=True)
        service.files().create(
            body={"name": up.filename, "parents": [folder]},
            media_body=media, fields="id,name", supportsAllDrives=True).execute()
        log.info("Audio uploaded via panel: %s (%d bytes).", up.filename, len(data))
    except Exception as e:  # noqa: BLE001
        log.exception("Audio upload failed: %s", e)
        return HTMLResponse(_render_panel(errors=[f"Audio upload failed: {e}"]), status_code=500)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return HTMLResponse(_render_panel(
        msg=f"Audio “{up.filename}” uploaded — it will be added to the next video."))


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/status")
def status():
    if _job is None:
        s = settings_store.load()
        return {"state": "idle",
                "speed_factor": config.SPEED_FACTOR,
                "duration_hours": settings_store.duration_hours(s),
                "next_run": _next_run_text(s),
                "disk_free_gb": round(free_gb(config.WORK_DIR), 2),
                "alive": False}
    return _job.status()


@app.post("/start")
def start(
    duration_hours: Optional[float] = Query(None),
    start: Optional[str] = Query(None),
    upload: bool = Query(True),
    keep_local: bool = Query(False),
):
    if _job_running():
        raise HTTPException(status_code=409,
                            detail=f"A job is already running ({_job.state.value}).")
    start_at = None
    if start:
        try:
            start_at = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            raise HTTPException(status_code=400, detail='start must be "YYYY-MM-DD HH:MM:SS".')
    if duration_hours is None:
        duration_hours = settings_store.duration_hours()
    _start_job(duration_hours=duration_hours, upload=upload,
               keep_local=keep_local, start_at=start_at)
    log.info("Started job via API (duration=%sh, upload=%s, start=%s).",
             _job.duration_hours, upload, start_at)
    # Browser form posts land here too -> send them back to the panel.
    return RedirectResponse(url="/", status_code=303)


@app.post("/stop")
def stop():
    if _job is None or not _job.is_alive():
        raise HTTPException(status_code=409, detail="No job is running.")
    _job.stop()
    return RedirectResponse(url="/", status_code=303)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.SERVER_HOST, port=config.SERVER_PORT)
