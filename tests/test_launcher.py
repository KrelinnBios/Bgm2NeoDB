import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows cmd.exe")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize("code_page", [936, 437])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_launcher_handles_encoding_and_preserves_exit_code(tmp_path, newline, code_page, exit_code):
    project = tmp_path / "project with spaces"
    project.mkdir()
    script = project / "start.bat"
    script.write_text(
        (ROOT / "start.bat").read_text(encoding="utf-8"), encoding="utf-8", newline=newline
    )
    venv.EnvBuilder(with_pip=False).create(project / ".venv")
    # Stub imports so the launcher test cannot install packages or contact the network.
    for module in ("fastapi", "uvicorn", "httpx", "jinja2", "keyring"):
        (project / f"{module}.py").write_text("", encoding="ascii")
    (project / "main.py").write_text(
        "import json, os, sys\n"
        "print('LAUNCH_ARGS=' + json.dumps(sys.argv[1:]), flush=True)\n"
        "raise SystemExit(int(os.environ['LAUNCH_EXIT_CODE']))\n",
        encoding="ascii",
    )
    command = (
        f'"{os.environ["COMSPEC"]}" /d /s /c '
        f'"chcp {code_page} >nul & call "{script}" --no-browser "value with spaces""'
    )
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env={
            **os.environ,
            "LAUNCH_EXIT_CODE": str(exit_code),
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        },
        input=b"",
        capture_output=True,
        timeout=15,
    )
    output = result.stdout.decode("utf-8", errors="replace")
    errors = result.stderr.decode("utf-8", errors="replace")
    assert result.returncode == exit_code, output + errors
    assert not errors, output + errors
    assert "LAUNCH_ARGS=" + json.dumps(["--no-browser", "value with spaces"]) in output
