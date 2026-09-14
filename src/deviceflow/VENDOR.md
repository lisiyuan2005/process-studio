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
  sub-second ones. Slabs split by a step's sample planes are merged back
  after the step, so the slab count stays bounded.
- `_internal/geometry/state.py`: `harmonize` gives a face that two regions
  of one slab both cover (two boundaries rounded onto each other) to the
  first of them, instead of rebuilding overlapping regions and failing its
  own disjointness check. `polygons.equals` short-circuits on identity.
- `process/conformal.py`: the z samples of a film are never finer than the
  conformal resolution (they were a quarter of the film's thickness when
  that was smaller). A 3 nm liner sampled every 0.75 nm gave every cavity
  four times the rings, and the rings of every slab are noded together
  downstream.
- `_internal/geometry/polygons.py`, `_internal/mesh/builder.py`,
  `_internal/geometry/state.py`: the arrangement of a device's rings is
  noded from each distinct undirected edge once (`unique_segments`)
  instead of from every region's boundary. A boundary between two
  materials is a ring of both and a film's ring repeats slab after slab,
  so a filled and polished stack fed GEOS 300k coordinates of coincident
  lines and ran out of memory; the same arrangement from 7k unique edges
  takes 19 MB.
- `process/planar.py`, `process/conformal.py`: planar deposition is the
  film that arrives from straight above instead of one blanket slab on
  the top plane. At each height the film is the solid's outline pushed
  out by `t` (wall film, flat-ended at the wall's top and bottom) plus
  every sky-visible horizontal face raised by `t` and reaching `t` past
  its edge (a square outer corner), minus the columns with a solid
  anywhere above. A recess under an overhang stays empty and both lips
  of its mouth end flat, a step stays a step, a hole narrows by `t` and
  keeps its depth while its floor rises. Nothing is rounded in z, so one
  sample between consecutive planes (and planes + `t`) is exact. The
  blanket slab remains for an empty device (the substrate).
- `process/square.py`, `process/planar.py`, `process/conformal.py`,
  `device.py`: two film models. `Device.deposit(..., square=True)` builds
  the simplified, square-cornered film of `process/square.py` (wall films
  flat-ended at the wall's edges, exposed faces moved by `t` with square
  corners, one sample between consecutive planes and planes ± `t`), for
  conformal and planar alike; without it the conformal film is the
  original ball dilation and the planar film is that dilation cut to the
  columns with no solid above (`deposit_conformal(from_above=True)`),
  which grows lips on both sides of a recess's mouth.
- `process/isotropic_etch.py`, `device.py`: `wet_etch(..., square=True)`
  grows the void by a box instead of a ball: the reach is the full depth
  at every height within it, and the front is sampled once between the
  planes and the planes ± depth instead of at the resolution. The
  barrier stepping is unchanged.
- `_internal/geometry/state.py`, `_internal/geometry/polygons.py`,
  `process/conformal.py`, `process/isotropic_etch.py`: `harmonize` still
  rebuilds every region from the shared arrangement, but opens pinches
  and seams only in the slabs a step changed (and their neighbours), no
  longer checks regions of one arrangement for overlap, and skips the
  seam overlays for a slab pair whose regions are identical or whose
  boundaries share no line. `_check_disjoint` asks the predicates before
  building an intersection, `polygons.equals` recognises identical rings
  by their bytes, a film is cut back from other materials only where a
  predicate says it does more than touch them, and the isotropic etch
  harmonizes the slabs it changed (`regions_mark`/`changed_since`) and,
  with the box front, shares only identical reaches between samples. The
  slowest films of the 3D NAND example went from 6.8 s to 3.2 s and from
  6.6 s to 2.2 s, the nitride removal from 5.6 s to 3.6 s.
- `process/oxidation.py`, `device.py`: `Device.oxidize(...)` takes the
  exposed skin of the listed materials the way `wet_etch` does and puts
  the product material back in its place (no swelling), rebuilding the
  pieces from the planes of both states so a layer consumed whole comes
  back as oxide.
- `cancellation.py` (new), `process/conformal.py`, `process/square.py`,
  `process/isotropic_etch.py`, `_internal/geometry/state.py`: a process
  step can be stopped part-way. `cancelling(check)` installs a check for
  the duration of a call, and the loops that cost the time — the z-sample
  walks of conformal, square and isotropic fronts, and the arrangement
  rebuild in `harmonize` — call `check_cancelled()` as they go, which
  raises `cancellation.Cancelled` (deliberately not a `DeviceFlowError`,
  so handlers that translate process errors let it past). Without a check
  installed nothing changes; with one, the cost is a context-variable read
  per sample, which does not show up in the timings above. The caller's
  state is untouched because a step mutates its own working copy.
- `process/isotropic_etch.py`, `process/conformal.py`,
  `_internal/geometry/polygons.py`: the isotropic etch's front is folded
  back to regions between steps. A step cut the stack at every z sample it
  took and handed its successor one piece per slab, so the front grew with
  the sampling — a stack etch with a liner reached 156 pieces that were
  four regions — and every piece cost a buffer at each sample within reach
  of it and made the sampling around its own two z planes fine.
  `_merge_front` joins pieces that share a region and touch in z (the
  union of the ball slices over two touching intervals is the slice over
  their union, so the taller piece buffers the same region by the larger
  of the two radii), and `_fold_runs` applies a reach that neighbouring
  samples deliberately share once over the whole run instead of once per
  sample. `polygons.equals` settles unequal regions by their areas before
  building a symmetric difference, and `_nearly_same` rejects on the
  bounding boxes before the quadratic Hausdorff distance. All of it is
  arithmetic: the box front's answer is unchanged to the bit.
  `_sample_intervals` now takes every reach as a critical offset rather
  than the largest alone — an etch with one depth per material had a kink
  half a depth from each plane where no sample looked, so the slower
  material's answer did not settle as the resolution was refined. On the
  cases measured: a stack etch 2.6 s to 0.75 s, the same at 4 nm 3.8 s to
  1.4 s, a two-rate undercut 6.4 s to 2.4 s (12.9 s to 4.5 s at 4 nm),
  and a 3D-NAND-like nitride pull-back 4.8 s to 0.38 s.
