#!/usr/bin/env python3
"""
GLB → Holographic Wireframe Converter
======================================
Converts a Smithsonian 3D scan GLB into line-segment data for the JARVIS
holographic bust rendering.

Pipeline (from the Handoff doc):
1. Parse GLB (handles Draco-compressed meshes via trimesh/DracoPy)
2. Normalize coordinates: center x/z, y in [0,1], uniform scale = 1/height
3. Slice the triangle mesh with ~100 horizontal planes (contour rings)
   and ~24 vertical half-planes (meridians)
4. Drop micro-segments (length < 0.0015 of bust height)
5. Output as a JS module with Float32Arrays (positions + per-vertex normals)

Usage:
    pip install trimesh numpy pygltflib
    # Optional for Draco: pip install DracoPy

    # Download the bust GLB from Smithsonian:
    # https://3d.si.edu/object/3d/george-washington:d8c63fde-4ebc-11ea-b77f-2e728ce88125
    # Click download, or use direct URL:
    # https://3d-api.si.edu/content/document/3d_package:d8c63fde-4ebc-11ea-b77f-2e728ce88125/npg_70_4_bust-hires_unwrapped-150k-1024-low.glb

    python converter.py bust.glb

    # Output: static/bust_lines.js
"""

import sys
import struct
import json
import numpy as np
from pathlib import Path

# ─── Try to import trimesh for easy GLB loading ───
try:
    import trimesh
    HAS_TRIMESH = True
except ImportError:
    HAS_TRIMESH = False

# ─── Try DracoPy for Draco-compressed meshes ───
try:
    import DracoPy
    HAS_DRACO = True
except ImportError:
    HAS_DRACO = False


def load_mesh_trimesh(glb_path):
    """Load GLB via trimesh (handles most formats including Draco if available)."""
    scene = trimesh.load(glb_path, force='scene')
    # Combine all meshes into one
    meshes = []
    for name, geom in scene.geometry.items():
        if isinstance(geom, trimesh.Trimesh):
            meshes.append(geom)
    if not meshes:
        raise ValueError("No triangle meshes found in GLB")
    combined = trimesh.util.concatenate(meshes)
    return combined.vertices, combined.faces, combined.face_normals


def load_mesh_manual(glb_path):
    """Manual GLB parser for when trimesh isn't available."""
    data = Path(glb_path).read_bytes()

    # GLB header: magic(4) + version(4) + length(4)
    magic, version, length = struct.unpack_from('<III', data, 0)
    if magic != 0x46546C67:  # 'glTF'
        raise ValueError("Not a valid GLB file")

    # Chunk 0: JSON
    chunk0_len, chunk0_type = struct.unpack_from('<II', data, 12)
    json_data = json.loads(data[20:20 + chunk0_len].decode('utf-8'))

    # Chunk 1: BIN
    bin_offset = 20 + chunk0_len
    chunk1_len, chunk1_type = struct.unpack_from('<II', data, bin_offset)
    bin_data = data[bin_offset + 8:bin_offset + 8 + chunk1_len]

    # Check for Draco compression
    meshes = json_data.get('meshes', [])
    if not meshes:
        raise ValueError("No meshes in GLB")

    all_vertices = []
    all_faces = []
    vertex_offset = 0

    for mesh in meshes:
        for prim in mesh.get('primitives', []):
            extensions = prim.get('extensions', {})
            if 'KHR_draco_mesh_compression' in extensions:
                if not HAS_DRACO:
                    raise ImportError(
                        "This GLB uses Draco compression. Install DracoPy:\n"
                        "  pip install DracoPy"
                    )
                draco_ext = extensions['KHR_draco_mesh_compression']
                bv_idx = draco_ext['bufferView']
                bv = json_data['bufferViews'][bv_idx]
                draco_bytes = bin_data[bv['byteOffset']:bv['byteOffset'] + bv['byteLength']]
                mesh_obj = DracoPy.decode(draco_bytes)
                verts = np.array(mesh_obj.points).reshape(-1, 3)
                faces = np.array(mesh_obj.faces).reshape(-1, 3)
            else:
                # Standard accessor-based mesh
                pos_idx = prim['attributes']['POSITION']
                idx_idx = prim.get('indices')

                verts = read_accessor(json_data, bin_data, pos_idx)
                if idx_idx is not None:
                    indices = read_accessor(json_data, bin_data, idx_idx).astype(int).flatten()
                    faces = indices.reshape(-1, 3)
                else:
                    faces = np.arange(len(verts)).reshape(-1, 3)

            all_faces.append(faces + vertex_offset)
            all_vertices.append(verts)
            vertex_offset += len(verts)

    vertices = np.vstack(all_vertices)
    faces = np.vstack(all_faces)

    # Compute face normals
    v0 = vertices[faces[:, 0]]
    v1 = vertices[faces[:, 1]]
    v2 = vertices[faces[:, 2]]
    face_normals = np.cross(v1 - v0, v2 - v1)
    norms = np.linalg.norm(face_normals, axis=1, keepdims=True)
    norms[norms == 0] = 1
    face_normals /= norms

    return vertices, faces, face_normals


