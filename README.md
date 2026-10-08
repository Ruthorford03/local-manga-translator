# 本機漫畫翻譯 · Local Manga Translator

在 Windows 上，把日文漫畫圖片資料夾轉成繁體中文，並保留原圖、翻譯紀錄、續跑資料與人工審查入口。

這是從實際使用環境整理出的 **2026-10-08 原始碼預覽版**。主要流程為 CTD 文字偵測 → Manga OCR → Magi 閱讀順序候選 → 本機 Sakura 翻譯 → OpenCC 繁體轉換 → 去字與排版。模型與 Python 套件準備好後，預設翻譯使用本機 CPU／GPU；首次安裝和下載需要網路。

## 能做什麼

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
| 模組分工、資料流、狀態與失敗邊界 | [ARCHITECTURE.zh-TW.md](docs/ARCHITECTURE.zh-TW.md) |
| 開發決策、遇到的問題、修正與驗證：20 個案例 | [DEVELOPMENT_HISTORY.zh-TW.md](docs/DEVELOPMENT_HISTORY.zh-TW.md) |
| 這次公開版改了什麼、測過什麼、尚未測什麼 | [VALIDATION.zh-TW.md](docs/VALIDATION.zh-TW.md) |
| 上游來源、授權、版本與本機修改 | [THIRD_PARTY_NOTICES.md](docs/THIRD_PARTY_NOTICES.md) |
| 模型下載位置、檔案大小與 SHA-256 | [MODELS.json](docs/MODELS.json) |

開發歷程保留失敗與取捨：例如分批排程曾讓首批提早約 29%，但該次八頁總耗時反而增加約 4%；58 ms 的單頁重繪結果不能寫成穩定 60 FPS；6.5 GiB 記憶體水位也不保證永不 OOM。文件會區分歷史實測與目前程式，避免把舊版驗收當成新版保證。

## 公開範圍與貢獻

程式以 GPL-3.0-or-later 提供，完整文字見 [LICENSE](LICENSE)；保留 BallonsTranslator 及其他原作者的聲明。模型、字型與漫畫素材各有自己的授權，尤其 Sakura／Magi 的非商業限制不因程式開源而消失。本倉庫不是 BallonsTranslator、Magi 或 Sakura 的官方發行版。

不包含模型權重、API 金鑰、使用者設定、漫畫、翻譯成品、私人測試資料或執行日誌。原始工作環境保留；公開副本的可攜性調整另有紀錄。回報問題時請使用自製或有權分享的最小範例，移除日誌中的路徑、OCR 與對白。建議先閱讀架構與驗證文件，再提出附有重現步驟的 Issue／PR。

English: A Windows local Japanese-to-Traditional-Chinese manga translation pipeline, developed on an Intel Core Ultra 7 258V / Arc 140V / 32 GB machine. Source preview; model weights and user artwork are not included. Detailed setup, engineering history and limitations are documented in Traditional Chinese.
