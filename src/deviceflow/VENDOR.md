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
- `process/conformal.py`, `process/isotropic_etch.py`, `device.py`: an
  optional `xy_resolution` beside `conformal_resolution`. The z step and the
  XY arc sagitta were one number; a project that wants a fine z step to
  shrink the staircase on a shoulder need not pay for finer rings as well.
  Unset, the XY value is the z value, exactly as before.
- `process/isotropic_etch.py`: the step size follows the thinnest *barrier
  layer* (a run of slabs with one non-target footprint that borders void or
  a target) instead of the thinnest slab, and the slabs are harmonised once
  after the last step instead of after every step; a sliver of a target
  left inside a barrier is cut back before validation. Each step after the
  first dilates only the void the previous step created, not the whole
  void: everything within a step of the older void is already gone and the
  barriers do not move, so the result is the same and each step costs a
  thin shell instead of every cavity. A replacement-gate flow went from a
  hundred-odd steps, each re-noding the whole stack, to a handful of
  sub-second ones.
