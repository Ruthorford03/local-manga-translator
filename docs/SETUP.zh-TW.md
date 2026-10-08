# Windows 安裝與模型準備

## 安裝範圍

本發佈是原始碼預覽版。原工作站有歷史完整翻譯紀錄；本次公開副本的安裝／下載工具做了無模型的合成測試，但**尚未在乾淨電腦完整下載、安裝並翻譯**。請先用一頁測試再處理長冊。已觀察版本見[硬體文件](HARDWARE.zh-TW.md)，不要把版本表當成所有組合都支援。

需要 Windows x64、Python 3.12 x64、Ollama、可顯示繁體中文的系統字型，以及足夠儲存空間。模型下載清單合計 14,918,052,798 bytes；Ollama 匯入可能另占 GGUF 副本空間，工作圖片與兩個 Python 環境也需空間。32 GB 是已使用機器的配置，不是實測得出的最低值。

先閱讀 [第三方來源與授權](THIRD_PARTY_NOTICES.md)。Sakura／Magi 有各自使用條件，程式的 GPL 授權不能替代模型授權。模型、Windows 字型與漫畫不包含在 GitHub ZIP 內。

## 1. 取得原始碼

在 GitHub 選 **Code → Download ZIP** 後解壓縮，或使用 Git：

```powershell
git clone https://github.com/Ruthorford03/local-manga-translator.git
cd local-manga-translator
```

以下命令均在倉庫根目錄執行。範例 `D:\Comics\Chapter01` 是示意路徑，請換成自己的圖片資料夾。不要把本倉庫直接覆蓋到現有可用的 ComfyUI／BallonsTranslator 環境。

## 2. 建立隔離 Python 環境

先以 `python --version` 確認是 Python 3.12 x64；若不是，將腳本 `-Python` 指向實際的 3.12 `python.exe`。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup_windows.ps1 -Python python
```

此命令只在這次 PowerShell 程序允許執行指定腳本，不修改系統執行原則。可先開啟腳本檢查內容。腳本只在新 clone 內建立：

```text
tools/BallonsTranslator/.venv/
tools/magi-pilot/.venv/
```

兩個環境先從 PyTorch 官方 CPU wheel index 安裝 `torch==2.14.0+cpu`、`torchvision==0.29.0+cpu`，再安裝各自套件。Magi 與主環境隔離是為了固定相依性，不是安全沙盒。CPU wheels 用於 OCR／Magi；Sakura 的 GPU 推論由 Ollama 負責。

腳本若看見既有 `.venv` 會停止並保留它。若首次安裝途中失敗，不必重建已可用環境；檢查錯誤後，在該新 clone 中對失敗的環境執行相應安裝命令：

```powershell
# 主流程（前提：這個 venv 已成功建立）
& .\tools\BallonsTranslator\.venv\Scripts\python.exe -m pip install torch==2.14.0+cpu torchvision==0.29.0+cpu --index-url https://download.pytorch.org/whl/cpu
& .\tools\BallonsTranslator\.venv\Scripts\python.exe -m pip install -r .\requirements-local.txt

