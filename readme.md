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
git clone https://github.com/ilya_ssh/GP2Mem.git
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


