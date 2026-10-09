# 本機漫畫翻譯 · Local Manga Translator

[繁體中文](README.md) | [English](README.en.md)

[![Source and offline tests](https://github.com/Ruthorford03/local-manga-translator/actions/workflows/checks.yml/badge.svg)](https://github.com/Ruthorford03/local-manga-translator/actions/workflows/checks.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
![Platform: Windows](https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows&logoColor=white)
![Python: 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)

在 Windows 上，把日文漫畫圖片資料夾轉成繁體中文，並保留原圖、翻譯紀錄、續跑資料與人工審查入口。

這是從實際使用環境整理出的 **2026-10-08 原始碼預覽版**。主要流程為 CTD 文字偵測 → Manga OCR → Magi 閱讀順序候選 → 本機 Sakura 翻譯 → OpenCC 繁體轉換 → 去字與自適應排版。專案深度整合自研的**自適應排版引擎（Adaptive Layout Engine）**與**字形墨跡級氣泡防壓框系統（Bubble Guard）**，解決常見機器翻譯排版中「字體呆板縮小」、「文字壓到氣泡邊線」、「長對話框腰斬留白」、「方形單字直橫排顛倒」等美觀與閱讀體驗痛點。模型與 Python 套件準備好後，預設翻譯使用本機 CPU／GPU；首次安裝和下載需要網路。

## 🌟 核心亮點：自研自適應排版與智慧氣泡守護

傳統漫畫自動翻譯多半只將譯文以固定字級或單純縮放塞入外接矩形（Bounding Box），常導致排版失真、文字踩線或過度留白。本專案實作了一套專為漫畫對白設計的自適應排版演算法與像素級防壓框防護：

### 1. 📐 氣泡幾何自適應求解（Adaptive Geometric Layout）
- **動態多欄最佳化求解**：針對每個對話框的長寬幾何，自適應窮舉 CJK 字符在 1 至 N 欄／行中的排列組合，動態評分求解出最大適配字級（Font Size）與最協調的行列數，拒絕死板固定字級。
- **高長型氣泡空間填充平衡（Tall Bubble Fill Balance）**：長型對話框（長寬比 ≥ 1.4）導入空間利用懲罰機制，防止演算法盲目切成短行而使氣泡下半部留下尷尬空白，再現漫畫原作縱向對白的飽滿視覺節奏。
- **孤字成行抑制（Orphan Penalty）**：自動抑制結尾單一字元或標點符號單獨落單成行（孤字）的現象，確保排版方正美觀。
- **垂直／水平對稱置中幾何修正**：動態計算文字排版後的實際渲染總高度，自動計算位移置中補償，徹底消除單向大片留白，上下留白對稱均勻。
- **CJK 字符完整性防護**：完整相容 Unicode 結合符、Emoji 與零寬連字（ZWJ），並在排版前後強制字元內容一致性核驗，保證排版過程「絕不掉字、絕不竄改譯文內容」。

### 2. 📏 解析度自適應動態縮放（Resolution-Adaptive Scaling）
- 以標準漫畫短邊 1050px 為基準尺度動態換算 Scale Factor。
- **大框字級上限動態解鎖**：針對特大氣泡、情緒吶喊對白動態拉高字級上限（閾值達 100px ~ 150px × Scale），不再被傳統小字級封頂限制；同時為小字提供可讀性底限保護，無論 720p 短漫還是 4K 高畫質跨頁，都能自適應呈現最適比例。

### 3. 👁️ 動態視覺光學間距（Dynamic Optical Spacing）
- 依字體排印學（Typography）原則動態計算光學間距：
  - **小字（< 18 pt）**：收緊字符間距（`char_ratio = 1.08`, `letter_spacing = 1.00`），避免小字筆劃過疏、渙散無凝聚力。
  - **中字（18 ~ 30 pt）**：舒適間距（`char_ratio = 1.12`, `letter_spacing = 1.03`），自然易讀。
  - **大字（> 30 pt）**：微幅舒展（`char_ratio = 1.16`, `letter_spacing = 1.05`），防止筆劃濃黑導致字形黏連。
  - **自適應行距**：單欄緊湊（1.00），多欄／多行自然舒展（直排 1.22、橫排 1.20），兼具緊湊度與呼吸感。

### 4. 🛡️ Qt 字形墨跡級氣泡防壓框守護（Ink-Level Bubble Guard）
- **真實字形 Alpha 墨跡提取（Raster Ink）**：突破一般工具僅比對矩形外框的侷限，直接調用 Qt 文字引擎抓取渲染圖層的真實 Alpha 墨跡像素，精準掌握每一個筆劃的實際畫素佔位。
- **封閉白色氣泡輪廓比對**：利用 OpenCV 演算法精確分析對話框封閉白底邊界。
- **三階漸進式最小侵入修復**：當偵測到文字墨跡越界壓到黑色框線時，**僅針對該觸發氣泡執行局部修復，整頁其他排版良好的氣泡原封不動**：
  1. **首選：微幅幾何移位**（保留原始最佳字級，僅微調位置）。
  2. **次選：框內重排換行**（維持字級清晰度）。
  3. **最後：有限度縮字**（受控縮小）。
  - 若候選方案無法消除越界像素，則立即觸發安全回退（Fail-safe Rollback）保留原生狀態並登記審查，絕不暴力硬改。
- **邊緣氣泡與窄旁白自適應防護**：
  - 支援被頁面邊緣截斷的「單邊跨頁邊氣泡（Clipped Bubble）」。
  - 支援緊鄰分格線的「窄頁邊旁白（Margin Caption）」，跨線時精準修正。

### 5. 🧭 上下文感知自適應文字方向（Context-Aware Direction Fitting）
- 針對單字、嘆號、數字等長寬比接近 1:1 的短區塊（幾何上極難區分直排或橫排），結合 Magi 角色連線關係與同氣泡內的長對白上下文證據，自動校正方形短詞的直排／橫排方向，消除單字方向倒錯。

### 6. 🔍 OCR 邊緣墨跡補全與雙重白邊複查（OCR Adaptive Crop Expansion）
- 針對被邊界切字的 OCR 裁切框，以連通墨跡探測雙閾值邊界，並經「無白邊」與「加 2 像素白邊」雙重一致性辨識核驗，從源頭確保日文辨識輸入完整性。

## 能做什麼與工作流

- 選一個圖片資料夾，依自然順序分批處理；保留原圖，另建中文輸出資料夾。
- 支援 JPG、PNG、WebP、BMP 等靜態單頁圖片；PDF、CBZ、ZIP 請先轉圖／解壓縮。
- 記錄來源 SHA-256、OCR、原始模型回覆與輸出對應；相容的未完成工作可續跑。
- 翻譯回覆不完整、漏行或錯位時有限重試；逐框救援結果先進待審流程。
- 提供單頁審查、音效局部還原與文字精修介面。

模型會翻錯，OCR 與 Magi 判斷也可能錯誤。完成 PNG、通過行數檢查與語意正確是不同的事；請對照原圖檢查成品。手動精修 `Ctrl+S` 的記錄同步仍有已知限制，詳見[使用說明](docs/USAGE.zh-TW.md)。

## 跑在什麼機器上

| 項目 | 本機實際配置，2026-10-08 重新讀取 |
| --- | --- |
| 筆電 | LG Electronics `16Z90TS-G.AU89C2` |
| CPU | Intel Core Ultra 7 258V，8 核心／8 執行緒 |
| GPU | Intel Arc 140V 整合式 GPU，Ollama 使用 Vulkan |
| 記憶體 | 32 GB 等級；Windows 回報實體記憶體約 31.54 GiB |
| 系統 | Windows 11 Pro x64，`10.0.26200` |
| 推論分工 | CTD／OCR／Magi：CPU；Sakura：Ollama + Intel GPU |

Arc 顯示的「16GB」為共享 GPU 記憶體資訊，不能再加到系統 32 GB 上。這是實際開發與使用配置，**不是最低需求，也不是其他硬體的支援保證**。完整 Python、套件、驅動、Ollama 版本與歷史效能限制見[硬體與執行環境](docs/HARDWARE.zh-TW.md)。

## 安裝與啟動

1. 使用 Windows x64、Python 3.12 x64 與 Ollama，先閱讀[第三方授權](docs/THIRD_PARTY_NOTICES.md)。
2. 按[安裝指南](docs/SETUP.zh-TW.md)建立兩個隔離 Python 環境、取得並驗證模型、匯入 Sakura。
3. 執行 `scripts/preflight.py`，確認環境與模型就緒。
4. 雙擊 `Start_Manga_Folder_Translator.cmd`，按「選擇資料夾…」與「開始翻譯」。

**這不是包含模型的一鍵 EXE。** 倉庫只提供來源、設定、安裝腳本和文件；模型下載清單約 14.9 GB，另需 Python 套件、Ollama 模型庫與工作圖的磁碟空間。乾淨電腦上的全新安裝及公開副本的完整模型推論尚未重新驗證；本次檢查範圍見[發布驗證](docs/VALIDATION.zh-TW.md)。

## 文件導覽

| 想了解的內容 | 文件 |
| --- | --- |
| 實機、版本、CPU／GPU 分工與效能限制 | [HARDWARE.zh-TW.md](docs/HARDWARE.zh-TW.md) |
| 從新資料夾安裝、模型驗證、Ollama 匯入 | [SETUP.zh-TW.md](docs/SETUP.zh-TW.md) |
| 按鈕操作、續跑、審查、精修與故障處理 | [USAGE.zh-TW.md](docs/USAGE.zh-TW.md) |
| 模組分工、資料流、自適應排版與失敗邊界 | [ARCHITECTURE.zh-TW.md](docs/ARCHITECTURE.zh-TW.md) |
| 開發決策、自適應防壓框演算法、修正與驗證：20 個案例 | [DEVELOPMENT_HISTORY.zh-TW.md](docs/DEVELOPMENT_HISTORY.zh-TW.md) |
| 這次公開版改了什麼、測過什麼、尚未測什麼 | [VALIDATION.zh-TW.md](docs/VALIDATION.zh-TW.md) |
| 上游來源、授權、版本與本機修改 | [THIRD_PARTY_NOTICES.md](docs/THIRD_PARTY_NOTICES.md) |
| 模型下載位置、檔案大小與 SHA-256 | [MODELS.json](docs/MODELS.json) |

開發歷程保留失敗與取捨：例如分批排程曾讓首批提早約 29%，但該次八頁總耗時反而增加約 4%；58 ms 的單頁重繪結果不能寫成穩定 60 FPS；6.5 GiB 記憶體水位也不保證永不 OOM。文件會區分歷史實測與目前程式，避免把舊版驗收當成新版保證。

## 公開範圍與貢獻

程式以 GPL-3.0-or-later 提供，完整文字見 [LICENSE](LICENSE)；保留 BallonsTranslator 及其他原作者的聲明。模型、字型與漫畫素材各有自己的授權，尤其 Sakura／Magi 的非商業限制不因程式開源而消失。本倉庫不是 BallonsTranslator、Magi 或 Sakura 的官方發行版。

不包含模型權重、API 金鑰、使用者設定、漫畫、翻譯成品、私人測試資料或執行日誌。原始工作環境保留；公開副本的可攜性調整另有紀錄。回報問題時請使用自製或有權分享的最小範例，移除日誌中的路徑、OCR 與對白。建議先閱讀架構與驗證文件，再提出附有重現步驟的 Issue／PR。

English: A Windows local Japanese-to-Traditional-Chinese manga translation pipeline featuring an adaptive bubble fitting engine and ink-level bubble guard. Developed on an Intel Core Ultra 7 258V / Arc 140V / 32 GB machine. Source preview; model weights and user artwork are not included. Detailed setup, engineering history and limitations are documented in Traditional Chinese.
