#!/usr/bin/env bash
# Runs Dev/tests on the Windows test VM, on this checkout's files.
#
# Usage: ./test-windows.sh [--keep-closed]
#   --keep-closed  leave ClipEm closed on the VM afterwards
#
# SSH lands in session 0, which has no desktop, so everything that needs one
# (Tk, the tray, OBS) runs as a scheduled task in the session of the logged-in
# user. The installed ClipEm is closed for the run and started again after it.
# The results end up in Dev/dist/windows-tests. Exits with pytest's status.
#
# CLIPEM_WINDOWS_HOST: the ssh host (default clipem-win11).
# CLIPEM_WINDOWS_TIMEOUT: seconds the tests get (default 1200).
set -euo pipefail

HOST="${CLIPEM_WINDOWS_HOST:-clipem-win11}"
TIMEOUT="${CLIPEM_WINDOWS_TIMEOUT:-1200}"
KEEP_CLOSED=0
for arg in "$@"; do
  case "$arg" in
    -h|--help) sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    --keep-closed) KEEP_CLOSED=1 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
RESULTS="$SCRIPT_DIR/dist/windows-tests"
TASK=ClipEmTests
PYTHON='%LOCALAPPDATA%\ClipEm\App\Windows\Python\python.exe'
PYTHONW='%LOCALAPPDATA%\ClipEm\App\Windows\Python\pythonw.exe'
REMOTE='%USERPROFILE%\ClipEmTest'
SSH_OPTIONS=(-o BatchMode=yes -o ConnectTimeout=10)

die() { echo "ERROR: $*" >&2; exit 2; }
# OpenSSH's post-quantum warning, which the VM's older server sets off.
quiet() { grep -viE 'post-quantum|store now, decrypt|server may need to be upgraded' || true; }
remote() { ssh "${SSH_OPTIONS[@]}" "$HOST" "$@" 2> >(quiet >&2); }

# in_desktop <helper command> <seconds>: runs test_windows_helper.py in the
# desktop session, its output in results\<command>.txt. Prints its status.
in_desktop() {
  local command="$1" limit="$2" waited=0 percent
  # pythonw and no batch file: with no console, nothing can stop on a
  # "Terminate batch job (Y/N)?" or take a stray Ctrl+C.
  remote "schtasks /End /TN $TASK >nul 2>nul & del $REMOTE\\results\\$command.done 2>nul & schtasks /Create /TN $TASK /TR \"$PYTHONW $REMOTE\\test_windows_helper.py $command\" /SC ONCE /ST 00:00 /RU %USERNAME% /IT /RL HIGHEST /F >nul 2>nul & schtasks /Run /TN $TASK >nul" >/dev/null
  while (( waited < limit )); do
    if remote "if exist $REMOTE\\results\\$command.done (type $REMOTE\\results\\$command.done)" | tr -d ' \r' | grep -E '^[0-9]+$'; then
      remote "schtasks /Delete /TN $TASK /F >nul 2>nul" || true
      return 0
    fi
    # Python itself can crash, and then there's no .done to wait for.
    if (( waited >= 10 )) && ! remote "schtasks /Query /TN $TASK /FO CSV /NH" | grep -q Running; then
      if remote "if exist $REMOTE\\results\\$command.done (type $REMOTE\\results\\$command.done)" | tr -d ' \r' | grep -E '^[0-9]+$'; then
        remote "schtasks /Delete /TN $TASK /F >nul 2>nul" || true
        return 0
      fi
      remote "schtasks /Delete /TN $TASK /F >nul 2>nul" || true
      echo "ERROR: $command stopped without finishing." >&2
      return 1
    fi
    sleep 5
    waited=$((waited + 5))
    if (( waited % 15 == 0 )) && [[ "$command" == pytest ]]; then
      # [ 42%], which release.sh's progress line picks up.
      percent="$(remote "powershell -NoProfile -Command \"(Get-Content \$env:USERPROFILE\\ClipEmTest\\results\\pytest.txt -Tail 1 -ErrorAction SilentlyContinue)\"" | grep -o '\[ *[0-9]*%\]' || true)"
      if [[ -n "$percent" ]]; then echo "   Windows tests... $percent" >&2; fi
    fi
  done
  remote "schtasks /End /TN $TASK >nul 2>nul & schtasks /Delete /TN $TASK /F >nul 2>nul" || true
  return 1
}

