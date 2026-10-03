#!/usr/bin/env python3
"""Exercise the local owner daemon through independent clients.

Run inside the pinned environment, for example:
    uv run python -m tools.local_service_probe

This is a local integration probe. It starts one disposable daemon and virtual
KCalc session, then uses the CLI, the JSONL Unix socket protocol, and the MCP
SDK stdio client. It never calls a paid model or prints screenshot payloads or
raw errors.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import importlib.metadata
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
MAX_FRAME = 2 * 1024 * 1024
READY_SECONDS = 20.0
CLIENT_TIMEOUT = 30.0


class ProbeError(RuntimeError):
    """Safe probe failure code; never contains child output or payload data."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
        self.probe_id: str | None = None
        self.runtime_dir: str | None = None
        self.evidence_dir: str | None = None


def _socket_request(path: Path, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    request = {
        "id": uuid.uuid4().hex,
        "method": method,
        "params": params or {},
        "context": {"caller_node": "local-service-probe", "transport": "unix"},
    }
    wire = (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")
    if len(wire) > MAX_FRAME:
        raise ProbeError("request_too_large")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(CLIENT_TIMEOUT)
    try:
        client.connect(str(path))
        client.sendall(wire)
        chunks: list[bytes] = []
        size = 0
        while True:
            block = client.recv(65536)
            if not block:
                break
            size += len(block)
            if size > MAX_FRAME:
                raise ProbeError("response_too_large")
            chunks.append(block)
            if b"\n" in block:
                break
    except (OSError, TimeoutError) as exc:
        raise ProbeError("unix_client_failed") from exc
    finally:
        client.close()
    try:
        response = json.loads(b"".join(chunks).split(b"\n", 1)[0])
    except (json.JSONDecodeError, UnicodeDecodeError, IndexError) as exc:
        raise ProbeError("invalid_unix_response") from exc
    if not isinstance(response, dict) or response.get("id") != request["id"]:
        raise ProbeError("unix_response_id_mismatch")
    if isinstance(response.get("result"), dict):
        return response["result"]
    error = response.get("error")
    return {"ok": False, "error": {"code": error.get("code", "rpc_failed") if isinstance(error, dict) else "rpc_failed"}}


def _run_cli(socket_path: Path, *args: str) -> tuple[int, dict[str, Any] | None]:
    env = os.environ.copy()
    env["JEV_DESKTOP_SOCKET"] = str(socket_path)
    command = [sys.executable, str(ROOT / "desktop_cli.py"), *args]
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=CLIENT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError("cli_client_failed") from exc
    parsed: dict[str, Any] | None = None
    try:
        value = json.loads(completed.stdout)
        if isinstance(value, dict):
            parsed = value
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return completed.returncode, parsed


def _mcp_observation_ok(call_result: Any, expected_session_id: str) -> bool:
    if bool(getattr(call_result, "is_error", getattr(call_result, "isError", False))):
        return False
    body = getattr(call_result, "structured_content", getattr(call_result, "structuredContent", None))
    if not isinstance(body, dict):
        body = None
        for block in getattr(call_result, "content", []):
            text_value = getattr(block, "text", None)
            if not isinstance(text_value, str):
                continue
            try:
                decoded = json.loads(text_value)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                body = decoded
                break
    if not isinstance(body, dict) or body.get("ok") is not True:
        return False
    observation = body.get("observation") if isinstance(body.get("observation"), dict) else {}
    capture = observation.get("capture") if isinstance(observation.get("capture"), dict) else {}
    window = capture.get("window") if isinstance(capture.get("window"), dict) else {}
    return window.get("app") in {"kcalc", "org.kde.kcalc"} and capture.get("session_id") == expected_session_id


async def _mcp_probe(socket_path: Path, expected_session_id: str) -> dict[str, Any]:
    """Use the official MCP SDK against the actual stdio client process."""
    try:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except ImportError as exc:
        raise ProbeError("mcp_sdk_unavailable") from exc

    env = os.environ.copy()
    env["JEV_DESKTOP_SOCKET"] = str(socket_path)
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "desktop_mcp.py")],
        env=env,
    )
    try:
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await asyncio.wait_for(client.initialize(), timeout=CLIENT_TIMEOUT)
                listed = await asyncio.wait_for(client.list_tools(), timeout=CLIENT_TIMEOUT)
                tool_names = sorted(
                    tool.name for tool in listed.tools if isinstance(getattr(tool, "name", None), str)
                )[:40]
                # Harmless observation through MCP, proving its call path reaches
                # the same daemon while the direct and CLI clients are available.
                observation = await asyncio.wait_for(
                    client.call_tool(
                        "desktop_observe",
                        {"app": "kcalc", "output": "metadata", "scope": "app"},
                    ),
                    timeout=CLIENT_TIMEOUT,
                )
                observation_ok = _mcp_observation_ok(observation, expected_session_id)
                return {
                    "initialized": True,
                    "tools": tool_names,
                    "kcalc_observe_ok": observation_ok,
                }
    except ProbeError:
        raise
    except Exception as exc:
        # Keep raw tool/process errors out of the result and terminal output.
        raise ProbeError("mcp_roundtrip_failed") from exc


