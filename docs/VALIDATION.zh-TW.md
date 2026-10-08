# 2026-10-08 公開副本驗證

本文件記錄這次上架準備，不把舊版漫畫成果當作此副本已完成端到端驗收。原工作站的完整模型、漫畫、日誌及翻譯成品皆未納入公開倉庫。

## 與原工作環境的差異

| 公開副本調整 | 理由 | 對既有工作環境的影響 |
| --- | --- | --- |
| 單獨的倉庫與相對目錄，附主／Magi 環境安裝腳本 | 不需要把整個 ComfyUI 工作目錄公開 | 原目錄不修改 |
| 啟動器優先使用 `.venv`，保留原 embedded Python 備援 | 新 clone 沒有內附 Python 二進位 | 原啟動器不修改 |
| 移除私人範例路徑、加 `MANGA_OLLAMA_EXE` | 使用者可安裝在自己的目錄 | 原設定不修改 |
| 模型以固定 layer 身分及每台機器的 manifest digest 驗證 | 原 manifest 包含安裝路徑，跨機器雜湊不同 | 不複製或更新原模型庫 |
| 移除兩組 OCR 詞句的寫死譯文特例 | 不把私人樣本特例當通用翻譯；回到模型回覆與繁體轉換 | 原模型輸出／既有成品不修改 |
| 修正 atomic JSON 錯誤訊息中的舊重試次數 | 現版為 8 次、排程等待合計 4.35 秒 | 重試機制本身不變 |
| 快速精修只允許已發布且核對通過的頁面 | 防止待審／失敗頁經 Ctrl+S 繞過審查 | 僅公開副本新增守門 |
| 保留上游原始 byte；排除上游 `.gitattributes` | 避免 Git 換行轉換使來源雜湊失效 | 上游原安裝檔不修改 |

模型身分與內容驗證是兩層：`model_identity.py` 驗證固定 recipe 的 layer 識別值並計算 manifest 雜湊；`prepare_models.py` 或 `preflight.py --full-hash` 才完整雜湊大型檔案／blob。一般 preflight 不載入模型，也不驗證推論結果。

## 本次實際通過的檢查

使用既有 BallonsTranslator Python 3.12.10 執行公開副本，沒有重新安裝或升級原工作環境。

| 檢查 | 結果與範圍 |
| --- | --- |
| 33 個離線測試 | 7 個模型身分、11 個下載／archive 合成測試、14 個精修發布守門測試、1 個主視窗 smoke test |
| 真實 PyQt6 offscreen 主視窗 | 可建構、沒有選來源時不啟動工作、不建立外部程序 |
| 真實 PyQt6 offscreen 審查視窗 | 合成已發布頁可儲存；磁碟改成待審後，即使視窗狀態過期，Ctrl+S 和直接呼叫儲存都拒絕且不寫檔 |
| 缺少安裝的 preflight | 回報 `ready: false`、缺少的 Magi 環境與模型，不誤報可推論 |
| CLI 說明入口 | `folder_translator.py --help` 與 `import_sakura.py --help` 可執行，未啟動模型 |
| 程式語法與 PowerShell | 公開 Python 檔可解析；安裝腳本由 PowerShell parser 檢查 |
| Vendor 來源 | 559 個來源／資源與固定 release ZIP 逐檔比對，唯一功能修改見 `LOCAL_CHANGES.patch` |
| Git 實際內容 | 排除模型／runtime／漫畫／輸出，檢查常見權杖、私鑰、私人使用者路徑與異常大型檔 |

下載測試將 HTTP 回應替換成合成 bytes，涵蓋正確內容、失配保留、大小超限、重複 archive member 與路徑逸出；沒有下載真實模型。守門測試只使用暫存純色小圖與合成 JSON，不使用私人漫畫。公開的 GitHub Actions 使用相同離線測試，CI 通過仍不等於模型推論驗收。

重跑：

```powershell
& .\tools\BallonsTranslator\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s tests -v
& .\tools\BallonsTranslator\.venv\Scripts\python.exe -B -X utf8 scripts/check_public_tree.py
```

第二個命令檢查 Git 已追蹤檔案；提交新檔前需先在這個倉庫暫存，再檢查。規則只補充人工審閱，不能保證能識別所有秘密或所有版權內容。

## 尚未驗證及保留問題

1. 沒有在乾淨新機重建兩個環境、下載約 14.9 GB 模型後跑完整翻譯，也沒有對公開副本重新跑真實長冊。歷史速度與品質不能直接套用。
2. 安裝腳本固定已觀察版本，但遠端 package index、模型服務及舊版 Windows 支援仍可能改變。內容 SHA 不符會停止。
3. 快速精修仍依序寫 override、PNG、file-map；不是跨檔案交易。CSV、批次 verification 與既有核准收據不會全部同步，人工修改後的續跑也未全面驗證。
4. 新增守門在儲存前重讀磁碟，防止一般過期視窗繞過；不提供跨程序交易鎖或抵抗任意同時寫入者的保證。使用時一次只開一個翻譯／精修工作。
5. 行數、來源雜湊、模型回覆一致等檢查不證明語意正確。OCR 漏字、音效、代詞、人物關係、細小文字與排版仍需人看。
6. 原生 Qt 批次渲染和 Pillow 快速精修的字形／標點度量不同，不保證兩條路徑逐像素一致。
7. 保留第三方原始空白與換行以支援 byte-level 來源核對；不以全樹格式化製造無關差異。自有新增／修改內容另做格式核對。

問題的設計原因、歷次嘗試與舊測試結果見[20 個開發案例](DEVELOPMENT_HISTORY.zh-TW.md)；版本、共享記憶體和測速條件見[硬體文件](HARDWARE.zh-TW.md)。
