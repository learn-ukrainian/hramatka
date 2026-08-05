from __future__ import annotations

import json
import os
import re
import select
import shutil
import signal
import socket
import sqlite3
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from hramatka.app.e2e import real_backend_server
from hramatka.ops import local_teacher

REPO_ROOT = Path(__file__).resolve().parents[1]


def _discover_test_python() -> Path:
    for command in ("python", "python3"):
        candidate = shutil.which(command)
        if candidate and os.access(candidate, os.X_OK):
            return Path(candidate).resolve()
    raise RuntimeError("tests require an explicit Python interpreter on PATH")


TEST_PYTHON = _discover_test_python()
PROCESS_EXIT_TIMEOUT_SECONDS = 5.0
_ESSENTIAL_RUNTIME_ENV_NAMES = {
    "CI",
    "HOME",
    "LANG",
    "LANGUAGE",
    "LD_LIBRARY_PATH",
    "PATH",
    "PATHEXT",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "WINDIR",
}


def _isolated_test_environment(**overrides: str) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in _ESSENTIAL_RUNTIME_ENV_NAMES
        or name.startswith(("LC_", "DYLD_"))
        or (name.startswith("PYTHON") and name != "PYTHONPATH")
    }
    environment.update(overrides)
    return environment


def _runtime_paths(root: Path) -> local_teacher.RuntimePaths:
    return local_teacher._runtime_paths(root)


def _data_environment(tmp_path: Path) -> dict[str, str]:
    release = tmp_path / "release"
    release.mkdir()
    (release / "data-manifest.json").write_text("{}", encoding="utf-8")
    return {
        "HOME": str(tmp_path / "home"),
        "PATH": os.environ.get("PATH", ""),
        "HRAMATKA_AIS_API_KEY": "test-ais-key",
        "HRAMATKA_DATA_DIR": str(release),
        "HRAMATKA_DATA_MANIFEST": str(release / "data-manifest.json"),
    }


def _free_ports() -> tuple[int, int]:
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        return int(first.getsockname()[1]), int(second.getsockname()[1])