def _version(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    line = completed.stdout.decode("utf-8", "replace").splitlines()
    return line[0][:120] if completed.returncode == 0 and line else None


def run_probe() -> dict[str, Any]:
    run_root = ROOT / "run"
    run_root.mkdir(mode=0o700, exist_ok=True)
    probe_dir = Path(tempfile.mkdtemp(prefix="local-service-probe-", dir=run_root))
    os.chmod(probe_dir, 0o700)
    socket_path = probe_dir / "owner.sock"
    env = os.environ.copy()
    env["JEV_DESKTOP_SOCKET"] = str(socket_path)
    daemon: subprocess.Popen[bytes] | None = None
    session_started = False
    session_id: str | None = None
    failure: ProbeError | None = None
    cleanup: dict[str, Any] = {
        "owner_cleanup_confirmed": False,
        "session_absent": False,
        "daemon_graceful": False,
        "daemon_exited": False,
        "runtime_dir_removed": False,
        "runtime_dir_preserved": False,
        "evidence_retained": False,
    }
    result: dict[str, Any] = {
        "probe_id": probe_dir.name,
        "runtime_dir": str(probe_dir),
        "failure_code": None,
        "paid_calls": 0,
        "clients": {},
        "versions": {
            "python": sys.version.split()[0],
            "kwin_mcp": importlib.metadata.version("kwin-mcp"),
            "kwin": _version(["kwin_wayland", "--version"]),
        },
    }
    try:
        command = [
            sys.executable,
            str(ROOT / "desktop_daemon.py"),
            "--foreground",
            "--socket",
            str(socket_path),
            "--run-dir",
            str(probe_dir),
            "--allow-app",
            "kcalc",
        ]
        daemon = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + READY_SECONDS
        while time.monotonic() < deadline:
            if daemon.poll() is not None:
                raise ProbeError("daemon_exited_before_ready")
            if socket_path.exists():
                try:
                    ready = _socket_request(socket_path, "status")
                    if ready.get("ok") is True:
                        break
                except ProbeError:
                    pass
            time.sleep(0.1)
        else:
            raise ProbeError("daemon_readiness_timeout")

        caps = _socket_request(socket_path, "capabilities")
        if caps.get("ok") is not True:
            raise ProbeError("capabilities_failed")
        result["capabilities_ok"] = True

        cli_code, cli_status = _run_cli(socket_path, "status")
        result["clients"]["cli"] = {"status_exit_code": cli_code, "status_ok": cli_code == 0 and cli_status is not None and cli_status.get("ok") is True}
        if not result["clients"]["cli"]["status_ok"]:
            raise ProbeError("cli_status_failed")

        started = _socket_request(socket_path, "session_start", {"app": "kcalc", "mode": "guarded"})
        if started.get("ok") is not True:
            raise ProbeError("kcalc_session_start_failed")
        session = started.get("session")
        if not isinstance(session, dict) or not isinstance(session.get("session_id"), str):
            raise ProbeError("session_id_missing")
        session_id = session["session_id"]
        session_started = True

        # MCP is a separate stdio process and observes the session started via
        # the Unix client above, proving both surfaces share the same owner.
        mcp = awaitable_result(_mcp_probe(socket_path, session_id))
        result["clients"]["mcp"] = mcp
        if not mcp.get("initialized") or not mcp.get("kcalc_observe_ok"):
            raise ProbeError("mcp_observation_failed")

        before = _socket_request(socket_path, "observe", {"app": "kcalc", "scope": "app", "output": "metadata"})
        capture = before.get("observation", {}).get("capture") if isinstance(before.get("observation"), dict) else None
        if before.get("ok") is not True or not isinstance(capture, dict):
            raise ProbeError("metadata_observation_failed")
        capture_id = capture.get("capture_id")
        image_result = _socket_request(
            socket_path,
            "observe",
            {"app": "kcalc", "scope": "app", "output": "image", "capture_id": capture_id},
        )
        image_observation = image_result.get("observation") if isinstance(image_result.get("observation"), dict) else {}
        image_capture = image_observation.get("capture") if isinstance(image_observation.get("capture"), dict) else {}
        encoded = image_observation.get("image_base64")
        try:
            image_bytes = base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else b""
        except (ValueError, base64.binascii.Error):
            image_bytes = b""
        same_capture = (
            image_result.get("ok") is True
            and image_capture.get("capture_id") == capture_id
            and image_capture.get("image_sha256") == capture.get("image_sha256")
            and len(image_bytes) > 0
            and hashlib.sha256(image_bytes).hexdigest() == capture.get("image_sha256")
        )
        result["clients"]["unix"] = {
            "capabilities_ok": True,
            "metadata_ok": before.get("ok") is True,
            "image_ok": image_result.get("ok") is True and bool(image_bytes),
            "metadata_image_same_capture": same_capture,
            "mcp_tools": mcp.get("tools", []),
        }
        del image_bytes
        if not same_capture:
            raise ProbeError("capture_identity_mismatch")

        cli_candidates_code, candidates_result = _run_cli(socket_path, "candidates", "kcalc")
        candidates_result = candidates_result or {}
        candidates = candidates_result.get("candidates") if isinstance(candidates_result.get("candidates"), list) else []
        ones = [item for item in candidates if isinstance(item, dict) and item.get("label") == "One" and "click" in item.get("actions", [])]
        action_result: dict[str, Any] | None = None
        action_exit_code: int | None = None
        if cli_candidates_code == 0 and candidates_result.get("ok") is True and len(ones) == 1 and isinstance(ones[0].get("ref"), str):
            action_exit_code, action_result = _run_cli(
                socket_path, "act", "kcalc", "click", ones[0]["ref"],
                "--verification", "display_text", "--expected", "1",
            )
        result["kcalc_one_click"] = {
            "supported": len(ones) == 1,
            "attempted": action_result is not None,
            "cli_exit_code": action_exit_code,
            "ok": action_exit_code == 0 and action_result is not None and action_result.get("ok") is True,
            "verified": action_result is not None and action_result.get("status") == "ok" and action_result.get("verification") == "passed",
        }
        if cli_candidates_code != 0 or len(ones) != 1 or action_result is None or action_exit_code != 0 or action_result.get("ok") is not True or action_result.get("status") != "ok" or action_result.get("verification") != "passed":
            raise ProbeError("kcalc_one_click_unverified")

        # Exercise the independent MCP process while the daemon owns a session.
        cancel = _socket_request(socket_path, "cancel", {"session_id": session_id})
        result["cancellation"] = {"ok": cancel.get("ok") is True}
        if not result["cancellation"]["ok"]:
            raise ProbeError("cancellation_failed")

        # Worker death is an explicit M3 recovery check. Only signal the exact
        # child PID the owner reports for this newly created KCalc session.
        live_status = _socket_request(socket_path, "status")
        live_session = live_status.get("session") if isinstance(live_status.get("session"), dict) else {}
        worker_pid = live_session.get("worker_pid")
        if live_status.get("ok") is not True or live_session.get("session_id") != session_id or live_session.get("app") != "kcalc" or type(worker_pid) is not int or worker_pid <= 1 or worker_pid == os.getpid():
            raise ProbeError("worker_pid_unavailable")
        os.kill(worker_pid, signal.SIGKILL)
        death = _socket_request(socket_path, "observe", {"app": "kcalc", "scope": "app", "output": "metadata", "session_id": session_id})
        broken_session: dict[str, Any] = {}
        broken_deadline = time.monotonic() + 5.0
        while time.monotonic() < broken_deadline:
            broken_status = _socket_request(socket_path, "status")
            current = broken_status.get("session") if isinstance(broken_status.get("session"), dict) else {}
            if current.get("state") == "broken":
                broken_session = current
                break
            time.sleep(0.1)
        result["worker_death"] = {
            "signaled_reported_worker": True,
            "failure_detected": death.get("ok") is False,
            "session_marked_broken": broken_session.get("state") == "broken",
        }
        stopped = _socket_request(socket_path, "stop_all")
        cleanup_report = stopped.get("cleanup")
        cleanup_confirmed = (
            cleanup_report == "confirmed"
            or cleanup_report is True
            or (isinstance(cleanup_report, dict) and cleanup_report.get("confirmed") is True)
        )
        cleanup["owner_cleanup_confirmed"] = stopped.get("ok") is True and stopped.get("stopped") is True and cleanup_confirmed
        status = _socket_request(socket_path, "status")
        status_session = status.get("session")
        result["shutdown"] = {
            "status_ok": status.get("ok") is True,
            "no_active_session": status_session is None,
        }
        cleanup["session_absent"] = status.get("ok") is True and status_session is None
        result["worker_death"]["teardown_ok"] = cleanup["owner_cleanup_confirmed"] and result["shutdown"]["no_active_session"]
        if not all(result["worker_death"].values()):
            raise ProbeError("worker_death_recovery_failed")
        if not result["shutdown"]["no_active_session"]:
            raise ProbeError("session_remains_after_stop_all")
        restarted = _socket_request(socket_path, "session_start", {"app": "kcalc"})
        replacement = restarted.get("session", {})
        result["restart_after_recovery"] = {
            "ok": restarted.get("ok") is True,
            "new_worker": replacement.get("worker_pid") not in (None, worker_pid),
        }
        if not all(result["restart_after_recovery"].values()):
            raise ProbeError("restart_after_recovery_failed")
        restopped = _socket_request(socket_path, "stop_all")
        if restopped.get("stopped") is not True or restopped.get("cleanup") != "confirmed":
            raise ProbeError("replacement_cleanup_failed")
    except ProbeError as exc:
        failure = exc
    except Exception as exc:
        failure = ProbeError(f"unexpected_{type(exc).__name__.casefold()}")
    finally:
        if daemon is not None:
            if daemon.poll() is None and socket_path.exists():
                try:
                    stop = _socket_request(socket_path, "stop_all")
                    report = stop.get("cleanup")
                    confirmed = report == "confirmed" or report is True or (isinstance(report, dict) and report.get("confirmed") is True)
                    cleanup["owner_cleanup_confirmed"] = cleanup["owner_cleanup_confirmed"] or (
                        stop.get("ok") is True
                        and stop.get("stopped") is True
                        and (confirmed or not session_started)
                    )
                    final_status = _socket_request(socket_path, "status")
                    cleanup["session_absent"] = (
                        final_status.get("ok") is True
                        and final_status.get("session") is None
                    )
                    _socket_request(socket_path, "shutdown")
                except ProbeError:
                    pass
            try:
                daemon.wait(timeout=8)
                cleanup["daemon_graceful"] = daemon.returncode == 0
            except subprocess.TimeoutExpired:
                cleanup["daemon_graceful"] = False
                # Reap only this process after recording failure of graceful shutdown.
                daemon.kill()
                daemon.wait(timeout=5)
            cleanup["daemon_exited"] = daemon.poll() is not None
        elif not session_started:
            cleanup["owner_cleanup_confirmed"] = True
            cleanup["session_absent"] = True

        # No worker was successfully started, so no virtual runtime needs
        # journal-based cleanup. A started session requires the owner's
        # explicit confirmation before its runtime directory can be removed.
        if not session_started:
            cleanup["owner_cleanup_confirmed"] = cleanup["owner_cleanup_confirmed"] or cleanup["daemon_graceful"]
            cleanup["session_absent"] = True

        # Preserve review evidence before removing the private socket/runtime
        # directory. The separate result dir is scoped to this unique probe id.
        evidence_root = run_root / "local-service-probe-results"
        evidence_dir = evidence_root / probe_dir.name
        try:
            evidence_root.mkdir(mode=0o700, exist_ok=True)
            os.chmod(evidence_root, 0o700)
            evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
            os.chmod(evidence_dir, 0o700)
            audit = probe_dir / "audit.jsonl"
            if audit.is_file() and not audit.is_symlink():
                shutil.copyfile(audit, evidence_dir / "audit.jsonl")
                os.chmod(evidence_dir / "audit.jsonl", 0o600)
            result["evidence_dir"] = str(evidence_dir.relative_to(ROOT))
        except OSError:
            cleanup["evidence_retained"] = False
        else:
            cleanup["evidence_retained"] = True

        can_remove_runtime = (
            cleanup["daemon_graceful"]
            and cleanup["daemon_exited"]
            and cleanup["owner_cleanup_confirmed"]
            and cleanup["session_absent"]
        )
        if can_remove_runtime:
            try:
                shutil.rmtree(probe_dir)
                cleanup["runtime_dir_removed"] = not probe_dir.exists()
            except OSError:
                cleanup["runtime_dir_removed"] = False
        else:
            cleanup["runtime_dir_preserved"] = probe_dir.exists()

        if failure is None and not (
            cleanup["daemon_graceful"]
            and cleanup["daemon_exited"]
            and cleanup["owner_cleanup_confirmed"]
            and cleanup["session_absent"]
            and cleanup["evidence_retained"]
            and cleanup["runtime_dir_removed"]
        ):
            failure = ProbeError("cleanup_incomplete")
        result["failure_code"] = failure.code if failure is not None else None
        result["runtime_dir"] = str(probe_dir)
        result["cleanup"] = cleanup
        if cleanup.get("evidence_retained"):
            try:
                (evidence_dir / "summary.json").write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
                os.chmod(evidence_dir / "summary.json", 0o600)
            except OSError:
                cleanup["evidence_retained"] = False
        if failure is not None:
            failure.probe_id = probe_dir.name
            failure.runtime_dir = str(probe_dir) if probe_dir.exists() else None
            failure.evidence_dir = str(evidence_dir) if evidence_dir.exists() else None

    if failure is not None:
        raise failure
    return result


def awaitable_result(coro: Any) -> Any:
    """Run the MCP round trip from the synchronous harness entry point."""
    return asyncio.run(coro)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        result = run_probe()
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except ProbeError as exc:
        print(json.dumps({
            "ok": False,
            "error_code": exc.code,
            "probe_id": exc.probe_id,
            "runtime_dir": exc.runtime_dir,
            "evidence_dir": exc.evidence_dir,
        }, sort_keys=True, separators=(",", ":")))
        return 1
    except Exception as exc:
        # Unexpected exceptions are named but never rendered with arguments.
        print(json.dumps({"ok": False, "error_code": type(exc).__name__}, sort_keys=True, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
