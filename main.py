# -*- coding: utf-8 -*-
"""
DocProc — ESG PDF 文件處理服務（FastAPI + Docling）
對應藍圖 §2.3。引擎用 Docling（金融/法律複雜表格最佳、~2-3GB VRAM，適合 GTX1650）。
輸出統一 DocJSON（blocks 含 blockId/type/text/html/bbox），由 .NET ReconstructStageHandler 寫回 ai.pages。

bbox 一律「左下原點」(PDF 座標, 與 .NET PdfPig 端一致)：[x0, y0, x1, y1]。
掃描頁/無文字層交給 Docling 內建 OCR；表格輸出 type=table 的 html。

執行（GTX1650 那台）：
    pip install -r requirements.txt
    uvicorn main:app --host 0.0.0.0 --port 8100
"""
import hashlib
from collections import OrderedDict
from io import BytesIO

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse

from docling.document_converter import DocumentConverter
from docling.datamodel.base_models import DocumentStream

app = FastAPI(title="DocProc (Docling)", version="0.2")

# Docling 模型載入一次（重用）
_converter = DocumentConverter()

# 逐頁 job 會對同一份 PDF 多次呼叫 /reconstruct；快取已轉換結果避免重複轉整份。
_CACHE_MAX = 8
_cache: "OrderedDict[str, object]" = OrderedDict()

# Docling label → DocJSON type
_TYPE_MAP = {
    "title": "title",
    "section_header": "title",
    "table": "table",
    "picture": "figure",
    "formula": "formula",
}


@app.get("/health")
def health():
    return {"status": "ok", "engine": "docling"}


def _convert(pdf_bytes: bytes):
    key = hashlib.sha256(pdf_bytes).hexdigest()
    document = _cache.get(key)
    if document is None:
        source = DocumentStream(name="doc.pdf", stream=BytesIO(pdf_bytes))
        document = _converter.convert(source).document
        _cache[key] = document
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return document


@app.post("/process")
async def process(file: UploadFile = File(...)):
    """文件級：收整份 PDF，一次 Docling 轉換回傳所有頁的 DocJSON（效率主路徑）。"""
    pdf_bytes = await file.read()
    try:
        document = _convert(pdf_bytes)
    except Exception as ex:
        return JSONResponse(status_code=500, content={"error": f"docling convert failed: {ex}"})

    page_count = _page_count(document)
    pages = [build_page_docjson(document, p) for p in range(1, page_count + 1)]
    return {"pageCount": page_count, "pages": pages}


@app.post("/reconstruct")
async def reconstruct(file: UploadFile = File(...), pageNo: int = Form(...)):
    """單頁版（相容保留）：收整份 PDF + pageNo，回傳該頁 DocJSON。"""
    pdf_bytes = await file.read()
    try:
        document = _convert(pdf_bytes)
    except Exception as ex:
        return JSONResponse(status_code=500, content={"error": f"docling convert failed: {ex}"})
    return build_page_docjson(document, pageNo)


def build_page_docjson(document, page_no: int) -> dict:
    """走訪 Docling 文件，取出指定頁的 items，組成 DocJSON（含 bbox、表格 html）。"""
    page_height = _page_height(document, page_no)

    items = []
    # texts + tables + pictures 都帶 prov（page_no + bbox）
    for collection in (
        getattr(document, "texts", None) or [],
        getattr(document, "tables", None) or [],
        getattr(document, "pictures", None) or [],
    ):
        for item in collection:
            for prov in (getattr(item, "prov", None) or []):
                if getattr(prov, "page_no", None) != page_no:
                    continue
                items.append((item, prov))

    # 依頁面由上而下排（左下原點：top 值大者在上）
    def sort_key(pair):
        _, prov = pair
        bbox = _bbox_bl(prov, page_height)
        return -(bbox[3])  # 以 top(y1) 由大到小

    items.sort(key=sort_key)

    blocks = []
    order = 0
    for item, prov in items:
        label = str(getattr(item, "label", "") or "").lower()
        btype = _TYPE_MAP.get(label, "paragraph")

        text = (getattr(item, "text", None) or "").strip()
        html = None
        if btype == "table":
            try:
                html = item.export_to_html(doc=document)
            except Exception:
                try:
                    html = item.export_to_html()
                except Exception:
                    html = None
            if not text:
                text = "[table]"

        if not text and not html:
            continue

        order += 1
        blocks.append({
            "blockId": f"p{page_no:03d}-b{order:03d}",
            "type": btype,
            "readingOrder": order,
            "text": text,
            "html": html,
            "bbox": _bbox_bl(prov, page_height),
            "confidence": None,
        })

    engine = "docling" if blocks else "image-needs-ocr"
    return {"pageNo": page_no, "engine": engine, "blocks": blocks}


def _page_count(document) -> int:
    try:
        pages = getattr(document, "pages", None) or {}
        return len(pages)
    except Exception:
        return 0


def _page_height(document, page_no: int) -> float:
    try:
        pages = getattr(document, "pages", None) or {}
        page = pages.get(page_no) if isinstance(pages, dict) else pages[page_no - 1]
        return float(page.size.height)
    except Exception:
        return 0.0


def _bbox_bl(prov, page_height: float):
    """Docling bbox 正規化為左下原點 [x0, y0, x1, y1]。"""
    bb = prov.bbox
    try:
        # Docling BoundingBox 內建轉換（若為 TOPLEFT 會用 page_height 翻轉）
        bb = bb.to_bottom_left_origin(page_height=page_height)
    except Exception:
        pass
    return [round(float(bb.l), 2), round(float(bb.b), 2), round(float(bb.r), 2), round(float(bb.t), 2)]
