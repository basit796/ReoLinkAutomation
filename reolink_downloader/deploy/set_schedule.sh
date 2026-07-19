#!/bin/bash
# Set (or change) the daily auto-recording times. Times are in the server's local
# timezone, which is America/New_York on this box (check with: timedatectl).
#
# Usage:
#   ./set_schedule.sh 4:00 16:30   -> two runs a day: 04:00 and 16:30
#   ./set_schedule.sh 4            -> a single run at 04:00
#   ./set_schedule.sh list         -> show the current schedule
#   ./set_schedule.sh off          -> remove all schedules
#
# Each entry POSTs /start to the local server, which records for
# RECORD_DURATION_HOURS (config.py). If a job is already running the server
# safely returns 409 and the extra trigger does nothing.
MARK="# reolink-daily-record"
PORT="${PORT:-8000}"

# everything in the current crontab that is NOT one of our lines
existing="$(crontab -l 2>/dev/null | grep -v "$MARK")"

show() {
    echo "Current schedule (${1:-$(date +%Z)}):"
    crontab -l 2>/dev/null | grep "$MARK" || echo "  (none)"
}

case "$1" in
    list|"")
        show
        exit 0
        ;;
    off)
        printf '%s\n' "$existing" | grep -v '^$' | crontab - || true
        echo "All recording schedules removed."
        exit 0
        ;;
esac

lines=""
for t in "$@"; do
    hour="${t%%:*}"
    min="${t#*:}"
    [ "$min" = "$t" ] && min=0          # "4" -> 4:00
    hour=$((10#$hour)); min=$((10#$min))
    if [ "$hour" -gt 23 ] || [ "$min" -gt 59 ]; then
        echo "Bad time: $t (use HH or HH:MM, 24-hour)"; exit 1
    fi
    lines="${lines}${min} ${hour} * * * curl -s -X POST localhost:${PORT}/start >> /home/ec2-user/cron-record.log 2>&1 ${MARK}"$'\n'
    printf 'Scheduled daily recording at %02d:%02d\n' "$hour" "$min"
done

{ printf '%s\n' "$existing" | grep -v '^$'; printf '%s' "$lines"; } | crontab -
echo
show