def read_accessor(gltf, bin_data, accessor_idx):
    """Read a glTF accessor into a numpy array."""
    acc = gltf['accessors'][accessor_idx]
    bv = gltf['bufferViews'][acc['bufferView']]
    offset = bv.get('byteOffset', 0) + acc.get('byteOffset', 0)

    comp_type_map = {5120: 'b', 5121: 'B', 5122: 'h', 5123: 'H',
                     5125: 'I', 5126: 'f'}
    comp_size = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
    type_count = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT4': 16}

    dtype = comp_type_map[acc['componentType']]
    count = acc['count'] * type_count[acc['type']]
    byte_len = count * comp_size[acc['componentType']]

    arr = np.frombuffer(bin_data, dtype=f'<{dtype}', count=count, offset=offset)
    cols = type_count[acc['type']]
    if cols > 1:
        arr = arr.reshape(-1, cols)
    return arr.astype(float)


def slice_mesh(vertices, faces, face_normals,
               n_horizontal=100, n_meridians=24, min_seg_len=0.0015):
    """
    Slice the mesh with horizontal planes and vertical meridian planes.
    Returns arrays of line segment positions and normals.
    """
    # Normalize: center x/z, scale y to [0,1]
    center_x = (vertices[:, 0].min() + vertices[:, 0].max()) / 2
    center_z = (vertices[:, 2].min() + vertices[:, 2].max()) / 2
    y_min = vertices[:, 1].min()
    y_max = vertices[:, 1].max()
    height = y_max - y_min
    if height == 0:
        raise ValueError("Mesh has zero height")

    verts = vertices.copy()
    verts[:, 0] -= center_x
    verts[:, 2] -= center_z
    verts[:, 1] -= y_min
    verts /= height  # Now y in [0, 1], x/z centered

    segments = []  # Each: (p1, p2, normal)

    # ─── Horizontal slices ───
    print(f"  Slicing {n_horizontal} horizontal planes...")
    for i in range(n_horizontal):
        y = (i + 0.5) / n_horizontal  # Avoid exact edges
        segs = slice_at_y(verts, faces, face_normals, y)
        segments.extend(segs)

    # ─── Vertical meridian slices ───
    print(f"  Slicing {n_meridians} meridian planes...")
    for i in range(n_meridians):
        angle = (i / n_meridians) * np.pi  # Half-planes through Y axis
        nx = np.cos(angle)
        nz = np.sin(angle)
        segs = slice_at_plane(verts, faces, face_normals, nx, nz)
        segments.extend(segs)

    print(f"  Raw segments: {len(segments)}")

    # ─── Drop micro-segments ───
    filtered = []
    for p1, p2, n in segments:
        seg_len = np.linalg.norm(p2 - p1)
        if seg_len >= min_seg_len:
            filtered.append((p1, p2, n))

    print(f"  After filtering: {len(filtered)}")
    return filtered


