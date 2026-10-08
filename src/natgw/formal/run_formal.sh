#!/bin/bash
# Run natgw formal tasks with SymbiYosys and print one status line per task.
# Usage: [FORMAL_TIMEOUT=seconds] ./run_formal.sh [parallel_jobs] [dir ...]
# With FORMAL_TIMEOUT, a BMC task still running at the limit is stopped and
# reported as BOUNDED with the last step whose assertions all passed.
# Needs sby, yosys with the slang plugin, and bitwuzla (OSS CAD Suite).
# Tasks of one .sby file run one after another (they share a status database);
# different .sby files run in parallel.
set -u
cd "$(dirname "$0")"
JOBS=${1:-6}
shift || true
DIRS=("$@")
[ ${#DIRS[@]} -eq 0 ] && DIRS=(*/)
jobs_list=()
for d in "${DIRS[@]}"; do
    for sby in ${d%/}/*.sby; do
        [ -f "$sby" ] && jobs_list+=("$sby")
    done
done
run_file() {
    sby=$1
    dir=$(dirname "$sby"); name=$(basename "$sby" .sby)
    cd "$dir"
    tl=$(awk '/^\[tasks\]/{p=1;next} /^\[/{p=0} p&&NF{print $1}' "$name.sby")
    [ -z "$tl" ] && tl="-"
    for task in $tl; do
        start=$(date +%s)
        if [ "$task" = "-" ]; then out="$name"; args=("$name.sby"); else out="${name}_$task"; args=("$name.sby" "$task"); fi
        if [ -n "${FORMAL_TIMEOUT:-}" ]; then
            timeout --signal=INT "$FORMAL_TIMEOUT" sby -f "${args[@]}" > "$out.log" 2>&1
        else
            sby -f "${args[@]}" > "$out.log" 2>&1
        fi
        st=$(head -1 "$out/status" 2>/dev/null)
        if [ -z "$st" ] || [ "${st%% *}" = "ERROR" ] && grep -q "Checking assertions in step" "$out/logfile.txt" 2>/dev/null; then
            last=$(grep -oE 'Checking assertions in step [0-9]+' "$out/logfile.txt" | tail -1 | grep -oE '[0-9]+$')
            if ! grep -q "Assert failed" "$out/logfile.txt"; then st="BOUNDED through step $((last - 1))"; fi
        fi
        printf "%-10s %-10s %-24s %6ss\n" "$dir" "$task" "${st:-NO STATUS}" "$(( $(date +%s) - start ))"
    done
}
export -f run_file
printf "%s\n" "${jobs_list[@]}" | xargs -P "$JOBS" -I{} bash -c 'run_file "{}"'
