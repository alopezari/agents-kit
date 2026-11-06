#!/usr/bin/env python3
"""bin/evidence-video: a check run through bin/evidence inside a VHS-recorded terminal. Most tests put a stub `vhs` on
PATH that runs the script the tape types and writes what it printed as the "video"; the ones that need the real vhs
skip without it (CI doesn't install it).
Run: python3 ~/.agents/tests/test_evidence_video.py   (exit 1 on any failure)
"""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
RECORDER = os.path.join(KIT, "bin", "evidence-video")
RESULTS = []
# Runs the script the tape types, as vhs's terminal would, and writes its screen text as the video. On a Wait it can't
# match before its timeout, it fails and leaves the check running, as the real vhs did with a check that ignores SIGHUP.
STUB_VHS = r'''#!/usr/bin/env python3
import os, re, shutil, subprocess, sys, threading, time
tape_path = [a for a in sys.argv[1:] if not a.startswith("-")][0]
tape = open(tape_path).read()
if os.environ.get("STUB_VHS_TAPE"):
    shutil.copy(tape_path, os.environ["STUB_VHS_TAPE"])
if os.environ.get("STUB_VHS_MODE") == "fail":
    print("failed to execute command: stub vhs broke", file=sys.stderr)
    sys.exit(1)
output = re.search(r'^Output "(.*)"$', tape, re.M).group(1)
typed = re.findall(r"^Type (.*)$", tape, re.M)
assert len(typed) == 1, typed
if os.environ.get("STUB_VHS_MODE") == "no-run":
    open(output, "w").write("a terminal that ran nothing")
    sys.exit(0)
seconds, pattern = re.search(r"^Wait\+Screen@(\d+)s /(.*)/$", tape, re.M).groups()
check = subprocess.Popen(["bash", "-c", typed[0].strip("'\"")], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         start_new_session=True)
screen = []
threading.Thread(target=lambda: [screen.append(line.decode()) for line in check.stdout], daemon=True).start()
deadline = time.time() + int(seconds)
while not re.search(pattern, "".join(screen)):
    if time.time() > deadline:
        print("failed to execute command: timeout waiting for the marker", file=sys.stderr)
        sys.exit(1)
    time.sleep(0.05)
open(output, "w").write("".join(screen))
print("Host your GIF on vhs.charm.sh: vhs publish <file>.gif")
print("Host your GIF on vhs.charm.sh: vhs publish <file>.gif", file=sys.stderr)
'''


class Skip(Exception):
    pass


def stub_path(base):
    bin_dir = os.path.join(base, "stub-bin")
    os.makedirs(bin_dir, exist_ok=True)
    with open(os.path.join(bin_dir, "vhs"), "w") as fh:
        fh.write(STUB_VHS)
    os.chmod(os.path.join(bin_dir, "vhs"), 0o755)
    return bin_dir + os.pathsep + os.environ["PATH"]


def record(base, check_id, *command, evidence=None, path=None, **env):
    """(the recorder's run, the evidence directory), run from base/work."""
    evidence = evidence or os.path.join(base, "evidence dir")
    work = os.path.join(base, "work")
    os.makedirs(evidence, exist_ok=True)
    os.makedirs(work, exist_ok=True)
    run_env = {**os.environ, "EVIDENCE_DIR": evidence, "PATH": path or stub_path(base), **env}
    done = subprocess.run([RECORDER, check_id, *command], cwd=work, capture_output=True, text=True, env=run_env, timeout=120)
    return done, evidence


def read(path):
    with open(path) as fh:
        return fh.read()


def header_exit(log):
    return int(re.match(r"# in .*, exit (\d+)", read(log)).group(1))


def need_real_vhs():
    missing = [p for p in ("vhs", "ttyd", "ffprobe") if not shutil.which(p)]
    if missing:
        raise Skip(f"{', '.join(missing)} not installed (brew install vhs ffmpeg)")


def duration(video):
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video],
                           capture_output=True, text=True, timeout=60)
    return float(probe.stdout.strip())


