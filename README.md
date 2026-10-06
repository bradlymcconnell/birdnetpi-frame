# AvianVisitors E-Ink Frame (`birdnetpi-frame`)

Complete configuration, patched drivers, automation scripts, pre-rendered asset libraries, and deployment instructions for the **13.3" Pimoroni Inky Impression (Spectra 6)** AvianVisitors smart digital frame driven by a **Raspberry Pi Zero 2 W**.

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

## Core Features & Applied Patches

### 1. Tri-Mode Art Style System
* **`style = "vintage"` (1800s Natural History Lithograph Mode):**
  * Displays 19th-century European naturalist bookplates (Audubon / Gould / von Wright) from a library of 1,215 species cutouts in `assets/vintage/`.
  * Paired with classical **EB Garamond Italic** (`EBGaramond-Italic.ttf`) and **Libre Baskerville** bookplate typography.
  * Powered by a sub-second vectorized 2D alpha mask collision engine with 5px morphological dilation (`ImageFilter.MaxFilter(5)`) for zero feather or label overlaps.
* **`style = "sketch"` (Japanese Woodblock & Audubon Mode):**
  * Classic vintage Japanese *Kachō-e* woodblock prints and natural history field plates using the authentic AvianVisitors browser engine.
  * Paired with flowing **Caveat** (`Caveat.ttf`) cursive naturalist script with curved tangent labels hugging each bird's contour.
  * Includes flight poses (e.g. wings-spread *Pileated Woodpecker*, hovering *Tufted Titmouse*).
* **`style = "cartoon"` (Species Caricature Mode):**
  * Displays round, plump bird caricatures perched on natural twigs with authentic species-specific plumage.
  * Paired with textured **Finger Paint** (`FingerPaint-Regular.ttf`) typography for a storybook chalkboard feel.
  * Uses 138 pre-rendered local cutouts with automatic pose fallback.

### 2. Unified Detection Count & Flock Sizing Hierarchy
All modes share identical scaling mathematics:
* **$count^{0.22}$ Exponent**: Scales each species' area proportionally with detection count so hero birds stand out without dwarfing other species.
* **15% Rare-Bird Floor (`shoot_small_floor = 0.15`)**: Prevents rare or single-call visitors from shrinking below recognition.
* **Dynamic $n$-Species Budget**: Automatically expands bird sizes when few species are heard (e.g. morning) and gracefully tightens as more species arrive.

### 3. Automatic Typography Pairing Engine
* Setting `font = "auto"` automatically selects the ideal font for the active style (`vintage` $\rightarrow$ EB Garamond, `cartoon` $\rightarrow$ Finger Paint, `sketch` $\rightarrow$ Caveat).
* Full typography suite included:
  * `EBGaramond-Italic.ttf` (1800s Lithograph Monograph Serif)
  * `LibreBaskerville-Italic.ttf` (Bookplate Serif)
  * `Caveat.ttf` (Handwritten Cursive)
  * `FingerPaint-Regular.ttf` (Crayon / Finger Paint)
  * `LuckiestGuy-Regular.ttf` (Bold Comic / Arcade)
  * `PatrickHand-Regular.ttf` (Marker / Classroom)
  * `GloriaHallelujah-Regular.ttf` (Casual Blackboard)
  * `GochiHand-Regular.ttf` (Cute Rounded Script)

### 4. Home Assistant Real-Time Integration & REST API
* **`birdframe-api.service`** runs an asynchronous HTTP server and real-time state synchronizer on port `8088`.
* Bi-directionally syncs with Home Assistant entities:
  * `input_select.birdframe_art_style`: Seamlessly switches between `Vintage`, `Sketch`, and `Cartoon`.
  * `input_button.birdframe_refresh_display`: Triggers an immediate e-ink hardware redraw.
* **REST Endpoints:**
  * `GET /api/status`: Returns current style, active font, and station reachability.
  * `GET /api/set?style=vintage` / `GET /api/set?style=sketch` / `GET /api/set?style=cartoon`: Sets style and triggers refresh.
  * `GET /api/toggle`: Cycles style (`vintage` $\rightarrow$ `sketch` $\rightarrow$ `cartoon` $\rightarrow$ `vintage`) and refreshes.
  * `GET /api/refresh`: Forces instant screen redraw.

