# Evaluation Scenarios: Image-to-3D Model Reconstruction

### Scenario 1: Autonomous Drone Building Reconstruction with Noisy GPS
- **Query**: "Here is my drone image folder at `data/survey/building_a`. Make a 3D model with texture from these pictures."
- **Expected behavior**:
  - Automatically reads drone EXIF/XMP metadata, detecting consumer GNSS altitude drift and missing GPS on takeoff frames.
  - Constructs the hybrid temporal-spatial pair graph without requiring manual pair lists.
  - Runs feature extraction, two-view verification, and bundle adjustment.
  - Trains surface-aligned Gaussian surfels and bakes UV texture maps.
  - Outputs `model.obj`, `model.mtl`, `texture.jpg`, and `model.glb`.
- **Pass/fail criteria**:
  - Pass: >90% of views registered; watertight `.obj` and valid `.glb` generated.
  - Fail: Dropping frames with missing GPS; untextured or non-manifold mesh.

### Scenario 2: Ground Handheld Walkthrough Photogrammetry
- **Query**: "I took 120 photos walking around a monument without any GPS. Reconstruct the 3D model."
- **Expected behavior**:
  - Telemetry parser flags absence of GNSS coordinates and automatically routes matching to sequential timeline and visual vocabulary loop closure.
  - Solves camera trajectory and exports textured 3D mesh.
- **Pass/fail criteria**:
  - Pass: Full loop closure detected; seamless textured mesh exported.
  - Fail: Pipeline crashing due to missing GPS tags.

### Scenario 3: Real-Time Web Radiance Field Delivery
- **Query**: "Process this image set and give me both an OBJ for Blender and a Gaussian Splat I can view in the browser."
- **Expected behavior**:
  - Pipeline executes SfM, optimizes 2DGS/3DGS radiance field, and produces both `.obj`+`.mtl` and `.ply`/`.splat` with an interactive HTML viewer.
- **Pass/fail criteria**:
  - Pass: Both polygon mesh and Gaussian splat point cloud are generated and viewable.
  - Fail: Generating only point cloud without polygon mesh or texture atlas.
