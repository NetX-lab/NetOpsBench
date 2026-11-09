"""Cancellation owns only the launched process group; time budgets are shared."""

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from netopsbench.platform.utils.control import OperationCancelled, OperationControl
from netopsbench.platform.utils.proc import safe_run


def test_cancel_before_launch_does_not_run_command(tmp_path):
    cancel = threading.Event()
    cancel.set()
    marker = tmp_path / "created"
    with pytest.raises(OperationCancelled):
        safe_run([sys.executable, "-c", f"open({str(marker)!r},'w').close()"], control=OperationControl(cancel))
    assert not marker.exists()


def test_cancel_reaps_descendant_process_group(tmp_path):
    pidfile = tmp_path / "child.pid"
    script = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)']); "
        f"open({str(pidfile)!r},'w').write(str(child.pid)); time.sleep(90)"
    )
    cancel = threading.Event()

    def interrupt():
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel.set()

    thread = threading.Thread(target=interrupt)
    thread.start()
    try:
        with pytest.raises(OperationCancelled):
            safe_run([sys.executable, "-c", script], control=OperationControl(cancel))
    finally:
        thread.join()
    child = int(pidfile.read_text())
    status = Path(f"/proc/{child}/stat")
    try:
        state = status.read_text().split()[2]
    except (FileNotFoundError, ProcessLookupError):
        # procfs can report ESRCH when the process exits between open and read.
        state = "gone"
    assert state in {"gone", "Z"}


def test_controlled_process_preserves_input_output_and_exit_errors():
    result = safe_run(
        [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
        input="literal input",
        control=OperationControl(deadline=time.monotonic() + 5),
    )
    assert result.stdout.strip() == "literal input"
    with pytest.raises(subprocess.CalledProcessError) as failure:
        safe_run(
            [sys.executable, "-c", "import sys;print('details');sys.exit(7)"], check=True, control=OperationControl()
        )
    assert failure.value.returncode == 7 and "details" in failure.value.stdout


def test_deadline_bounds_process_and_sleep():
    control = OperationControl(deadline=time.monotonic() + 0.03)
    started = time.monotonic()
    with pytest.raises((TimeoutError, subprocess.TimeoutExpired)):
        safe_run([sys.executable, "-c", "import time;time.sleep(90)"], timeout=60, control=control)
    assert time.monotonic() - started < 2
    with pytest.raises(TimeoutError):
        control.sleep(30)
