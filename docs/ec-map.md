# EC register map: MSI GT76 Titan DT 10SGS (EC firmware 17H3EMS1.102)

Built from four sources:
- the firmware's own register names (ACPI DSDT, `\_SB.PCI0.LPCB.EC`);
- MSI's WMI interface in the same firmware (device `SCM0`, the interface MSI Center uses on Windows),
  which groups the registers into nine blocks;
- the [msi-ec](https://github.com/BeardOverflow/msi-ec) maps for sibling 2020 models (GE66 / GS66 /
  GE76, config `CONF_G1_3`);
- measurements on this machine (snapshot values below were taken on AC, battery full, idle).

**Confidence:** **C** confirmed by measurement or by how the firmware uses it · **F** firmware name or
WMI grouping, consistent with the values · **S** sibling-model map, not yet verified here ·
**?** unknown.

Every host read of any register makes this EC raise two ACPI events (see the README). Read sparingly.

## MSI Center settings → registers

| MSI Center setting (Windows) | Register | Values | Conf. |
|---|---|---|---|
| Fan Speed: Auto / Advanced | `0xF4` | `0x0C` auto, `0x8C` advanced (siblings also `0x1D` silent, `0x4D` basic, low bit set) | C |
| Advanced fan curves | `0x6A–0x6F`, `0x72–0x78` (CPU); `0x82–0x87`, `0x8A–0x90` (GPU) | thresholds °C, speeds % | C |
| User Scenario (Extreme Performance / Balanced / Silent / Super Battery) | `0xF2` shift mode, plus `0xF4` | siblings: `0xC4` turbo, `0xC0` sport, `0xC1` comfort, `0xC2` eco. Here `0x80`, not a sibling value | S |
| Cooler Boost | `0x98` bit 7 | 1 = on (now `0x02`: off) | S |
| Battery Master (100 / 80 / 60 %) | `0xEF` | `0x80` + percent. Reads `0xBC` (60 %), yet the battery charged to 100 %, so it is not acting as a limit now | S |
| Webcam on/off (Fn+F6) | `0x2E` bit 1; `0x2F` block register | 1 = on (now on) | S |
| Win key / Fn–Win swap | `0xBF` bit 4 | now 1 | S |
| Keyboard RGB, Mystic Light | not EC | GT76 keyboard lighting is a USB device | F |

## All registers by WMI block

