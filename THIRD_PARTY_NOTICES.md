# Third-Party Notices

This project incorporates architectural patterns, algorithms, and open-source software libraries.
We gratefully acknowledge the following upstream projects:

---

## 1. NVIDIA Isaac Lab / Isaac Sim Architecture
- **Upstream**: [isaac-sim/IsaacLab](https://github.com/isaac-sim/IsaacLab) & [isaac-sim/IsaacSim](https://github.com/isaac-sim/IsaacSim)
- **License**: BSD-3-Clause & Apache-2.0
- **Notice**: Architecture blueprints, canonical `source/` topology, multi-asset skill validation (`tools/skills/cli.py`), and lean agent governance.

---

## 2. Agisoft Metashape Professional Python API
- **Upstream**: [Agisoft Metashape](https://www.agisoft.com/)
- **License**: Commercial / Proprietary API runtime
- **Notice**: Multi-view stereo depth maps, camera bundle adjustment, rolling shutter compensation, mosaic texture blending, and GeoTIFF orthomosaic export.

---

## 3. gsplat (Berkeley / Nerfstudio)
- **Upstream**: [nerfstudio-project/gsplat](https://github.com/nerfstudio-project/gsplat)
- **License**: Apache-2.0
- **Notice**: Differentiable 2D/3D Gaussian rasterization and CUDA kernel optimizations.

---

## 4. 2D Gaussian Splatting (2DGS)
- **Upstream**: [hbb1/2d-gaussian-splatting](https://github.com/hbb1/2d-gaussian-splatting)
- **Authors**: Binbin Huang, Zehao Yu, Anpei Chen, Andreas Geiger, Shenghua Gao (ETH Zurich)
- **License**: Apache-2.0
- **Notice**: Planar surfel representation and exact ray-splat intersection equations.

---

## 5. SuGaR: Surface-Aligned Gaussian Splatting
- **Upstream**: [Anttwo/SuGaR](https://github.com/Anttwo/SuGaR)
- **Authors**: Antoine Guédon, Vincent Lepetit (LIGM, Ecole des Ponts ParisTech)
- **License**: Apache-2.0
- **Notice**: Surface-aligned Gaussian regularization and direct mesh-binding texturing.

---

## 6. COLMAP
- **Upstream**: [colmap/colmap](https://github.com/colmap/colmap)
- **Authors**: Johannes L. Schönberger, Jan-Michael Frahm
- **License**: BSD-3-Clause
- **Notice**: Structure-from-Motion (SfM) feature extraction, epipolar matching, and bundle adjustment.
