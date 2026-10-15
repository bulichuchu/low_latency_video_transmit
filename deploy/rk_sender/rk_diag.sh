#!/usr/bin/env bash
# Read-only diagnostics for the robot's RK3588 board: who shares the cameras,
# the MPP encoder and the CPUs with rk_sender, and what the board does while
# a stream stalls.
#
#   bash rk_diag.sh off    # rk_sender stopped: the robot's own baseline
#   bash rk_diag.sh on     # while rk_sender is streaming (start it first)
#
# Writes one text report, ~/Public/fzh/run/rk-diag-<time>-<label>.txt
# (another folder: RK_DIAG_DIR=...), and nothing else. Asks for sudo once; you
# type the password yourself and it is not stored. Root is only used to read:
# which processes have device files open, the kernel log, the owners of UDP
# sockets and root-only Rockchip procfs entries. RK_DIAG_SUDO=0 skips those.
#
# Never opens camera (/dev/video*, /dev/v4l-subdev*, /dev/media*), encoder
# (/dev/mpp_service) or RGA devices, and runs no GStreamer program (loading the
# MPP plugin opens the encoder; a registry rescan opens every /dev/video*).
# Never writes /proc or /sys, never changes priorities, affinities, clocks, the
# network or services. Takes about 15 seconds (RK_DIAG_SECONDS sets the
# measurement window, default 5).
#
# The Rockchip camera/encoder procfs files are read last, through the drivers'
# own status callbacks; RK_DIAG_PROCFS=0 skips them.

VERSION=2
label=${1:-run}
case "$label" in
  *[!A-Za-z0-9_.-]*) echo "label: use letters, digits, '.', '_' or '-'" >&2; exit 2 ;;
esac
window=${RK_DIAG_SECONDS:-5}
case "$window" in ''|*[!0-9]*) window=5 ;; esac
if [ "$window" -lt 1 ] || [ "$window" -gt 60 ]; then window=5; fi
out_dir=${RK_DIAG_DIR:-$HOME/Public/fzh/run}
mkdir -p "$out_dir" || exit 1
report="$out_dir/rk-diag-$(date +%Y%m%d-%H%M%S)-$label.txt"

ROOT=0
if [ "${RK_DIAG_SUDO:-1}" != 0 ]; then
  echo "[rk-diag] sudo is only used to read (open device files, dmesg, socket owners, procfs). Ctrl+C to stop." >&2
  if sudo -v; then ROOT=1; else echo "[rk-diag] continuing without sudo; root-only sections are skipped" >&2; fi
fi

