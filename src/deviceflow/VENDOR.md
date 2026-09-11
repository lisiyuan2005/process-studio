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

The license question is the one the emulator repository records in
`LICENSE_STATUS.md`: the supplied source carries no license grant here, so
confirm ownership and third-party obligations before publishing or
distributing either repository broadly.
