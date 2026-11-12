# PCB

KiCad 10 project. Footprints, symbols and the XIAO 3D model are in [`lib/`](lib/).

## Screw holes

The PCB is screwed to the plate, clamping the housings and dome flanges between them, so where the screws go decides how evenly every key is clamped, which matters for EC since you want every dome to be equally snug against the PCB.

The board has 42 M2 holes, but only the 20 with a silk ring on the back are used. 20 seemed a reasonable compromise between an even clamp and ease of assembly. [`tools/pick_screws.py`](../tools/pick_screws.py) chose them. It tries every combination of holes, mirrored left to right, and keeps the one where keys and the outer edge are, on average, closest to a screw.

To re-pick, close the project in KiCad and run:

```sh
uv run tools/pick_screws.py -n 20 --apply
```

This moves the rings to the new picks on the board and in the schematic, and writes a plot comparing them to the current set.
