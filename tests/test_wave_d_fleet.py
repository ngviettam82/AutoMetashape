# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Hermetic adversarial regression tests for Wave D verification, fleet updater, staging, and supervisor fixes."""

from __future__ import annotations

import io
import json
import os
import socket
import struct
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, ".."))
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
_SOURCE_DIR = os.path.join(_REPO_ROOT, "source")
for _p in (_SCRIPTS_DIR, _SOURCE_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from autometashape.cluster import hardware as hw_mod
from autometashape.cluster import staging as staging_mod
from autometashape.cluster import supervisor as sup_mod
from autometashape.cluster import updater as updater_mod
from autometashape.core import verification as ver_mod

from scripts import cluster_monitor as monitor_mod
from scripts import inspect_checkpoint as inspect_mod
from scripts import submit_cluster_batch as submit_mod
from scripts import update_fleet as fleet_mod


class TestWaveDFleetHardening(unittest.TestCase):
    """Mutation-resistant tests defending Wave D verification, monitoring, staging, and fleet fixes."""

    def test_verification_psx_only_xyz_validation_glb_zero_and_cross_project_isolation(self) -> None:
        """PSX archives, XYZ ASCII clouds, GLB zero/chunk checks, and project prefix isolation work cleanly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            psx = os.path.join(tmpdir, "SitePSX.psx")
            files_dir = os.path.join(tmpdir, "SitePSX.files")
            os.makedirs(files_dir, exist_ok=True)
            with open(psx, "w", encoding="utf-8") as fh:
                fh.write('<?xml version="1.0"?><document version="1.2.0" path="SitePSX.files/project.zip"/>\n')
            pz = os.path.join(files_dir, "project.zip")
            with zipfile.ZipFile(pz, "w") as zf:
                zf.writestr("doc.xml", '<document version="1.2.0"><chunks next_id="1"/></document>')

            # Valid PSX-only folder passes both explicit expect=['psx'] and default auto-detection
            res_exp = ver_mod.verify_project_deliverables(tmpdir, project_name="SitePSX", expected_assets=["psx"])
            self.assertEqual(res_exp["status"], "PASS")
            res_auto = ver_mod.verify_project_deliverables(tmpdir, project_name="SitePSX")
            self.assertEqual(res_auto["status"], "PASS")
            self.assertIn("psx", res_auto["assets_checked"])

            # Missing doc.xml inside project.zip fails verification
            with zipfile.ZipFile(pz, "w") as zf:
                zf.writestr("other.txt", "no doc xml")
            ok_psx, errs_psx = ver_mod.verify_psx_project(psx)
            self.assertFalse(ok_psx)
            self.assertTrue(any("doc.xml" in e for e in errs_psx))

            # GLB declared_len == 0 fails even with min_size_bytes=0; bad chunk-0 type fails
            zero_glb = os.path.join(tmpdir, "zero.glb")
            with open(zero_glb, "wb") as fh:
                fh.write(struct.pack("<4sII", b"glTF", 2, 0) + b"\x00" * 16)
            self.assertFalse(ver_mod.verify_glb_model(zero_glb, min_size_bytes=0)[0])

            bad_chunk_glb = os.path.join(tmpdir, "bad_chunk.glb")
            body = b'{"asset":{"version":"2.0"}}'
            with open(bad_chunk_glb, "wb") as fh:
                fh.write(struct.pack("<4sII", b"glTF", 2, 20 + len(body)))
                fh.write(struct.pack("<I4s", len(body), b"BAD!") + body)
            self.assertFalse(ver_mod.verify_glb_model(bad_chunk_glb, min_size_bytes=10)[0])

            good_chunk_glb = os.path.join(tmpdir, "good_chunk.glb")
            with open(good_chunk_glb, "wb") as fh:
                fh.write(struct.pack("<4sII", b"glTF", 2, 20 + len(body)))
                fh.write(struct.pack("<I4s", len(body), b"JSON") + body)
            self.assertTrue(ver_mod.verify_glb_model(good_chunk_glb, min_size_bytes=10)[0])

            # XYZ ASCII coordinate validation rejects non-numeric text and accepts valid XYZ lines
            good_xyz = os.path.join(tmpdir, "SitePSX_cloud.xyz")
            bad_xyz = os.path.join(tmpdir, "bad_cloud.xyz")
            with open(good_xyz, "w", encoding="utf-8") as fh:
                fh.write("// header\n100.5 200.25 12.75 255 255 255\n")
            with open(bad_xyz, "w", encoding="utf-8") as fh:
                fh.write("<html><body>not a point cloud</body></html>\n")
            self.assertTrue(ver_mod.verify_point_cloud(good_xyz, min_size_bytes=10)[0])
            self.assertFalse(ver_mod.verify_point_cloud(bad_xyz, min_size_bytes=10)[0])

            # Cross-project spoofing guard: SiteB.obj must NOT satisfy SiteA
            iso_dir = os.path.join(tmpdir, "shared_out")
            os.makedirs(iso_dir, exist_ok=True)
            with open(os.path.join(iso_dir, "SiteB.obj"), "w", encoding="utf-8") as fh:
                fh.write("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n" + ("# pad\n" * 120))
            res_spoof = ver_mod.verify_project_deliverables(iso_dir, project_name="SiteA", expected_assets=["obj"])
            self.assertEqual(res_spoof["status"], "FAIL")

    def test_cluster_monitor_accepts_psx_only_and_glb_only_while_failing_corrupt_assets(self) -> None:
        """cluster_monitor.verify_deliverables validates PSX-only and GLB-only sites and rejects corrupt files."""
        with tempfile.TemporaryDirectory() as psx_dir:
            psx = os.path.join(psx_dir, "SiteOnlyPSX.psx")
            fdir = os.path.join(psx_dir, "SiteOnlyPSX.files")
            os.makedirs(fdir, exist_ok=True)
            with open(psx, "w", encoding="utf-8") as fh:
                fh.write('<?xml version="1.0"?><document version="1.2.0" path="SiteOnlyPSX.files/project.zip"/>\n')
            with zipfile.ZipFile(os.path.join(fdir, "project.zip"), "w") as zf:
                zf.writestr("doc.xml", '<document version="1.2.0"/>')
            res_psx = monitor_mod.verify_deliverables(psx_dir, "SiteOnlyPSX")
            self.assertTrue(res_psx["intact"], msg=str(res_psx))

            # Corrupt OBJ added alongside valid PSX fails verification
            with open(os.path.join(psx_dir, "SiteOnlyPSX.obj"), "w", encoding="utf-8") as fh:
                fh.write("v 0 0 0\n" * 200)
            res_bad_obj = monitor_mod.verify_deliverables(psx_dir, "SiteOnlyPSX")
            self.assertFalse(res_bad_obj["intact"])

        with tempfile.TemporaryDirectory() as glb_dir:
            payload = b'{"asset":{"version":"2.0"}}' + (b" " * 1500)
            with open(os.path.join(glb_dir, "SiteGLB.glb"), "wb") as fh:
                fh.write(struct.pack("<4sII", b"glTF", 2, 20 + len(payload)))
                fh.write(struct.pack("<I4s", len(payload), b"JSON") + payload)
            res_glb = monitor_mod.verify_deliverables(glb_dir, "SiteGLB")
            self.assertTrue(res_glb["intact"], msg=str(res_glb))

        with tempfile.TemporaryDirectory() as empty_dir:
            res_empty = monitor_mod.verify_deliverables(empty_dir, "EmptySite")
            self.assertFalse(res_empty["intact"])

    def test_submit_cluster_batch_skips_appledouble_ignored_dirs_and_uses_relpath_under(self) -> None:
        """find_photos_in_directory ignores AppleDouble/hidden/model folders; UNC relpath handles casing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            keep = os.path.join(tmpdir, "Flight_1", "IMG_0001.JPG")
            skip_ad = os.path.join(tmpdir, "Flight_1", "._IMG_0001.JPG")
            skip_hid = os.path.join(tmpdir, ".Trash", "IMG_0002.JPG")
            skip_files = os.path.join(tmpdir, "Proj.files", "thumb.jpg")
            skip_model = os.path.join(tmpdir, "model", "tex.png")
            for p in (keep, skip_ad, skip_hid, skip_files, skip_model):
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as fh:
                    fh.write(b"\xff\xd8\xff\xd9")

            found = submit_mod.find_photos_in_directory(tmpdir)
            self.assertEqual(found, [os.path.normpath(keep)])

        self.assertEqual(
            submit_mod.get_cluster_relative_path(r"\\srv\Share\Sub\Proj.psx", r"\\SRV\Share"),
            "Sub/Proj.psx",
        )

    def test_updater_multi_worker_per_host_reconnect_fqdn_and_sha256(self) -> None:
        """Multi-worker host updates match strictly new IDs, FQDN hosts, temp signal fallback, and SHA-256."""
        # 1. Two workers on the SAME host: updating worker 1 must wait for NEW worker 3, not claim worker 2
        fake_ms = SimpleNamespace(
            Tasks=SimpleNamespace(RunScript=lambda: SimpleNamespace(code="", toNetworkTask=lambda: object()))
        )
        client = MagicMock()
        client.workerList.side_effect = [
            # Initial lookup (worker 1 & sibling worker 2 both online on GPU-RIG)
            {
                "workers": [
                    {"worker_id": 1, "hostname": "GPU-RIG", "hostaddr": "10.5.10.250:5001", "state": "ready"},
                    {"worker_id": 2, "hostname": "GPU-RIG", "hostaddr": "10.5.10.250:5002", "state": "ready"},
                ]
            },
            # Poll 1 after quitWorker(1): only sibling worker 2 is ready (must NOT claim ID 2!)
            {
                "workers": [
                    {"worker_id": 2, "hostname": "GPU-RIG", "hostaddr": "10.5.10.250:5002", "state": "ready"},
                ]
            },
            # Poll 2: new worker 3 comes online
            {
                "workers": [
                    {"worker_id": 2, "hostname": "GPU-RIG", "hostaddr": "10.5.10.250:5002", "state": "ready"},
                    {"worker_id": 3, "hostname": "GPU-RIG", "hostaddr": "10.5.10.250:5003", "state": "ready"},
                ]
            },
        ]
        client.createBatch.return_value = 42
        client.batchList.return_value = {"batches": [{"batch_id": 42, "state": "completed"}]}

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(updater_mod, "Metashape", fake_ms):
            res = updater_mod.update_remote_worker(
                client=client,
                worker_id_or_name=1,
                shared_root=tmpdir,
                timeout=5.0,
                restart=True,
                poll_interval=0.001,
            )
        self.assertTrue(res["relaunched"])
        self.assertEqual(res["new_worker_id"], 3)

        # 2. Parallel update on same host assigns distinct new_worker_id values (never duplicates)
        par_client = MagicMock()
        par_client.workerList.side_effect = [
            {
                "workers": [
                    {"worker_id": 1, "hostname": "GPU-RIG", "hostaddr": "10.5.10.250:5001", "state": "ready"},
                    {"worker_id": 2, "hostname": "GPU-RIG", "hostaddr": "10.5.10.250:5002", "state": "ready"},
                ]
            },
            {
                "workers": [
                    {"worker_id": 3, "hostname": "GPU-RIG", "hostaddr": "10.5.10.250:5003", "state": "ready"},
                    {"worker_id": 4, "hostname": "GPU-RIG", "hostaddr": "10.5.10.250:5004", "state": "ready"},
                ]
            },
        ]
        par_client.createBatch.side_effect = [51, 52]
        par_client.batchList.return_value = {
            "batches": [
                {"batch_id": 51, "state": "completed"},
                {"batch_id": 52, "state": "completed"},
            ]
        }
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(updater_mod, "Metashape", fake_ms):
            par_res = updater_mod.update_all_workers_parallel(
                client=par_client,
                shared_root=tmpdir,
                timeout=5.0,
                restart=True,
                poll_interval=0.001,
            )
        self.assertEqual({r["new_worker_id"] for r in par_res}, {3, 4})

        # 3. Generated updater script matches FQDN hostname, embeds sha256, and writes temp fallback signal
        expected_sha = "deadbeef" * 8
        with tempfile.TemporaryDirectory() as fake_home:
            script = updater_mod.generate_worker_updater_code(
                repo_url="https://example.com/main.zip",
                expected_hostname="worker01",
                shared_cluster_dir="",
                expected_sha256=expected_sha,
            )
            fake_env = {
                "COMPUTERNAME": "worker01.corp.lan",
                "USERPROFILE": fake_home,
                "HOME": fake_home,
                "TEMP": fake_home,
                "TMP": fake_home,
            }
            with (
                patch.dict(os.environ, fake_env, clear=True),
                patch.object(tempfile, "tempdir", fake_home),
            ):
                exec(script, {})
            temp_sig = os.path.join(fake_home, f"{updater_mod.UPDATE_SIGNAL_FILENAME}_worker01")
            self.assertTrue(os.path.isfile(temp_sig))
            with open(temp_sig, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            self.assertEqual(payload.get("sha256"), expected_sha)

        # 4. update_fleet must NOT redirect a vanished hostname to another machine reusing its worker_id
        fleet_client = MagicMock()
        fleet_client.workerList.side_effect = [
            [{"worker_id": 1, "hostname": "Host-Vanished", "hostaddr": "10.0.0.1:5000", "state": "ready"}],
            [{"worker_id": 1, "hostname": "Other-Host", "hostaddr": "10.0.0.2:5000", "state": "ready"}],
        ]
        # Adapt to dict return schema expected by list_online_workers
        fleet_client.workerList.side_effect = [
            {"workers": [{"worker_id": 1, "hostname": "Host-Vanished", "hostaddr": "10.0.0.1:5000", "state": "ready"}]},
            {"workers": [{"worker_id": 1, "hostname": "Other-Host", "hostaddr": "10.0.0.2:5000", "state": "ready"}]},
            {"workers": [{"worker_id": 1, "hostname": "Other-Host", "hostaddr": "10.0.0.2:5000", "state": "ready"}]},
        ]
        fleet_ms = SimpleNamespace(NetworkClient=lambda: fleet_client)
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.object(fleet_mod, "Metashape", fleet_ms),
            patch.object(
                sys,
                "argv",
                [
                    "update_fleet.py",
                    "--server",
                    "127.0.0.1:5840",
                    "--root",
                    tmpdir,
                    "--node",
                    "Host-Vanished",
                    "--no-reexec",
                ],
            ),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            rc = fleet_mod.main()
        self.assertEqual(rc, 1)
        fleet_client.createBatch.assert_not_called()

    def test_staging_linux_tmpfs_deprioritization_and_smb_rename_retry(self) -> None:
        """Linux tmpfs detection deprioritizes RAM mounts and SMB rename retries before fallback copy."""
        # 1. AUTOMETASHAPE_SCRATCH_DIR override and Linux /proc/mounts tmpfs deprioritization
        with tempfile.TemporaryDirectory() as env_dir:
            with patch.dict(os.environ, {"AUTOMETASHAPE_SCRATCH_DIR": env_dir}):
                self.assertEqual(staging_mod.get_optimal_local_scratch_dir(), os.path.normpath(env_dir))

        mounts_txt = "tmpfs /tmp tmpfs rw,nosuid 0 0\n/dev/nvme0n1p1 /var/tmp ext4 rw 0 0\n"
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(staging_mod.os.path, "realpath", side_effect=lambda p: p),
            patch.object(staging_mod.os.path, "exists", return_value=True),
            patch("builtins.open", unittest.mock.mock_open(read_data=mounts_txt)),
        ):
            self.assertTrue(staging_mod._is_linux_tmpfs_path("/tmp"))
            self.assertFalse(staging_mod._is_linux_tmpfs_path("/var/tmp"))

        # 2. _sync_directory_atomic retries PermissionError on tmp_d -> d_path without redundant copytree
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = os.path.join(tmpdir, "src_files")
            dst_dir = os.path.join(tmpdir, "dst_files")
            os.makedirs(src_dir, exist_ok=True)
            os.makedirs(dst_dir, exist_ok=True)
            with open(os.path.join(src_dir, "data.txt"), "w", encoding="utf-8") as fh:
                fh.write("updated")

            orig_rename = os.rename
            orig_copytree = staging_mod.shutil.copytree
            rename_attempts = {"swap": 0}
            copytree_calls = {"count": 0}

            def _tracked_copytree(*args, **kwargs):
                copytree_calls["count"] += 1
                return orig_copytree(*args, **kwargs)

            def _flaky_rename(src: str, dst: str) -> None:
                if ".sync_new_" in src and dst == dst_dir:
                    rename_attempts["swap"] += 1
                    if rename_attempts["swap"] == 1:
                        raise PermissionError("Transient AV lock")
                return orig_rename(src, dst)

            with (
                patch.object(staging_mod.shutil, "copytree", side_effect=_tracked_copytree),
                patch.object(staging_mod.os, "rename", side_effect=_flaky_rename),
                patch("time.sleep"),
            ):
                staging_mod._sync_directory_atomic(src_dir, dst_dir, "tag", ignore_fn=None)

            self.assertEqual(rename_attempts["swap"], 2)
            self.assertEqual(copytree_calls["count"], 1)
            with open(os.path.join(dst_dir, "data.txt"), "r", encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "updated")

    def test_hardware_airgapped_lan_ip_env_override_and_ipv6_bat(self) -> None:
        """LAN IP probes RFC1918 routes offline, Metashape env override wins, and IPv6 hosts bracket cleanly."""

        class _FakeSock:
            def __init__(self) -> None:
                self._target = ("", 0)

            def connect(self, addr):
                self._target = addr
                if addr[0] == "8.8.8.8":
                    raise OSError("Unreachable")

            def getsockname(self):
                return ("10.5.10.99", 0)

            def close(self):
                pass

        with (
            patch.object(socket, "socket", side_effect=lambda *_a, **_kw: _FakeSock()),
            patch.object(socket, "gethostbyname", return_value="127.0.1.1"),
        ):
            self.assertEqual(hw_mod.get_primary_lan_ip(), "10.5.10.99")

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_exe = os.path.join(tmpdir, "custom_metashape.exe")
            with open(fake_exe, "wb") as fh:
                fh.write(b"MZ")
            with patch.dict(os.environ, {"AUTOMETASHAPE_METASHAPE_PATH": fake_exe}):
                self.assertEqual(hw_mod.find_metashape_path(), os.path.normpath(fake_exe))

        bat = hw_mod.generate_worker_bat_content(host="fe80::1", port=5840, root_path=r"\\SRV\Share")
        self.assertIn("--host [fe80::1]:5840", bat)

    def test_supervisor_preserves_attached_batches_on_stop_on_failure_and_skips_appledouble(self) -> None:
        """Ensure stop_on_failure preserves attached batches, skips AppleDouble, and defaults exports."""
        self.assertEqual(
            sup_mod._derive_default_expected_assets({"export_obj": False, "export_ortho": False}),
            ["psx"],
        )

        # AppleDouble folder discovery check
        with tempfile.TemporaryDirectory() as scan_dir:
            ghost_dir = os.path.join(scan_dir, "GhostFlight")
            real_dir = os.path.join(scan_dir, "RealFlight")
            os.makedirs(ghost_dir, exist_ok=True)
            os.makedirs(real_dir, exist_ok=True)
            with open(os.path.join(ghost_dir, "._DJI_0001.JPG"), "wb") as fh:
                fh.write(b"\xff\xd8\xff\xd9")
            with open(os.path.join(real_dir, "DJI_0001.JPG"), "wb") as fh:
                fh.write(b"\xff\xd8\xff\xd9")
            jobs = sup_mod.discover_dataset_projects(scan_dir)
            self.assertEqual([j.project_name for j in jobs], ["RealFlight"])

        # Concurrent supervisor stop_on_failure aborts ONLY owned batches, preserving attached Batch #10
        with tempfile.TemporaryDirectory() as tmpdir:
            for name in ("AttachedSite", "FailOwned", "OtherOwned"):
                img_d = os.path.join(tmpdir, "src", name)
                out_d = os.path.join(tmpdir, "res", name)
                os.makedirs(img_d, exist_ok=True)
                os.makedirs(out_d, exist_ok=True)
                with open(os.path.join(img_d, "IMG_0001.JPG"), "wb") as fh:
                    fh.write(b"\xff\xd8\xff\xd9")

            sup = sup_mod.ClusterSupervisor(min_headroom_gb=0.01, stop_on_failure=True, concurrent=True)
            for name in ("AttachedSite", "FailOwned", "OtherOwned"):
                sup.add_job(
                    images_dir=os.path.join(tmpdir, "src", name),
                    output_dir=os.path.join(tmpdir, "res", name),
                    project_name=name,
                    expected_assets=["glb"],
                    extra_args={"network_path": tmpdir},
                )

            fake_client = MagicMock()
            fake_client.workerList.return_value = {"workers": []}
            fake_client.batchInfo.side_effect = lambda bid: (
                {"batch_id": 11, "state": "completed", "error_count": 0}
                if bid == 11
                else {"batch_id": bid, "state": "inprogress", "error_count": 0}
            )
            class _FakeDoc:
                def save(self, path):
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write("<document/>")

                def clear(self):
                    pass

            fake_ms = SimpleNamespace(
                NetworkClient=lambda: fake_client,
                Document=_FakeDoc,
            )

            create_seq = [(10, True), (11, False), (12, False)]
            with (
                patch.object(sup_mod, "Metashape", fake_ms),
                patch.object(sup_mod, "build_monolithic_runscript_task", return_value=(MagicMock(), "s.py", "")),
                patch.object(sup_mod, "safe_create_cluster_batch", side_effect=create_seq),
                patch.object(
                    sup_mod,
                    "verify_project_deliverables",
                    return_value={"status": "FAIL", "errors": ["bad"]},
                ),
                patch("shutil.disk_usage", return_value=SimpleNamespace(free=500 * (1024**3))),
                patch("time.sleep"),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                sup.run_concurrent_queue()

            aborted_calls = [c.args[0] for c in fake_client.abortBatch.call_args_list]
            self.assertIn(12, aborted_calls)
            self.assertNotIn(10, aborted_calls)

        # Malformed manifest returns exit code 1 cleanly without KeyError
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_manifest = os.path.join(tmpdir, "bad.json")
            with open(bad_manifest, "w", encoding="utf-8") as fh:
                json.dump([{"project_name": "MissingImages"}], fh)
            fake_ms_cli = SimpleNamespace()
            with (
                patch.object(sup_mod, "Metashape", fake_ms_cli),
                patch.object(sys, "argv", ["supervisor.py", "--manifest", bad_manifest, "--no-reexec"]),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(sup_mod.main(), 1)

    def test_inspect_checkpoint_reports_crs_textures_and_json_output(self) -> None:
        """inspect_project adds CRS, sensor, marker, and texture metadata; main --json emits valid JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            psx = os.path.join(tmpdir, "Diag.psx")
            with open(psx, "w", encoding="utf-8") as fh:
                fh.write("<doc/>")

            fake_model = SimpleNamespace(
                faces=[1, 2],
                vertices=[1, 2, 3],
                textures=[object()],
                meta={"BuildUV/page_count": "4"},
            )
            fake_chunk = SimpleNamespace(
                label="Chunk1",
                crs=SimpleNamespace(name="UTM48N", wkt='PROJCS["UTM48N"]'),
                cameras=[SimpleNamespace(transform=object(), enabled=True)],
                sensors=[object(), object()],
                markers=[object(), object(), object()],
                tie_points=SimpleNamespace(points=[1, 2, 3]),
                depth_maps={"0": b"d"},
                point_cloud=SimpleNamespace(point_count=500),
                model=fake_model,
                orthomosaic=object(),
                elevation=object(),
            )
            fake_doc = MagicMock()
            fake_doc.chunks = [fake_chunk]
            fake_ms = SimpleNamespace(Document=lambda: fake_doc)

            with patch.object(inspect_mod, "Metashape", fake_ms), redirect_stdout(io.StringIO()):
                info = inspect_mod.inspect_project(psx)

            c0 = info["chunks"][0]
            self.assertEqual(c0["crs"], "UTM48N")
            self.assertEqual(c0["crs_kind"], "projected")
            self.assertEqual(c0["sensor_count"], 2)
            self.assertEqual(c0["marker_count"], 3)
            self.assertEqual(c0["texture_pages"], 4)

            buf = io.StringIO()
            with (
                patch.object(inspect_mod, "Metashape", fake_ms),
                patch.object(sys, "argv", ["inspect_checkpoint.py", "--project", psx, "--json", "--no-reexec"]),
                redirect_stdout(buf),
            ):
                rc = inspect_mod.main()
            self.assertEqual(rc, 0)
            parsed = json.loads(buf.getvalue())
            self.assertEqual(parsed["chunks"][0]["texture_pages"], 4)


if __name__ == "__main__":
    unittest.main()
