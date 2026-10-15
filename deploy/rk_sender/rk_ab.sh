#!/usr/bin/env bash
# Back-to-back two-camera runs of rk_sender, each with --frame-log and --trace,
# to compare MPP builds and sender settings on the same board and receiver.
#
#   bash rk_ab.sh <receiver-ip> [seconds per run, default 75]
#
# Start the receiver page first (RTP, 2 cameras) and leave it running: every run
# is a new sender process, and the receiver picks each one up by itself.
#
#   stagger  rk_sender 0.4.3 defaults: MPP from mpp-pkt/ (the packet-poll fix),
#            the two streams' IDRs kept half a GOP apart. No debug log
#   aligned  the same with --idr-stagger off: both streams' IDRs at once, as
#            in the 10-15 pkt runs
#   gst      the board's MPP (--mpp-lib system); GStreamer debug log of the
#            encoder plugin: when each frame enters handle_frame, is handed to
#            MPP, and comes back
#   pkt      MPP 2024-11 with mpp-pkt-poll-fix.patch (mpp-pkt/ next to the pyz),
#            with the GStreamer log. On 10-15 it removed the encoder stalls
#   mpp      like gst, plus MPP's own debug log of its encoder thread (each
#            wait/work decision and notification). MPP also writes these lines to
#            the system log (about 40k lines per run)
#   fix      MPP 2024-11 with upstream's 2026-07 wait-condition fix (mpp-fix/),
#            with the GStreamer log; changed nothing on 10-15
#   base     MPP 2024-11 unchanged (mpp-base/), with the GStreamer log
#   sys      the board's MPP without any debug log
#
# The default is "stagger aligned stagger aligned". Only the sender process
# loads mpp-*/ (--mpp-lib); the system library and the robot's services are not
# touched. RK_AB_RUNS="gst pkt" picks other runs or another order. A run whose
# library is missing, or that cannot start, is logged and skipped. Ctrl+C stops
# the current run and skips the rest. Keep the robot lying down or supported, as
# for every two-camera test.
#
# Files: ~/Public/fzh/run/ab-<time>/<run>.{log,stats.jsonl,frames.jsonl,trace.jsonl};
# the debug logs are in <run>.log.

host=${1:-}
secs=${2:-75}
case "$host" in ''|-*) echo "usage: bash rk_ab.sh <receiver-ip> [seconds per run]" >&2; exit 2 ;; esac
case "$secs" in ''|*[!0-9]*) echo "seconds must be a whole number above 0" >&2; exit 2 ;; esac
[ "$secs" -gt 0 ] || { echo "seconds must be a whole number above 0" >&2; exit 2; }
pause=${RK_AB_PAUSE:-10}
python=${RK_PYTHON:-/usr/bin/python3}
pyz=${RK_SENDER_PYZ:-$HOME/Public/fzh/rk_sender.pyz}
libs=${RK_MPP_LIBS:-$(dirname "$pyz")}
dir=${RK_AB_DIR:-$HOME/Public/fzh/run}/ab-$(date +%Y%m%d-%H%M%S)
gstlog='GST_DEBUG=mppenc:5,mpph264enc:5 GST_DEBUG_NO_COLOR=1'
mpplog='mpp_enc_debug=0x90 mpp_syslog_perror=1'
mkdir -p "$dir" || exit 1
stopped=0
trap 'stopped=1' INT TERM
used=' '

one() {  # one <name> <VAR=value ... or empty> [sender arguments...]
  local name=$1 envs=$2 rc
  shift 2
  [ "$stopped" = 0 ] || return 0
  echo "[ab] $(date +%T) run $name: ${envs:-no debug log}; $*; ${secs}s"
  # shellcheck disable=SC2086  # $envs is a list of VAR=value words
  env $envs "$python" "$pyz" --host "$host" --port 5004 \
    --device /dev/video23 --label 前摄像头 --device /dev/video32 --label 后摄像头 \
    --width 1280 --height 720 --fps 30 --bitrate-kbps 3000 --duration "$secs" \
    --stats-file "$dir/$name.stats.jsonl" --frame-log "$dir/$name.frames.jsonl" \
    --trace "$dir/$name.trace.jsonl" "$@" > "$dir/$name.log" 2>&1
  rc=$?
  echo "[ab] $(date +%T) run $name: exit $rc"
  grep -E '^\[rk( |\])' "$dir/$name.log" | head -n 3
  if [ "$stopped" = 0 ]; then sleep "$pause"; fi
}

for run in ${RK_AB_RUNS:-stagger aligned stagger aligned}; do
  name=$run k=1
  while case "$used" in *" $name "*) true ;; *) false ;; esac; do
    k=$((k + 1))
    name=$run$k
  done
  used="$used$name "
  case "$run" in
    gst) one "$name" "$gstlog" --mpp-lib system ;;
    mpp) one "$name" "$gstlog $mpplog" --mpp-lib system ;;
    sys) one "$name" '' --mpp-lib system ;;
    base|fix|pkt|stagger|aligned)
      case "$run" in
        stagger|aligned) lib=$libs/mpp-pkt/librockchip_mpp.so.1 envs= ;;
        *) lib=$libs/mpp-$run/librockchip_mpp.so.1 envs=$gstlog ;;
      esac
      if [ ! -f "$lib" ]; then
        echo "[ab] skip $name: $lib not found"
      elif [ "$run" = aligned ]; then
        one "$name" "$envs" --mpp-lib "$lib" --idr-stagger off
      else
        one "$name" "$envs" --mpp-lib "$lib"
      fi ;;
    *) echo "[ab] skip unknown run: $run" ;;
  esac
done
echo "[ab] files: $dir"
