#!/usr/bin/env python3
"""
FastAPI control server for the Reolink timelapse pipeline.

Runs one recording job at a time. Endpoints:

  GET  /                 -> quick human summary
  GET  /health           -> {"ok": true}  (for systemd / load checks)
  POST /start            -> start a 4h recording NOW (or ?duration_hours=, ?start=)
  GET  /status           -> full job status (state, disk free, progress, ...)
  POST /stop             -> stop the running job cleanly (finalize -> process ->
                            upload what was captured -> clean local files)

Start it with:  uvicorn server:app --host 0.0.0.0 --port 8000
(or just `python server.py`). systemd runs it on boot; a daily cron can hit
/start for the "scheduled" mode.

Only one job runs at a time. /start returns immediately (the work happens on a
background thread), so the call "takes no time".
"""
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

import config
from pipeline import RecordingJob, JobState, setup_logging, free_gb

app = FastAPI(title="Reolink Timelapse Recorder", version="1.0")
log = setup_logging()

# The single active/last job. Guarded by the GIL for these simple reads/writes;
# job internals are thread-safe on their own.
_job: Optional[RecordingJob] = None


def _job_running() -> bool:
    return _job is not None and _job.is_alive() and _job.state not in (
        JobState.DONE, JobState.ERROR, JobState.STOPPED)


@app.on_event("startup")
def _cleanup_orphans():
    """After a crash or reboot there may be leftover chunk/ts scratch files.
    Sweep them so the 30 GB disk starts clean. Finished *_NNNx.mp4 outputs are
    left alone (they may still need uploading)."""
    import glob
    import os
    work = config.WORK_DIR
    if not os.path.isdir(work):
        return
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


@app.get("/")
def root():
    if _job is None:
        return {"message": "Idle. POST /start to begin a recording.",
                "disk_free_gb": round(free_gb(config.WORK_DIR), 2)}
    return _job.status()


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/status")
def status():
    if _job is None:
        return {"state": "idle",
                "disk_free_gb": round(free_gb(config.WORK_DIR), 2),
                "alive": False}
    return _job.status()


@app.post("/start")
def start(
    duration_hours: Optional[float] = Query(
        None, description="Recording length in hours (default from config)."),
    start: Optional[str] = Query(
        None, description='Optional future start "YYYY-MM-DD HH:MM:SS". '
                          "Omit to start now."),
    upload: bool = Query(True, description="Upload the result to Google Drive."),
    keep_local: bool = Query(False, description="Keep the final file locally."),
):
    global _job
    if _job_running():
        raise HTTPException(status_code=409,
                            detail=f"A job is already running ({_job.state.value}). "
                                   "Stop it first.")

    start_at = None
    if start:
        try:
            start_at = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            raise HTTPException(status_code=400,
                                detail='start must be "YYYY-MM-DD HH:MM:SS".')

    _job = RecordingJob(duration_hours=duration_hours, upload=upload,
                        keep_local=keep_local, start_at=start_at)
    _job.start()
    log.info("Started job via API (duration=%sh, upload=%s, start=%s).",
             _job.duration_hours, upload, start_at)
    return {"message": "Recording started.", "status": _job.status()}


@app.post("/stop")
def stop():
    if _job is None or not _job.is_alive():
        raise HTTPException(status_code=409, detail="No job is running.")
    _job.stop()
    return {"message": "Stop requested. The job will finalize the current chunk, "
                       "process/upload what was captured, then clean up.",
            "status": _job.status()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.SERVER_HOST, port=config.SERVER_PORT)
