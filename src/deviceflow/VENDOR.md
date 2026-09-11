# Bundled slab kernel

`deviceflow` here is the DeviceFlow 0.2.0 source, copied from
ProcessFlow-Emulator's `worker/vendor/deviceflow_src/deviceflow`, which is in
turn the supplied `deviceflow_20260904.zip` package plus the desktop
integration's own changes to it (a `state_io` checkpoint format, a lazily
imported `Layout`, `Mask.transformed`, and `TopView.to_dict`). Keeping it
identical is what lets the two applications run the same engine and read each
other's results in principle, so Process Studio drives it through
`process_studio/kernels/slab.py` and keeps its own edits to this directory to
fixes, listed here so a newer drop of the kernel can carry them or replace
them:

- `process/conformal.py`: the staircase snap between consecutive z samples
  acts on the dilation, not on the film ring cut from it. Snapping the ring
  moved its interface with the solid it grew on, so a second film of the same
  material met the first along a nanometre-wide crack and the mesh failed
  from the fourth film. This is the "repeated-deposition mesh defect" the
  emulator's changelog and `benchmarks/test_guardrails.py` record as open.

DeviceFlow is the author's own work and is released with this repository
under the MIT license in `LICENSE`.
- `_internal/mesh/builder.py`: `build_material_meshes` and `build_one_material`
  take `manifold=False` to skip the pinch-vertex splitting. Process Studio's
  3D view shades each face on its own and never shares vertices, so the split
  bought it nothing at a third of the build time. Validation and export still
  build the manifold mesh.
- `_internal/mesh/builder.py`, `_internal/mesh/loft.py`: an optional `loft`
  (with `loft_reach`) on the same two functions. Slabs no thicker than
  `loft` are the samples of a conformal film; where a ring of one continues
  a ring of the next (matched one to one by overlap, no vertex further than
  the reach), the two outlines are joined at the slabs' mid heights by
  slanted triangles instead of a cap and two half-walls. The rings are the
  arrangement's own edges, so the result is still closed. Display only;
  the default builds the stored staircase exactly as before.
- `process/conformal.py`, `process/isotropic_etch.py`, `device.py`: an
  optional `xy_resolution` beside `conformal_resolution`. The z step and the
  XY arc sagitta were one number; a project that wants a fine z step to
  shrink the staircase on a shoulder need not pay for finer rings as well.
  Unset, the XY value is the z value, exactly as before.