| Range | Firmware names | Meaning | Snapshot | Conf. |
|---|---|---|---|---|
| `0x00–0x25` | SMPR SMST SMAD SMCM SMD0 SMAA | ACPI SMBus host interface (protocol, status, address, command, 32 data bytes, alarm) | idle | F |
| **System block (WMI AA)** | | | | |
| `0x2D` | T2D0 (bit 0), T2D1 (bits 1–4), T2D5 (bits 5–7) | system flags | 0, 3, 0 | ? |
| `0xA0–0xAB` | | EC firmware version | `17H3EMS1.102` | C |
| `0xAC–0xB3` / `0xB4–0xBB` | | EC build date / time | `05192020` / `09:52:11` | C |
| `0xE5`, `0xE6`, `0xEE` | | system values; `0xEE` is also written by the WMI event path | 0, 0, 0 | ? |
| **Device block (AB)** | | | | |
| `0x2B`, `0x2C` | HSTT = `0x2C` bit 0 | device flags (`0x2C` is keyboard-backlight mode on some siblings) | `0x80`, `0xE0` | ? |
| `0x2E` | PXCT = bit 6 | bit 1 webcam (S); bit 6 = brightness keys go to the NVIDIA display (F) | `0x4B` | S/F |
| `0x2F` | | webcam block (S) | `0x0B` | S |
| **Power block (AC)** | | | | |
| `0x30` | POWS bit 0, LIDS bit 1, KBCS bit 2 | AC present, lid open, keyboard | AC, lid open | C |
| `0xBC–0xBD` | OSC1, OSC2 | full-charge capacity reported to the OS | 5,794 mAh (= Linux `charge_full`) | C |
| **Main battery block (AD)** | | | | |
| `0x31` | MBTS, MBCS, MBDS, MBFS, MBWS, MBLS, MBCL, MBFL (bits 0–7) | present, charging, discharging, full, warning, low, critical, fault | present, full | C |
| `0x35`, `0x36` | | likely low / critical battery % | 10, 5 | ? |
| `0x38–0x4D` | MDC, MDV, MCA, MST, MCC, MPO, MFC, MCU, MRC, MVO, MTE (16-bit, little-endian) | design capacity, design voltage, alarm capacity, status flags, cycle count, charge %, full capacity, current, remaining capacity, voltage, temperature (0.1 K) | 6,250 mAh, 14,400 mV, 625 mAh, `0x00E0`, 0, 100 %, 5,886 mAh, 0 mA, 5,886 mAh, 16,609 mV, 30.8 °C | C |
| `0xE8` | | battery value | 0 | ? |
| **Second battery block (AE)** | | | | |
| `0x32`, `0x50–0x67`, `0xE9` | SDC … STE | same layout for a second battery (not fitted) | zeros | F |
| **CPU fan block (AF)** | | | | |
| `0x33` | | fan control flags (shared with the GPU block) | `0x0D` | ? |
| `0x68` | CPUT | CPU temperature °C | 48 | C |
| `0x69` | | ? | 0 | ? |
| `0x6A–0x6F` | | CPU fan thresholds °C | 50 61 68 75 80 90 | C |
| `0x70` | | CPU critical temperature (matches TjMax 100 °C) | 100 | F |
| `0x71` | | CPU fan level % now | 40 | C |
| `0x72–0x78` | | CPU fan curve % | 20 40 50 60 65 65 65 | C |
| `0x79` | | 8th curve slot (unused) | 0 | F |
| `0x7A–0x7F` | RES1 = `0x7E` bits 0–2 | one value per threshold; perhaps step-down hysteresis °C | 4 5 3 4 3 5 | ? |
| **GPU fan block (AG)** | | | | |
| `0x80` | SYST | GPU temperature °C | 46 | C |
| `0x81` | | ? | 0 | ? |
| `0x82–0x87` | | GPU fan thresholds °C | 50 61 69 74 80 88 | C |
| `0x88` | | GPU critical temperature (matches the GPU's 97 °C slowdown) | 97 | F |
| `0x89` | | GPU fan level % now | 20 | C |
| `0x8A–0x90` | | GPU fan curve % | 20 30 45 60 65 65 65 | C |
| `0x91–0x97` | | like `0x79–0x7F` for the GPU | 0, 4 5 4 4 3 3 | ? |
| **Settings block (AH)** | | | | |
| `0xE0`, `0xE3` | CPUC = `0xE3` | ? | `0xE2`, 200 | ? |
| `0xE4` | TE40 (bits 0–2), TURB (bit 1), TE43 (bits 3–7) | turbo flag and mode fields | 0, 2 | ? |
| `0xE7`, `0xF0` | | ? | 0, 0 | ? |
| `0xF1` | BRLV | screen brightness level | 0 | F |
| `0xF2` | SYSM (bits 0–5) | shift mode / user scenario | `0x80` | S |
| `0xF3` | | keyboard backlight on siblings; unused here | 0 | S |
| `0xF4` | | fan mode | `0x8C` | C |
| `0xF5–0xFC` | | ? | 0 5 0 1 10 50 0 0 | ? |
| `0xFD` | EDID (bits 0–3) | panel EDID / display select | 3 | F |
| `0xFE`, `0xFF` | DCBL, ACBL | backlight on battery / on AC | 0, 0 | F |
| **Fans block (AI)** | | | | |
| `0xC8–0xC9` | | tach period, 4th fan slot; reads about the CPU fan's speed. Earlier ramp tests didn't confirm it | 2,438 rpm | ? |
| `0xCA–0xCB` | | GPU fan tach period (rpm = 478000 / value) | 1,197 rpm | C |
| `0xCC–0xCD` | | CPU fan tach period | 2,414 rpm | C |
| `0xCE–0xCF` | ADP = `0xCF` bit 0, CHG = bit 2 | 4th tach slot; adapter and charging bits | 0 | ? |
| **Not in any WMI block** | | | | |
| `0x34` | OSVR (bits 4–7) | OS version reported to the EC | 5 | F |
| `0x98` | | Cooler Boost = bit 7 (S) | `0x02` | S |
| `0x9A–0x9D` | | ? | `0x64 0x02 0x00 0x6E` | ? |
| `0xBE` | EC6I, EC6O, FBST, ESGI, ESGO, ESGN, DTDR | EC flags | `0x40` | ? |
| `0xBF` | RECY bit 0, RSUS bit 1 | bit 4 Fn/Win swap (S) | `0x10` | S |
| `0xC5` | OPTS | ? | `0x9E` | ? |
| `0xD0`, `0xD5` | PCMD, PSNM | CPU performance-state limit the EC can impose (EC event 0x81 copies it to the CPU's `_PPC`) | 0, 0 | F |
| `0xD7–0xDA`, `0xDD` | WDT, TSIT, TSTU, TSTL, BCLR | watchdog and thermal-sensor fields | | F |
| `0xED` | SCIC | number of the last EC event (each event handler writes it) | `0x9A` | C |

## EC events (ACPI queries)

| Event | Firmware action |
|---|---|
| `0x81`, `0x82` | CPU performance-state limit (from `0xD5`) / remove limit |
| `0x83` | charger plugged or unplugged |
| `0x84` | lid |
| `0x85`, `0x8A` | thermal-zone update |
| `0x87` | battery status change |
| `0xB7`, `0xB8` | brightness up / down |
| `0xB9` | clears `0x2C` bit 0 |
| `0xC0` | MSI WMI event (MSI Center) |
| `0xD1–0xD5` | NVIDIA GPU performance level (D1 = no limit … D5). Sent twice after every host read |
| `0x70 0x74 0x77 0x78 0x80 0x86 0x88 0x89 0x8B 0x8C 0x90 0x91 0xB4 0xB5 0xB6 0xBD` | record their number only (`0xB4` also waits 0.75 s) |

## Verifying the S and ? entries

- **Hotkeys (from Linux):** watch for EC changes while pressing Fn+F6 (webcam), the Cooler Boost key and
  Fn+Win. Each full read costs about 500 ACPI events, which is fine for a short session.
- **MSI Center settings (from Windows):** change one setting, warm-reboot into Linux and compare a
  snapshot. EC RAM usually survives a warm reboot, but the BIOS may reinitialize some registers, so
  an unchanged value is not proof.
