## What's diferent in this version

This is a fork of [ilya-ssh/GP2Mem](https://github.com/ilya-ssh/GP2Mem) extended with real-time JSON export, timing engine, and additional tooling for live broadcast overlays.

---

## 1. JSON Export

Four files are now exported in real time to a configurable folder:

- **`focused_car.json`** — position, lap, speed, gear, revs and throttle of the car currently on camera
- **`sector_times.json`** — sector splits, colors (purple/green/yellow), gap to leader, post-lap window and out-lap detection for the focused car
- **`timing_table.json`** — full 26-driver timing table sorted by best lap, including personal and global sector bests and live S1/S2 columns for the current in-progress lap
- **`race.json`** — race order with gap to car ahead in real time, pit status, and retirement detection

The export folder is configured directly from the UI via a native Windows folder picker. A **Reset Timing** button clears all accumulated timing data — useful before qualifying and practice sessions.

---

## 2. Fuel Drain Script

You can use it to "eliminate" extra cars before the race if your F1 season doesn't have as much as 13 teams. The fuel drain duration is now configured in **seconds** (float, max 60s). The deadline is calculated using `time.monotonic()`, making the drain time consistent regardless of the game's refresh rate. Additionally, if memory writes are not enabled when firing, the script now prompts the user to enable them on the spot instead of silently doing nothing.

---

## 3. Focused Car Detection

GP2Mem now reads which car is currently on camera from two mirrored memory addresses. The focused car index is updated every refresh cycle and used to populate `focused_car.json` and `sector_times.json`. If the primary address fails, it falls back to the secondary one automatically.

---

## 4. Main Table — New Columns

The car table now includes the following additional columns:

- **Foco** — marks which car is currently in camera focus
- **splitNr** — current sector the car is in (0, 1 or 2)
- **timeLastSpl1 / timeLastSpl2** — raw split times from the last lap
- **timeLast / timeBest** — last lap and personal best lap times
- **field_9E** — cumulative track position field used for gap calculation
- **in_pits** — pit lane detection flag
- **is_invisible** — retirement/out-of-race detection flag


This is the OG Description
# Disclaimer

GP2Mem edits the memory of a running process. It can cause crashes, corrupted game state, or unexpected behavior. Use at your own risk.

# GP2Mem

GP2Mem is a memory editing tool for the classic racing game *Geoff Crammond’s Grand Prix 2*. It’s compatible with both DOSBox-based setups and the x86GP2 mod.

**This tool wouldn’t be possible without Hatcher’s help on Discord, rremedio’s comments on the GP2 forum, and most notably Rene Smith’s IDA disassembly of the game.**

## Features

- Live viewing of per-car game state (positions, speed, flags, fuel, etc.)
- Editing selected fields for one car or all cars
- Built-in helpers for enums/bitflags and readable “pretty” displays
- **Custom scripts** (notably Safety Car and Slipstream)
- Full struct view to explore additional/unidentified fields

## Installation

### Option 1: Download a release (recommended)
1. Go to **Releases** on GitHub.
2. Download the latest `.zip`.
3. Extract it and run `GP2Mem`.

> Note: The packaged build is made with **PyInstaller**. Some antivirus products may show malware warnings because bad actors sometimes abuse PyInstaller, and because this tool **reads/writes process memory**. If you don’t trust the binary, use the “Run from source” method below.

### Option 2: Run from source (venv)
> Note: Use Python 3.10 or above
1. Clone the repo:
```code
git clone https://github.com/ilya-ssh/GP2Mem.git
cd GP2Mem
```

2. Create and activate a virtual environment:
```code
python -m venv .venv
```

Windows:
```code
.venv\Scripts\activate
```

3. Install dependencies:
```code
python -m pip install -U pip
python -m pip install -r requirements.txt
```

4. Run:
```code
python src/main.py
```


## How to start

1. Start Grand Prix 2 via DOSBox or the x86GP2 mod
2. Start GP2Mem
3. Ensure the game executable name matches the process name shown in GP2Mem (default is `x86GP2_inline.exe`)
4. Launch a Quickrace (recommended: start with AI cars only)
5. While the AI cars are revving up at the start, press **Find Base (Grid)** at the top of GP2Mem.
   - GP2Mem will scan memory and find the required base address.
6. You can now exit Quickrace and start your actual race/practice/quali session.

When you restart the game, you may need to repeat the **Find Base (Grid)** process again. This design is intentional to maintain compatibility with older GP2 builds used in DOSBox and older x86 versions.

## Editing values

GP2Mem is organized into tabs for different groups of fields (race state, timing, controls, pit/failure, tires/damage, flags, aero/weight, etc.). In general:

- Select a car in the **Overview** table.
- Use the **Details** tabs to view/edit fields.
- Many fields can be written to the **selected car** or to **all cars**.

### Custom scripts
There are also custom scripts, most notably:

- **Safety Car**
- **Slipstream**

These can change gameplay behavior by applying repeated memory writes during runtime.

### Warning about replays
Do **not** attempt to use the game’s replay feature while using GP2Mem—this can break the race state.

### Exploring deeper (Full Struct)
If you want to dig further, open the **Full Struct** tab to browse and experiment with additional/unidentified values and flags.

## Contributing / Feedback

Please feel free to:
- Open issues for bugs or feature requests
- Submit pull requests
- Fork the repo and modify the tool to suit your needs

Experimentation is encouraged just do it carefully and expect that some values/flags may have side effects.

## Support

If you like this repo, please give it a **star**.

If you want to donate something (not encouraged but appreciated), you can do so here:

- Ethereum: `0xE85984123B0449e6EFDC7FcaF28FFa9731868799`
- Bitcoin: `bc1qgkkfx6ef69avp5d6fkx7v606j9g8vsdtz7cwaw`





