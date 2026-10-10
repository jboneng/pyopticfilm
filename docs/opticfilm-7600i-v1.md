# OpticFilm 7600i v1 (GL843)

`07b3:0c3b`, bcdDevice `0x0400`. Capture-derived: each scan replays SilverFast's job sequence for
that resolution, from USB captures of a real 7600i v1 (sequences and findings from
[OpenOptic](https://github.com/bazfp/OpenOptic), where they run on this hardware). The 7600i v2
(bcdDevice `0x0605`, GL845) is unaffected.

| | |
|---|---|
| Colour | 1440, 3600, 7200 dpi full frame, 16-bit linear RGB |
| Speed | the main scan samples the nominal resolution once vertically (SilverFast samples twice and averages pairs), no CCD dummy lines, MAXWD two lines, BUFSEL 0x10 and backtracking off (0x02 ACDCDIS), so a busy host pauses the carriage instead of reversing it. 7200 dpi: image in ~52 s at 8.5 MB/s, whole job ~84 s (SilverFast's timing: 237 s). `single_sample=False` and `dummy_lines="recorded"` restore SilverFast's job |
| Area | 36.61 × 24.72 mm: sensor pixels 80–10458 (3600 dpi: 82–10458; 1440 dpi: 83–10463), the full area. SilverFast's window is 210–10458 (36.15 mm). The pixel count must stay even: with an odd count the scanner sends a pixel less per line and the read stalls |
| Infrared | 3600, 7200 dpi (`mode="infrared"`, or `infrared=True` with colour) |
| Crop | `area=`, on the host |
| Not yet | multi-exposure, other resolutions, grey |

Differences from the SANE GL843 path:

- Register reads set the address (`0x83`, one byte) then read `0x84`; the address auto-increments
  after each read. Every write is acknowledged by polling `0x8E`/`0x20` bit 0.
- Boot, AFE, motor tables and shading are the vendor's. The first positioning move is stopped at
  the recorded time (2.56–2.57 s). Homing uses the vendor's motor primitives.
- The image arrives mirrored, with twice as many lines as columns and fractional R/G/B delays;
  7200 dpi has an 8-line column stagger. Infrared is read by the red row.
- Infrared at 7200 dpi was not captured: it is the 7200 dpi colour job with the changes SilverFast
  makes for infrared at 3600 dpi (lamp off, infrared LED, AFE), and shading computed from the job's
  white frame as SilverFast does (`0x13000 × 0x2000 / white`).

- Every scan calibrates from its live probes: four-pixel peak means select AFE gains;
  32 black pixels determine offsets; 128-line references discard eight extremes at each end.
  Dark correction uses a forward 100-value mean (separate column parities at 7200 dpi).
  Both shading uploads cover the actual window, including added pixels and infrared hot columns.

Validation: `tests/test_opticfilm_7600i_v1.py` replays every job against strict playback of the
capture (any transfer that differs from SilverFast's fails), and covers boot, homing, image
assembly and an end-to-end `Scanner.open_fake` scan on a simulated GL843.

The initial gains, offsets and shading reproduce eight captured calibration passes. The dark
outlier gate uses inclusive 64; captures constrain this setting to 63..67, rather than uniquely
identifying it. Final colour gain increments remain 0/+1/+2 as captured. Optical verification on
hardware is still needed. `apply_calib=False` retains recorded AFE/shading for comparison.

Data: `device/data/opticfilm_7600i_v1.json.gz`, rebuilt with
`python tools/import_7600i_v1_profiles.py /path/to/OpenOptic`.