[[ -f "$ROOT_DIR/App/App/Shared/VERSION" ]] || die "Run this from a ClipEm checkout."

# --- The VM ------------------------------------------------------------------
sessions="$(remote "query user" || true)"
[[ -n "$sessions" ]] || die "Can't reach $HOST over ssh with its key (is the VM running?)."
grep -qiE 'console +[0-9]+ +Active' <<<"$sessions" \
  || die "Nobody is logged in on $HOST's desktop, and the tests need one."

echo " - Copying this checkout to $HOST..."
# (Parentheses: everything after an unbracketed "if" would be part of it.)
for folder in repo tmp results; do
  remote "(if exist $REMOTE\\$folder rmdir /s /q $REMOTE\\$folder)" >/dev/null
done
remote "mkdir $REMOTE\\repo $REMOTE\\results" >/dev/null
(cd "$ROOT_DIR" && git ls-files -z --cached --others --exclude-standard -- App Dev \
  | while IFS= read -r -d '' file; do if [[ -e "$file" ]]; then printf '%s\0' "$file"; fi; done \
  | tar czf - --null -T -) | remote "tar xzf - -C ClipEmTest/repo"
scp -q "${SSH_OPTIONS[@]}" "$SCRIPT_DIR/test_windows_helper.py" "$HOST:ClipEmTest/test_windows_helper.py" 2> >(quiet >&2)

echo " - Installing the test packages..."
remote "\"$PYTHON\" -m pip install --quiet --disable-pip-version-check --no-warn-script-location --upgrade --target $REMOTE\\deps pytest==9.1.* -r $REMOTE\\repo\\App\\App\\Shared\\tray-widget\\requirements.txt -r $REMOTE\\repo\\App\\App\\Shared\\Installer\\mic-filter\\requirements.txt"

# --- The tests ---------------------------------------------------------------
restart() {
  if (( KEEP_CLOSED )); then
    remote "del $REMOTE\\was_running.json 2>nul" || true
  else
    # Its own task, not elevated like the tests: what the user would have.
    # Left running: it lasts as long as run.py does, update prompt included.
    if remote "type $REMOTE\\was_running.json 2>nul" | grep -q true; then
      remote "schtasks /Create /TN ClipEmStart /TR \"$PYTHONW %LOCALAPPDATA%\\ClipEm\\App\\Shared\\Installer\\run.py\" /SC ONCE /ST 00:00 /RU %USERNAME% /IT /F >nul 2>nul & schtasks /Run /TN ClipEmStart >nul & schtasks /Delete /TN ClipEmStart /F >nul" >/dev/null \
        || echo "WARNING: couldn't start ClipEm again on $HOST." >&2
    fi
    remote "del $REMOTE\\was_running.json 2>nul" >/dev/null || true
  fi
}

echo " - Closing ClipEm on $HOST..."
close_status="$(in_desktop close 60)" || die "ClipEm didn't close on $HOST in time."
[[ "$close_status" == 0 ]] || die "Closing ClipEm on $HOST failed (status $close_status): see results\\close.txt there."
trap restart EXIT
trap 'exit 130' INT TERM

echo " - Running the tests on $HOST..."
status=0
pytest_status="$(in_desktop pytest "$TIMEOUT")" || { status=2; echo "ERROR: the tests didn't finish (crashed, or over ${TIMEOUT}s)." >&2; }
[[ -n "${pytest_status:-}" ]] && status="$pytest_status"

mkdir -p "$RESULTS"
rm -f "${RESULTS:?}/pytest.txt" "${RESULTS:?}/junit.xml" "${RESULTS:?}/failures.txt"
scp -q "${SSH_OPTIONS[@]}" "$HOST:ClipEmTest/results/pytest.txt" "$HOST:ClipEmTest/results/junit.xml" "$RESULTS/" 2> >(quiet >&2) || true
if [[ -f "$RESULTS/pytest.txt" ]]; then
  tr -d '\r' < "$RESULTS/pytest.txt" | sed -n '/short test summary info/,$p'
  if (( status != 0 )); then
    tr -d '\r' < "$RESULTS/pytest.txt" | sed -n '/= FAILURES =/,/short test summary info/p' > "$RESULTS/failures.txt"
    grep -q 'short test summary info' "$RESULTS/pytest.txt" || tail -n 30 "$RESULTS/pytest.txt"  # it died
  fi
fi
echo " - Windows results: $RESULTS"
exit "$status"
