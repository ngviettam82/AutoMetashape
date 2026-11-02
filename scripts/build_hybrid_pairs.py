# Copyright (c) 2026, AETHER Project Developers.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

import os, glob, re, math
import numpy as np

def extract_metadata(images_dir):
    image_paths = sorted(glob.glob(os.path.join(images_dir, "*.JPG")))
    metadata = []
    
    for idx, p in enumerate(image_paths):
        basename = os.path.basename(p)
        with open(p, 'rb') as f:
            data = f.read(65536)
        
        m_lat = re.search(r'drone-dji:GpsLatitude="([^"]+)"', str(data))
        m_lon = re.search(r'drone-dji:GpsLongitude="([^"]+)"', str(data))
        m_alt = re.search(r'drone-dji:RelativeAltitude="([^"]+)"', str(data))
        m_yaw = re.search(r'drone-dji:GimbalYawDegree="([^"]+)"', str(data))
        m_pitch = re.search(r'drone-dji:GimbalPitchDegree="([^"]+)"', str(data))
        
        lat = float(m_lat.group(1)) if m_lat else 0.0
        lon = float(m_lon.group(1)) if m_lon else 0.0
        alt = float(m_alt.group(1)) if m_alt else 0.0
        yaw = float(m_yaw.group(1)) if m_yaw else 0.0
        pitch = float(m_pitch.group(1)) if m_pitch else 0.0
        
        has_gps = (lat > 1.0 and lon > 50.0)
        metadata.append({
            "id": idx,
            "filename": basename,
            "path": p,
            "has_gps": has_gps,
            "lat": lat,
            "lon": lon,
            "alt": alt,
            "yaw": yaw,
            "pitch": pitch
        })
    return metadata

def build_pairs(metadata, max_temporal_dist=8, max_spatial_neighbors=25, max_spatial_dist_m=30.0, output_pairs_path=None):
    valid_gps = [m for m in metadata if m["has_gps"]]
    lat_ref = np.mean([m["lat"] for m in valid_gps])
    lon_ref = np.mean([m["lon"] for m in valid_gps])
    
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * np.cos(np.radians(lat_ref))
    
    for m in metadata:
        if m["has_gps"]:
            m["x"] = (m["lon"] - lon_ref) * m_per_deg_lon
            m["y"] = (m["lat"] - lat_ref) * m_per_deg_lat
            m["z"] = m["alt"]
        else:
            m["x"], m["y"], m["z"] = None, None, None

    pairs = set()
    n = len(metadata)
    
    # 1. Sequential Temporal Connections (Connects takeoff, landing, and consecutive flight)
    for i in range(n):
        for step in range(1, max_temporal_dist + 1):
            if i + step < n:
                p1, p2 = sorted([metadata[i]["filename"], metadata[i + step]["filename"]])
                pairs.add((p1, p2))
                
    # 2. Top-K Spatial Proximity Connections (For frames with valid GNSS)
    for i in range(n):
        if not metadata[i]["has_gps"]:
            continue
        xi, yi, zi = metadata[i]["x"], metadata[i]["y"], metadata[i]["z"]
        dists = []
        for j in range(n):
            if i == j or not metadata[j]["has_gps"]:
                continue
            xj, yj, zj = metadata[j]["x"], metadata[j]["y"], metadata[j]["z"]
            dist = math.sqrt((xi - xj)**2 + (yi - yj)**2 + (zi - zj)**2)
            if dist <= max_spatial_dist_m:
                dists.append((dist, metadata[j]["filename"]))
        
        # Keep top-K closest spatial neighbors
        dists.sort(key=lambda x: x[0])
        for _, neighbor_fn in dists[:max_spatial_neighbors]:
            p1, p2 = sorted([metadata[i]["filename"], neighbor_fn])
            pairs.add((p1, p2))

    if output_pairs_path:
        os.makedirs(os.path.dirname(output_pairs_path), exist_ok=True)
        with open(output_pairs_path, "w") as f:
            for p1, p2 in sorted(pairs):
                f.write(f"{p1} {p2}\n")
                
    return pairs

if __name__ == "__main__":
    images_dir = r"c:\Users\ADMIN\Documents\Code\GaussianSplat\data\toanha1\images"
    out_pairs = r"c:\Users\ADMIN\Documents\Code\GaussianSplat\data\from_scratch_sfm\hybrid_pairs.txt"
    meta = extract_metadata(images_dir)
    pairs = build_pairs(meta, output_pairs_path=out_pairs)
    print(f"Generated {len(pairs)} optimized hybrid pairs (saved to {out_pairs})")
