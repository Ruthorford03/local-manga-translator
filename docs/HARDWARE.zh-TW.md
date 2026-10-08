# 實際機器、執行環境與效能邊界

## 2026-10-08 實機盤點

以下由當天的 Windows CIM 與既有 Python 套件 metadata 讀取，不包含主機名稱、序號、帳號、MAC 或 IP。這張表描述開發機，不是最低硬體需求。

| 項目 | 實際讀值 |
| --- | --- |
| 製造商／型號 | LG Electronics / `16Z90TS-G.AU89C2` |
| CPU | `Intel(R) Core(TM) Ultra 7 258V` |
| CPU 核心／邏輯處理器 | 8 / 8 |
| 實體記憶體 | 33,864,134,656 bytes，約 31.54 GiB；32 GB 等級機型 |
| 顯示裝置名稱 | `Intel(R) Arc(TM) 140V GPU (16GB)` |
| Intel 顯示驅動 | `32.0.101.8991` |
| OS | Microsoft Windows 11 Pro，64-bit |
| OS 版本 | `10.0.26200` |

Arc 140V 是整合式 GPU，使用共享系統記憶體。裝置名稱中的 16GB 不是額外 16 GB 專用顯示記憶體，不能把它與 32 GB RAM 相加。這台機器雖有 NPU，本專案沒有建立或驗證 NPU 推論路徑。

## 兩個 Python 環境與 Ollama

既有工作環境的 BallonsTranslator 使用內附 Python 3.12.10；Magi 使用獨立 Python 3.12.7。公開安裝腳本使用兩個 Python 3.12 x64 venv，不複製整個既有 runtime，也不依賴 ComfyUI。

| 套件／工具 | 主流程環境 | Magi 環境 |
| --- | --- | --- |
| Python | 3.12.10 | 3.12.7 |
| PyTorch | 2.14.0+cpu | 2.14.0+cpu |
| torchvision | 0.29.0+cpu | 0.29.0+cpu |
| Transformers | 4.57.6 | 4.57.6 |
| NumPy | 2.5.3 | 2.5.2 |
| Pillow | 12.3.0 | 12.3.0 |
| requests | 2.34.2 | 2.34.2 |
| PyQt6 / Qt | 6.11.0 / 6.11.2 | 不使用 Qt |
| qtpy | 2.4.3 | 不使用 |
| OpenCV | 5.0.0.93 | 不負責主流程去字 |
| openai Python client | 3.22.1，本機相容端點 | 不負責翻譯 |
| OpenCC | opencc-python-reimplemented 0.1.7，`s2tw` | 不負責繁體轉換 |
| fugashi / unidic-lite | 1.5.2 / 1.0.8 | 不使用日文 OCR |
| spacy-pkuseg | 1.0.1 | 不負責中文換行 |
| psutil / timm | — | 7.2.2 / 1.0.30 |
| PuLP | — | 4.0.0 |

BallonsTranslator 基準為 1.5.17，保留一個 `trans_sakura.py` 本機修改。主流程相依性見 [`requirements-local.txt`](../requirements-local.txt)，Magi 的完整觀察版本見 [`requirements-resolved.txt`](../tools/magi-pilot/requirements-resolved.txt)。這些是已安裝環境的版本紀錄，並未測試所有更舊／更新套件的交叉組合。

本機 Ollama client 於 2026-10-08 查得 **0.17.7**，也與歷史實跑紀錄一致；Intel Arc 路徑使用 Vulkan。程式設定 `OLLAMA_VULKAN=1`、`OLLAMA_NO_CLOUD=1`。CPU PyTorch 環境不表示 Sakura 也在 CPU 上，因為 LLM 由另一個 Ollama 程序執行。安裝其他 Ollama 版本時要重新核對 Vulkan 裝置、模型匯入後的 recipe 與實際回覆，不能只檢查埠是否打開。

## 推論工作如何分配

| 階段 | 預設裝置／工具 | 理由與限制 |
| --- | --- | --- |
| 文字區域偵測 | CTD 1280，CPU | 沿用已驗證的 Ballons 模組；遮罩膨脹預設 5 |
| 日文 OCR／裁切複查 | Manga OCR，CPU | 保留獨立來源框，不採用 Magi 的英文 OCR |
| 閱讀順序候選 | Magi v2，CPU | 浮點 32 位，停用 OCR 和額外 backbone 下載；XPU 路徑未驗證 |
| 翻譯 | Sakura 14B Qwen2.5 v1.0 Q6_K，Ollama Vulkan | GGUF 約 12.1 GB；不是先前試過的小型翻譯模型 |
| 去字 | OpenCV Telea，CPU | 遮罩品質仍會影響畫面；不是通用背景重建保證 |
| 貼字／預覽 | Qt 主流程、Pillow 快速精修 | 兩條渲染路徑的字距與結果可能不同 |

排程使用一個 CPU 前處理 worker 加一個本機翻譯階段，限制預取量。現版以 6.5 GiB 可用記憶體作為批次間模型保留／卸載判斷，並使用 60 分鐘保活。這是減少壓力的啟發式規則；其他應用、解析度、頁數、KV cache 與驅動仍會影響需求，無法保證不 OOM。

## 有證據的歷史觀察

| 測試範圍 | 歷史結果 | 如何解讀 |
| --- | --- | --- |
| 2026-10-01，Magi CPU 兩頁 | 載入約 4.82 秒，兩頁分別約 3.02／2.86 秒；程序尖峰 working set 約 1.27 GiB | 僅兩頁一次執行，不是整條流水線記憶體需求 |
| 2026-10-01，六頁已選批次 | 約 4 分 50 秒，81 段輸出／模型對應檢查 | 結構及來源一致不代表每句語意都正確 |
| 2026-10-01，八頁分批比較 | 首批約提早 29%，總耗時約增加 4% | 分批改善等待第一批的體驗；不宣稱固定吞吐提升 |
| 2026-10-03，真實長冊 | 206 頁測試及部分待審紀錄 | 明細見開發歷程；並非 206 頁完全自動無人工通過 |

現版的高解析排版、快速精修、記憶體策略及本次公開可攜性修改，並未全部包含在這些舊執行中。不得用舊測試數字宣稱目前版本在任意漫畫、任意機器上有相同速度或品質。

## 未驗證範圍

- Linux、macOS、ARM Windows：包含 Windows 程序／字型依賴，沒有測試。
- NVIDIA CUDA、AMD GPU、Intel 獨顯：未跑相同端到端驗收。
- 16 GB RAM 或更低：沒有量出可靠最低需求。
- 乾淨電腦的首次完整安裝：提供可審閱腳本與內容雜湊，但尚未實跑。

移植前先用一張有權使用的小圖驗證，保存具體版本與時間；再提高解析度與頁數。詳細原因與歷史問題見[開發歷程](DEVELOPMENT_HISTORY.zh-TW.md)。
