# Bundled slab kernel

`deviceflow` here is the DeviceFlow 0.2.0 source, copied verbatim from
ProcessFlow-Emulator's `worker/vendor/deviceflow_src/deviceflow`, which is in
turn the supplied `deviceflow_20260904.zip` package plus the desktop
integration's own changes to it (a `state_io` checkpoint format, a lazily
imported `Layout`, `Mask.transformed`, and `TopView.to_dict`). Process Studio
changes nothing in this directory: keeping it identical is what lets the two
applications run the same engine and read each other's results in principle.

Process Studio drives it through `process_studio/kernels/slab.py` and never
edits it, so a newer drop of the kernel can replace this directory whole.

The license question is the one the emulator repository records in
`LICENSE_STATUS.md`: the supplied source carries no license grant here, so
confirm ownership and third-party obligations before publishing or
distributing either repository broadly.
