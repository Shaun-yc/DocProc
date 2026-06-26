# DocProc — ESG PDF 文件處理服務

藍圖 §2.3 的 Python 文件處理服務。補 .NET/PdfPig 的弱項：**表格結構化**、**掃描頁 OCR**。
跑在 GTX1650 那台（有 Python/GPU）。

## 角色

```
.NET ReconstructStageHandler
  │ 若設定 DocProc:Endpoint → 送整份 PDF + pageNo 給本服務；否則沿用 PdfPig（fallback）
  ▼  POST /reconstruct (multipart: file=PDF, pageNo)
DocProc (FastAPI + Docling)
  Docling 版面/表格/OCR/閱讀順序 → 逐頁取 items（含 prov: page_no + bbox）
  表格 → type=table 的 html；掃描頁 → Docling 內建 OCR
  ▼ DocJSON（blocks 含 blockId/type/text/html/bbox，bbox 左下原點）
.NET 寫回 ai.pages.docjson
```

引擎用 **Docling**（金融複雜表格最佳、~2-3GB VRAM，適合 GTX1650；不與 .94 的 LLM 搶卡）。
逐頁 job 會多次呼叫同一份 PDF，服務以 sha256 快取已轉換結果避免重複轉整份。

## 啟動

```bash
cd services/docproc
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8100
```

健康檢查：`curl http://localhost:8100/health`

## 在 .NET 端啟用

`YC.Pipeline/appsettings.json`：
```json
"DocProc": { "Endpoint": "http://192.168.0.<1650IP>:8100", "TimeoutSeconds": 180 }
```
- 不設 `DocProc:Endpoint` → ReconstructStageHandler 沿用本機 PdfPig（零破壞）。
- 設了 → 改走本服務。

## 整合點（v1 為 PyMuPDF 基線，逐步補）

| 函式 | 補什麼 |
|---|---|
| `try_paddleocr()` | 掃描頁 OCR + 表格（PaddleOCR-VL，GTX1650） |
| `reconstruct_mineru()` | 表格密集頁結構化（MinerU 2.5，輸出 type=table 的 html） |

## DocJSON 契約

```jsonc
{
  "pageNo": 12,
  "engine": "pymupdf|paddleocr-vl|mineru|image-needs-ocr",
  "blocks": [
    { "blockId": "p012-b001", "type": "paragraph|table|title",
      "readingOrder": 1, "text": "...", "html": null,
      "bbox": [x0, y0, x1, y1], "confidence": null }
  ]
}
```
bbox 一律「左下原點」(PDF 座標)，與 .NET PdfPig 端一致，證據鏈框紅才不會錯位。
