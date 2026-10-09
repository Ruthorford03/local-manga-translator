# 未來開發路線圖與優化追蹤 · Roadmap

本文件記錄《本機漫畫翻譯 · Local Manga Translator》後續的優化方向、架構演進規劃與功能待辦清單，供開發者與社群追蹤專案進度。

---

## 📋 待辦與優化維度清單

### 1. 🖼️ 視覺展示與效果對照 (Visual Demonstrations)
- [ ] **無侵權局部氣泡對照組 (Before vs. After)**
  - [ ] **防壓框對比**：傳統外接矩形硬塞（字體踩到對話框線） vs. Bubble Guard（局部自動移位／重排避開黑框）。
  - [ ] **空間填充對比**：傳統長氣泡腰斬排版（下方大片空白） vs. 自適應長氣泡填充平衡（自然飽滿垂直置中）。
  - [ ] **光學字距對比**：小字間距過疏渙散 vs. 光學字距收緊凝聚。
  - [ ] *素材原則*：嚴格使用純白底對話框黑線或自製開源示範素材，不露出商業角色肖像與分鏡畫面，確保無版權爭議。
- [ ] **操作介面動態展示 (UI Showcase)**
  - [ ] 錄製「人工審查與單頁視覺精修工作臺」左右對照、即時熱重繪的操作 GIF / 靜態截圖。
  - [ ] 演示快捷鍵 `Ctrl+S` 即時更新與音效局部遮罩還原操作。

---

### 2. 🌍 目標語言與國際化擴展 (Translation Targets & i18n)
- [ ] **多翻譯模型後端支援**
  - [ ] 目前專案深度綁定日翻繁中（Sakura + OpenCC s2tw）；未來規劃抽象出通用翻譯後端介面。
  - [ ] 支援透過 Ollama 調用通用多語言大模型（例如 Qwen2.5-14B/7B、Llama-3 等），提供英翻中、日翻英（Japanese to English）等管道。
- [ ] **西文橫排自適應排版引擎**
  - [ ] 研發針對英文/西文的動態斷字換行（Hyphenation）演算法。
  - [ ] 針對西文字母單詞間距與行距的動態視覺調整。

---

### 3. 📦 安裝門檻與模型分發優化 (Onboarding Experience)
- [ ] **下載管理與斷點續傳**
  - [ ] 強化 `prepare_models.py`，加入更清晰的終端下載進度條（如 `tqdm`）與校驗重試機制。
  - [ ] 支援國內外雙鏡像切換（Hugging Face 與 ModelScope），方便不同網路環境的使用者快速取得模型。
- [ ] **輕量化體驗模式 (Lite Profile)**
  - [ ] 支援 7B 級別或更小的輕量模型（如 Sakura-7B 或 HY-MT2-1.8B）作為快速體驗選項，降低初次下載 14.9 GB 模型之門檻。
- [ ] **一鍵整合打包探索**
  - [ ] 評估提供 Portable（免安裝可攜版）或精簡整合包的可能性，進一步減少 Python 環境配置步驟。

---

### 4. ⚙️ 工程細節與架構精進 (Architecture & Performance)
- [ ] **單頁視覺精修雙向原子同步**
  - [ ] 現狀：按 `Ctrl+S` 儲存 overrides 與 PNG 時，未同步寫入 `翻譯對照.csv` 與批次 `translations.json`。
  - [ ] 目標：實作原子寫入同步函式，在覆寫精修成品時，一併更新對應的 CSV 與 JSON 帳冊，達成資料全流程一致性。
- [ ] **CPU / GPU 異質運算負載管線化 (Pipeline Overlapping)**
  - [ ] 引入生產者-消費者佇列（Producer-Consumer Queue）。
  - [ ] 讓 CPU 前處理第 $N+1$ 批（CTD + OCR + Magi）的同時，GPU 平行推論第 $N$ 批（Sakura LLM），最大化發揮如 Intel Core Ultra 7 258V / Arc 140V 的異質硬體效能。
- [ ] **OCR 裁切候選池容錯升級**
  - [ ] 擴展 `ocr_crop_review.py` 的邊緣墨跡擴展邏輯，支援旋轉文字或傾斜氣泡的自適應補全。

---

### 5. 🏷️ 社群維護與發布管理 (Community & Governance)
- [ ] **版本釋出規範化**
  - [ ] 持續維護 GitHub Releases 頁面，隨版本迭代發布正式 Tag 與雙語 Release Notes。
  - [ ] 在 GitHub 倉庫首頁配置完整熱門 Topics 標籤，提高搜尋能見度。
- [ ] **回饋與測試案例庫**
  - [ ] 建立一套無版權的合成漫畫測試用例庫（Synthetic Test Cases），用於自動化回歸測試排版演算法。