exec 3>&2  # progress goes to the terminal; everything else into the report
section() { printf '\n===== %s =====\n' "$*"; echo "[rk-diag] $*" >&3; }
# Every command is time-limited and may fail without stopping the script.
run() { timeout 10 "$@" 2>&1 || echo "(exit $?: $*)"; }
asroot() {
  if [ "$ROOT" = 1 ]; then timeout 20 sudo -n "$@" 2>&1 || echo "(exit $?: sudo $*)"
  else echo "(skipped without sudo: $*)"; fi
}
value() { timeout 2 cat "$1" 2>/dev/null | tr -d '\0' | tr '\n' ' '; }
values() { local f; for f in "$@"; do [ -r "$f" ] && printf '%s: %s\n' "$f" "$(value "$f")"; done; }
first_line() { printf '%s\n' "$1" | head -n 1; }
uptime_s() { cut -d' ' -f1 /proc/uptime; }
# Readable regular files under procfs paths (2 levels deep), first 120 lines of each.
dump() {
  local p files reader='for f; do printf -- "--- %s\n" "$f"; timeout 3 head -n 120 "$f" 2>&1; done'
  files=$(for p in "$@"; do
            [ -e "$p" ] || continue
            if [ -f "$p" ]; then printf '%s\n' "$p"; else find "$p" -maxdepth 2 -type f -perm /0444 2>/dev/null; fi
          done | sort)
  if [ -z "$files" ]; then echo "(none)"; return; fi
  if [ "$ROOT" = 1 ]; then
    printf '%s\n' "$files" | tr '\n' '\0' | timeout 60 sudo -n xargs -0 sh -c "$reader" sh
  else
    printf '%s\n' "$files" | tr '\n' '\0' | timeout 60 xargs -0 sh -c "$reader" sh
  fi
}
# One line per thread: "<path>:<run ns> <wait ns> <slices>". Without schedstat
# (CONFIG_SCHED_INFO off) run time comes from utime+stime and wait is unknown (-1).
SCHEDSTAT=1
[ -r /proc/self/schedstat ] || SCHEDSTAT=0
threads() {
  if [ "$SCHEDSTAT" = 1 ]; then
    grep -H . /proc/[0-9]*/task/[0-9]*/schedstat 2>/dev/null
  else
    grep -H . /proc/[0-9]*/task/[0-9]*/stat 2>/dev/null | awk -v hz="$(getconf CLK_TCK 2>/dev/null || echo 100)" '
      { p = index($0, ":"); path = substr($0, 1, p - 1); sub(/\/stat$/, "/schedstat", path)
        line = substr($0, p + 1); q = 0; while ((r = index(substr(line, q + 1), ")")) > 0) q += r
        n = split(substr(line, q + 2), a, " "); if (n < 13) next
        printf "%s:%.0f -1 0\n", path, (a[12] + a[13]) * 1e9 / hz }'
  fi
}

# Shared awk helpers: scheduling class, last CPU and allowed CPUs of a thread.
AWK_TASK='
function lastparen(s,   p, q) { p = 0; while ((q = index(substr(s, p + 1), ")")) > 0) p += q; return p }
function taskinfo(pid, tid,   f, line, rest, a, n, cpus, pol, names) {
  f = "/proc/" pid "/task/" tid "/stat"; line = ""; n = 0
  if ((getline line < f) > 0) { rest = substr(line, lastparen(line) + 2); n = split(rest, a, " ") }
  close(f)
  split("OTHER FIFO RR BATCH ISO IDLE DEADLINE", names, " ")
  pol = (n >= 39 && (a[39] + 1) in names) ? names[a[39] + 1] : "?"
  f = "/proc/" pid "/task/" tid "/status"; cpus = "?"
  while ((getline line < f) > 0) if (line ~ /^Cpus_allowed_list:/) { sub(/^Cpus_allowed_list:[ \t]*/, "", line); cpus = line }
  close(f)
  return sprintf("cpu=%s %s rtprio=%s nice=%s allowed=%s", (n >= 37 ? a[37] : "?"), pol,
                 (n >= 38 ? a[38] : "?"), (n >= 17 ? a[17] : "?"), cpus)
}
function comm(path,   c) { c = "?"; if ((getline c < path) <= 0) c = "?"; close(path); return c }
'

