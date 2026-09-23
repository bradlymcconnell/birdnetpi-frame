# AvianVisitors E-Ink Frame (`birdnetpi-frame`)

Complete configuration, patched drivers, automation scripts, and deployment instructions for the **13.3" Pimoroni Inky Impression (Spectra 6)** AvianVisitors smart digital frame driven by a **Raspberry Pi Zero 2 W**.

---

## Hardware Specifications

* **Display:** Pimoroni Inky Impression 13.3" Spectra 6 (`el133uf1`)
* **Resolution:** $1600 \times 1200$ pixels (4:3 aspect ratio, 150 PPI)
* **Controller:** Raspberry Pi Zero 2 W (512MB RAM, 64-bit OS)
* **Color Capability:** 6 Physical E-Ink Pigments (Black, White, Red, Yellow, Blue, Green)
* **Refresh Timing:** ~42.15 seconds full charge-pump wave cycle

### Pinout Mapping (Dual-Controller Bank Architecture)

The 13.3" Spectra 6 panel drives its $1600 \times 1200$ resolution in two $600 \times 1600$ vertical banks using software GPIO chip selects:

| Signal | BCM GPIO Pin | Header Pin | Description |
| :--- | :--- | :--- | :--- |
| **CS0** | **GPIO 26** | Pin 37 | Left bank chip select (Active Low) |
| **CS1** | **GPIO 16** | Pin 36 | Right bank chip select (Active Low) |
| **DC** | **GPIO 22** | Pin 15 | Data / Command select |
| **RST** | **GPIO 27** | Pin 13 | Hardware display reset |
| **BUSY** | **GPIO 17** | Pin 11 | Busy wait pin (Active Low during refresh) |
| **MOSI** | **GPIO 10** | Pin 19 | SPI Data Out |
| **SCLK** | **GPIO 11** | Pin 23 | SPI Clock |

---

## Applied Patches & Enhancements

1. **4KB SPI DMA Chunking (`patched_source/inky_el133uf1.py`):**
   * Eliminates Linux kernel DMA buffer exhaustion (`[Errno 12] Cannot allocate memory`) on 512MB Pi Zero 2 W by converting NumPy image slices to standard Python lists chunked in 4096-byte transfers.
2. **Daily Reset & Day-Rollover Refresh (`patched_source/display.py`):**
   * Implements true calendar-date filtering (`hours = "today"`).
   * Stores `last_date` in `state.json` and automatically triggers a redraw at dawn to reset the frame with the empty nest title card and accumulate birds throughout the day.
3. **Direct Species Pass-Through:**
   * Passes the fetched species array directly into the Chromium screenshot renderer, eliminating network timeout races on the Pi Zero 2 W.
4. **Memory Management (`gc.collect()`):**
   * Reclaims Chromium browser memory prior to executing the 42-second SPI push.
5. **Layout & Scaling Tuning (`COLLAGE_FRAC = 0.85`, `opening = 0.95`, `mat = -0.10`):**
   * Expands the collage by +28% over stock to fill the entire 13.3" glass viewport.
6. **Vibrance & Contrast Enhancement:**
   * Applies PIL `ImageEnhance` (+40% saturation, +20% contrast, +30% sharpness) and pure-white pixel clamping for museum-quality e-ink presentation.
7. **Device Tree Overlay Configuration (`boot/config.txt`):**
   * Sets `dtoverlay=spi0-0cs` to prevent Linux kernel hardware CS conflicts with software GPIOs.

---

## Repository Structure

```text
.
├── config/
│   └── .birdframe/
│       └── config.toml       # Production configuration file
├── scripts/
│   ├── patch-frame.sh       # Master automated re-patching script
│   └── backup-frame.sh      # Automated full system backup script
├── patched_source/
│   ├── display.py           # Patched main frame runner
│   ├── shoot.py             # Patched Playwright headless screenshot shooter
│   ├── inky_el133uf1.py     # Patched 4KB-chunked Inky 13.3" hardware driver
│   └── birdframe-names      # CLI script to toggle bird names on/off
├── systemd/
│   ├── birdframe.service    # Oneshot frame update service
│   └── birdframe.timer      # Scheduled refresh timer
├── boot/
│   ├── config.txt           # Required Raspberry Pi boot configuration
│   ├── cmdline.txt          # Kernel boot line
│   └── hosts                # Local IP DNS resolution map
└── fonts/
    └── Caveat.ttf           # Handwritten label font
```

---

## Quick Installation & Disaster Recovery (From Scratch)

### Option A: One-Line Automated Installer (Recommended)
After flashing a fresh Raspberry Pi OS and running the initial AvianVisitors setup, run this single command to apply all patches, install fonts, deploy configs, and enable systemd timers:

```bash
curl -sSL https://raw.githubusercontent.com/bradlymcconnell/birdnetpi-frame/main/install.sh | bash
```

---

### Option B: Manual Git Clone & Setup
1. **Initial setup on Pi Zero 2 W:**
   ```bash
   ssh birder@birdnetframe1.local
   sudo apt update && sudo apt install -y git python3-pip python3-venv
   git clone https://github.com/Twarner491/AvianVisitors
   cd AvianVisitors/frame && ./install.sh
   ```

2. **Deploy custom patches from this repository:**
   ```bash
   git clone https://github.com/bradlymcconnell/birdnetpi-frame.git
   cd birdnetpi-frame && ./install.sh
   ```

---

## Operational Commands

### Manually Forcing a Refresh
```bash
rm -f ~/.birdframe/state.json
/home/birder/AvianVisitors/frame/.venv/bin/python /home/birder/AvianVisitors/frame/display.py --config /home/birder/.birdframe/config.toml
```

### Toggle Bird Names On/Off
```bash
/home/birder/AvianVisitors/frame/birdframe-names on
/home/birder/AvianVisitors/frame/birdframe-names off
```

### Checking Logs & Timers
```bash
journalctl -u birdframe.service -n 50 --no-pager
systemctl status birdframe.timer
```

---

## License

Derived from [AvianVisitors](https://github.com/Twarner491/AvianVisitors) and [Pimoroni inky](https://github.com/pimoroni/inky).