def _assert_port_reusable(port: int) -> None:
    with socket.socket() as available:
        available.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        available.bind(("127.0.0.1", port))


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for_pid_exit(pid: int, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return
        time.sleep(0.05)
    pytest.fail(f"process {pid} did not exit")


def _start_real_backend_proxy(
    _tmp_path: Path,
    **environment_overrides: str,
) -> tuple[subprocess.Popen[bytes], int, int, int, int, Path]:
    app_directory = REPO_ROOT / "hramatka" / "app"
    state_directory = app_directory / ".real-e2e"
    assert not state_directory.exists()
    https_port = 5174
    _assert_port_reusable(https_port)
    environment = _isolated_test_environment(PATH=os.environ.get("PATH", ""))
    environment.update(environment_overrides)
    process = subprocess.Popen(
        [str(app_directory / "e2e" / "run-real-backend.sh")],
        cwd=app_directory,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    state_path = state_directory / "api-state.json"
    proxy_path = state_directory / "proxy.pid"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            pytest.fail(f"real-backend proxy exited early: {stdout!r} {stderr!r}")
        if state_path.is_file() and proxy_path.is_file():
            identity = json.loads(state_path.read_text(encoding="utf-8"))
            owner_token = (state_directory / "owner").read_text(encoding="ascii").strip()
            assert re.fullmatch(r"[a-f0-9]{64}", owner_token)
            assert identity["owner"] == owner_token
            context = ssl.create_default_context(cafile=str(state_directory / "cert.pem"))
            try:
                with urllib.request.urlopen(
                    f"https://127.0.0.1:{https_port}/api/healthz",
                    context=context,
                    timeout=1,
                ) as response:
                    if response.status == 200:
                        return (
                            process,
                            int(proxy_path.read_text(encoding="ascii").strip()),
                            int(identity["pid"]),
                            int(identity["port"]),
                            https_port,
                            state_directory,
                        )
            except OSError:
                pass
        time.sleep(0.05)
    process.terminate()
    stdout, stderr = process.communicate(timeout=5)
    pytest.fail(f"real-backend proxy did not become ready: {stdout!r} {stderr!r}")


def _start_hung_playwright_proxy() -> tuple[subprocess.Popen[bytes], int, Path]:
    app_directory = REPO_ROOT / "hramatka" / "app"
    state_directory = app_directory / ".real-e2e"
    assert not state_directory.exists()
    environment = _isolated_test_environment(
        PATH=os.environ.get("PATH", ""),
        HRAMATKA_E2E_TEST_HUNG_PROXY="1",
    )
    process = subprocess.Popen(
        [str(app_directory / "e2e" / "run-real-backend.sh")],
        cwd=app_directory,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proxy_path = state_directory / "proxy.pid"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            pytest.fail(f"hung-proxy rig exited early: {stdout!r} {stderr!r}")
        if proxy_path.is_file():
            return (
                process,
                int(proxy_path.read_text(encoding="ascii").strip()),
                state_directory,
            )
        time.sleep(0.05)
    process.terminate()
    stdout, stderr = process.communicate(timeout=5)
    pytest.fail(f"hung-proxy rig did not start: {stdout!r} {stderr!r}")


FAKE_CI_PREFIX = "/fake/ci-interpreter-prefix"


def _make_real_backend_shell_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    fake_repo = tmp_path / "fake-repo"
    app_directory = fake_repo / "hramatka" / "app"
    e2e_directory = app_directory / "e2e"
    fake_bin = tmp_path / "fake-bin"
    e2e_directory.mkdir(parents=True)
    fake_bin.mkdir()
    shutil.copy2(
        REPO_ROOT / "hramatka" / "app" / "e2e" / "run-real-backend.sh",
        e2e_directory / "run-real-backend.sh",
    )
    fake_python = fake_bin / "python"
    # The launcher asks the selected interpreter for its own prefix, so the
    # stand-in has to answer like a Python rather than merely exist.
    fake_python.write_text(
        '#!/bin/sh\nif [ "$1" = "-c" ]; then printf "%s\\n" "'
        f"{FAKE_CI_PREFIX}"
        '"; fi\nexit 0\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_npm = fake_bin / "npm"
    fake_npm.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_npm.chmod(0o755)
    fake_node = fake_bin / "node"
    fake_node.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$HRAMATKA_E2E_PYTHON" > "$E2E_PYTHON_CAPTURE"\n'
        'printf "%s\\n" "$HRAMATKA_E2E_VENV_PREFIX" > "$E2E_PREFIX_CAPTURE"\n'
        "printf '999999\\n' > .real-e2e/api.pid\nexit 23\n",
        encoding="utf-8",
    )
    fake_node.chmod(0o755)
    return app_directory, fake_bin, fake_python


def _wait_for_output(process: subprocess.Popen[str], needle: str) -> str:
    assert process.stdout is not None
    deadline = time.monotonic() + 30
    chunks: list[str] = []
    stdout_fd = process.stdout.fileno()
    os.set_blocking(stdout_fd, False)
    while time.monotonic() < deadline:
        readable, _, _ = select.select([stdout_fd], [], [], 0.1)
        if not readable:
            if process.poll() is not None:
                break
            continue
        chunk = os.read(stdout_fd, 4096).decode("utf-8")
        if chunk:
            chunks.append(chunk)
            output = "".join(chunks)
            if needle in output:
                return output
        elif process.poll() is not None:
            break
    stderr = (
        process.stderr.read()
        if process.poll() is not None and process.stderr is not None
        else ""
    )
    pytest.fail(f"launcher did not print {needle!r}: {''.join(chunks)}{stderr}")


def _make_smoke_repo(tmp_path: Path, *, delayed_npm: bool = False) -> tuple[Path, Path]:
    fake_repo = tmp_path / "fake repo"
    package = fake_repo / "hramatka"
    package.mkdir(parents=True)
    for entry in (REPO_ROOT / "hramatka").iterdir():
        if entry.name in {"app", "ops", "__pycache__"}:
            continue
        (package / entry.name).symlink_to(entry)

    ops = package / "ops"
    ops.mkdir()
    for name in ("local-teacher.sh", "local_teacher.py"):
        shutil.copyfile(REPO_ROOT / "hramatka" / "ops" / name, ops / name)
    (ops / "local-teacher.sh").chmod(0o755)
    app = package / "app"
    (app / "scripts").mkdir(parents=True)
    (app / "dist").mkdir()
    (app / "package.json").write_text('{"scripts": {"build": "true"}}', encoding="utf-8")
    (app / "dist" / "index.html").write_text("<main>teacher</main>", encoding="utf-8")
    (app / "scripts" / "https-static-proxy.mjs").symlink_to(
        REPO_ROOT / "hramatka" / "app" / "scripts" / "https-static-proxy.mjs"
    )
    (app / "scripts" / "csp-guard.mjs").write_text("// smoke-test no-op\n", encoding="utf-8")
    venv_python = fake_repo / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(TEST_PYTHON)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    npm = fake_bin / "npm"
    if delayed_npm:
        npm.write_text(
            "#!/bin/bash\n"
            "set -eu\n"
            "printf '%s' \"$$\" > \"$FAKE_NPM_PID\"\n"
            "touch \"$FAKE_NPM_STARTED\"\n"
            "trap 'exit 0' TERM INT\n"
            "while :; do sleep 1; done\n",
            encoding="utf-8",
        )
    else:
        npm.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    npm.chmod(0o755)
    node_implementation = fake_bin / "fake-node.py"
    node_implementation.write_text(
        """from __future__ import annotations

import http.client
import os
import socket
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if len(sys.argv) == 2 and Path(sys.argv[1]).name == "csp-guard.mjs":
    raise SystemExit(0)

api_port = int(os.environ["HRAMATKA_PROXY_API_PORT"])


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/teacher":
            self.send_response(308)
            self.send_header("Location", "/teacher/")
            self.end_headers()
            return
        if self.path.startswith("/teacher/"):
            body = b"<main>teacher</main>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/"):
            upstream = http.client.HTTPConnection("127.0.0.1", api_port, timeout=3)
            try:
                upstream.request("GET", self.path)
                response = upstream.getresponse()
                body = response.read()
                self.send_response(response.status)
                self.send_header(
                    "Content-Type",
                    response.getheader("Content-Type", "application/json"),
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                upstream.close()
            return
        self.send_error(404)

    def log_message(self, _format: str, *_args: object) -> None:
        return


listen_fd = int(os.environ["HRAMATKA_PROXY_LISTEN_FD"])
inherited = socket.socket(fileno=listen_fd)
server_address = inherited.getsockname()
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(
    os.environ["HRAMATKA_PROXY_TLS_CERT"],
    os.environ["HRAMATKA_PROXY_TLS_KEY"],
)
server = ThreadingHTTPServer(server_address, Handler, bind_and_activate=False)
server.socket.close()
server.socket = context.wrap_socket(inherited, server_side=True)
server.server_address = server_address
server.server_name = str(server_address[0])
server.server_port = int(server_address[1])
server.serve_forever()
""",
        encoding="utf-8",
    )
    node = fake_bin / "node"
    node.write_text(
        "#!/bin/bash\n"
        'script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        'exec "${script_dir}/../fake repo/.venv/bin/python" '
        '"${script_dir}/fake-node.py" "$@"\n',
        encoding="utf-8",
    )
    node.chmod(0o755)
    release = tmp_path / "release"
    release.mkdir()
    from hramatka.engine.fixtures import _build_fixture_bundle

    bundle = _build_fixture_bundle(release)
    (release / "data-manifest.json").write_text(
        json.dumps(bundle.manifest), encoding="utf-8"
    )
    return fake_repo, fake_bin


def test_help_works_without_repository_python_or_credentials(tmp_path: Path) -> None:
    fake_repo = tmp_path / "repo without venv"
    script = fake_repo / "hramatka" / "ops" / "local-teacher.sh"
    script.parent.mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / "hramatka" / "ops" / "local-teacher.sh", script)
    script.chmod(0o755)
    completed = subprocess.run(
        [str(script), "--help"],
        cwd=fake_repo,
        env={"HOME": str(tmp_path / "missing-home"), "PATH": "/usr/bin:/bin"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "local Hramatka teacher application" in completed.stdout
    assert "invite" not in completed.stderr.lower()


def test_reserved_loopback_ports_are_exclusive_then_reusable() -> None:
    api_port, https_port = _free_ports()
    config = local_teacher.LaunchConfig(
        repo_root=REPO_ROOT, api_port=api_port, https_port=https_port
    )
    with local_teacher._reserved_ports(config):
        for port in (config.api_port, config.https_port):
            with pytest.raises(OSError):
                _assert_port_reusable(port)
    _assert_port_reusable(config.api_port)
    _assert_port_reusable(config.https_port)


def test_shell_wrapper_fails_closed_when_repository_python_is_absent(tmp_path: Path) -> None:
    script = tmp_path / "repo with spaces" / "hramatka" / "ops" / "local-teacher.sh"
    script.parent.mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / "hramatka" / "ops" / "local-teacher.sh", script)
    completed = subprocess.run(
        ["/bin/bash", str(script)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 1
    assert ".venv/bin/python is missing or not executable" in completed.stderr
    assert "pyproject.toml" in completed.stderr


def test_shell_wrapper_loads_operator_key_fallbacks_without_printing_them(
    tmp_path: Path,
) -> None:
    fake_repo = tmp_path / "repo with spaces"
    script = fake_repo / "hramatka" / "ops" / "local-teacher.sh"
    script.parent.mkdir(parents=True)
    shutil.copyfile(REPO_ROOT / "hramatka" / "ops" / "local-teacher.sh", script)
    fake_python = fake_repo / ".venv" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text(
        """#!/bin/bash
set -eu
[ "$HRAMATKA_AIS_API_KEY" = "fallback-ais" ]
[ "$HRAMATKA_GEMMA_FALLBACK_API_KEY_FILE" = "$HOME/.secret/openrouter.key" ]
printf 'configured\\n'
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    home = tmp_path / "operator-home"
    secret_dir = home / ".secret"
    secret_dir.mkdir(parents=True)
    (secret_dir / "google-ais.key").write_text("fallback-ais\n", encoding="utf-8")
    (secret_dir / "openrouter.key").write_text("fallback-openrouter\n", encoding="utf-8")

    completed = subprocess.run(
        ["/bin/bash", str(script)],
        env={"HOME": str(home), "PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == "configured\n"
    assert "fallback-ais" not in completed.stdout + completed.stderr
    assert "fallback-openrouter" not in completed.stdout + completed.stderr


def test_runtime_is_private_and_each_run_gets_fresh_state(tmp_path: Path) -> None:
    config = local_teacher.LaunchConfig(repo_root=REPO_ROOT)
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir(mode=0o700)
    second_root.mkdir(mode=0o700)
    base_environment = _data_environment(tmp_path)

    first = local_teacher._runtime_environment(
        config, _runtime_paths(first_root), base_environment
    )
    second = local_teacher._runtime_environment(
        config, _runtime_paths(second_root), base_environment
    )

    assert first["HRAMATKA_DB_PATH"] != second["HRAMATKA_DB_PATH"]
    assert first["HRAMATKA_CSRF_HMAC_KEY"] != second["HRAMATKA_CSRF_HMAC_KEY"]
    assert len(first["HRAMATKA_CSRF_HMAC_KEY"]) == 43
    assert stat.S_IMODE(first_root.stat().st_mode) == 0o700
    assert first["HRAMATKA_MOCK_MODE"] == "0"
    assert first["HRAMATKA_PROMPT_PACK"] == "1"
    assert first["HRAMATKA_SLOT_REPAIR"] == "1"
    assert first["HRAMATKA_GEN_JSON_MODE"] == "0"
    assert first["HRAMATKA_BAKE_PROVIDERS"] == "google-ais"


def test_slot_repair_has_an_explicit_local_opt_out(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    config = local_teacher._parse_args(["--no-slot-repair"])

    resolved = local_teacher._runtime_environment(
        config, _runtime_paths(root), _data_environment(tmp_path)
    )

    assert config.slot_repair is False
    assert resolved["HRAMATKA_SLOT_REPAIR"] == "0"


def test_openrouter_is_enabled_only_when_credential_is_configured(tmp_path: Path) -> None:
    config = local_teacher.LaunchConfig(repo_root=REPO_ROOT)
    root = tmp_path / "runtime"
    root.mkdir()
    environment = _data_environment(tmp_path)
    key_file = tmp_path / "openrouter.key"
    key_file.write_text("test-openrouter-key", encoding="utf-8")
    environment["HRAMATKA_GEMMA_FALLBACK_API_KEY_FILE"] = str(key_file)

    resolved = local_teacher._runtime_environment(config, _runtime_paths(root), environment)

    assert resolved["HRAMATKA_BAKE_PROVIDERS"] == "google-ais,openrouter"


def test_explicit_unconfigured_provider_fails_instead_of_falling_back(tmp_path: Path) -> None:
    config = local_teacher.LaunchConfig(repo_root=REPO_ROOT)
    root = tmp_path / "runtime"
    root.mkdir()
    environment = _data_environment(tmp_path)
    environment["HRAMATKA_BAKE_PROVIDERS"] = "google-ais,OpenRouter"

    with pytest.raises(local_teacher.LauncherError, match="explicitly requests openrouter"):
        local_teacher._runtime_environment(config, _runtime_paths(root), environment)


def test_explicit_unknown_provider_is_rejected_but_models_are_not_allowlisted(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    environment = _data_environment(tmp_path)
    environment["HRAMATKA_BAKE_PROVIDERS"] = "future-provider"
    with pytest.raises(local_teacher.LauncherError, match="unknown provider future-provider"):
        local_teacher._runtime_environment(
            local_teacher.LaunchConfig(repo_root=REPO_ROOT), _runtime_paths(root), environment
        )


def test_runtime_discards_legacy_process_global_model(tmp_path: Path) -> None:
    config = local_teacher.LaunchConfig(repo_root=REPO_ROOT)
    root = tmp_path / "runtime"
    root.mkdir()
    environment = _data_environment(tmp_path)
    environment["HRAMATKA_GEN_MODEL"] = "legacy/process-global-model"

    resolved = local_teacher._runtime_environment(
        config, _runtime_paths(root), environment
    )

    assert "HRAMATKA_GEN_MODEL" not in resolved


def test_legacy_model_argument_fails_with_per_lesson_migration_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        local_teacher._parse_args(["--model", "google-ais/gemma-4-31b-it"])

    assert exit_info.value.code == 2
    assert "choose a qualified model for each lesson" in capsys.readouterr().err


def test_frontend_and_proxy_tooling_never_receive_runtime_credentials() -> None:
    sanitized = local_teacher._sanitized_tool_environment(
        {
            "PATH": "/usr/bin",
            "HRAMATKA_AIS_API_KEY": "ais-secret",
            "HRAMATKA_CSRF_HMAC_KEY": "csrf-secret",
            "HRAMATKA_GEMMA_FALLBACK_API_KEY": "fallback-secret",
            "DEEPINFRA_API_KEY": "deepinfra-secret",
            "OPENROUTER_API_KEY": "openrouter-secret",
            "FORCE_COLOR": "1",
            "NO_COLOR": "1",
        }
    )
    assert sanitized == {"PATH": "/usr/bin"}


def test_isolated_test_environment_keeps_loader_state_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/test/python/lib")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("HRAMATKA_AIS_API_KEY", "must-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-leak")

    environment = _isolated_test_environment(HOME="/test/home")

    assert environment["CI"] == "true"
    assert environment["LD_LIBRARY_PATH"] == "/test/python/lib"
    assert environment["LC_ALL"] == "C.UTF-8"
    assert environment["HOME"] == "/test/home"
    assert "OPENAI_API_KEY" not in environment
    assert "HRAMATKA_AIS_API_KEY" not in environment
    assert "GITHUB_TOKEN" not in environment


def test_each_temporary_database_produces_a_fresh_unredeemed_invite(tmp_path: Path) -> None:
    config = local_teacher.LaunchConfig(repo_root=REPO_ROOT)
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    urls: list[str] = []
    database_paths: list[Path] = []
    for index in range(2):
        database = tmp_path / f"run-{index}.sqlite3"
        environment = _isolated_test_environment(
            HOME=str(isolated_home),
            TMPDIR=str(tmp_path),
            PYTHONPATH=str(REPO_ROOT),
            HRAMATKA_DB_PATH=str(database),
            HRAMATKA_PILOT_ORIGIN="https://127.0.0.1:8443",
        )
        urls.append(
            local_teacher._create_fresh_invite(
                TEST_PYTHON, config, environment, local_teacher.StopState()
            )
        )
        database_paths.append(database)

    assert urls[0] != urls[1]
    assert all(local_teacher.INVITE_RE.fullmatch(url) for url in urls)
    for database in database_paths:
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT redeemed_at, revoked_at FROM pilot_invites"
            ).fetchone()
            assert row == (None, None)
            serialized = repr(connection.execute("SELECT * FROM pilot_invites").fetchall())
            raw_token = urls[database_paths.index(database)].split("#invite=", 1)[1]
            assert raw_token not in serialized


def test_child_failure_terminates_process_groups_and_removes_runtime() -> None:
    supervisor = local_teacher.ProcessSupervisor()
    children: list[subprocess.Popen[bytes]] = []
    runtime_root: Path | None = None
    try:
        with pytest.raises(local_teacher.LauncherError, match="API server stopped unexpectedly"):
            with local_teacher._private_runtime(supervisor) as runtime_root:
                (runtime_root / "private-state").write_text("temporary", encoding="utf-8")
                for name in ("API server", "HTTPS frontend"):
                    child = subprocess.Popen(
                        [str(TEST_PYTHON), "-c", "import time; time.sleep(60)"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    children.append(child)
                    supervisor.add(name, child)

                os.killpg(children[0].pid, signal.SIGTERM)
                children[0].wait(timeout=3)
                supervisor.ensure_running()
    finally:
        supervisor.terminate_all(grace_seconds=1.0)

    assert runtime_root is not None
    assert not runtime_root.exists()
    assert all(child.poll() is not None for child in children)


def test_interruptible_command_exits_conventionally_and_reaps_its_process_group(
    tmp_path: Path,
) -> None:
    descendant_pid = tmp_path / "descendant.pid"
    stop_requested = local_teacher.StopState()
    command = [
        str(TEST_PYTHON),
        "-c",
        (
            "import pathlib, subprocess, time; "
            f"child = subprocess.Popen(['/bin/sh', '-c', 'sleep 60']); "
            f"pathlib.Path({str(descendant_pid)!r}).write_text(str(child.pid)); "
            "time.sleep(60)"
        ),
    ]
    # Wait in a separate, conventional thread rather than injecting a signal into pytest.
    def request_after_child_starts() -> None:
        deadline = time.monotonic() + 5
        while not descendant_pid.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        stop_requested.request(signal.SIGINT)

    interrupter = threading.Thread(target=request_after_child_starts, daemon=True)
    interrupter.start()
    with pytest.raises(local_teacher.LauncherInterrupted) as interrupted:
        local_teacher._run_interruptible_command(
            command,
            cwd=REPO_ROOT,
            environment=os.environ,
            stop_requested=stop_requested,
        )
    assert interrupted.value.exit_code == 130
    interrupter.join(timeout=1)
    assert descendant_pid.exists()
    pid = int(descendant_pid.read_text(encoding="utf-8"))
    _wait_for_pid_exit(pid, timeout=PROCESS_EXIT_TIMEOUT_SECONDS)


def test_process_group_cleanup_kills_term_ignoring_descendants(tmp_path: Path) -> None:
    descendant_pid = tmp_path / "descendant.pid"
    descendant_ready = tmp_path / "descendant.ready"
    descendant_command = (
        "import pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(descendant_ready)!r}).write_text('ready'); "
        "time.sleep(60)"
    )
    command = (
        "import pathlib, subprocess, time; "
        f"child = subprocess.Popen([{str(TEST_PYTHON)!r}, '-c', {descendant_command!r}]); "
        f"pathlib.Path({str(descendant_pid)!r}).write_text(str(child.pid)); "
        "time.sleep(60)"
    )
    process = subprocess.Popen(
        [
            str(TEST_PYTHON),
            "-c",
            command,
        ],
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not descendant_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert descendant_ready.exists()
        assert descendant_pid.exists()
        pid = int(descendant_pid.read_text(encoding="utf-8"))

        local_teacher._terminate_process_group(process, grace_seconds=0.1)

        _wait_for_pid_exit(pid, timeout=PROCESS_EXIT_TIMEOUT_SECONDS)
    finally:
        local_teacher._terminate_process_group(process, grace_seconds=0.1)


def test_process_group_exit_wait_has_a_bounded_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter((0.0, 0.0, 0.05, 0.1))
    sleeps: list[float] = []

    monkeypatch.setattr(local_teacher.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(local_teacher.time, "sleep", sleeps.append)
    monkeypatch.setattr(local_teacher.os, "killpg", lambda _pid, _signal: None)

    assert not local_teacher._wait_for_process_group_exit(12345, timeout=0.1)
    assert sleeps == [0.05, 0.05]


def test_pid_exit_wait_has_a_bounded_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter((0.0, 0.0, 0.05, 0.1))
    sleeps: list[float] = []

    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(time, "sleep", sleeps.append)
    monkeypatch.setattr(os, "kill", lambda _pid, _signal: None)

    with pytest.raises(pytest.fail.Exception, match="process 12345 did not exit"):
        _wait_for_pid_exit(12345, timeout=0.1)
    assert sleeps == [0.05, 0.05]


def test_interruptible_capture_drains_output_larger_than_pipe_buffers() -> None:
    stop_requested = local_teacher.StopState()
    watchdog = threading.Timer(5, stop_requested.request, args=(signal.SIGTERM,))
    watchdog.start()
    try:
        completed = local_teacher._run_interruptible_command(
            [
                str(TEST_PYTHON),
                "-c",
                (
                    "import os; "
                    "os.write(1, b'o' * (1024 * 1024)); "
                    "os.write(2, b'e' * (1024 * 1024))"
                ),
            ],
            cwd=REPO_ROOT,
            environment=os.environ,
            stop_requested=stop_requested,
            capture_output=True,
        )
    finally:
        watchdog.cancel()

    assert completed.returncode == 0
    assert completed.stdout == "o" * (1024 * 1024)
    assert completed.stderr == "e" * (1024 * 1024)


def test_cleanup_ignores_process_group_os_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class FinishedAfterSignal:
        pid = 12345

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def wait(timeout: float) -> int:
            assert timeout >= 0
            return 0

    def unavailable_group(_pid: int, _signum: int) -> None:
        raise PermissionError("process group cannot be signalled")

    monkeypatch.setattr(os, "killpg", unavailable_group)
    process = FinishedAfterSignal()

    local_teacher._terminate_process_group(process)  # type: ignore[arg-type]
    supervisor = local_teacher.ProcessSupervisor()
    supervisor.add("worker", process)  # type: ignore[arg-type]
    supervisor.terminate_all()


class _FakeProcess:
    """Minimal Popen stand-in: a pid to signal and a liveness answer."""

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> None:
        return None


def test_supervisor_uses_process_group_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _FakeProcess(pid=101)
    second = _FakeProcess(pid=102)
    signalled: list[tuple[int, int]] = []
    calls: list[tuple[object, float]] = []
    supervisor = local_teacher.ProcessSupervisor()
    supervisor.add("first", first)  # type: ignore[arg-type]
    supervisor.add("second", second)  # type: ignore[arg-type]

    def terminate(process: object, grace_seconds: float) -> None:
        calls.append((process, grace_seconds))

    monkeypatch.setattr(local_teacher, "_terminate_process_group", terminate)
    monkeypatch.setattr("os.killpg", lambda pid, sig: signalled.append((pid, sig)))

    supervisor.terminate_all(grace_seconds=0.25)

    # Every child is signalled before any of them is waited on, so one wedged
    # child cannot hold back a sibling's SIGTERM.
    assert signalled == [(102, signal.SIGTERM), (101, signal.SIGTERM)]
    assert [process for process, _ in calls] == [second, first]
    # One shared deadline: the later child gets what is left of it, never a
    # fresh full grace period.
    budgets = [grace for _, grace in calls]
    assert budgets[0] <= 0.25
    assert budgets[1] <= budgets[0]

    # A second sweep has nothing left to do.
    supervisor.terminate_all(grace_seconds=0.25)
    assert len(calls) == 2


def test_supervisor_teardown_is_bounded_by_one_shared_grace_period() -> None:
    """Wedged children must be torn down together, not one after another.

    Terminating sequentially costs grace_seconds per child, so a launcher with
    a few unresponsive children takes a multiple of the grace period to exit --
    long enough for a caller or CI job to kill it first and leave behind the
    very orphans this reaping exists to prevent.
    """
    grace = 0.6
    wedged = [
        subprocess.Popen(
            ["/bin/sh", "-c", "trap '' TERM; sleep 30"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(3)
    ]
    supervisor = local_teacher.ProcessSupervisor()
    for index, process in enumerate(wedged):
        supervisor.add(f"wedged-{index}", process)
    time.sleep(0.2)

    started = time.monotonic()
    supervisor.terminate_all(grace_seconds=grace)
    elapsed = time.monotonic() - started

    for process in wedged:
        assert process.poll() is not None, "every wedged child must be reaped"
    # One grace period plus the SIGKILL settle, not one per child.
    assert elapsed < grace * len(wedged), (
        f"teardown took {elapsed:.2f}s for {len(wedged)} children "
        f"with a {grace}s grace period; it is scaling per child"
    )


def test_real_backend_shell_allows_path_python_only_in_ci(tmp_path: Path) -> None:
    app_directory, fake_bin, fake_python = _make_real_backend_shell_repo(tmp_path)
    capture = tmp_path / "selected-python"
    prefix_capture = tmp_path / "selected-prefix"

    completed = subprocess.run(
        [str(app_directory / "e2e" / "run-real-backend.sh")],
        cwd=tmp_path,
        env=_isolated_test_environment(
            CI="true",
            PATH=f"{fake_bin}:{os.environ['PATH']}",
            E2E_PYTHON_CAPTURE=str(capture),
            E2E_PREFIX_CAPTURE=str(prefix_capture),
        ),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 23
    assert Path(capture.read_text(encoding="utf-8").strip()) == fake_python
    # CI declares the prefix of the interpreter it picked. It used to pass an
    # empty one and rely on the server skipping the check, which is the hole
    # this asserts is closed.
    assert prefix_capture.read_text(encoding="utf-8").strip() == FAKE_CI_PREFIX
    assert not (app_directory / ".real-e2e" / "python").exists()
    assert not (app_directory / ".real-e2e").exists()


def test_real_backend_shell_rejects_path_python_outside_ci(tmp_path: Path) -> None:
    app_directory, fake_bin, _fake_python = _make_real_backend_shell_repo(tmp_path)
    capture = tmp_path / "selected-python"

    completed = subprocess.run(
        [str(app_directory / "e2e" / "run-real-backend.sh")],
        cwd=tmp_path,
        env=_isolated_test_environment(
            CI="",
            PATH=f"{fake_bin}:{os.environ['PATH']}",
            E2E_PYTHON_CAPTURE=str(capture),
        ),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 1
    assert "repository Python is required outside CI" in completed.stderr
    assert not capture.exists()
    assert not (app_directory / ".real-e2e").exists()


def test_real_backend_shell_selects_repository_venv_outside_ci(tmp_path: Path) -> None:
    app_directory, fake_bin, fake_python = _make_real_backend_shell_repo(tmp_path)
    repo_root = app_directory.parents[1]
    repo_python = repo_root / ".venv" / "bin" / "python"
    repo_python.parent.mkdir(parents=True)
    repo_python.symlink_to(fake_python)
    capture = tmp_path / "selected-python"
    prefix_capture = tmp_path / "selected-prefix"

    completed = subprocess.run(
        [str(app_directory / "e2e" / "run-real-backend.sh")],
        cwd=tmp_path,
        env=_isolated_test_environment(
            CI="",
            PATH=f"{fake_bin}:{os.environ['PATH']}",
            E2E_PYTHON_CAPTURE=str(capture),
            E2E_PREFIX_CAPTURE=str(prefix_capture),
        ),
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 23
    assert Path(capture.read_text(encoding="utf-8").strip()) == repo_python
    assert prefix_capture.read_text(encoding="utf-8").strip() == str(repo_root / ".venv")
    assert not (app_directory / ".real-e2e").exists()


def test_real_backend_sigterm_reaps_api_releases_ports_and_removes_state(
    tmp_path: Path,
) -> None:
    process, _proxy_pid, api_pid, api_port, https_port, state_directory = (
        _start_real_backend_proxy(tmp_path)
    )

    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=10) == 143
    _wait_for_pid_exit(api_pid)
    _assert_port_reusable(api_port)
    _assert_port_reusable(https_port)
    assert not state_directory.exists()


def test_real_backend_proxy_sigkill_cannot_orphan_api_or_state(tmp_path: Path) -> None:
    process, proxy_pid, api_pid, api_port, https_port, state_directory = (
        _start_real_backend_proxy(tmp_path)
    )

    os.kill(proxy_pid, signal.SIGKILL)
    assert process.wait(timeout=10) == 128 + signal.SIGKILL
    _wait_for_pid_exit(api_pid)
    _assert_port_reusable(api_port)
    _assert_port_reusable(https_port)
    assert not state_directory.exists()


def test_playwright_shell_force_kills_term_unresponsive_proxy_within_grace() -> None:
    process, proxy_pid, state_directory = _start_hung_playwright_proxy()

    started = time.monotonic()
    process.send_signal(signal.SIGTERM)

    assert process.wait(timeout=5) == 143
    assert time.monotonic() - started < 4.5
    _wait_for_pid_exit(proxy_pid)
    _assert_port_reusable(5174)
    assert not state_directory.exists()


def test_concurrent_playwright_shell_fails_without_disturbing_owner(
    tmp_path: Path,
) -> None:
    process, proxy_pid, api_pid, api_port, https_port, state_directory = (
        _start_real_backend_proxy(tmp_path)
    )
    app_directory = REPO_ROOT / "hramatka" / "app"
    contender = subprocess.run(
        [str(app_directory / "e2e" / "run-real-backend.sh")],
        cwd=app_directory,
        env=_isolated_test_environment(PATH=os.environ.get("PATH", "")),
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert contender.returncode == 1
    assert "another real-backend E2E run owns .real-e2e" in contender.stderr
    assert _pid_exists(proxy_pid)
    assert _pid_exists(api_pid)
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=10) == 143
    _wait_for_pid_exit(api_pid)
    _assert_port_reusable(api_port)
    _assert_port_reusable(https_port)
    assert not state_directory.exists()


def test_old_run_refuses_to_delete_recreated_successor_state(tmp_path: Path) -> None:
    process, proxy_pid, api_pid, api_port, https_port, state_directory = (
        _start_real_backend_proxy(tmp_path)
    )
    successor_owner = "f" * 64
    shutil.rmtree(state_directory)
    state_directory.mkdir(mode=0o700)
    (state_directory / "owner").write_text(f"{successor_owner}\n", encoding="ascii")

    os.kill(proxy_pid, signal.SIGKILL)
    assert process.wait(timeout=10) == 128 + signal.SIGKILL
    _wait_for_pid_exit(api_pid)
    _assert_port_reusable(api_port)
    _assert_port_reusable(https_port)
    assert (state_directory / "owner").read_text(encoding="ascii") == (
        f"{successor_owner}\n"
    )
    shutil.rmtree(state_directory)


def test_real_backend_server_rejects_unexpected_state_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unexpected = tmp_path / ".real-e2e"
    unexpected.mkdir(mode=0o700)
    owner_token = "a" * 64
    sentinel = unexpected / "do-not-delete"
    (unexpected / "owner").write_text(f"{owner_token}\n", encoding="ascii")
    sentinel.write_text("outside the application state root", encoding="utf-8")
    monkeypatch.setenv("HRAMATKA_E2E_STATE_DIR", str(unexpected))
    monkeypatch.setenv("HRAMATKA_E2E_DB_PATH", str(unexpected / "pilot.sqlite3"))
    monkeypatch.setenv("HRAMATKA_E2E_INVITE_PATH", str(unexpected / "invite-token"))
    monkeypatch.setenv("HRAMATKA_E2E_OWNER_TOKEN", owner_token)

    with pytest.raises(RuntimeError, match="unexpected real-backend state directory"):
        real_backend_server._runtime_directory()
    assert sentinel.read_text(encoding="utf-8") == "outside the application state root"


def test_real_backend_uses_repository_venv_prefix_outside_ci(tmp_path: Path) -> None:
    # The subject is the enforcement mechanism: launched outside CI under a
    # declared prefix, the server must accept it and report it back.  Pinning
    # this to a repository-local .venv would make the test unrunnable exactly
    # where it matters most -- a runner that installs into an ambient
    # interpreter -- so it asserts against the interpreter actually running the
    # suite, which is a real prefix on every host.
    state_directory = REPO_ROOT / "hramatka" / "app" / ".real-e2e"
    assert not state_directory.exists()
    owner_token = "b" * 64
    state_directory.mkdir(mode=0o700)
    (state_directory / "owner").write_text(f"{owner_token}\n", encoding="ascii")
    read_fd, write_fd = os.pipe()
    os.set_inheritable(write_fd, True)
    process = subprocess.Popen(
        [sys.executable, "-m", "hramatka.app.e2e.real_backend_server"],
        cwd=REPO_ROOT / "hramatka" / "app",
        env=_isolated_test_environment(
            CI="",
            PYTHONPATH=str(REPO_ROOT),
            HRAMATKA_E2E_READY_FD=str(write_fd),
            HRAMATKA_E2E_STATE_DIR=str(state_directory),
            HRAMATKA_E2E_DB_PATH=str(state_directory / "pilot.sqlite3"),
            HRAMATKA_E2E_INVITE_PATH=str(state_directory / "invite-token"),
            HRAMATKA_E2E_OWNER_TOKEN=owner_token,
            HRAMATKA_E2E_ORIGIN="https://127.0.0.1:5174",
            HRAMATKA_E2E_VENV_PREFIX=sys.prefix,
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(write_fd,),
    )
    os.close(write_fd)
    try:
        ready, _, _ = select.select([read_fd], [], [], 10)
        assert ready
        identity = json.loads(os.read(read_fd, 256).decode("ascii"))
        assert identity["python_prefix"] == sys.prefix
    finally:
        os.close(read_fd)
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=10)
        shutil.rmtree(state_directory)


def test_real_backend_rejects_wrong_interpreter_before_runtime_imports(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [str(TEST_PYTHON), "-m", "hramatka.app.e2e.real_backend_server"],
        cwd=REPO_ROOT,
        env=_isolated_test_environment(
            # An ambient CI must not change the outcome; the guard has no
            # CI exception left. Setting it here is the point of the test.
            CI="true",
            PYTHONPATH=str(REPO_ROOT),
            HRAMATKA_E2E_VENV_PREFIX=str(tmp_path / "wrong-venv"),
        ),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "E2EInterpreterError: real-backend E2E interpreter prefix mismatch" in completed.stderr
    assert "ModuleNotFoundError" not in completed.stderr


@pytest.mark.parametrize("ci_value", ["true", "1", ""])
def test_interpreter_guard_has_no_environment_escape_hatch(
    monkeypatch: pytest.MonkeyPatch, ci_value: str
) -> None:
    """No environment variable may switch the interpreter check off.

    The guard once returned early when CI was set, which disabled it exactly
    where E2E results gate a release and let any process opt out by exporting
    one variable. A harness that reports success without having checked is
    worse than no harness, because the result still looks trustworthy.
    """
    monkeypatch.setenv("CI", ci_value)
    monkeypatch.delenv("HRAMATKA_E2E_VENV_PREFIX", raising=False)

    with pytest.raises(real_backend_server.E2EInterpreterError):
        real_backend_server._assert_expected_interpreter()


def test_interpreter_guard_accepts_an_equivalent_prefix_spelling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A different spelling of the same environment is not a mismatch.

    Symlinked prefixes and trailing separators name the identical interpreter,
    so rejecting them would be a false alarm that teaches everyone to work
    around the guard.
    """
    link = tmp_path / "venv-link"
    link.symlink_to(Path(sys.prefix), target_is_directory=True)

    for spelling in (f"{sys.prefix}/", str(link)):
        monkeypatch.setenv("HRAMATKA_E2E_VENV_PREFIX", spelling)
        real_backend_server._assert_expected_interpreter()


def test_readiness_wait_cancels_promptly() -> None:
    stop_requested = local_teacher.StopState()
    stop_requested.request(signal.SIGTERM)
    started = time.monotonic()
    with pytest.raises(local_teacher.LauncherInterrupted) as interrupted:
        local_teacher._wait_for_ready(
            "http://127.0.0.1:1/api/readyz",
            local_teacher.ProcessSupervisor(),
            stop_requested,
            timeout_seconds=10,
        )
    assert interrupted.value.exit_code == 143
    assert time.monotonic() - started < 0.2


def test_local_teacher_shell_smoke_serves_one_invite_and_releases_ports(tmp_path: Path) -> None:
    fake_repo, fake_bin = _make_smoke_repo(tmp_path)
    assert (fake_repo / ".venv" / "bin" / "python").resolve() == TEST_PYTHON
    api_port, https_port = _free_ports()
    environment = _isolated_test_environment(
        HOME=str(tmp_path / "home"),
        TMPDIR=str(tmp_path / "tmp"),
        PATH=f"{fake_bin}:{os.environ['PATH']}",
        HRAMATKA_AIS_API_KEY="smoke-ais-key",
        HRAMATKA_DATA_DIR=str(tmp_path / "release"),
        HRAMATKA_DATA_MANIFEST=str(tmp_path / "release" / "data-manifest.json"),
    )
    Path(environment["HOME"]).mkdir()
    Path(environment["TMPDIR"]).mkdir()
    process = subprocess.Popen(
        [
            str(fake_repo / "hramatka" / "ops" / "local-teacher.sh"),
            "--api-port",
            str(api_port),
            "--https-port",
            str(https_port),
        ],
        cwd=fake_repo,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        output = _wait_for_output(process, "#invite=")
        invites = [line for line in output.splitlines() if "#invite=" in line]
        assert len(invites) == 1
        context = ssl.create_default_context(
            cafile=str(next(Path(environment["TMPDIR"]).glob("hramatka-local-teacher-*/tls-cert.pem")))
        )
        with urllib.request.urlopen(
            f"https://127.0.0.1:{https_port}/teacher", context=context, timeout=5
        ) as response:
            assert response.status == 200
        with urllib.request.urlopen(
            f"https://127.0.0.1:{https_port}/api/readyz", context=context, timeout=5
        ) as response:
            assert response.status == 200
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=10) == 130
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
    stderr = process.stderr.read() if process.stderr is not None else ""
    assert "smoke-ais-key" not in output + stderr
    assert not list(Path(environment["TMPDIR"]).glob("hramatka-local-teacher-*"))
    _assert_port_reusable(api_port)
    _assert_port_reusable(https_port)


def test_local_teacher_termination_during_frontend_build_releases_reserved_ports(
    tmp_path: Path,
) -> None:
    fake_repo, fake_bin = _make_smoke_repo(tmp_path, delayed_npm=True)
    assert (fake_repo / ".venv" / "bin" / "python").resolve() == TEST_PYTHON
    api_port, https_port = _free_ports()
    started = tmp_path / "npm-started"
    npm_pid = tmp_path / "npm.pid"
    runtime_dir = tmp_path / "tmp"
    environment = _isolated_test_environment(
        HOME=str(tmp_path / "home"),
        TMPDIR=str(runtime_dir),
        PATH=f"{fake_bin}:{os.environ['PATH']}",
        HRAMATKA_AIS_API_KEY="delayed-ais-key",
        HRAMATKA_DATA_DIR=str(tmp_path / "release"),
        HRAMATKA_DATA_MANIFEST=str(tmp_path / "release" / "data-manifest.json"),
        FAKE_NPM_STARTED=str(started),
        FAKE_NPM_PID=str(npm_pid),
    )
    Path(environment["HOME"]).mkdir()
    runtime_dir.mkdir()
    process = subprocess.Popen(
        [
            str(fake_repo / "hramatka" / "ops" / "local-teacher.sh"),
            "--api-port",
            str(api_port),
            "--https-port",
            str(https_port),
        ],
        cwd=fake_repo,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert started.exists(), process.stderr.read() if process.stderr is not None else ""
        for port in (api_port, https_port):
            with pytest.raises(OSError):
                _assert_port_reusable(port)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 143
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    pid = int(npm_pid.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not list(runtime_dir.glob("hramatka-local-teacher-*"))
    _assert_port_reusable(api_port)
    _assert_port_reusable(https_port)