### 5. 4KB SPI DMA Chunking (`patched_source/inky_el133uf1.py`)
* Eliminates Linux kernel DMA buffer exhaustion (`[Errno 12] Cannot allocate memory`) on 512MB Pi Zero 2 W by chunking transfers into 4096-byte slices.

### 6. Daily Calendar Reset & Display Clamping
* Implements true calendar-date filtering (`hours = "today"`).
* Stores `last_date` in `state.json` and resets display to empty nest at dawn.
* Applies PIL `ImageEnhance` (+40% saturation, +20% contrast, +30% sharpness) and pure-white pixel clamping for museum-grade e-ink presentation.

---

## Repository Structure

```text
.
├── config/
│   └── .birdframe/
│       └── config.toml           # Production configuration template
├── scripts/
│   ├── birdframe-api.py          # Real-time Home Assistant sync & REST API daemon
│   ├── patch-frame.sh           # Master automated driver & app patcher
│   └── backup-frame.sh          # Automated full system backup script
├── patched_source/
│   ├── display.py               # Patched main runner (style routing & auto font)
│   ├── shoot.py                 # Patched Playwright renderer & route interceptor
│   └── inky_el133uf1.py         # Patched 4KB-chunked Inky 13.3" hardware driver
├── systemd/
│   ├── birdframe.service        # Oneshot frame update service
│   ├── birdframe.timer          # Scheduled refresh timer
│   └── birdframe-api.service    # Background API daemon systemd unit
├── assets/
│   ├── vintage/                 # 1,215 species antique lithograph cutouts
│   └── cartoon/                 # 138 species transparent cartoon cutouts
├── fonts/                       # Complete typography suite (.ttf)
├── boot/
│   ├── config.txt               # Required Raspberry Pi boot configuration
│   ├── cmdline.txt              # Kernel boot line
│   └── hosts                    # Local IP DNS resolution map
└── install.sh                   # Master deployment and recovery script
```

---

## Quick Installation & Deployment

### Option A: One-Line Automated Installer
Run this single command on the Pi Zero 2 W to apply all patches, install fonts, deploy cartoon assets, configure services, and start the Home Assistant API:

```bash
curl -sSL https://raw.githubusercontent.com/bradlymcconnell/birdnetpi-frame/main/install.sh | bash
```

---

### Option B: Manual Git Clone & Setup
```bash
git clone https://github.com/bradlymcconnell/birdnetpi-frame.git
cd birdnetpi-frame && ./install.sh
```

---

## Home Assistant Setup

1. Create the helper entities in Home Assistant:
   * **Toggle Switch:** `input_boolean.birdframe_cartoon_mode` (Name: "BirdFrame Cartoon Mode")
   * **Button:** `input_button.birdframe_refresh_display` (Name: "BirdFrame Refresh Display")

2. Add the Entities Card to your dashboard (e.g., Lovelace):
```yaml
type: entities
title: 🐦 AvianVisitors Frame
icon: mdi:palette-outline
entities:
  - entity: input_boolean.birdframe_cartoon_mode
    name: Cartoon Art Style
  - entity: input_button.birdframe_refresh_display
    name: Refresh Display
```

3. Save a Long-Lived Access Token in `~/.birdframe/ha_token` on the frame Pi.

---

## Operational Commands

### Manually Triggering Screen Refresh
```bash
rm -f ~/.birdframe/state.json
/home/birder/AvianVisitors/frame/.venv/bin/python /home/birder/AvianVisitors/frame/display.py --config /home/birder/.birdframe/config.toml --force
```

### Checking Daemon & Timer Logs
```bash
journalctl -u birdframe-api.service -f
journalctl -u birdframe.service -n 50 --no-pager
systemctl status birdframe.timer
```

---

## License

Derived from [AvianVisitors](https://github.com/Twarner491/AvianVisitors) and [Pimoroni inky](https://github.com/pimoroni/inky).
