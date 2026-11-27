# Third-Party Notices

This project incorporates architectural patterns, algorithms, and open-source software libraries.
We gratefully acknowledge the following upstream projects:

---

## 1. NVIDIA Isaac Lab / Isaac Sim Architecture
- **Upstream**: [isaac-sim/IsaacLab](https://github.com/isaac-sim/IsaacLab) & [isaac-sim/IsaacSim](https://github.com/isaac-sim/IsaacSim)
- **License**: BSD-3-Clause & Apache-2.0
- **Notice**: Architecture blueprints, canonical `source/` topology, multi-asset skill validation (`tools/skills/cli.py`), SPDX license auditing (`tools/check_licenses.py`), and lean agent governance.

---

## 2. Agisoft Metashape Professional Python API
- **Upstream**: [Agisoft Metashape](https://www.agisoft.com/)
- **License**: Commercial / Proprietary API runtime
- **Notice**: Multi-view stereo depth maps, camera bundle adjustment, rolling shutter compensation, mosaic texture blending, distributed network processing (`NetworkClient`), and GeoTIFF orthomosaic export.

---

## 3. Open3D
- **Upstream**: [isl-org/Open3D](https://github.com/isl-org/Open3D)
- **License**: MIT License
- **Notice**: Offline CPU point-cloud normal estimation, voxel downsampling, multi-scale Tukey Sim(3) Point-to-Plane ICP, and gated Colored ICP registration executed via the isolated portable worker runtime (`source/autometashape/core/icp_worker.py` and `.cluster/bridge_runtime/`).

---

## 4. NumPy & SciPy
- **Upstream**: [numpy/numpy](https://github.com/numpy/numpy) & [scipy/scipy](https://github.com/scipy/scipy)
- **License**: BSD-3-Clause
- **Notice**: Numerical linear algebra and geometric array processing inside the isolated offline Open3D ICP bridge worker runtime.

---

## 5. Pillow (PIL Fork)
- **Upstream**: [python-pillow/Pillow](https://github.com/python-pillow/Pillow)
- **License**: MIT-CMU (HPND) License
- **Notice**: Optional EXIF metadata extraction and luminance radiometry sampling in `source/autometashape/core/telemetry.py`.

---

## 6. CPython & python-build-standalone
- **Upstream**: [python/cpython](https://github.com/python/cpython) & [astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone)
- **License**: Python Software Foundation License v2 (PSF-2.0) & MPL-2.0 / Apache-2.0
- **Notice**: Portable standalone CPython runtime packaged into the offline SHA-256-verified `.cluster/bridge_runtime/` worker bundle.

---

## 7. Ruff
- **Upstream**: [astral-sh/ruff](https://github.com/astral-sh/ruff)
- **License**: MIT License
- **Notice**: Python linter and formatter used in `format_code.bat`, `format_code.sh`, `.pre-commit-config.yaml`, and `.github/workflows/lint-and-format.yml`.

---

## 8. Geist Font Family
- **Upstream**: [vercel/geist-font](https://github.com/vercel/geist-font)
- **License**: SIL Open Font License 1.1 (OFL-1.1)
- **Notice**: Typography referenced in the standalone browser telemetry dashboard (`web/projects_dashboard.html`).