# Magi（前提：這個 venv 已成功建立）
& .\tools\magi-pilot\.venv\Scripts\python.exe -m pip install torch==2.14.0+cpu torchvision==0.29.0+cpu --index-url https://download.pytorch.org/whl/cpu
& .\tools\magi-pilot\.venv\Scripts\python.exe -m pip install -r .\tools\magi-pilot\requirements-resolved.txt
```

如果其中一個環境尚未建立，可用 `python -m venv .\tools\magi-pilot\.venv` 等命令只建立缺少的環境。不要用 `pip install -U` 對所有套件無條件升級；目前的觀察版本不等於任意新版本相容。

## 3. 安裝 Ollama 與準備模型

從 [Ollama 官方下載頁](https://ollama.com/download/windows) 安裝 Windows 版本。開發機的 client 為 0.17.7；其他版本尚未完成相同端到端驗證。預設尋找 `%LOCALAPPDATA%\Programs\Ollama\ollama.exe`；自訂位置可在啟動本專案的 PowerShell 設定：

```powershell
$env:MANGA_OLLAMA_EXE = 'D:\Apps\Ollama\ollama.exe'
```

此設定只影響該 PowerShell 啟動的程序。由桌面直接雙擊的啟動器不會繼承另一個 PowerShell 的臨時變數。

下載模型前，先檢視 [MODELS.json](MODELS.json) 的來源與 [模型授權](THIRD_PARTY_NOTICES.md)。明確執行以下命令才下載：

```powershell
& .\tools\BallonsTranslator\.venv\Scripts\python.exe -B .\scripts\prepare_models.py --download
```

這會下載 CTD、Manga OCR、Magi v2、中文分詞資源與 Sakura GGUF。下載先寫 `.part`，大小與 SHA-256 相符才改成正式檔；已存在且吻合的檔案會跳過，已存在但不同的檔案會保留並報錯。中斷留下的 `.part` 目前不支援 HTTP 續傳，請先移到自己的暫存位置再重試，不要把它改名假裝下載完成。

Magi 與 Sakura 使用固定 revision。其他部分 URL 使用 `main`，但每個檔案仍固定內容 SHA-256；上游若換檔會停止，不能悄悄接受新內容。Magi 的 custom model code 也在清單中，取得後請保留 revision／雜湊；推論使用 `trust_remote_code=True` 執行這份本機 code，因此內容驗證很重要。

只有離線核對、不下載：

```powershell
& .\tools\BallonsTranslator\.venv\Scripts\python.exe -B .\scripts\prepare_models.py
```

此命令會完整雜湊所有清單檔案，缺檔回傳非零狀態。`MODELS.json` 不是權重，也不會把漫畫送往 Hugging Face。

## 4. 匯入固定 Sakura recipe

```powershell
& .\tools\BallonsTranslator\.venv\Scripts\python.exe -B .\scripts\import_sakura.py
```

腳本重新核對 Sakura GGUF，啟動自己擁有的本機 Ollama 程序，使用 `Modelfile.sakura` 建立別名 `sukinishiro:latest`，儲存於 `tools/local-manga-translation/ollama-models`，完成後停止自己建立的程序。此步不下載模型，不會改動預設的全域 Ollama 模型庫。若 11436 已被使用則停止，可先關閉正在使用該埠的本專案工作，或在 setup 命令加 `--port 11446`。

Ollama manifest 的 `from` 路徑因安裝位置而不同，因此公開版不要求每台電腦都等於作者的 manifest 雜湊。它先核對固定的權重、template、parameters 和 config layer，再以本機 manifest 的實際 SHA-256 綁定續跑紀錄。不同模型／template／參數會拒絕，不要直接刪掉或改掉檢查來繞過失敗。

若新 Ollama 對相同 Modelfile 產生不同 recipe，匯入後會明確報錯且保留資料供比較；這不是已有完整跨版本相容測試。原工作機可用的版本與各 layer 指紋可供重現，但本次沒有重新下載舊 Ollama 做安裝測試。

## 5. 啟動前核對

```powershell
& .\tools\BallonsTranslator\.venv\Scripts\python.exe -B .\scripts\preflight.py --full-hash
```

看到 `ready: true` 代表檔案、套件 metadata、字型、Ollama 路徑與模型 recipe 通過檢查，**不代表已執行推論或語意驗收**。拿掉 `--full-hash` 可加快後續檢查，但只核對大型檔案存在與大小；第一次請做完整雜湊。缺檔／失配會列於 `errors` 並回傳非零狀態，不會自行載入模型或啟動服務。

啟動器優先使用本倉庫 `.venv`；若沒有，才尋找既有 `ballontrans_pylibs_win` runtime。沒有任一環境時會提示先安裝。

```powershell
.\Start_Manga_Folder_Translator.cmd
```

GUI 操作見[使用說明](USAGE.zh-TW.md)。命令列處理單一資料夾：

```powershell
& .\tools\BallonsTranslator\.venv\Scripts\python.exe -B -X utf8 .\tools\local-manga-translation\folder_translator.py --headless --source 'D:\Comics\Chapter01'
# 接續同來源最近一輪相容的未完成工作：在上面命令加 --resume
```

需要一般 BallonsTranslator GUI 時執行 `Start_Ballons_Local_Translator.cmd`。該 GUI 的原生 OCR 順序與資料夾排程的 Magi 候選順序不是同一條流程，不要拿它們作為相同輸出的保證。

## 6. 本機／網路界線與故障處理

安裝套件與下載模型會連網；日常的預設翻譯端點是 loopback Ollama。Magi 推論程序有自己的網路阻擋；整套應用沒有作業系統防火牆沙盒。上游 BallonsTranslator 仍包含其他線上翻譯器及缺少資源時的下載程式。不要把「預設本機」寫成「任何設定都絕不連網」。

| 現象 | 檢查與處理 |
| --- | --- |
| 雙擊沒有 Python | 先完成隔離環境；從 CMD 入口看訊息，確認不是只下載程式就啟動 |
| preflight 缺套件 | 使用正確的主／Magi Python 路徑安裝相應 requirements，不改其他專案的 venv |
| preflight 缺模型 | 執行 `prepare_models.py --download`；已下載檔失配時先保留供核對 |
| recipe mismatch | 比對 Ollama 版本、Modelfile、固定 layer 指紋，不能只改模型名稱／刪除 hash |
| 11436 被占用 | 一般 GUI 和 setup 要求可用埠；資料夾批次可選空閒埠並驗證 PID；不要終止不屬於此工作的服務 |
| 舊工作拒絕續跑 | 程式／模型 profile 已改變，從來源建立新工作；保留舊成果與紀錄 |
| 中文缺字／字型錯誤 | 透過 Windows 準備微軟正黑體；不要把受限制的系統字型上傳 GitHub |
| 某頁待審 | 開啟左右對照審查，不能以快速儲存代替核准；詳見使用文件 |

提交 Issue 時只貼必要的版本、錯誤類型和脫敏步驟。原始 `pipeline.log`、請求／回覆、CSV 與 JSON 可能包含漫畫對白、私人路徑和作品名，不宜整包公開。