# A check that only SIGKILL stops, with the token in its own argv, so `ps` finds it.
STUBBORN = ("import signal, time; signal.signal(signal.SIGHUP, signal.SIG_IGN); signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "print('started', flush=True); time.sleep({seconds})")


def leftovers(token):
    listed = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    return [line for line in listed.splitlines() if token in line and "ps -axo" not in line]


# AC1

def a_check_leaves_its_log_and_video_and_exits_with_its_code(base):
    for check_id, code in (("P1", 0), ("F1", 3)):
        done, evidence = record(base, check_id, "bash", "-c", f'echo "step: open the cart"; echo "in $PWD as $KIT_SEEN"; exit {code}',
                                KIT_SEEN="the caller")
        assert done.returncode == code, (check_id, done.returncode, done.stderr)
        log = read(os.path.join(evidence, f"{check_id}.txt"))
        work = os.path.join(base, "work")
        assert log.startswith(f"# in {work}, ") and header_exit(os.path.join(evidence, f"{check_id}.txt")) == code, log
        assert "$ bash -c 'echo \"step: open the cart\"" in log, log
        assert "step: open the cart\n" in log and f"in {work} as the caller\n" in log, log
        video = os.path.join(evidence, f"{check_id}.webm")
        assert os.path.getsize(video) > 0, "no video"
        screen = read(video)
        assert "step: open the cart" in screen and "bash -c" in screen, screen
    assert sorted(os.listdir(evidence)) == ["F1.txt", "F1.webm", "P1.txt", "P1.webm"], os.listdir(evidence)


def the_log_is_printed_and_the_output_is_not_lost(base):
    done, _ = record(base, "O1", "bash", "-c", "echo the line the runner reads")
    assert done.returncode == 0, done.stderr
    assert "the line the runner reads" in done.stdout, done.stdout


def an_empty_evidence_dir_is_refused(base):
    done, _ = record(base, "E1", "true", EVIDENCE_DIR="")
    assert done.returncode == 74 and "EVIDENCE_DIR" in done.stderr, (done.returncode, done.stderr)


def a_bad_id_is_refused_before_anything_is_removed(base):
    evidence = os.path.join(base, "evidence dir")
    os.makedirs(evidence)
    outside = os.path.join(base, "x.txt")
    open(outside, "w").write("keep me")
    done, _ = record(base, "../x", "true", evidence=evidence)
    assert done.returncode == 64 and os.path.exists(outside), (done.returncode, done.stderr)
    assert subprocess.run([RECORDER, "only-an-id"], capture_output=True).returncode == 64


# AC2

def any_argument_reaches_the_command_unchanged(base):
    tricky = 'a b "c" \'d\' `e` $HOME $(echo f) \\ \n end'
    tape = os.path.join(base, "tape.txt")
    done, evidence = record(base, "Q1", "python3", "-c", "import json, sys; print(json.dumps(sys.argv[1:]))", tricky,
                            STUB_VHS_TAPE=tape)
    assert done.returncode == 0, done.stderr
    printed = [line for line in read(os.path.join(evidence, "Q1.txt")).splitlines() if line.startswith("[")]
    assert printed and json.loads(printed[-1]) == [tricky], printed
    typed = re.findall(r"^Type (.*)$", read(tape), re.M)
    assert len(typed) == 1 and "import json" not in typed[0] and "evidence dir" not in read(tape), read(tape)


def an_early_exit_line_or_an_old_marker_does_not_end_the_recording(base):
    old = "evidence-video: M1 exit 0, run 0123456789abcdef0123456789abcdef"
    done, evidence = record(base, "M1", "bash", "-c", f'echo "exit 0"; echo "{old}"; sleep 1; echo "the late line"')
    assert done.returncode == 0, done.stderr
    assert "the late line" in read(os.path.join(evidence, "M1.webm")), "the recording stopped before the check ended"


# AC3

def without_vhs_the_check_runs_on_bin_evidence_alone(base):
    evidence = os.path.join(base, "evidence dir")
    os.makedirs(evidence)
    open(os.path.join(evidence, "N1.webm"), "w").write("an older run's video")
    done, _ = record(base, "N1", "bash", "-c", "echo ran; exit 3", evidence=evidence, path="/usr/bin:/bin")
    assert done.returncode == 3, (done.returncode, done.stderr)
    assert "evidence-video: no video: vhs is not installed" in done.stderr, done.stderr
    assert "ran" in done.stdout and header_exit(os.path.join(evidence, "N1.txt")) == 3
    assert not os.path.exists(os.path.join(evidence, "N1.webm")), "an older run's video is left beside this run's log"


def old_files(evidence, check_id):
    os.makedirs(evidence, exist_ok=True)
    open(os.path.join(evidence, f"{check_id}.txt"), "w").write("# in /x, 2026-10-26 09:00:00 +0100, exit 0, commit none\n$ true\n\n")
    open(os.path.join(evidence, f"{check_id}.webm"), "w").write("an older run's video")


def a_failing_vhs_leaves_no_video_and_exits_74(base):
    evidence = os.path.join(base, "evidence dir")
    old_files(evidence, "V1")
    done, _ = record(base, "V1", "true", evidence=evidence, STUB_VHS_MODE="fail")
    assert done.returncode == 74, (done.returncode, done.stderr)
    assert "vhs failed" in done.stderr and "stub vhs broke" in done.stderr, done.stderr
    assert not os.path.exists(os.path.join(evidence, "V1.webm")) and not os.path.exists(os.path.join(evidence, "V1.txt"))


def no_log_written_leaves_no_video_and_exits_74(base):
    evidence = os.path.join(base, "evidence dir")
    old_files(evidence, "W1")
    done, _ = record(base, "W1", "true", evidence=evidence, STUB_VHS_MODE="no-run")
    assert done.returncode == 74, (done.returncode, done.stderr)
    assert "no log" in done.stderr, done.stderr
    assert os.listdir(evidence) == [], os.listdir(evidence)


# AC4

def the_cap_stops_the_check_and_leaves_nothing(base):
    token = f"cap-check-{os.getpid()}-{time.time_ns()}"
    started = time.time()
    done, evidence = record(base, "C1", "python3", "-c", STUBBORN.format(seconds=30), token, EVIDENCE_VIDEO_MAX="2")
    assert done.returncode == 124, (done.returncode, done.stderr)
    assert time.time() - started < 20, "the cap didn't stop it"
    for words in ("stopped at the cap", "may have partly run", "bin/evidence"):
        assert words in done.stderr, (words, done.stderr)
    assert os.listdir(evidence) == [], os.listdir(evidence)
    time.sleep(0.5)
    assert leftovers(token) == [], leftovers(token)


def nothing_is_ever_uploaded(base):
    source = read(RECORDER)
    assert "publish" not in source and not re.search(r"\svhs\b[^\n]*\s-p\b", source), "the recorder names VHS's upload"
    assert "VHS_PUBLISH=false" in source, "a caller's VHS_PUBLISH=true would upload the check's terminal"
    tape = os.path.join(base, "tape.txt")
    done, _ = record(base, "U1", "true", STUB_VHS_TAPE=tape, VHS_PUBLISH="true")
    assert done.returncode == 0, done.stderr
    assert "publish" not in read(tape), read(tape)
    assert "vhs.charm.sh" not in done.stdout + done.stderr and "publish" not in done.stdout + done.stderr, (done.stdout, done.stderr)


# Real vhs

def real_vhs_records_the_check_until_it_ends(base):
    need_real_vhs()
    tricky = 'a b "c" \'d\' `e` $HOME'
    done, evidence = record(base, "R1", "python3", "-c",
                            "import json, sys, time; print('step: print the arguments'); print(json.dumps(sys.argv[1:]));"
                            "print('exit 0', flush=True); time.sleep(2); print('after the exit line')", tricky,
                            path=os.environ["PATH"])
    assert done.returncode == 0, (done.returncode, done.stderr)
    log = read(os.path.join(evidence, "R1.txt"))
    assert json.loads([line for line in log.splitlines() if line.startswith("[")][0]) == [tricky], log
    assert "step: print the arguments" in log and "after the exit line" in log, log
    video = os.path.join(evidence, "R1.webm")
    assert os.path.getsize(video) > 0
    assert duration(video) > 2.5, f"the video ends at {duration(video)}s, before the check's last line"


def real_vhs_cap_stops_the_check(base):
    need_real_vhs()
    token = f"real-cap-{os.getpid()}-{time.time_ns()}"
    started = time.time()
    # A check far longer than the cap, so the bound tells a stop from a wait even when vhs starts slowly.
    done, evidence = record(base, "R2", "python3", "-c", STUBBORN.format(seconds=60), token, path=os.environ["PATH"], EVIDENCE_VIDEO_MAX="3")
    assert done.returncode == 124, (done.returncode, done.stderr)
    assert time.time() - started < 40, f"took {time.time() - started:.1f}s"
    assert os.listdir(evidence) == [], os.listdir(evidence)
    time.sleep(1)
    assert leftovers(token) == [], leftovers(token)


def kit_validate_sends_its_own_checks_to_the_recorder(base):
    skill = read(os.path.join(KIT, "skills", "kit-validate", "SKILL.md"))
    step = skill[skill.index("\n## 4. "):skill.index("\n## 5. ")]
    for needle in ("`~/.agents/bin/evidence-video <id> <command> [args...]`", "CLI, API and database checks", "`EVIDENCE_VIDEO_MAX`",
                   "The full suite, mutation runs and the checks guide's steps stay on `bin/evidence`"):
        assert needle in step, needle
    assert re.search(r"\[output\]\([^)]*/A1\.txt\) \[video\]\([^)]*/A1\.webm\)", skill), "the Evidence cell example links no log and video pair"
    assert "marking the ones step 4 records with `bin/evidence-video`" in skill, "the skill doesn't tell the runner which checks to record"
    runner = read(os.path.join(KIT, "mods", "kit", "hooks", "register.js"))
    runner = runner[runner.index("const RUNNER_PROMPT"):runner.index("].join", runner.index("const RUNNER_PROMPT"))]
    assert "`~/.agents/bin/evidence-video <id> <command>`" in runner, "kit:validate-runner's prompt doesn't name the recorder"


def test(fn):
    with tempfile.TemporaryDirectory(prefix="agents-test-evidence-video-") as base:
        try:
            fn(os.path.realpath(base))
            RESULTS.append((fn.__name__, None, None))
        except Skip as skipped:
            RESULTS.append((fn.__name__, None, str(skipped)))
        except Exception:
            RESULTS.append((fn.__name__, traceback.format_exc(), None))


for t in kit_home.selected([a_check_leaves_its_log_and_video_and_exits_with_its_code, the_log_is_printed_and_the_output_is_not_lost,
                            an_empty_evidence_dir_is_refused, a_bad_id_is_refused_before_anything_is_removed,
                            any_argument_reaches_the_command_unchanged, an_early_exit_line_or_an_old_marker_does_not_end_the_recording,
                            without_vhs_the_check_runs_on_bin_evidence_alone, a_failing_vhs_leaves_no_video_and_exits_74,
                            no_log_written_leaves_no_video_and_exits_74, the_cap_stops_the_check_and_leaves_nothing,
                            nothing_is_ever_uploaded, real_vhs_records_the_check_until_it_ends, real_vhs_cap_stops_the_check,
                            kit_validate_sends_its_own_checks_to_the_recorder]):
    test(t)

failed = [(n, e) for n, e, _ in RESULTS if e]
for name, err, skipped in RESULTS:
    if skipped:
        print(f"skip {name}: {skipped}")
        continue
    print(f"{'FAIL' if err else 'ok  '} {name}")
    if err:
        print("     " + err.strip().replace("\n", "\n     "))
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