def slice_at_y(verts, faces, face_normals, y_plane):
    """Slice all triangles with a horizontal plane at y=y_plane."""
    segments = []
    for fi in range(len(faces)):
        tri = verts[faces[fi]]
        points = []
        for e in [(0, 1), (1, 2), (2, 0)]:
            y0, y1 = tri[e[0], 1], tri[e[1], 1]
            if (y0 - y_plane) * (y1 - y_plane) < 0:  # Edge crosses plane
                t = (y_plane - y0) / (y1 - y0)
                p = tri[e[0]] + t * (tri[e[1]] - tri[e[0]])
                points.append(p)
        if len(points) == 2:
            segments.append((points[0], points[1], face_normals[fi]))
    return segments


def slice_at_plane(verts, faces, face_normals, nx, nz):
    """Slice triangles with a vertical plane through Y axis: nx*x + nz*z = 0."""
    segments = []
    for fi in range(len(faces)):
        tri = verts[faces[fi]]
        d = tri[:, 0] * nx + tri[:, 2] * nz  # Signed distance to plane
        points = []
        for e in [(0, 1), (1, 2), (2, 0)]:
            if d[e[0]] * d[e[1]] < 0:
                t = d[e[0]] / (d[e[0]] - d[e[1]])
                p = tri[e[0]] + t * (tri[e[1]] - tri[e[0]])
                points.append(p)
        if len(points) == 2:
            segments.append((points[0], points[1], face_normals[fi]))
    return segments


def write_js_module(segments, output_path):
    """Write line segments as a JS module with Float32Arrays."""
    positions = []
    normals = []
    for p1, p2, n in segments:
        positions.extend(p1.tolist())
        positions.extend(p2.tolist())
        # Same normal for both endpoints of the segment
        normals.extend(n.tolist())
        normals.extend(n.tolist())

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w') as f:
        f.write("// Auto-generated by converter.py — do not edit\n")
        f.write(f"// {len(segments)} line segments, {len(positions)} position floats\n\n")

        f.write("export const bustPositions = new Float32Array([\n")
        for i in range(0, len(positions), 6):  # 2 vertices per segment
            chunk = positions[i:i+6]
            f.write("  " + ",".join(f"{v:.6f}" for v in chunk) + ",\n")
        f.write("]);\n\n")

        f.write("export const bustNormals = new Float32Array([\n")
        for i in range(0, len(normals), 6):
            chunk = normals[i:i+6]
            f.write("  " + ",".join(f"{v:.6f}" for v in chunk) + ",\n")
        f.write("]);\n")

    size_kb = output_path.stat().st_size / 1024
    print(f"\n  Written to {output_path} ({size_kb:.0f} KB)")
    print(f"  {len(segments)} segments, {len(positions) // 3} vertices")


def main():
    if len(sys.argv) < 2:
        print("Usage: python converter.py <path-to-bust.glb> [output.js]")
        print("\nDownload the bust GLB from:")
        print("  https://3d.si.edu/object/3d/george-washington:d8c63fde-4ebc-11ea-b77f-2e728ce88125")
        print("\nDirect download URL (bust, low quality, 150k faces):")
        print("  https://3d-api.si.edu/content/document/3d_package:d8c63fde-4ebc-11ea-b77f-2e728ce88125/npg_70_4_bust-hires_unwrapped-150k-1024-low.glb")
        sys.exit(1)

    glb_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else "static/bust_lines.js"

    print(f"\n=== JARVIS Bust Converter ===")
    print(f"  Input:  {glb_path}")
    print(f"  Output: {output_path}\n")

    # Load mesh
    print("Loading GLB...")
    if HAS_TRIMESH:
        print("  Using trimesh loader")
        vertices, faces, face_normals = load_mesh_trimesh(glb_path)
    else:
        print("  Using manual GLB parser")
        vertices, faces, face_normals = load_mesh_manual(glb_path)

    print(f"  Vertices: {len(vertices)}, Faces: {len(faces)}")

    # Slice
    print("\nSlicing mesh...")
    segments = slice_mesh(vertices, faces, face_normals)

    # Write output
    print("\nWriting JS module...")
    write_js_module(segments, output_path)

    print("\n=== Done! ===")
    print("Add this to your HTML to use the line data:")
    print('  <script type="module" src="/static/bust_lines.js"></script>')
    print("\nOr serve static files from your FastAPI app:")
    print('  app.mount("/static", StaticFiles(directory="static"), name="static")')


if __name__ == "__main__":
    main()
