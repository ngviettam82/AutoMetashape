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
    """Remove server PID tracking file on clean exit if it belongs to this process."""
    try:
        if os.path.isfile(PID_FILE_PATH):
            with open(PID_FILE_PATH, "r", encoding="utf-8") as f:
                recorded = f.read().strip()
            if recorded and recorded != str(os.getpid()):
                return
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
        child = subprocess.Popen(cmd)
        while True:
            try:
                rc = child.wait()
                break
            except KeyboardInterrupt:
                continue
        sys.exit(rc)
    else:
        os.execv(py_exe, cmd)


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser for server launcher."""
    parser = argparse.ArgumentParser(
        description="AutoMetashape Distributed Cluster Server Launcher",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", type=str, default=None, help="Binding host IP for control server (default: 0.0.0.0)")
    parser.add_argument(
        "--port", type=str, default=None, help="Control port for client and worker connections (default: 5840)"
    )
    parser.add_argument(
        "--root",
        "--result-root",
        "--result-share",
        dest="root",
        type=str,
        default="",
        help="Optional shared cluster Result storage root persisted to server_config.json for cluster tools",
    )
    parser.add_argument(
        "--dispatch-port",
        type=str,
        default=None,
        help="(Deprecated: Metashape Pro 2.x uses a single unified control port)",
    )
    parser.add_argument("--config", type=str, default="", help="Path to custom JSON server configuration file")
    parser.add_argument("--server", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    """Main server launcher entrypoint."""
    return _run_server_lifecycle(manage_pid=True)


def _run_server_lifecycle(manage_pid: bool = False) -> int:
    """Execute server configuration resolution, diagnostics, and process lifecycle."""
    parser = build_arg_parser()
    args, unknown = parser.parse_known_args()
    if unknown:
        cleaned = [u for u in unknown if u != "--server"]
        if cleaned:
            print(f"[NOTICE] Ignoring unrecognized server arguments: {' '.join(cleaned)}")

    # 1. Resolve Configuration (explicit CLI --host overrides saved config, including 0.0.0.0)
    read_cfg_path = args.config or cu.get_default_config_path(cu.DEFAULT_SERVER_CONFIG_FILENAME, for_write=False)
    saved_config = cu.load_cluster_config(read_cfg_path)

    raw_host = args.host if args.host is not None else saved_config.get("host", "0.0.0.0")
    raw_port = args.port or saved_config.get("port", "5840")
    try:
        fallback_port = int(raw_port)
    except (ValueError, TypeError):
        fallback_port = 5840
    host, parsed_port = cu.split_host_port(str(raw_host or "0.0.0.0"), default_port=fallback_port)
    port = args.port if args.port is not None else str(parsed_port)
    shared_root = args.root.strip() or saved_config.get("root", "")

    if args.root.strip():
        updated_cfg = dict(saved_config)
        updated_cfg.setdefault("host", host)
        updated_cfg.setdefault("port", str(port))
        updated_cfg["root"] = args.root.strip()
        try:
            cu.save_cluster_config(updated_cfg, args.config or None, filename=cu.DEFAULT_SERVER_CONFIG_FILENAME)
        except Exception:
            pass

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

    # 3. Port Pre-flight Check (refuse duplicate server launch before touching PID file)
    try:
        port_num = int(port)
        if not (1 <= port_num <= 65535):
            raise ValueError(f"Port {port_num} out of valid TCP range 1..65535")
    except (ValueError, TypeError):
        print(f"[WARNING] Invalid port '{port}'; falling back to 5840.", file=sys.stderr)
        port_num = 5840
        port = "5840"

    print("=" * 72)
    print("  AutoMetashape Distributed Cluster - Server Daemon Launcher")
    print(f"  Bind Control IP : {cu.format_host_port(host, port_num)}")
    if shared_root:
        print(f"  Shared Root     : {shared_root}")
    print(f"  Coordinator LAN : {lan_ip}")
    print(f"  Server Binary   : {server_bin}")
    print("=" * 72)

    clean_probe_host = str(host or "").strip()
    if clean_probe_host.startswith("[") and clean_probe_host.endswith("]"):
        clean_probe_host = clean_probe_host[1:-1].strip()
    if clean_probe_host in ("0.0.0.0", ""):
        probe_host = "127.0.0.1"
    elif clean_probe_host == "::":
        probe_host = "::1"
    else:
        probe_host = clean_probe_host

    if cu.test_tcp_connection(probe_host, port_num, timeout=1.0):
        print(f"\n[ERROR] Port {port_num} is ALREADY IN USE on this PC!", file=sys.stderr)
        print(
            "A Metashape Server or other service is already active on this port. Aborting duplicate launch.",
            file=sys.stderr,
        )
        return 1

    if manage_pid:
        record_server_pid()
    proc = None
    try:
        # 4. Construct Command
        server_cmd = [
            server_bin,
            "--host",
            cu.format_host_port(host, port_num),
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

        popen_kwargs: dict[str, object] = {"cwd": target_dir}
        if sys.platform != "win32":
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(server_cmd, **popen_kwargs)
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
        if manage_pid:
            clear_server_pid()


if __name__ == "__main__":
    sys.exit(main())
