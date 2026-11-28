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
import tempfile
import time

# Ensure scripts directory is importable
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_SCRIPTS_DIR, ".."))
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

PID_FILE_PATH = os.path.join(tempfile.gettempdir(), "autometashape_worker.pid")
_OWN_PID_FILES: list[str] = []


def record_worker_pid() -> None:
    """Record current process ID safely without clobbering another live co-located worker."""
    my_pid = os.getpid()
    per_pid_path = os.path.join(tempfile.gettempdir(), f"autometashape_worker_{my_pid}.pid")
    write_primary = True
    try:
        if os.path.isfile(PID_FILE_PATH):
            with open(PID_FILE_PATH, "r", encoding="utf-8") as rf:
                existing = rf.read().strip()
            if existing.isdigit() and int(existing) != my_pid:
                try:
                    from autometashape.cluster.abort import _is_pid_alive

                    if _is_pid_alive(int(existing)):
                        write_primary = False
                except Exception:
                    pass
    except Exception:
        pass
    targets = [per_pid_path]
    if write_primary:
        targets.insert(0, PID_FILE_PATH)
    for dest in targets:
        try:
            with open(dest, "w", encoding="utf-8") as f:
                f.write(str(my_pid))
            if dest not in _OWN_PID_FILES:
                _OWN_PID_FILES.append(dest)
        except Exception:
            pass


def clear_worker_pid() -> None:
    """Remove worker PID tracking files on clean exit if they belong to this process."""
    for cand in list(dict.fromkeys([PID_FILE_PATH] + _OWN_PID_FILES)):
        try:
            if os.path.isfile(cand):
                with open(cand, "r", encoding="utf-8") as f:
                    recorded = f.read().strip()
                if recorded and recorded != str(os.getpid()):
                    continue
                os.remove(cand)
        except Exception:
            pass
    _OWN_PID_FILES.clear()


cleanup_stale_worker_processes = cu.cleanup_stale_worker_processes
check_update_pending = cu.check_update_pending


def reexec_supervisor(target_dir: str) -> None:
    """Re-execute the supervisor process image with updated Python code from disk."""
    launcher_py = os.path.join(target_dir, "scripts", "launch_worker.py")
    if not os.path.isfile(launcher_py):
        launcher_py = os.path.abspath(__file__)

    # STRICT INVARIANT: Always prioritize Metashape Pro bundled Python
    py_exe = cu.find_metashape_python_path() or sys.executable
    cmd = [py_exe, launcher_py] + sys.argv[1:]
    print(f"[SUPERVISOR] Re-executing fresh process image: {cmd[0]} {cmd[1]}")
    sys.stdout.flush()
    sys.stderr.flush()
    clear_worker_pid()
    if sys.platform == "win32":
        # Avoid Windows _wexecv (which detaches from parent .bat/Task Scheduler and mangles trailing backslashes)
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
    """Build CLI argument parser for worker launcher."""
    parser = argparse.ArgumentParser(
        description="AutoMetashape Distributed Cluster Worker Launcher",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host", type=str, default="", help="Cluster Server IP or hostname (e.g. 192.168.1.100 or server-host)"
    )
    parser.add_argument("--port", type=str, default=None, help="Cluster Server control port (default: 5840)")
    parser.add_argument(
        "--root",
        "--result-root",
        "--result-share",
        dest="root",
        type=str,
        default="",
        help="Shared Network Storage UNC path for Metashape --root / Result SSD (e.g. \\\\SERVER\\ResultShare)",
    )
    parser.add_argument(
        "--source-root",
        "--source-share",
        dest="source_root",
        type=str,
        default="",
        help="Optional Data Source Share UNC path (SSD #1, read-only raw photos) validated on worker startup",
    )
    parser.add_argument(
        "--cpu-enable",
        "--cpu",
        dest="cpu_enable",
        type=int,
        default=None,
        choices=[0, 1],
        help="Enable CPU compute cores (1=yes, 0=no)",
    )
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
    parser.add_argument(
        "--non-interactive",
        "--headless",
        dest="non_interactive",
        action="store_true",
        help="Run without interactive prompts (headless/automated execution)",
    )
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
    gpu_devices = cu.detect_gpu_devices()
    detected_gpu_names = []
    for d in gpu_devices:
        mem_str = f" ({d['mem_mb'] / 1024:.1f} GB)" if d.get("mem_mb") else ""
        detected_gpu_names.append(f"{d['name']}{mem_str}")
    cpu_cores = os.cpu_count() or 4
    if detected_gpu_names:
        print(f"[Hardware] Detected {len(detected_gpu_names)} discrete GPU(s): {', '.join(detected_gpu_names)}")
        print("           Hardware Strategy: Metashape Pro native engine benchmarks and engages all discrete GPUs.")
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
        root_input = input(
            f"[?] Enter Shared Storage UNC Path (e.g. \\\\HOST_PC\\SharedMetashape) {prompt_root}: "
        ).strip()
        target_root = root_input or default_root
        if not target_root:
            print(
                "[!] Shared storage UNC path is required for cluster network processing. Please provide a valid path."
            )

    # 5. GPU Mask
    suggested_gpu = default_gpu or "Auto"
    gpu_input = input(
        f"[?] Enter GPU Device Bitmask (e.g. 1=GPU0, 3=GPU0+1, 0=CPU-only, Auto=Native) [{suggested_gpu}]: "
    ).strip()
    target_gpu_mask = gpu_input or suggested_gpu
    if target_gpu_mask.lower() in ("auto", "default", "none", ""):
        target_gpu_mask = ""

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
    return _run_worker_lifecycle(manage_pid=True)


