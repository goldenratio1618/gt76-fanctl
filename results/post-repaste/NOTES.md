# Repaste notes (2026-11-15)

CPU and GPU repaste of the MSI GT76 Titan DT 10SGS, first time. Results: GPU 87 → 75 °C under a
5-minute load and now limited by power instead of temperature (1,545 → 1,805 MHz); CPU at 4.0 GHz
87 → 80 °C after 2 minutes. Data in this folder; baseline in `../pre-repaste/`.

## What was used

- **GPU:** Thermal Grizzly PhaseSheet PTM, cut from a 50 × 40 mm sheet.
- **CPU:** Arctic MX-7 (X pattern). The PhaseSheet tore while peeling its second film.
- **Pads:** two stacked layers of Arctic TP-4 0.5 mm (1.0 mm total) on the six chokes and six
  power stages above the CPU, and on the Linux NVMe SSD. The originals were light green and
  measured 0.7–0.9 mm, compressed after years in use.

## For next time

- **Buy 1 mm TP-4.** The 0.5 mm sheet tore easily, mostly while peeling its backing, and the whole
  sheet was used up replacing the pads the CPU cooler pulled off. The SSD pad ended up patched.
- **Expect the CPU cooler to rip the power-stage pads.** Have replacement pads ready.
- **PhaseSheet:** the first film peels easily; the second film pulls the pad with it. The GPU piece
  had to be cut twice (the first was too small). Cut generously and have MX-7 as a fallback.
- **A kitchen knife cut the film better than the hobby knife.** Bring a ruler with millimeters.
- **The battery connector is very tight.** Removing it took several plastic tools and broke off a
  small piece of the housing. It still holds and the battery charges normally.
- **Wear the wrist strap on the non-watch hand.** It came unclipped while cutting.
- **Disconnecting the battery reset the BIOS.** Before booting, set the storage mode back to RAID
  (Intel RST): until then it reboots repeatedly without a picture. The hardware clock also reset.
