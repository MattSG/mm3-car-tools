# MM3 car tools

The car editor and model tools for the
[Midtown Madness 3 recompilation](https://github.com/MattSG/mm3-recomp). That
repository includes this one as a submodule at `tools/car-tools`, and the tools
expect that layout: they find the extracted game in `game_files\` at the root of
the mm3-recomp checkout. No game files or car models are included here.

- [car-mods](car-mods/README.md): the Car Workshop editor and installer for the
  nine extra cars. It covers paint, ratings, physics, gearing and speedometers.
  Close the game first, then run `tools\car-tools\car-mods\editor.cmd` from the
  mm3-recomp checkout.
- [car-models](car-models/README.md): converts imported exteriors into game model
  packages, with previews and light, reflection and menu-artwork repairs.
- [mm3view](mm3view/mm3view.py): a viewer for the game's textures, car models and
  city maps.

The tests are in mm3-recomp's `tests\` folder (`test_car_*.py`, `test_mm3_*.py`),
because they need the game checkout.

## Licence

[GPL-3.0-only](LICENSE), as for mm3-recomp. This history was split out of
mm3-recomp, and revisions it released under MIT stay available under MIT.
Midtown Madness 3 and its assets belong to their rights holders, and this
repository grants no rights to them.
