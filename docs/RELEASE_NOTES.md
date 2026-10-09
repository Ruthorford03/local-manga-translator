# Release Notes · 版本發布紀錄

## v0.3.1-preview (2026-10-09)

### 繁體中文

這是專為 Windows 本機設計的日文漫畫轉繁體中文翻譯流水線 **v0.3.1-preview 原始碼預覽版**。整合 Comic Text Detector、Manga OCR、Magi 閱讀順序辨識、本機 Sakura 14B 大模型（經 Ollama 支援 GPU 推論）、OpenCC 繁體正規化，以及自研的自適應排版與防壓框防護引擎。

#### 🌟 核心技術亮點
1. **氣泡幾何自適應排版求解 (`sakura_layout.py`)**：
   - 動態窮舉 1~N 欄 CJK 字符組合，自動求解最佳行列數與最大適配字級。
   - **高長型氣泡空間填充平衡**：長寬比 $\ge 1.4$ 導入空間利用懲罰機制，防止對話腰斬切短列而留下底部大半空白。
   - **孤字成行抑制**：防止段落末尾單一字元或標點符號單獨落單成行。
   - **垂直幾何置中**：動態位移補償，實現上下均勻留白對稱。
2. **漫畫解析度動態適配與大字級解鎖**：
   - 依 1050px 基準動態縮放；大面積吶喊對話框動態解鎖字級上限（最高達 150px × Scale），避免大字被鎖死在小字級。
3. **字體排印學動態光學間距**：
   - 小字（<18pt）光學收緊防止渙散、中字（18~30pt）自然舒適、大字（>30pt）舒展防筆劃濃黑黏連；直排與橫排自適應行距。
4. **Qt 字形墨跡級氣泡防壓框守護 (`bubble_guard.py`)**：
   - Qt 離屏渲染提取真實 Alpha 墨跡像素（$\text{alpha} > 32$），單一像素級輪廓比對。
   - 連通域白色氣泡邊界分析。
   - 三階最小侵入修復：微幅幾何移位 $\to$ 框內重排換行 $\to$ 有限度縮字 $\to$ 安全回退。**只修問題氣泡，整頁其他氣泡零改動**。
5. **上下文感知文字方向適配**：
   - 結合同氣泡長句子證據與 Magi 角色連線，自適應校正長寬比接近 1:1 的方形單字/短詞方向。
6. **工程可靠性與資料保護**：
   - 全程 SHA-256 雜湊帳冊，絕不修改或覆蓋原始圖片。
   - 支援斷點續跑、行數對齊檢驗與逐框重試救援。
   - 雙軌審查：待審逐框確認、音效局部遮罩還原、所見即所得單頁精修工作臺（Ctrl+S 快速保存）。

---

### English

The **v0.3.1-preview** release of Local Manga Translator brings an offline Windows translation pipeline for Japanese manga with a heavy emphasis on typography and reliability engineering.

#### 🌟 Key Features
- **Adaptive Geometric Layout Solver**: Exhaustively evaluates CJK glyphs in 1 to N columns, dynamically optimizing column count and font size.
- **Tall Bubble Fill Balance**: Prevents tall bubbles (aspect ratio $\ge 1.4$) from being awkwardly broken into short columns with trailing whitespace.
- **Orphan Suppression & Auto-Centering**: Eliminates dangling punctuation/single characters and centers text blocks vertically.
- **Resolution-Adaptive Scaling**: Dynamically unlocks large font ceilings for shout bubbles on high-resolution manga.
- **Dynamic Optical Spacing**: Applies typography rules (tight for small text, relaxed for large text).
- **Qt Ink-Level Bubble Guard**: Renders offscreen alpha text masks to detect true stroke collisions with bubble boundaries. Employs minimal-invasive 3-stage repair (*shift → re-wrap → shrink → safe fallback*). Only affects offending bubbles.
- **Context-Aware Direction**: Resolves ambiguous 1:1 square blocks (single characters/exclamation marks) via Magi character grouping context.
- **Audit Ledger & Review Tools**: SHA-256 integrity protection, interrupt-safe resume, and interactive WYSIWYG single-page inspector.