echo "[rk-diag] writing $report" >&2
{
  section "meta"
  echo "rk_diag.sh v$VERSION label=$label window_s=$window sudo=$ROOT schedstat=$SCHEDSTAT"
  echo "time: $(date '+%F %T %z')  host: $(hostname)  user: $(id -un)"
  run uname -a
  values /proc/device-tree/model /etc/os-release /proc/uptime /proc/loadavg /proc/cmdline
  awk '/^processor/ { p = $3 } /^CPU part/ { printf "cpu%s=%s ", p, $4 } END { print "(0xd05 A55, 0xd0b A76)" }' /proc/cpuinfo
  values /sys/devices/system/cpu/online /sys/devices/system/cpu/isolated /sys/devices/system/cpu/nohz_full \
         /sys/kernel/realtime /proc/sys/kernel/sched_rt_runtime_us /proc/sys/kernel/sched_rt_period_us \
         /proc/sys/kernel/sched_autogroup_enabled
  echo "--- last -x (boots, shutdowns, crashes)"
  run last -x -n 12

  cpufreq() {
    local p d
    for p in /sys/devices/system/cpu/cpufreq/policy*; do
      [ -d "$p" ] && echo "${p##*/} cpus=$(value "$p/related_cpus")gov=$(value "$p/scaling_governor")cur=$(value "$p/scaling_cur_freq")min=$(value "$p/scaling_min_freq")max=$(value "$p/scaling_max_freq")hw_max=$(value "$p/cpuinfo_max_freq")"
    done
    for d in /sys/class/devfreq/*; do
      [ -d "$d" ] && echo "${d##*/} gov=$(value "$d/governor")cur=$(value "$d/cur_freq")min=$(value "$d/min_freq")max=$(value "$d/max_freq")"
    done
  }
  section "cpufreq and devfreq (DDR/GPU/NPU), start of window"
  cpufreq

  section "thermal"
  for z in /sys/class/thermal/thermal_zone*; do
    [ -d "$z" ] && echo "${z##*/} $(value "$z/type")$(value "$z/temp")"
  done
  for c in /sys/class/thermal/cooling_device*; do
    [ -d "$c" ] && echo "${c##*/} $(value "$c/type")cur=$(value "$c/cur_state")max=$(value "$c/max_state")"
  done

  section "memory, CMA and pressure"
  grep -E '^(MemTotal|MemFree|MemAvailable|Cached|SwapTotal|SwapFree|Dirty|Writeback|Shmem|Slab|SUnreclaim|KernelStack|PageTables|Mlocked|Cma[A-Za-z]+):' /proc/meminfo
  cat /proc/buddyinfo 2>&1
  for f in /proc/pressure/cpu /proc/pressure/memory /proc/pressure/io; do
    [ -r "$f" ] && sed "s#^#${f##*/}: #" "$f"
  done

  section "running services"
  run systemctl list-units --type=service --state=running --no-pager --no-legend --plain
  section "processes by CPU (lifetime average; current load is in the window section)"
  ps -eo pid,ppid,user,cls,rtprio,ni,psr,pcpu,pmem,rss,etime,args --sort=-pcpu 2>&1 | head -n 40 | cut -c1-300
  section "camera / media / codec related processes"
  ps -eo pid,ppid,user,cls,rtprio,ni,psr,pcpu,etime,args 2>&1 \
    | grep -iE 'cam|video|gst|v4l|rkaiq|isp|mpp|rga|rtsp|ros|media|stream|rk_sender' | grep -v -e 'grep -i' | cut -c1-400

  section "open camera / encoder / RGA / DRM / dma-heap / UART devices, by process"
  if [ "$ROOT" = 1 ]; then
    timeout 30 sudo -n find /proc/[0-9]*/fd -maxdepth 1 \( -lname '/dev/video*' -o -lname '/dev/media*' \
        -o -lname '/dev/v4l-subdev*' -o -lname '/dev/mpp_service' -o -lname '/dev/rga' -o -lname '/dev/dri/*' \
        -o -lname '/dev/dma_heap/*' -o -lname '/dev/ttyS*' \) -printf '%h %l\n' 2>/dev/null \
      | awk "$AWK_TASK"'{ split($1, p, "/"); print p[3], comm("/proc/" p[3] "/comm"), $2 }' | sort | uniq -c | sort -k2,2n
  else
    echo "(skipped without sudo)"
  fi

  section "V4L2 nodes (sysfs names only; no device is opened)"
  for v in /sys/class/video4linux/*; do
    [ -e "$v" ] && echo "${v##*/} $(value "$v/name")-> $(readlink -f "$v/device" 2>/dev/null | sed 's#.*/##')"
  done

  section "real-time threads (FIFO/RR/DEADLINE) and their allowed CPUs"
  ps -eLo pid=,tid=,cls=,rtprio=,comm= 2>/dev/null \
    | awk "$AWK_TASK"'$3 ~ /^(FF|RR|DLN)$/ { printf "%7s %7s %-3s %3s %-18s %s\n", $1, $2, $3, $4, $5, taskinfo($1, $2) }' \
    | sort -k4,4nr

  section "rk_sender"
  sender_pids=$(pgrep -f 'python[0-9.]* .*rk_sender\.pyz' | tr '\n' ' ')
  echo "pids: ${sender_pids:-none}"
  for pid in $sender_pids; do
    tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null; echo
    for t in /proc/"$pid"/task/*; do
      echo "${t##*/} $(value "$t/comm")"
    done | awk -v pid="$pid" "$AWK_TASK"'{ printf "  tid %-7s %-18s %s\n", $1, $2, taskinfo(pid, $1) }'
  done

  # ---- one measurement window: rates, not totals since boot ----------------
  # Both snapshot sets are taken in the same order, so each counter's interval
  # is the time between the two starts (measured, not the nominal window).
  section "measurement window: ${window}s"
  t0=$(uptime_s)
  I0=$(cat /proc/interrupts); S0=$(cat /proc/softirqs); P0=$(cat /proc/stat); T0=$(threads)
  U0=$(cat /proc/net/snmp); D0=$(cat /proc/net/dev)
  sleep "$window"
  t1=$(uptime_s)
  I1=$(cat /proc/interrupts); S1=$(cat /proc/softirqs); P1=$(cat /proc/stat); T1=$(threads)
  U1=$(cat /proc/net/snmp); D1=$(cat /proc/net/dev)
  dt=$(awk -v a="$t0" -v b="$t1" -v w="$window" 'BEGIN { d = b - a; if (d <= 0) d = w; printf "%.2f", d }')
  echo "measured interval: ${dt}s"

  section "CPU use per core over the window (%)"
  awk -v dt="$dt" '
    $1 ~ /^cpu/ { t = $2 + $3 + $4 + $5 + $6 + $7 + $8 + $9
                  if (FNR == NR) { b[$1] = t; i0[$1] = $5 + $6; u0[$1] = $2 + $3; s0[$1] = $4; q0[$1] = $7; r0[$1] = $8; w0[$1] = $6; next }
                  d = t - b[$1]; if (d <= 0) next
                  printf "%-5s busy %5.1f  user %5.1f  sys %5.1f  irq %5.1f  softirq %5.1f  iowait %5.1f\n", $1,
                         100 * (d - ($5 + $6 - i0[$1])) / d, 100 * ($2 + $3 - u0[$1]) / d, 100 * ($4 - s0[$1]) / d,
                         100 * ($7 - q0[$1]) / d, 100 * ($8 - r0[$1]) / d, 100 * ($6 - w0[$1]) / d }
    $1 == "ctxt" { if (FNR == NR) c0 = $2; else printf "context switches %.0f/s\n", ($2 - c0) / dt }
    $1 == "procs_running" && FNR != NR { print "runnable now:", $2 }
    $1 == "procs_blocked" && FNR != NR { print "blocked now:", $2 }' \
    <(printf '%s\n' "$P0") <(printf '%s\n' "$P1")

  section "interrupts per second over the window (top 30; per-CPU columns; effective affinity)"
  first_line "$I1"
  awk -v dt="$dt" '
    FNR == 1 { ncpu = NF; next }
    { key = $1; sub(/:$/, "", key); n = 0
      for (i = 2; i <= NF && n < ncpu; i++) { if ($i ~ /^[0-9]+$/) { n++; c[n] = $i } else break }
      name = ""; for (j = i; j <= NF; j++) name = name " " $j
      if (FNR == NR) { for (k = 1; k <= n; k++) a[key, k] = c[k]; seen[key] = 1; next }
      if (!(key in seen)) next
      tot = 0; per = ""
      for (k = 1; k <= n; k++) { d = c[k] - a[key, k]; tot += d; per = per sprintf(" %6.0f", d / dt) }
      if (tot <= 0) next
      aff = ""; if (key ~ /^[0-9]+$/) { f = "/proc/irq/" key "/effective_affinity_list"; if ((getline aff < f) <= 0) aff = "?"; close(f); aff = " cpus=" aff }
      printf "%8.0f/s %-6s%s %s%s\n", tot / dt, key, per, name, aff }' \
    <(printf '%s\n' "$I0") <(printf '%s\n' "$I1") | sort -rn | head -n 30

  section "softirqs per second over the window (per CPU)"
  first_line "$S1"
  awk -v dt="$dt" '
    FNR == 1 { next }
    FNR == NR { for (i = 2; i <= NF; i++) a[$1, i] = $i; next }
    { line = sprintf("%-9s", $1); for (i = 2; i <= NF; i++) line = line sprintf(" %7.0f", ($i - a[$1, i]) / dt); print line }' \
    <(printf '%s\n' "$S0") <(printf '%s\n' "$S1")

  section "threads waiting for a CPU (runnable but not running), ms per second, top 30"
  if [ "$SCHEDSTAT" = 1 ]; then
    echo "wait_ms/s run_ms/s   pid     tid  process / thread  (cpu = last CPU)"
    waiting=$(awk -v dt="$dt" "$AWK_TASK"'
      { p = index($0, ":"); path = substr($0, 1, p - 1); split(substr($0, p + 1), v, " ") }
      FNR == NR { r0[path] = v[1]; w0[path] = v[2]; next }
      path in r0 { split(path, q, "/"); w = (v[2] - w0[path]) / 1e6 / dt; r = (v[1] - r0[path]) / 1e6 / dt
                   if (w >= 1 || r >= 20) printf "%9.1f %8.1f %7s %7s  %s / %s  %s\n", w, r, q[3], q[5],
                     comm("/proc/" q[3] "/comm"), comm("/proc/" q[3] "/task/" q[5] "/comm"), taskinfo(q[3], q[5]) }' \
      <(printf '%s\n' "$T0") <(printf '%s\n' "$T1") | sort -rn | head -n 30)
    printf '%s\n' "${waiting:-(no thread waited 1 ms/s or ran 20 ms/s)}"
  else
    echo "(no /proc/*/schedstat on this kernel: wait times unavailable; CPU time below comes from utime+stime)"
  fi

  section "threads by CPU time over the window (ms per second = 0.1% of one core), top 30"
  awk -v dt="$dt" "$AWK_TASK"'
    { p = index($0, ":"); path = substr($0, 1, p - 1); split(substr($0, p + 1), v, " ") }
    FNR == NR { r0[path] = v[1]; w0[path] = v[2]; next }
    path in r0 { split(path, q, "/"); w = (v[2] - w0[path]) / 1e6 / dt; r = (v[1] - r0[path]) / 1e6 / dt
                 if (r >= 5) printf "%8.1f run %7.1f wait %7s %7s  %s / %s  %s\n", r, w, q[3], q[5],
                   comm("/proc/" q[3] "/comm"), comm("/proc/" q[3] "/task/" q[5] "/comm"), taskinfo(q[3], q[5]) }' \
    <(printf '%s\n' "$T0") <(printf '%s\n' "$T1") | sort -rn | head -n 30

  if [ -n "$sender_pids" ]; then
    section "rk_sender threads over the window (ms per second)"
    for pid in $sender_pids; do
      awk -v dt="$dt" -v pid="$pid" "$AWK_TASK"'
        { p = index($0, ":"); path = substr($0, 1, p - 1); split(substr($0, p + 1), v, " ")
          split(path, q, "/"); if (q[3] != pid) next }
        FNR == NR { r0[path] = v[1]; w0[path] = v[2]; next }
        path in r0 { printf "run %7.1f wait %7.1f  tid %-7s %-18s %s\n", (v[1] - r0[path]) / 1e6 / dt,
                       (v[2] - w0[path]) / 1e6 / dt, q[5], comm("/proc/" pid "/task/" q[5] "/comm"), taskinfo(pid, q[5]) }' \
        <(printf '%s\n' "$T0") <(printf '%s\n' "$T1")
    done
  fi

  section "UDP and network counters over the window"
  awk -v dt="$dt" '
    $1 == "Udp:" { if (!h[FILENAME]++) { for (i = 2; i <= NF; i++) name[i] = $i; next }
                   if (FNR == NR) { for (i = 2; i <= NF; i++) a[i] = $i; next }
                   for (i = 2; i <= NF; i++) if ($i != a[i]) printf "Udp %s %+.0f (%.1f/s)\n", name[i], $i - a[i], ($i - a[i]) / dt }' \
    <(printf '%s\n' "$U0") <(printf '%s\n' "$U1")
  awk -v dt="$dt" '
    NR == FNR { if (FNR > 2) { sub(/^ */, ""); split($0, x, /[: ]+/); for (i = 2; i <= 17; i++) a[x[1], i] = x[i] } next }
    FNR > 2 { sub(/^ */, ""); split($0, x, /[: ]+/)
              printf "%-10s rx %8.0f pkt/s %7.2f Mbit/s drop %+.0f err %+.0f | tx %8.0f pkt/s %7.2f Mbit/s drop %+.0f err %+.0f\n", x[1],
                (x[3] - a[x[1], 3]) / dt, (x[2] - a[x[1], 2]) * 8 / 1e6 / dt, x[5] - a[x[1], 5], x[4] - a[x[1], 4],
                (x[11] - a[x[1], 11]) / dt, (x[10] - a[x[1], 10]) * 8 / 1e6 / dt, x[13] - a[x[1], 13], x[12] - a[x[1], 12] }' \
    <(printf '%s\n' "$D0") <(printf '%s\n' "$D1")

  section "cpufreq and devfreq, end of window"
  cpufreq

  section "network links"
  run ip -br addr
  for n in /sys/class/net/*; do
    [ -e "$n" ] && echo "${n##*/} speed=$(value "$n/speed")duplex=$(value "$n/duplex")mtu=$(value "$n/mtu")state=$(value "$n/operstate")"
  done
  for n in /sys/class/net/*; do
    i=${n##*/}
    if [ "$i" = lo ] || [ "$(value "$n/operstate")" != "up " ]; then continue; fi
    echo "--- ethtool $i"; run ethtool "$i"
    run ethtool -S "$i" | grep -iE 'drop|err|fifo|miss|over|pause|busy' | grep -vE ': 0$' | head -n 40
  done
  echo "--- UDP sockets"
  asroot ss -uanp

  section "GStreamer / MPP packages (versions only; no GStreamer program is run)"
  run dpkg-query -W -f '${Package} ${Version}\n' 'gstreamer1.0-rockchip*' 'librockchip-mpp*' 'librga*' \
      'libgstreamer1.0-0' 'gstreamer1.0-plugins-base' 'gstreamer1.0-plugins-good' 'python3-gi'
  ls -l /usr/lib/*/gstreamer-1.0/*rockchip* /usr/lib/*/gstreamer-1.0/*mpp* /usr/lib/*/librockchip_mpp* 2>/dev/null

  section "kernel log (last 300 lines; rtk_btcoex Bluetooth lines removed)"
  if [ "$ROOT" = 1 ]; then timeout 10 sudo -n dmesg -T 2>&1; else timeout 10 dmesg -T 2>&1; fi \
    | grep -v rtk_btcoex | tail -n 300

  # Last on purpose: these files are produced by the camera and encoder
  # drivers' own status callbacks. Put everything above on disk first, so a
  # freeze here still leaves it (sync with a file argument syncs only that file).
  sync "$report" 2>/dev/null
  section "Rockchip camera and codec procfs (driver status, read only)"
  if [ "${RK_DIAG_PROCFS:-1}" = 0 ]; then
    echo "(skipped: RK_DIAG_PROCFS=0)"
  else
    dump /proc/rkcif* /proc/rkisp* /proc/mpp_service
  fi

  section "done"
  date '+%F %T %z'
} > "$report" 2>&1

echo "[rk-diag] report: $report ($(wc -c < "$report") bytes)" >&3
