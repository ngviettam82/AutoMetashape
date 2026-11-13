# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""AutoMetashape: Production Resilient Cluster Worker Node Launcher.

Connects the local PC's CPU and GPU compute resources to the Agisoft Metashape
Distributed Network Processing Cluster. Features:
- 1-click execution via auto-saved configuration (worker_config.json).
- Interactive first-run wizard with smart defaults and LAN auto-discovery.
- Pre-flight health diagnostics (Metashape binary, TCP port 5840, UNC storage access).
- Resilient auto-reconnect loop against temporary network or server interruptions.
- Clean process lifecycle management with Ctrl+C termination.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

# Ensure scripts directory is importable
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import cluster_utils as cu

# Configure Windows console streams for international Unicode paths
if sys.platform == "win32":
    for _stream_name in ("stdout", "stderr", "stdin"):
        _stream = getattr(sys, _stream_name, None)
        if _stream and hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser for worker launcher."""
    parser = argparse.ArgumentParser(
        description="AutoMetashape Distributed Cluster Worker Launcher",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", type=str, default="", help="Cluster Server IP or hostname (e.g. 192.168.1.100 or server-host)")
    parser.add_argument("--port", type=str, default=None, help="Cluster Server control port (default: 5840)")
    parser.add_argument("--root", type=str, default="", help="Shared Network Storage UNC path (e.g. \\\\SERVER\\SharedMetashape)")
    parser.add_argument("--cpu-enable", "--cpu", dest="cpu_enable", type=int, default=None, choices=[0, 1], help="Enable CPU compute cores (1=yes, 0=no)")
    parser.add_argument(
        "--gpu-mask",
        "--gpu",
        dest="gpu_mask",
        type=str,
        default=None,
        help="Bitmask of GPU devices to engage (default: auto-detected by Metashape)",
    )
    parser.add_argument("--config", type=str, default="", help="Path to custom JSON worker configuration file")
    parser.add_argument("--interactive", action="store_true", help="Force interactive configuration prompt")
    parser.add_argument("--no-reconnect", action="store_true", help="Do not automatically reconnect on disconnection")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def run_interactive_wizard(
    default_host: str = "",
    default_port: str = "5840",
    default_root: str = "",
    default_cpu: int = 1,
    default_gpu: str | None = None,
) -> tuple[str, str, str, int, str, bool]:
    """Run interactive setup wizard with dynamic hardware discovery."""
    print("=" * 72)
    print("  AutoMetashape Distributed Cluster - Worker Setup Wizard")
    print("=" * 72)
    print("No complete configuration found. Please configure this worker node.\n")

    # 1. Hardware Discovery
    detected_mask, detected_gpu_names = cu.calculate_optimal_gpu_mask()
    cpu_cores = os.cpu_count() or 4
    if detected_gpu_names:
        print(f"[Hardware] Detected {len(detected_gpu_names)} GPU(s): {', '.join(detected_gpu_names)}")
        print(f"           Optimal GPU Mask: {detected_mask} (Enables all {len(detected_gpu_names)} GPU(s))")
    else:
        print("[Hardware] No discrete GPUs detected (running in CPU-only compute mode).")
    print(f"[Hardware] Detected {cpu_cores} CPU threads.\n")

    # 2. Host IP
    detected_ip = cu.get_primary_lan_ip()
    suggested_host = default_host or (detected_ip if detected_ip != "127.0.0.1" else "")
    prompt_host = f"[{suggested_host}]" if suggested_host else ""

    host_input = input(f"[?] Enter Cluster Server IP / Hostname {prompt_host}: ").strip()
    target_host = host_input or suggested_host
    if not target_host:
        target_host = "127.0.0.1"

    # 3. Port
    prompt_port = f"[{default_port}]" if default_port else "[5840]"
    port_input = input(f"[?] Enter Cluster Server Port {prompt_port}: ").strip()
    target_port = port_input or default_port or "5840"

    # 4. Storage Root
    target_root = ""
    while not target_root:
        prompt_root = f"[{default_root}]" if default_root else ""
        root_input = input(f"[?] Enter Shared Storage UNC Path (e.g. \\\\HOST_PC\\SharedMetashape) {prompt_root}: ").strip()
        target_root = root_input or default_root
        if not target_root:
            print("[!] Shared storage UNC path is required for cluster network processing. Please provide a valid path.")

    # 5. GPU Mask
    suggested_gpu = default_gpu if default_gpu is not None else (str(detected_mask) if detected_mask > 0 else "0")
    gpu_input = input(f"[?] Enter GPU Device Bitmask (e.g. 1=GPU0, 3=GPU0+1, 0=CPU-only) [{suggested_gpu}]: ").strip()
    target_gpu_mask = gpu_input or suggested_gpu

    # 6. CPU Enable
    suggested_cpu = str(default_cpu)
    cpu_input = input(f"[?] Enable CPU compute cores? (1=yes, 0=no) [{suggested_cpu}]: ").strip()
    try:
        target_cpu_enable = int(cpu_input) if cpu_input in ("0", "1") else default_cpu
    except Exception:
        target_cpu_enable = default_cpu

    # 7. Save Option
    save_input = input("\n[?] Save settings to worker_config.json for 1-click launch? [Y/n]: ").strip().lower()
    save_config = save_input in ("", "y", "yes")

    print("=" * 72)
    return target_host, target_port, target_root, target_cpu_enable, target_gpu_mask, save_config


def main() -> int:
    """Main worker launcher entrypoint with pre-flight diagnostics and reconnect loop."""
    parser = build_arg_parser()
    args, unknown = parser.parse_known_args()
    if unknown:
        cleaned = [u for u in unknown if u != "--worker"]
        if cleaned:
            print(f"[NOTICE] Ignoring unrecognized worker arguments: {' '.join(cleaned)}")

    # 1. Resolve configuration (CLI args -> Config File -> Dynamic Discovery -> Interactive Wizard)
    config_path = args.config or cu.get_default_config_path(cu.DEFAULT_WORKER_CONFIG_FILENAME)
    saved_config = cu.load_cluster_config(config_path)

    detected_mask, detected_gpu_names = cu.calculate_optimal_gpu_mask()
    default_gpu_mask = str(detected_mask) if detected_mask > 0 else "0"

    host = args.host or saved_config.get("host", "")
    port = args.port or saved_config.get("port", "5840")
    root_path = args.root or saved_config.get("root", "")
    # CPU Enable Resolution
    if args.cpu_enable is not None:
        cpu_enable = args.cpu_enable
    elif "cpu_enable" in saved_config:
        cpu_enable = int(saved_config["cpu_enable"])
    else:
        cpu_enable = 1

    # GPU Mask Resolution: CLI arg -> saved config -> dynamically detected optimal mask
    raw_gpu_mask = args.gpu_mask if args.gpu_mask is not None else saved_config.get("gpu_mask", None)
    if raw_gpu_mask is not None:
        gpu_str = str(raw_gpu_mask).strip()
        if gpu_str.lower() in ("auto", "none", "", "default"):
            gpu_mask = default_gpu_mask
        else:
            gpu_mask = gpu_str
    else:
        gpu_mask = default_gpu_mask

    if args.interactive or not host or not root_path:
        host, port, root_path, cpu_enable, gpu_mask, should_save = run_interactive_wizard(
            default_host=host,
            default_port=port,
            default_root=root_path,
            default_cpu=cpu_enable,
            default_gpu=gpu_mask,
        )
        if should_save:
            conf_data = {
                "host": host,
                "port": port,
                "root": root_path,
                "cpu_enable": cpu_enable,
            }
            if gpu_mask is not None:
                conf_data["gpu_mask"] = gpu_mask
            saved_file = cu.save_cluster_config(conf_data, config_path)
            print(f"[OK] Configuration saved to: {saved_file}\n")

    # 2. Pre-Flight Diagnostic 1: Metashape Pro Binary Locator
    ms_exe = cu.find_metashape_path()
    if not ms_exe or not os.path.isfile(ms_exe):
        print(f"[FATAL ERROR] Agisoft Metashape Pro executable was not found on this system.", file=sys.stderr)
        print("Please verify Metashape Professional is installed and accessible.", file=sys.stderr)
        return 1

    print("=" * 72)
    print("  AutoMetashape Distributed Cluster - Worker Node Launcher")
    print(f"  Cluster Server  : {host}:{port}")
    print(f"  Shared Storage  : {root_path}")
    print(f"  Metashape Binary: {ms_exe}")
    if gpu_mask and str(gpu_mask).strip() not in ("0", ""):
        gpu_count_label = f" ({len(detected_gpu_names)} GPU(s) active)" if detected_gpu_names else ""
        print(f"  GPU Device Mask : {gpu_mask}{gpu_count_label}")
    else:
        print("  Compute Mode    : CPU Only (All CPU threads)")
    print(f"  CPU Cores Active: {'Yes' if cpu_enable else 'No'}")
    print("=" * 72)

    # 3. Pre-Flight Diagnostic 2: TCP Socket Reachability Test
    try:
        port_num = int(port)
    except ValueError:
        port_num = 5840

    print(f"\n[PRE-FLIGHT] Testing network connection to {host}:{port_num}...")
    if cu.test_tcp_connection(host, port_num, timeout=3.0):
        print(f"  [OK] Successfully connected to Cluster Server at {host}:{port_num}!")
    else:
        print(f"  [WARNING] Could not reach server at {host}:{port_num}.", file=sys.stderr)
        print("  Check that metashape-server is running and TCP port 5840 is open in firewall.", file=sys.stderr)

    # 4. Pre-Flight Diagnostic 3: Shared Storage UNC Access Test
    print(f"[PRE-FLIGHT] Validating shared network storage access: {root_path}...")
    valid_unc, unc_err = cu.test_unc_path(root_path)
    if valid_unc:
        print(f"  [OK] Shared storage root is accessible and verified.")
    else:
        print(f"  [WARNING] Storage verification issue: {unc_err}", file=sys.stderr)
        print("  Ensure network credentials and file sharing permissions are granted.", file=sys.stderr)

    # 5. Build Worker Command
    worker_cmd = [
        ms_exe,
        "--worker",
        "--host",
        f"{host}:{port}",
        "--root",
        root_path,
        "--cpu-enable",
        str(cpu_enable),
    ]
    if gpu_mask and str(gpu_mask).strip() not in ("0", ""):
        worker_cmd.extend(["--gpu-mask", str(gpu_mask).strip()])

    print("\n[READY] Launching Metashape Worker daemon...")
    print(f"Command: {' '.join(worker_cmd)}")
    print("Press Ctrl+C in this terminal at any time to disconnect this worker node.\n")

    # 6. Resilient Execution & Auto-Reconnect Loop
    reconnect_delay = 10.0
    while True:
        try:
            start_t = time.time()
            proc = subprocess.Popen(worker_cmd)
            proc.wait()
            ret_code = proc.returncode

            elapsed = time.time() - start_t
            print(f"\n[DISCONNECTED] Worker process exited with code {ret_code} (Active: {elapsed:.1f}s).")

            if args.no_reconnect:
                return ret_code

            print(f"[AUTO-RECONNECT] Attempting reconnect in {int(reconnect_delay)} seconds (Ctrl+C to abort)...")
            time.sleep(reconnect_delay)

        except KeyboardInterrupt:
            print("\n[SHUTDOWN] Worker node stopped by user (Ctrl+C). Goodbye!")
            return 0
        except Exception as e:
            print(f"\n[ERROR] Worker execution error: {e}", file=sys.stderr)
            if args.no_reconnect:
                return 1
            time.sleep(reconnect_delay)


if __name__ == "__main__":
    sys.exit(main())
