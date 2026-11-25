# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""AutoMetashape: Production Cluster Server Launcher.

Starts the central Agisoft Metashape Network Processing Server daemon on the
coordinator workstation. Features:
- Port availability pre-flight check (guards against port collisions).
- LAN IP auto-detection and instructions for remote worker connection.
- Clean shutdown on Ctrl+C and deterministic PID lifecycle tracking.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

# Ensure scripts directory is importable
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_SCRIPTS_DIR, ".."))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import cluster_utils as cu

PID_FILE_PATH = os.path.join(tempfile.gettempdir(), "autometashape_server.pid")


def record_server_pid() -> None:
    """Record current server process ID to a well-known temp file for deterministic process management."""
    try:
        with open(PID_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def clear_server_pid() -> None:
    """Remove server PID tracking file on clean exit."""
    try:
        if os.path.isfile(PID_FILE_PATH):
            os.remove(PID_FILE_PATH)
    except Exception:
        pass


# Configure Windows console streams for international Unicode paths
if sys.platform == "win32":
    for _stream_name in ("stdout", "stderr", "stdin"):
        _stream = getattr(sys, _stream_name, None)
        if _stream and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


check_update_pending = cu.check_update_pending


def reexec_server(target_dir: str) -> None:
    """Re-execute the server supervisor process image with updated Python code from disk."""
    launcher_py = os.path.join(target_dir, "scripts", "launch_server.py")
    if not os.path.isfile(launcher_py):
        launcher_py = os.path.abspath(__file__)

    # STRICT INVARIANT: Always prioritize Metashape Pro bundled Python
    py_exe = cu.find_metashape_python_path() or sys.executable
    cmd = [py_exe, launcher_py] + sys.argv[1:]
    print(f"[SUPERVISOR] Re-executing fresh server process image: {cmd[0]} {cmd[1]}")
    sys.stdout.flush()
    sys.stderr.flush()
    clear_server_pid()
    if sys.platform == "win32":
        # Windows wexecv does not quote arguments containing spaces, which causes
        # 'C:\Program: can't open file...' crash when py_exe or path has spaces.
        quoted_cmd = [f'"{c}"' if " " in c and not (c.startswith('"') and c.endswith('"')) else c for c in cmd]
        os.execv(py_exe, quoted_cmd)
    else:
        os.execv(py_exe, cmd)


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser for server launcher."""
    parser = argparse.ArgumentParser(
        description="AutoMetashape Distributed Cluster Server Launcher",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Binding host IP for control server")
    parser.add_argument(
        "--port", type=str, default=None, help="Control port for client and worker connections (default: 5840)"
    )
    parser.add_argument(
        "--dispatch-port", type=str, default=None, help="Dispatch port for task distribution (default: 5841)"
    )
    parser.add_argument("--config", type=str, default="", help="Path to custom JSON server configuration file")
    parser.add_argument("--server", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    """Main server launcher entrypoint."""
    record_server_pid()
    try:
        return _run_server_lifecycle()
    finally:
        clear_server_pid()


def _run_server_lifecycle() -> int:
    """Execute server configuration resolution, diagnostics, and process lifecycle."""
    parser = build_arg_parser()
    args, unknown = parser.parse_known_args()
    if unknown:
        cleaned = [u for u in unknown if u != "--server"]
        if cleaned:
            print(f"[NOTICE] Ignoring unrecognized server arguments: {' '.join(cleaned)}")

    # 1. Resolve Configuration
    config_path = args.config or cu.get_default_config_path(cu.DEFAULT_SERVER_CONFIG_FILENAME)
    saved_config = cu.load_cluster_config(config_path)

    host = args.host if args.host != "0.0.0.0" else saved_config.get("host", "0.0.0.0")
    port = args.port or saved_config.get("port", "5840")
    dispatch_port = args.dispatch_port or saved_config.get("dispatch_port", "5841")

    # 2. Locate Metashape Server binary (Metashape Pro 2.x requires standalone metashape-server)
    ms_exe = cu.find_metashape_path()
    server_bin = cu.find_metashape_server_path(ms_exe)
    if not server_bin or not os.path.isfile(server_bin):
        print(
            "[FATAL ERROR] Standalone 'metashape-server' executable was not found. "
            "Agisoft Metashape Pro 2.x requires the standalone metashape-server binary "
            "(metashape.exe no longer supports the legacy 1.x --server flag).",
            file=sys.stderr,
        )
        return 1

    lan_ip = cu.get_primary_lan_ip()

    print("=" * 72)
    print("  AutoMetashape Distributed Cluster - Server Daemon Launcher")
    print(f"  Bind Control IP : {host}:{port}")
    print(f"  Dispatch Port   : {dispatch_port}")
    print(f"  Coordinator LAN : {lan_ip}")
    print(f"  Server Binary   : {server_bin}")
    print("=" * 72)

    # 3. Port Pre-flight Check
    try:
        port_num = int(port)
    except ValueError:
        port_num = 5840

    if cu.test_tcp_connection("127.0.0.1", port_num, timeout=1.0):
        print(f"\n[WARNING] Port {port_num} is ALREADY IN USE on this PC!", file=sys.stderr)
        print("A Metashape Server or other service is already active on this port.", file=sys.stderr)

    # 4. Construct Command
    server_cmd = [
        server_bin,
        "--host",
        f"{host}:{port}",
        "--no-database",
        "--resume-workers",
    ]

    print("\n[INSTRUCTIONS FOR REMOTE WORKERS]")
    print(f'Run on each worker PC:  .\\launch_worker.bat --host {lan_ip} --root "\\\\{lan_ip}\\<share>"')
    print("\n[READY] Starting Metashape Server daemon...")
    print(f"Command: {' '.join(server_cmd)}")
    print("Press Ctrl+C in this terminal to stop the cluster server.\n")

    target_dir = os.environ.get("AUTOMETASHAPE_DIR", "").strip()
    if not target_dir or not os.path.isdir(target_dir):
        if os.path.isdir(_REPO_ROOT):
            target_dir = _REPO_ROOT
        else:
            user_profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
            target_dir = os.path.join(user_profile, "Documents", "AutoMetashape")

    proc = None
    try:
        proc = subprocess.Popen(server_cmd, cwd=target_dir)
        proc.wait()
        ret_code = proc.returncode

        # Check if an autonomous update was requested on the coordinator host
        signal_data = check_update_pending(target_dir)
        if signal_data is not None:
            print("\n" + "=" * 72)
            print("  [SUPERVISOR] Autonomous Update Signal (.update_pending) Detected on Server!")
            print("  Applying Git-parity codebase update with file pruning...")
            print("=" * 72)
            update_ok = cu.perform_worker_codebase_update(signal_data, target_dir)
            if update_ok:
                print("[SUPERVISOR] Server codebase update applied! Re-executing supervisor image...")
                reexec_server(target_dir)
                return 0

        return ret_code
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Cluster server stopped by user (Ctrl+C). Goodbye!")
        return 0
    except Exception as e:
        print(f"\n[ERROR] Server execution error: {e}", file=sys.stderr)
        return 1
    finally:
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


if __name__ == "__main__":
    sys.exit(main())