def _run_worker_lifecycle(manage_pid: bool = False) -> int:
    """Execute worker configuration resolution, pre-flight diagnostics, and resilient execution loop."""
    parser = build_arg_parser()
    args, unknown = parser.parse_known_args()
    if unknown:
        cleaned = [u for u in unknown if u != "--worker"]
        if cleaned:
            print(f"[NOTICE] Ignoring unrecognized worker arguments: {' '.join(cleaned)}")

    if manage_pid:
        record_worker_pid()
    try:
        return _execute_worker_lifecycle(args)
    finally:
        if manage_pid:
            clear_worker_pid()


def _execute_worker_lifecycle(args: argparse.Namespace) -> int:
    """Run configuration resolution, GPU bitmask probing, pre-flight checks, and supervisor loop."""
    # 1. Resolve headless / non-interactive execution mode
    is_tty = sys.stdin.isatty() if hasattr(sys.stdin, "isatty") else False
    is_headless = args.non_interactive or not is_tty

    # 2. Resolve configuration (CLI args -> Environment -> Config File -> Dynamic Discovery)
    read_config_path = args.config or cu.get_default_config_path(cu.DEFAULT_WORKER_CONFIG_FILENAME, for_write=False)
    saved_config = cu.load_cluster_config(read_config_path)

    gpu_devices = cu.detect_gpu_devices()
    detected_gpu_names = []
    for d in gpu_devices:
        mem_str = f" ({d['mem_mb'] / 1024:.1f} GB)" if d.get("mem_mb") else ""
        detected_gpu_names.append(f"{d['name']}{mem_str}")

    raw_host = args.host or os.environ.get("AUTOMETASHAPE_CLUSTER_HOST", "").strip() or saved_config.get("host", "")
    raw_port = args.port or os.environ.get("AUTOMETASHAPE_CLUSTER_PORT", "").strip() or saved_config.get("port", "5840")
    try:
        fallback_port = int(raw_port)
    except (ValueError, TypeError):
        fallback_port = 5840
    if str(raw_host or "").strip():
        host, parsed_port = cu.split_host_port(str(raw_host).strip(), default_port=fallback_port)
        port = args.port if args.port is not None else str(parsed_port)
    else:
        host = ""
        port = str(raw_port or "5840")
    root_path = args.root or os.environ.get("AUTOMETASHAPE_CLUSTER_ROOT", "").strip() or saved_config.get("root", "")
    source_root = (
        args.source_root
        or os.environ.get("AUTOMETASHAPE_SOURCE_ROOT", "").strip()
        or saved_config.get("source_root", "")
    )

    # CPU Enable Resolution
    if args.cpu_enable is not None:
        cpu_enable = args.cpu_enable
    elif "cpu_enable" in saved_config:
        try:
            cpu_enable = 1 if int(saved_config["cpu_enable"]) else 0
        except (ValueError, TypeError):
            cpu_enable = 1
    else:
        cpu_enable = 1

    # Dynamic Hardware Discovery & GPU Mask Resolution:
    # 1. CLI Override: --gpu-mask <val> takes top priority.
    #    - If val is "auto", "default", "none", "": dynamic discrete bitmask computed via Metashape probe.
    #    - Otherwise (e.g. "0", "1", "2", "3"): explicit manual override passed to Metashape.
    # 2. Saved Config: worker_config.json
    #    - If val is "auto", "none", "default", "": purge non-override sentinel, use dynamic probe.
    #    - If val is explicit numeric override (e.g. "0", "1", "2", "3"): respect as saved override.
    # 3. Default: Probe Metashape's native enumGPUDevices() to compute the exact discrete GPU bitmask
    #    (skipping Intel & AMD iGPUs and immune to poisoned HKCU Registry gpu_mask=0).
    explicit_gpu_mask = None
    is_manual_override = False
    if args.gpu_mask is not None:
        raw = str(args.gpu_mask).strip()
        if raw and raw.lower() not in ("auto", "none", "default"):
            explicit_gpu_mask = raw
            is_manual_override = True
    elif "gpu_mask" in saved_config:
        raw_cfg = str(saved_config["gpu_mask"]).strip()
        is_legacy_auto_one = raw_cfg == "1" and not bool(saved_config.get("gpu_mask_explicit"))
        if raw_cfg.lower() in ("auto", "none", "default", "") or is_legacy_auto_one:
            saved_config.pop("gpu_mask", None)
            saved_config.pop("gpu_mask_explicit", None)
        else:
            explicit_gpu_mask = raw_cfg
            is_manual_override = True

    # 3. Interactive Wizard or Headless Error Handling
    should_auto_persist = True
    if not host or not root_path or args.interactive:
        if is_headless:
            print(
                f"[FATAL ERROR] Missing required cluster parameters in non-interactive/headless mode (host='{host}', root='{root_path}').",
                file=sys.stderr,
            )
            print(
                "Please provide --host and --root via CLI arguments, environment variables (AUTOMETASHAPE_CLUSTER_HOST, AUTOMETASHAPE_CLUSTER_ROOT), or worker_config.json.",
                file=sys.stderr,
            )
            return 1

        suggested_gpu = explicit_gpu_mask if explicit_gpu_mask is not None else "Auto"
        host, port, root_path, cpu_enable, target_gpu_mask, should_save = run_interactive_wizard(
            default_host=host,
            default_port=port,
            default_root=root_path,
            default_cpu=cpu_enable,
            default_gpu=suggested_gpu,
        )
        should_auto_persist = should_save
        if target_gpu_mask and target_gpu_mask.lower() not in ("auto", "default", "none", ""):
            explicit_gpu_mask = target_gpu_mask
            is_manual_override = True
        else:
            explicit_gpu_mask = None
            is_manual_override = False

        if should_save:
            conf_data = {
                "host": host,
                "port": port,
                "root": root_path,
                "cpu_enable": cpu_enable,
            }
            if source_root:
                conf_data["source_root"] = source_root
            if explicit_gpu_mask is not None:
                conf_data["gpu_mask"] = explicit_gpu_mask
                conf_data["gpu_mask_explicit"] = True
            saved_file = cu.save_cluster_config(
                conf_data, args.config or None, filename=cu.DEFAULT_WORKER_CONFIG_FILENAME
            )
            print(f"[OK] Configuration saved to: {saved_file}\n")

    # 4. Auto-persist valid configuration to target config_path if missing/incomplete (respecting wizard decline)
    if host and root_path and should_auto_persist:
        write_cfg_path = args.config or cu.get_default_config_path(cu.DEFAULT_WORKER_CONFIG_FILENAME, for_write=True)
        existing_cfg = cu.load_cluster_config(write_cfg_path) if os.path.isfile(write_cfg_path) else dict(saved_config)
        # Purge legacy auto-written "gpu_mask": "1" (without gpu_mask_explicit), sentinel values,
        # or an existing saved override when the operator explicitly passes --gpu-mask auto.
        cli_requested_auto = args.gpu_mask is not None and str(args.gpu_mask).strip().lower() in (
            "auto",
            "none",
            "default",
            "",
        )
        if "gpu_mask" in existing_cfg and (args.gpu_mask is None or cli_requested_auto):
            val = str(existing_cfg.get("gpu_mask", "")).strip().lower()
            is_legacy_one = val == "1" and not bool(existing_cfg.get("gpu_mask_explicit"))
            if cli_requested_auto or val in ("auto", "none", "default", "") or is_legacy_one:
                existing_cfg.pop("gpu_mask", None)
                existing_cfg.pop("gpu_mask_explicit", None)
                try:
                    cu.save_cluster_config(
                        existing_cfg, args.config or None, filename=cu.DEFAULT_WORKER_CONFIG_FILENAME
                    )
                except Exception:
                    pass

        is_missing = not os.path.isfile(read_config_path) and not os.path.isfile(write_cfg_path)
        is_incomplete = not existing_cfg.get("host") or not existing_cfg.get("root")

        if is_missing or is_incomplete:
            persist_data = {
                "host": host,
                "port": str(port),
                "root": root_path,
                "cpu_enable": int(cpu_enable),
            }
            if source_root:
                persist_data["source_root"] = source_root
            # STRICT INVARIANT: Only persist gpu_mask if explicitly configured with a non-auto override
            if explicit_gpu_mask is not None:
                persist_data["gpu_mask"] = explicit_gpu_mask
                persist_data["gpu_mask_explicit"] = True
            try:
                saved_p = cu.save_cluster_config(
                    persist_data, args.config or None, filename=cu.DEFAULT_WORKER_CONFIG_FILENAME
                )
                print(f"[OK] Auto-persisted worker configuration to: {saved_p}")
            except Exception:
                pass

    # 5. Pre-Flight Diagnostic 1: Metashape Pro Binary Locator
    ms_exe = cu.find_metashape_path()
    if not ms_exe or not os.path.isfile(ms_exe):
        print("[FATAL ERROR] Agisoft Metashape Pro executable was not found on this system.", file=sys.stderr)
        print("Please verify Metashape Professional is installed and accessible.", file=sys.stderr)
        return 1

    # Resolve active GPU mask via Metashape's native enumGPUDevices() (immune to poisoned HKCU Registry & iGPUs)
    if is_manual_override and explicit_gpu_mask is not None:
        active_gpu_mask = explicit_gpu_mask
        override_src = "via CLI" if args.gpu_mask is not None else "via Config"
        gpu_display_label = f"{explicit_gpu_mask} (Manual Override {override_src})"
    else:
        probed_mask, probed_devs = cu.probe_metashape_gpu_mask(ms_exe)
        if probed_mask is not None:
            active_gpu_mask = str(probed_mask)
            if probed_devs:
                dev_labels = [
                    f"{d['name']} ({d['mem_mb'] / 1024:.1f} GB)" if d.get("mem_mb") else d["name"] for d in probed_devs
                ]
                gpu_display_label = f"{active_gpu_mask} (Dynamic Discrete GPU: {', '.join(dev_labels)})"
            else:
                gpu_display_label = "0 (Dynamic Probe: No discrete GPU detected — CPU mode)"
        else:
            active_gpu_mask = None
            detected_label = f" ({len(detected_gpu_names)} discrete GPU(s) detected)" if detected_gpu_names else ""
            gpu_display_label = f"Auto (Metashape probe fallback){detected_label}"

    # Prevent 0-compute zombie workers (gpu_mask == 0 AND cpu_enable == 0)
    if active_gpu_mask == "0" and cpu_enable == 0:
        print(
            "[WARNING] GPU mask resolved to 0 (no discrete GPU) with --cpu-enable 0; forcing --cpu-enable 1 "
            "so worker retains compute capability.",
            file=sys.stderr,
        )
        cpu_enable = 1

    try:
        port_num = int(port)
        if not (1 <= port_num <= 65535):
            raise ValueError(f"Port {port_num} out of valid TCP range 1..65535")
    except (ValueError, TypeError):
        print(f"  [WARNING] Invalid port '{port}'; falling back to 5840.", file=sys.stderr)
        port_num = 5840
        port = "5840"

    formatted_host_port = cu.format_host_port(host, port_num)

    print("=" * 72)
    print("  AutoMetashape Distributed Cluster - Worker Node Launcher")
    print(f"  Cluster Server  : {formatted_host_port}")
    print(f"  Result Storage  : {root_path}")
    if source_root:
        print(f"  Data Source SSD : {source_root}")
    print(f"  Metashape Binary: {ms_exe}")
    print(f"  GPU Device Mask : {gpu_display_label}")
    print(f"  CPU Cores Active: {'Yes' if cpu_enable else 'No'}")
    print("=" * 72)

    # 6. Pre-Flight Diagnostic 2: TCP Socket Reachability Test
    print(f"\n[PRE-FLIGHT] Testing network connection to {formatted_host_port}...")
    if cu.test_tcp_connection(host, port_num, timeout=3.0):
        print(f"  [OK] Successfully connected to Cluster Server at {formatted_host_port}!")
    else:
        print(f"  [WARNING] Could not reach server at {formatted_host_port}.", file=sys.stderr)
        print("  Check that metashape-server is running and TCP port 5840 is open in firewall.", file=sys.stderr)

    # 7. Pre-Flight Diagnostic 3: Shared Storage UNC Access Test
    print(f"[PRE-FLIGHT] Validating shared Result storage access: {root_path}...")
    valid_unc, unc_err = cu.test_unc_path(root_path)
    if valid_unc:
        print("  [OK] Shared Result storage root is accessible and verified.")
    else:
        print(f"  [WARNING] Result storage verification issue: {unc_err}", file=sys.stderr)
        print("  Ensure network credentials and file sharing permissions are granted.", file=sys.stderr)

    if source_root:
        print(f"[PRE-FLIGHT] Validating Data Source storage access (read-only): {source_root}...")
        valid_src, src_err = cu.test_unc_path(source_root)
        if valid_src:
            print("  [OK] Data Source share is accessible and verified.")
        else:
            print(f"  [WARNING] Data Source share verification issue: {src_err}", file=sys.stderr)
            print("  Ensure worker node has read access to the Data Source SMB share.", file=sys.stderr)

    # 8. Build Worker Command
    worker_cmd = [ms_exe]
    worker_env = os.environ.copy()
    if sys.platform.startswith("linux") and not worker_env.get("DISPLAY"):
        worker_cmd.extend(["-platform", "offscreen"])
        worker_env.setdefault("QT_QPA_PLATFORM", "offscreen")
    worker_cmd.extend(
        [
            "--worker",
            "--host",
            formatted_host_port,
            "--root",
            root_path,
            "--cpu-enable",
            str(cpu_enable),
        ]
    )
    # Pass explicit discrete GPU bitmask (computed dynamically via Metashape enumGPUDevices or manual override)
    if active_gpu_mask is not None:
        worker_cmd.extend(["--gpu-mask", active_gpu_mask])

    print("\n[READY] Launching Metashape Worker daemon...")
    print(f"Command: {' '.join(worker_cmd)}")
    print("Press Ctrl+C in this terminal at any time to disconnect this worker node.\n")

    # 9. Resilient Execution & Auto-Reconnect Loop
    reconnect_delay = 10.0
    target_dir = os.environ.get("AUTOMETASHAPE_DIR", "").strip()
    if not target_dir or not os.path.isdir(target_dir):
        if os.path.isdir(_REPO_ROOT):
            target_dir = _REPO_ROOT
        else:
            user_profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
            target_dir = os.path.join(user_profile, "Documents", "AutoMetashape")

    worker_env["AUTOMETASHAPE_DIR"] = target_dir
    if is_manual_override and explicit_gpu_mask is not None:
        worker_env["AUTOMETASHAPE_EXPLICIT_GPU_MASK"] = explicit_gpu_mask
    else:
        worker_env.pop("AUTOMETASHAPE_EXPLICIT_GPU_MASK", None)
    while True:
        proc = None
        try:
            # If boot-time GPU probe transiently failed in Auto mode, retry before spawning worker
            if not is_manual_override and active_gpu_mask is None:
                retry_mask, _ = cu.probe_metashape_gpu_mask(ms_exe)
                if retry_mask is not None:
                    active_gpu_mask = str(retry_mask)
                    if "--gpu-mask" not in worker_cmd:
                        worker_cmd.extend(["--gpu-mask", active_gpu_mask])
                    if active_gpu_mask == "0" and cpu_enable == 0:
                        cpu_enable = 1
                        if "--cpu-enable" in worker_cmd:
                            ce_idx = worker_cmd.index("--cpu-enable")
                            if ce_idx + 1 < len(worker_cmd):
                                worker_cmd[ce_idx + 1] = "1"
                    print(f"[Hardware] Dynamic GPU probe recovered on reconnect: --gpu-mask {active_gpu_mask}")
            start_t = time.time()
            popen_kwargs: dict[str, object] = {"env": worker_env, "cwd": target_dir}
            if sys.platform != "win32":
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(worker_cmd, **popen_kwargs)

            # Watchdog polling loop: check for abort signals while proc is running
            aborted_by_supervisor = False
            while proc.poll() is None:
                abort_sig = cu.check_abort_signal(target_dir=target_dir, root_path=root_path)
                if abort_sig is not None:
                    batch_id = abort_sig.get("batch_id", "unknown")
                    sig_node = abort_sig.get("node_name") or cu.get_local_hostname()
                    print(
                        f"\n[SUPERVISOR] Abort signal detected for worker '{sig_node}' (Target Batch: {batch_id}). "
                        "Forcibly terminating stuck child process...",
                        flush=True,
                    )
                    proc_pid = getattr(proc, "pid", None)
                    killed_pids: set[int] = {int(proc_pid)} if proc_pid else set()
                    try:
                        from autometashape.cluster.abort import _enumerate_windows_descendant_pids

                        if proc_pid:
                            killed_pids.update(_enumerate_windows_descendant_pids(int(proc_pid)))
                    except Exception:
                        pass
                    # 1. Kill child metashape.exe process tree immediately and wait for handles to release
                    cu.kill_worker_process(proc)
                    try:
                        proc.wait(timeout=2.0)
                    except Exception:
                        pass
                    aborted_by_supervisor = True

                    # 2. Clean up scratch locks: remove any stale lock files in target_dir or scratch dirs
                    try:
                        cu.cleanup_scratch_locks(
                            target_dir=target_dir,
                            root_path=root_path,
                            worker_pid=proc_pid,
                            killed_pids=killed_pids,
                        )
                    except TypeError:
                        try:
                            cu.cleanup_scratch_locks(
                                target_dir=target_dir, root_path=root_path, worker_pid=proc_pid
                            )
                        except TypeError:
                            cu.cleanup_scratch_locks(target_dir=target_dir, root_path=root_path)

                    # 3. Acknowledge and clear signal file
                    cu.acknowledge_abort_signal(abort_sig)
                    break
                time.sleep(1.0)

            ret_code = proc.poll() if proc.poll() is not None else proc.returncode
            elapsed = time.time() - start_t
            if aborted_by_supervisor:
                print(
                    f"[SUPERVISOR] Child process terminated by watchdog after {elapsed:.1f}s. Preparing clean restart."
                )
            else:
                print(f"\n[DISCONNECTED] Worker process exited with code {ret_code} (Active: {elapsed:.1f}s).")

            # Check if an autonomous update was requested via .update_pending
            signal_data = check_update_pending(target_dir)
            if signal_data is not None:
                print("\n" + "=" * 72)
                print("  [SUPERVISOR] Autonomous Update Signal (.update_pending) Detected!")
                print("  Applying Git-parity codebase update with file pruning...")
                print("=" * 72)
                update_ok = cu.perform_worker_codebase_update(signal_data, target_dir)
                if update_ok:
                    print("[SUPERVISOR] Update applied successfully! Re-executing fresh supervisor image...")
                    reexec_supervisor(target_dir)
                    return 0
                else:
                    print("[SUPERVISOR WARNING] Update failed. Resuming worker execution...", file=sys.stderr)

            if args.no_reconnect:
                return ret_code if ret_code is not None else 0

            delay = 1.0 if aborted_by_supervisor else reconnect_delay
            print(f"[AUTO-RECONNECT] Attempting reconnect in {int(delay)} seconds (Ctrl+C to abort)...")
            time.sleep(delay)

        except KeyboardInterrupt:
            print("\n[SHUTDOWN] Worker node stopped by user (Ctrl+C). Goodbye!")
            return 0
        except Exception as e:
            print(f"\n[ERROR] Worker execution error: {e}", file=sys.stderr)
            if args.no_reconnect:
                return 1
            time.sleep(reconnect_delay)
        finally:
            if proc and proc.poll() is None:
                cu.kill_worker_process(proc)


if __name__ == "__main__":
    sys.exit(main())
