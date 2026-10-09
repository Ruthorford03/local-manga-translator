# Local Manga Translator · 本機漫畫翻譯

[繁體中文](README.md) | [English](README.en.md)

[![Source and offline tests](https://github.com/Ruthorford03/local-manga-translator/actions/workflows/checks.yml/badge.svg)](https://github.com/Ruthorford03/local-manga-translator/actions/workflows/checks.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
![Platform: Windows](https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows&logoColor=white)
![Python: 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)

An offline Windows manga translation pipeline that converts Japanese manga image folders into Traditional Chinese while preserving original images, full audit ledgers, checkpoint resume data, and human-in-the-loop review tools.

This repository represents the **2026-10-08 source preview**. The core pipeline orchestrates: Comic Text Detector (CTD) → Manga OCR → Magi reading-order candidates → Local Sakura translation (via Ollama) → OpenCC Traditional Chinese normalization → Inpainting & Adaptive Typesetting. Once models and environments are set up, translation runs locally on CPU/GPU without external API dependencies.

---

## 🌟 Core Highlights: Adaptive Typesetting & Bubble Guard

Conventional automated manga translators typically squeeze translated strings into bounding boxes using rigid font scaling. This often leads to text overflowing speech bubbles, harsh downscaling, unnatural multi-column breaks, awkward empty space, or flipped vertical/horizontal directions. 

This project integrates a purpose-built **Adaptive Layout Engine** and an **Ink-Level Bubble Guard System**:

### 1. 📐 Adaptive Geometric Layout Solver (`sakura_layout.py`)
- **Dynamic Multi-Column Solver**: Dynamically analyzes the aspect ratio of each speech bubble, exhaustively evaluating CJK glyph arrangements across 1 to N columns/rows to find the optimal balance between maximum legible font size and harmonious column count.
- **Tall Bubble Fill Balance**: For tall vertical speech bubbles (aspect ratio $\ge 1.4$), a fill-ratio penalty is applied (`(fill_ratio / 0.55) ** 0.85`). This prevents the algorithm from arbitrarily breaking text into too many short columns and leaving the bottom half of the bubble awkwardly empty.
- **Orphan Suppression Penalty**: Automatically penalizes trailing orphan characters or isolated punctuation marks in multi-column layouts, preserving clean block typography.
- **Symmetric Auto-Centering**: Calculates actual rendered text bounding dimensions and applies geometric offset centering ($y_1 \leftarrow y_1 + \frac{\text{height} - \text{actual\_h}}{2}$), eliminating one-sided margins and ensuring balanced top/bottom whitespace.
- **CJK Grapheme Protection**: Full support for Unicode combining characters, emoji sequences, and zero-width joiners (ZWJ). Enforces strict text identity validation before and after layout to guarantee zero dropped characters or unintended alterations.

### 2. 📏 Resolution-Adaptive Scaling
- Dynamically derives scale factors against a standard manga resolution benchmark (short side 1050 px).
- **Dynamic Cap Unlocking for Emphasis**: For extra-large bubbles or dramatic shout dialogues, the font size ceiling dynamically scales up (thresholds at $100\text{ px} \sim 150\text{ px} \times \text{Scale}$), preventing impactful lines from being artificially clamped to small text. Simultaneously enforces minimum font size limits for small side-notes.

### 3. 👁️ Dynamic Optical Spacing (Typography)
- Spacing adapts dynamically based on optical typography principles:
  - **Small text (< 18 pt)**: Tighter character ratio (1.08) and letter spacing (1.00) to keep strokes compact and cohesive without sparse gaps.
  - **Medium text (18 ~ 30 pt)**: Balanced spacing (char ratio 1.12, letter spacing 1.03) for effortless reading.
  - **Large text (> 30 pt)**: Relaxed spacing (char ratio 1.16, letter spacing 1.05) to prevent dense ink strokes from colliding.
  - **Adaptive Line Spacing**: Tight for single columns (1.00) and naturally expanded for multi-column vertical (1.22) or horizontal (1.20) text blocks.

### 4. 🛡️ Qt Ink-Level Bubble Guard (`bubble_guard.py`)
- **True Raster Ink Extraction**: Unlike tools that only check rectangular bounding boxes, Bubble Guard renders the text layer onto an offscreen Qt canvas to extract true alpha ink masks ($\text{alpha} > 32$), measuring visible stroke pixels at true 1-pixel fidelity.
- **Closed White Bubble Detection**: Employs connected-component analysis to identify reliable bubble boundaries while filtering out complex backgrounds.
- **Three-Stage Minimal Invasive Repair**: When rendered ink crosses the bubble border, **only the offending bubble is repaired—all other intact bubbles on the page remain 100% untouched**:
  1. *Step 1 (Preferred)*: Geometric shifting while preserving the original optimal font size.
  2. *Step 2*: In-bubble re-wrapping and column reorganization.
  3. *Step 3*: Controlled, bounded font reduction.
  - If candidate repairs fail to eliminate ink spillover, the system executes a safe rollback, retains the original render, and flags the item for manual review (`needs_review`).
- **Edge & Margin Caption Handling**: Dedicated boundary detection for bubbles clipped at page edges and narrow margin captions running alongside panel lines.

### 5. 🧭 Context-Aware Direction Fitting (`source_direction.py`, `source_layout.py`)
- Single characters, exclamation marks, and short numbers often have an aspect ratio close to 1:1, making geometric direction detection ambiguous.
- By cross-referencing Magi character grouping evidence and neighboring long-sentence orientation within the same bubble, the system automatically resolves ambiguous short blocks to match the intended reading direction.

### 6. 🔍 Adaptive OCR Crop Expansion (`ocr_crop_review.py`)
- For OCR boxes that truncate glyph strokes at the border, the system expands the box based on connected ink analysis across dual thresholds. Source text is only updated if dual passes (unpadded vs. 2px white-padded) yield identical, non-empty OCR outputs.

### 7. 📚 Global Terminology & Entity Consistency (`terminology_harmonizer.py`)
- **Custom Glossary Prompt Injection**: Automatically loads `glossary.json` from the source or work directory and injects matched character names and recurring terminology directly into Sakura translation prompts.
- **Whole-Volume Terminology Audit & Alignment**: Automatically scans Katakana named entities and recurring nouns across the entire volume upon completion, producing `terminology-audit.json`. Supports majority-vote alignment and batch replacement across ledgers and project files to ensure 100% naming consistency from first page to last.

---

## 🚀 Key Features & Pipeline Capabilities

- **Folder Batch Processing**: Select any manga image folder. Processes pages in natural numerical order while leaving source files completely untouched; generates a separate Chinese output directory.
- **Format Support**: Supports static images (JPG, PNG, WebP, BMP). Split PDFs, CBZs, and ZIPs into images before processing.
- **Reliable Checkpoints & Resume**: Logs source SHA-256 hashes, OCR outputs, raw model responses, and rendered mappings. Safe to stop and resume at any time.
- **Failure Recovery**: Automatically handles model timeouts or incomplete outputs. Line-count misalignment initiates controlled single-box recovery before queuing for review.
- **Dual-Track Review & WYSIWYG Inspector**:
  - *Box-by-Box Review*: Side-by-side Japanese original vs. Chinese preview with bounding-box zoom.
  - *SFX Mask Restore*: Selectively restores hand-drawn sound effects using verified audit masks.
  - *WYSIWYG Single-Page Inspector*: Interactive re-rendering with instant preview, `Ctrl+S` quick save, font resizing, direction toggling, and bubble resizing.

> [!NOTE]
> AI models, OCR, and Magi ordering can make mistakes. Producing an output PNG and passing line-count checks does not guarantee semantic perfection. Always inspect critical pages using the provided review tools.

---

## 💻 Tested Hardware & Environment

Benchmarked on actual development hardware (re-verified 2026-10-08):

| Component | Specification |
| --- | --- |
| Laptop Model | LG Electronics `16Z90TS-G.AU89C2` |
| CPU | Intel Core Ultra 7 258V (8 cores / 8 threads) |
| GPU | Intel Arc 140V Integrated GPU (Ollama via Vulkan backend) |
| Memory | 32 GB class (Windows reports ~31.54 GiB physical RAM) |
| OS | Windows 11 Pro x64 (`10.0.26200`) |
| Inference Split | CTD / OCR / Magi: CPU; Sakura: Ollama + Intel GPU |

*Note: Arc "16GB" indicates shared GPU memory and cannot be added on top of the 32 GB system RAM. This reflects the test environment, not a minimum requirement or support guarantee for other configurations.*

---

## 🛠️ Quick Start & Installation

1. Requires Windows x64, Python 3.12 x64, and Ollama. Review [Third-Party Notices](docs/THIRD_PARTY_NOTICES.md) before proceeding.
2. Clone repository:
   ```powershell
   git clone https://github.com/Ruthorford03/local-manga-translator.git
   cd local-manga-translator
   ```
3. Follow [Setup Guide](docs/SETUP.zh-TW.md) to initialize the two isolated Python virtual environments:
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1 -Python python
   ```
4. Download required models (listed in [MODELS.json](docs/MODELS.json), total ~14.9 GB) and import Sakura into Ollama.
5. Run the preflight check:
   ```powershell
   python scripts/preflight.py
   ```
6. Launch the translator by double-clicking `Start_Manga_Folder_Translator.cmd`. Select your image folder and click **Start Translation**.

---

## 📚 Documentation Index

| Topic | Document |
| --- | --- |
| Tested Hardware, Versions & Resource Bounds | [HARDWARE.zh-TW.md](docs/HARDWARE.zh-TW.md) |
| Clean Setup, Model Checksums & Ollama Import | [SETUP.zh-TW.md](docs/SETUP.zh-TW.md) |
| User Interface, Resuming, Review & Troubleshooting | [USAGE.zh-TW.md](docs/USAGE.zh-TW.md) |
| Pipeline Architecture, Data Flow & Failure Boundaries | [ARCHITECTURE.zh-TW.md](docs/ARCHITECTURE.zh-TW.md) |
| Engineering History & 20 Case Studies | [DEVELOPMENT_HISTORY.zh-TW.md](docs/DEVELOPMENT_HISTORY.zh-TW.md) |
| Release Verification Scope & Offline Tests | [VALIDATION.zh-TW.md](docs/VALIDATION.zh-TW.md) |
| Upstream Licenses, Versions & Modifications | [THIRD_PARTY_NOTICES.md](docs/THIRD_PARTY_NOTICES.md) |
| Release Notes & Changelogs | [RELEASE_NOTES.md](docs/RELEASE_NOTES.md) |
| Future Roadmap & Optimization Tracking | [ROADMAP.md](ROADMAP.md) |
| Model Download URLs, Sizes & SHA-256 Checksums | [MODELS.json](docs/MODELS.json) |

---

## ⚖️ Scope & License

- Code is released under **GPL-3.0-or-later** (see [LICENSE](LICENSE)), retaining upstream notices from BallonsTranslator and respective authors.
- Model weights, fonts, and comic artwork carry their own respective licenses (Sakura and Magi include non-commercial restrictions). This repository is not an official release of BallonsTranslator, Magi, or Sakura.
- No model weights, API keys, private user settings, or comic artwork are distributed in this repository.
