"""Level-set geometry kernel.

Nothing here is re-exported at the package level: every module that uses
one of these pulls it in from its own submodule (``.kernel.grid``,
``.kernel.material_state``, and so on), because ``grid.py`` alone is needed
by code that runs regardless of which simulation kernel a project uses
(the worker's window and spacing handling), while the rest of this package
is the level-set engine's own geometry and pulls in scipy. A re-export
here would run all of it — scipy included — the moment anything imported
even the grid-only piece, which is exactly what a slab-only build must
not require.
"""
