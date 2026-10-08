# 第三方來源、版本與授權

本專案整合既有開源工具，並提供本機批次流程、檢查與人工修正介面。第三方程式的作者、版權聲明與授權仍由各自的原始檔案保留；模型、字型、漫畫素材的授權不能由本專案的程式授權取代。下列來源於 2026-10-08 整理。

## BallonsTranslator

| 項目 | 固定資訊 |
|---|---|
| 作者與專案 | [dmMaze / BallonsTranslator](https://github.com/dmMaze/BallonsTranslator) 及其貢獻者 |
| 納入位置 | `tools/BallonsTranslator/` |
| 基準版本 | [v1.5.17](https://github.com/dmMaze/BallonsTranslator/releases/tag/v1.5.17) |
| release commit | `9c7863c1e10c5bd927312eca0a0860177b0e5539` |
| 程式授權 | `GPL-3.0-or-later`，依隨附 `pyproject.toml`；完整 GPL v3 文字見 [`LICENSE`](../tools/BallonsTranslator/LICENSE) |
| 基準封存檔 | [Ballonstranslator_win_minium.zip](https://github.com/dmMaze/BallonsTranslator/releases/download/v1.5.17/Ballonstranslator_win_minium.zip)，33,169,559 bytes |
| 封存檔 SHA-256 | `fa8d0016860b8f89bc82f2854c94f0ac1657b742d2a74dfcb6399cb88e6ec401` |

本機安裝目錄沒有 `.git`，因此不把它描述成可追溯每筆 commit 的 fork。本次重新計算保留的 release ZIP 雜湊，與當初保存的官方 asset digest 一致，再將發佈檔案逐一和該 ZIP 比對。納入的 559 個來源與資源檔，只有 `ballontranslator/modules/translators/trans_sakura.py` 和基準不同。比對結果及每個檔案的雙方 SHA-256 見 [`VENDOR_MANIFEST.json`](../tools/BallonsTranslator/VENDOR_MANIFEST.json)。

本機變更發生於 2026-10-01，2026-10-08 整理發佈時重驗：Sakura 參數改由 `get_param_value()` 取得實際值，字典路徑由 `set_param_value()` 寫回，以保留 GUI／設定所使用的值包裝；`galtransl-v1` 的字典格式對應到 `1.0`。完整可審閱差異見 [`LOCAL_CHANGES.patch`](../tools/BallonsTranslator/LOCAL_CHANGES.patch)。這項變更是本專案的本機修正，不表示上游已合併。

納入範圍為 Python 程式、上游測試、維護文件、啟動／安裝腳本、UI 圖示、語系、樣式與必要的文字資料。沒有納入本機 Python runtime、已安裝套件、模型權重、使用者設定、近期專案、日誌、漫畫圖片或翻譯成果。`data/font_demo_cache.bin` 是上游的選配字型辨識快取，本發佈也省略；預設流程不啟用字型辨識。上游的巢狀 `.gitignore` 與 `.gitattributes` 不納入公開包，由根目錄統一管理排除規則與 Git 文字屬性；vendor 使用 `-text` 保留 release 封存檔的原始 bytes，避免 Git 換行轉換使雜湊清單失效。上游 README 中的示範連結屬於上游文件，並非本專案自有素材或本機測試成果。

## 隨附 Abel 字型

`tools/BallonsTranslator/ballontranslator/assets/font_refresh/Abel-Regular.ttf` 是上游用於暫時 Qt 字型註冊的 Abel 字型，不是漫畫嵌字字型。其 [原始來源](https://github.com/google/fonts/tree/9437b806936896fa1a8c812e561067a5f30f5933/ofl/abel)、用途與雜湊保留在同目錄的 `README.md`；完整 SIL Open Font License 1.1 保留於 [`OFL.txt`](../tools/BallonsTranslator/ballontranslator/assets/font_refresh/OFL.txt)。

程式會使用使用者 Windows 系統上的中文字型，預設為 Microsoft JhengHei／微軟正黑體。本專案不散布 Windows 字型檔。

## 需另行取得的模型與推論工具

| 元件 | 原始來源／版本 | 授權與散布邊界 |
|---|---|---|
| Sakura 14B Qwen2.5 v1.0，Q6_K GGUF | [SakuraLLM 官方模型庫](https://huggingface.co/SakuraLLM/Sakura-14B-Qwen2.5-v1.0-GGUF)，revision `f370f85114971e556ba9ab7cdfd00fe9fe88dd53` | 官方模型卡標示 `CC-BY-NC-SA-4.0`；權重不隨此 repository 散布。程式碼公開不代表該模型可任意商用。 |
| Magi v2 | [ragavsachdeva/magiv2](https://huggingface.co/ragavsachdeva/magiv2)，revision `fbc890fec52977142e8ee00bfe26e9458b65517c` | 官方模型卡允許個人、研究、非商業與非營利使用，其他情境需向作者取得適用授權。權重及其 custom model code 均由使用者自原始來源另行取得。 |
| manga-ocr-base | [kha-white/manga-ocr-base](https://huggingface.co/kha-white/manga-ocr-base) | 模型卡標示 Apache-2.0；權重不隨此 repository 散布。BallonsTranslator 內的 OCR wrapper 保留其來源註記。 |
| Comic Text Detector | [dmMaze/comic-text-detector](https://github.com/dmMaze/comic-text-detector)；Ballons 的下載清單指向 [dreMaz/mit_models](https://huggingface.co/dreMaz/mit_models) | 請以各原始專案及模型來源的授權為準；沒有將權重重新標示為本專案的 GPL。 |
| Ollama | [ollama/ollama](https://github.com/ollama/ollama) | 使用者自行安裝。此 repository 僅包含本機服務啟動與 HTTP 呼叫程式。 |
| Python／PyTorch／Transformers／Qt 與其他套件 | 各套件官方 distribution 與套件 metadata | 安裝時取得各自授權及版本；沒有將整個已安裝 runtime 或套件庫複製進本 repository。 |

Sakura GGUF 的已驗證 SHA-256 為 `2c1fc22a43c15cc42cf1443822115b979c2f9143671f5369df0158d9fc184b77`，大小 12,124,683,584 bytes；Magi `pytorch_model.bin` 的已驗證 SHA-256 為 `56392403204d3a4cca38694a3a260a6929d741869d802d6b14de35b4eab4c4b8`，大小 2,063,693,064 bytes。這些識別值用於固定已測組合，並非模型授權的替代文件。

## 素材與使用者資料

發佈只包含程式、文件和明確列入的上游 UI 資源。請自行提供有權處理的輸入圖片；原圖、OCR、翻譯稿、中間圖、審核紀錄與最終成圖保留在使用者本機，不作為此 repository 的示範素材。公開 issue、log 或測試案例時也應先移除帳號、絕對路徑與作品內容。
