# Keystroke Dynamics PIN Verification on LPC1768

A research prototype for a fixed four-digit PIN on a 4×4 keypad. The LPC1768 scans keys, records press/release timing, and sends measurements over UART. Python collects sessions, trains per-user timing verifiers, and sends the live accept/reject decision back to the board for LCD display. The PC must stay connected during verification; this is not an onboard ML implementation or a production authentication system.

## Repository map

| Path | Purpose |
| --- | --- |
| `firmware/lpc1768_keypad_lcd.c` | Current UART/LCD application; the only firmware `main()` |
| `keil/` | Keil project and startup source; needs local CMSIS device support |
| `keystroke_ml.py` | Serial collection, training, evaluation, and command-line verification |
| `demo_lcd.py` | PC verification and UART replies for the LCD demonstration |
| `plot_results.py` | Regenerates plots from local model reports and predictions |
| `graphs/` | Figures and aggregate summary from the recorded experiment |
| `tests/`, `test_toolkit.py` | Host-side software tests; `tests/LPC17xx.h` is a mock header only |
| `data/`, `models/` | Local study data and model outputs, excluded from Git |

The three source archives contained the same `keystroke_ml.py`, `test_toolkit.py`, and dataset in both Python packages. This repository keeps one copy of each shared script, the newer lab-demo firmware, and the existing plotting code. The older `proj.c` and older firmware version are not compiled.

## Set up Python on Windows

Use Python 3.11. In PowerShell, from this folder:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python test_toolkit.py
```

The dependency versions include scikit-learn 1.8.0 and joblib 1.5.3, matching the saved model snapshot in the source materials. Run `python tests/test_demo.py` only after placing the trusted `models/models.joblib`, `models/report.json`, `models/test_predictions.csv`, and `data/dataset.csv` locally. Never load an untrusted joblib file.

## Build the firmware

Open `keil/proj.uvproj` in Keil µVision. Its application source points to `../firmware/lpc1768_keypad_lcd.c`; its startup file remains in `keil/`. Add the LPC1768 CMSIS/device pack for your installed toolchain, and provide `system_lpc17xx.c` and `LPC17xx.h` from that pack. The original project referenced `D:\ARM\Startup\NXP\LPC17xx\system_LPC17xx.c` on another PC; the project now expects `keil/system_lpc17xx.c`, which is intentionally not bundled. Check device, include directories, compiler, memory settings, and board wiring before building and flashing. The mock `tests/LPC17xx.h` must **never** be added to Keil. No Keil build or physical board run was performed while assembling this repository.

The firmware uses a 1 ms scan and 20 ms debounce; UART0 is 9600 baud, 8N1 with no flow control. Connect the same keypad and LCD used for collection. Have exactly one serial program open at a time.

## Collect and evaluate

The research setup uses five pseudonymous participants (U01–U05), three sessions each (S01–S03), and 30 valid attempts per session, with a common study PIN. The example commands use `1234` and `COM4`; change the serial port to your Device Manager listing. Collect only with participants' consent.

```powershell
python keystroke_ml.py collect --port COM4 --user U01 --session S01 --pin 1234 --samples 30
python keystroke_ml.py train --csv data/dataset.csv --train-sessions S01 --cal-sessions S02 --test-sessions S03 --out models
python plot_results.py --models models --out graphs
python demo_lcd.py --port COM4 --user U01 --method svm
```

Repeat collection for each user and session before training. S01 fits models, S02 calibrates thresholds, and S03 is held out for testing. Do not tune thresholds on S03. The pipeline compares total duration, Manhattan distance, diagonal variance, and an SVM. The demo checks a **claimed user**; it does not identify an unknown user. Enter four digits, releasing each key, and allow the PC's UART reply to update the LCD.

## Data availability

Raw UART logs, the row-level timing CSV, individual test predictions, and the trained model are intentionally excluded from this public-ready package. 
