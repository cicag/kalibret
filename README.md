# Kalibret

GUI to set LIBINPUT_CALIBRATION_MATRIX

## Purpose

This repository contains the GUI and scripts for testing and calibrating the touchscreen on the Sway tablet. It includes:

- `touch-test.py` - The main Python GUI application for drawing and calibration
- `touch-test` - Wrapper script (waybar hiding optional via `--hide-waybar` flag)
- `touch-test.desktop` - Desktop file for launching the application
- `uinput-touch.py` - Helper script for injecting touch events
- `pair-ds4.py` - Helper script for pairing DualShock 4 controllers over Bluetooth

## Usage

### From the command line:
```bash
~/.local/bin/touch-test
```

### From the desktop menu:
The application will appear in your application menu after installing the .desktop file.

### Key features:
- Free draw mode: Draw anywhere on the panel to test touch responsiveness
- Calibrate mode: Tap five targets (four corners and center) for touchscreen calibration
- Result screen: Shows calibration results and allows applying/reverting calibration
- Waybar hiding: optional via `--hide-waybar` flag (defaults to off)
- Data persistence: Calibration data is saved to ~/.local/share/touch-test/

## Files

- `touch-test.py` - Main GUI application (free draw + calibration modes)
- `touch-test` - Wrapper script (waybar hiding optional via `--hide-waybar` flag)
- `touch-test.desktop` - Desktop entry for launching from the app menu
- `uinput-touch.py` - Helper script for injecting touch events
- `pair-ds4.py` - Helper script for pairing DualShock 4 controllers over Bluetooth