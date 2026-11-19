# Audia hardware: building it myself

Plan: build in two stages. Software never waits on hardware, because
`--serial sim` runs the exact code path a real board uses.

## Safety rules (non-negotiable, read before every session)
1. Electrodes on your body only while the laptop runs **on battery with the charger unplugged**.
2. Nothing mains-powered connected to the laptop at the same time: no monitor, no powered USB hub, no desk speakers.
3. The front end and microcontroller get power only from the laptop's USB port or a battery. Never from a wall adapter or bench supply.
4. Nothing in this project ever puts current *into* the body. We only measure.
5. Treat any DIY board as not isolated. A galvanic USB isolator is a good upgrade before Stage 2 (unverified part choice, research first).

Why: an amplifier like this is not isolated. A surge from mains power could reach the electrodes. Running everything on battery removes that path.

## Stage 1 (weeks 3 to 4): one channel, off-the-shelf front end
| Part | Why | Price |
|---|---|---|
| BioAmp EXG Pill (Upside Down Labs), open hardware, 1 channel, any 5 V MCU with an ADC | Proven EEG front end, KiCad files public | 2021 crowdfunding pack was $69 for 2 boards + 15 gel electrodes; current price **unverified** |
| Arduino Uno or any 5 V board with an ADC | Streams samples over USB | likely already owned (ES4) |
| Gel electrodes + snap cables | Contact through hair at the back of the head | unverified |

Wiring: front end `VCC -> 5V`, `GND -> GND`, `OUT -> A0`. Flash `firmware/audia_adc/audia_adc.ino`, then:
```bash
ls /dev/cu.usb*                                  # find the port on a Mac
python audia_live.py --serial /dev/cu.usbmodemXXXX
```
Electrode placement: follow Upside Down Labs' EEG guide for the EXG Pill. For the alpha test you want the measuring electrode over the back of the head and the reference behind the ear.

### Gates (must pass in order, logged in BOARD_LOG)
- **Gate 1, it's alive:** blinks show up as big spikes in the raw wave. Blinks are easy to see, so this proves the chain works.
- **Gate 2, it's a brain:** with eyes closed, alpha clearly rises in 3 out of 3 one-minute trials compared with eyes open. Same test the simulator fakes every 20 s.
- **Gate 3, it's usable:** 10 minutes of a study session with "signal OK" on for most of the time.

## Stage 2 (week 7 onward, stretch): my own multi-channel board
Only after Gate 2 passes. Paper design first, reviewed by the board.
- Chip: TI ADS1299 (8-channel, 24-bit). Same chip as OpenBCI Cyton, HackEEG, PiEEG, and Cerelog ESP-EEG, all open designs to learn from.
- Reference build: an 8-channel ADS1299 + ESP32 headset for under $350, with a noise floor around 1 µV when the inputs are shorted.
- No human use until a shorted-input noise test passes on the bench.
- Form factor research (headband vs. around-the-ear) happens here, with the Neuroscientist.
