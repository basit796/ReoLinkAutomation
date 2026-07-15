#!/bin/bash
# Set (or change) the daily auto-recording time. Times are in the server's local
# timezone, which is America/New_York on this box (check with: timedatectl).
#
# Usage:
#   ./set_schedule.sh 4        -> every day at 04:00
#   ./set_schedule.sh 4 30     -> every day at 04:30
#   ./set_schedule.sh off      -> remove the schedule
#
# It POSTs /start to the local server; if a job is already running the server
# safely returns 409 and does nothing.
set -e
MARK="# reolink-daily-record"
PORT="${PORT:-8000}"

if [ "$1" = "off" ]; then
    ( crontab -l 2>/dev/null | grep -v "$MARK" ) | crontab - || true
    echo "Daily recording schedule removed."
    exit 0
fi

HOUR="$1"
MIN="${2:-0}"
if ! [[ "$HOUR" =~ ^[0-9]+$ ]] || [ "$HOUR" -gt 23 ]; then
    echo "Usage: ./set_schedule.sh HOUR [MINUTE]   (HOUR 0-23) | off"
    exit 1
fi

LINE="$MIN $HOUR * * * curl -s -X POST localhost:$PORT/start >> /home/ec2-user/cron-record.log 2>&1 $MARK"
( crontab -l 2>/dev/null | grep -v "$MARK"; echo "$LINE" ) | crontab -
printf 'Scheduled daily recording at %02d:%02d (%s).\n' "$HOUR" "$MIN" "$(date +%Z)"
echo "Current crontab:"
crontab -l | grep "$MARK"
