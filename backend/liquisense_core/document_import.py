"""Local, review-first transaction extraction from PDF and image uploads."""
from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
from pathlib import Path
import re
from typing import Any

MAX_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 20
_DATE = re.compile(r"(?<!\d)(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})(?!\d)")
_AMOUNT = re.compile(r"(?<![A-Za-z0-9])(?:₹\s*|INR\s*)?\(?-?\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?\)?\s*(?:DR|CR)?(?![A-Za-z0-9])", re.I)
_REFERENCE = re.compile(r"\b(?:INV|INVOICE|TX|REF|UPI|NEFT|RTGS|CHQ|CHEQUE|BILL|PO|ID)[-/#]?[A-Z0-9-]+\b", re.I)
_INFLOW = re.compile(r"\b(CREDIT|CR|DEPOSIT|RECEIVED|RECEIPT|INFLOW|REFUND)\b|\+\s*₹?", re.I)
_OUTFLOW = re.compile(r"\b(DEBIT|DR|WITHDRAW(AL)?|PAYMENT|PURCHASE|EXPENSE|FEE|CHARGE)\b|[-(]\s*₹?", re.I)

class DocumentImportError(ValueError):
    pass


def _ocr_lines(image_bytes: bytes) -> list[str]:
    try:
        from PIL import Image
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR
        image = Image.open(BytesIO(image_bytes))
        image.verify()
        image = Image.open(BytesIO(image_bytes)).convert("RGB")
        if image.width * image.height > 25_000_000:
            raise DocumentImportError("Image is too large to process (maximum 25 megapixels).")
        engine = _ocr_engine()
        result, _ = engine(np.asarray(image))
        cells=[]
        for item in (result or []):
            if len(item) < 2 or not str(item[1]).strip(): continue
            box=item[0]; xs=[float(point[0]) for point in box]; ys=[float(point[1]) for point in box]
            cells.append((sum(ys)/len(ys),min(xs),max(ys)-min(ys),str(item[1]).strip()))
        cells.sort(key=lambda cell:(cell[0],cell[1])); groups=[]
        for cell in cells:
            target=next((group for group in reversed(groups) if abs(group[0]-cell[0])<=max(10.0,0.55*max(group[2],cell[2]))),None)
            if target is None: groups.append([cell[0],cell[1],cell[2],[cell]])
            else:
                target[3].append(cell);target[0]=sum(x[0] for x in target[3])/len(target[3]);target[2]=max(x[2] for x in target[3])
        return [" ".join(x[3] for x in sorted(group[3],key=lambda cell:cell[1])) for group in groups]
    except DocumentImportError:
        raise
    except Exception as exc:
        raise DocumentImportError(f"Could not read this image: {exc}") from exc


_OCR = None
def _ocr_engine():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    return _OCR


def _pdf_text(data: bytes) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise DocumentImportError("This PDF is password-protected. Export an unlocked copy and try again.")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise DocumentImportError(f"PDF has {len(reader.pages)} pages; the limit is {MAX_PDF_PAGES} pages per import.")
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        if len(re.sub(r"\s+", "", text)) >= 40:
            return text, len(reader.pages)
    except DocumentImportError:
        raise
    except Exception as exc:
        raise DocumentImportError(f"Could not read this PDF: {exc}") from exc

    # Scanned PDFs need page rendering before local OCR.
    try:
        import fitz
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR
        document = fitz.open(stream=data, filetype="pdf")
        if document.page_count > MAX_PDF_PAGES:
            raise DocumentImportError(f"PDF has {document.page_count} pages; the limit is {MAX_PDF_PAGES} pages per import.")
        engine = _ocr_engine()
        lines: list[str] = []
        for page in document:
            pix = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            result, _ = engine(arr)
            lines.extend(str(item[1]).strip() for item in (result or []) if len(item) > 1 and str(item[1]).strip())
        document.close()
        return "\n".join(lines), len(reader.pages)
    except DocumentImportError:
        raise
    except Exception as exc:
        raise DocumentImportError(f"Could not OCR this PDF. Try a text-based PDF or an image: {exc}") from exc


def _parse_date(token: str) -> str | None:
    value = re.sub(r"\s+", " ", token.strip())
    formats = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y", "%d %b %Y", "%d %B %Y", "%m/%d/%Y")
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _amount_value(token: str) -> float | None:
    value = re.sub(r"(?i)INR|₹|,|\s|DR|CR", "", token).strip()
    negative = value.startswith("-") or (value.startswith("(") and value.endswith(")"))
    value = value.strip("()")
    try:
        number = abs(float(value))
        return round(-number if negative else number, 2)
    except ValueError:
        return None


def parse_transaction_lines(text: str) -> tuple[list[dict[str, Any]], int]:
    candidates: list[dict[str, Any]] = []
    ignored = 0
    for original in text.splitlines():
        line = re.sub(r"\s+", " ", original).strip(" \t|¦")
        if len(line) < 8 or re.search(r"\b(date|particulars|description|debit|credit|balance|transaction details)\b", line, re.I) and not _DATE.search(line):
            continue
        dates = _DATE.findall(line)
        if not dates:
            continue
        parsed_date = _parse_date(dates[0])
        if not parsed_date:
            ignored += 1
            continue
        remainder = _DATE.sub(" ", line, count=1)
        references=[]
        def keep_reference(match):
            references.append(match.group(0))
            return f" __REF{len(references)-1}__ "
        analysis_line=_REFERENCE.sub(keep_reference,remainder)
        amount_matches=list(_AMOUNT.finditer(analysis_line))
        nonzero=[(m,_amount_value(m.group(0))) for m in amount_matches]
        nonzero=[(m,v) for m,v in nonzero if v is not None and abs(v)>0]
        if not nonzero:
            ignored += 1
            continue
        currency_matches=[(m,v) for m,v in nonzero if "₹" in m.group(0) or "INR" in m.group(0).upper()]
        amount_match,amount=currency_matches[0] if currency_matches else nonzero[0]
        inflow = bool(_INFLOW.search(remainder))
        outflow = bool(_OUTFLOW.search(remainder))
        if amount < 0:
            direction = "outflow"
        elif inflow and not outflow:
            direction = "inflow"
        elif outflow and not inflow:
            direction = "outflow"
        else:
            direction = ""
        description = _DATE.sub(" ", line, count=1)
        description = _REFERENCE.sub(keep_reference, description)
        description = _AMOUNT.sub(" ", description)
        description = re.sub(r"\b(CREDIT|CR|DEBIT|DR|INR|WITHDRAW(AL)?|PAYMENT|PURCHASE|EXPENSE|FEE|CHARGE|DEPOSIT|RECEIVED|RECEIPT|INFLOW|REFUND)\b", " ", description, flags=re.I)
        description = re.sub(r"__REF(\d+)__",lambda m:references[int(m.group(1))],description)
        description = re.sub(r"[|¦]+", " ", description)
        description = re.sub(r"\s+", " ", description).strip(" -–—:;,.|")
        if not description:
            description = "Imported transaction"
        confidence = 0.92 if (inflow ^ outflow) else 0.66
        candidates.append({"date": parsed_date, "description": description[:180], "direction": direction, "amount": abs(amount), "confidence": confidence, "source_line": line[:300]})
    return candidates, ignored


def extract_document(filename: str, data: bytes) -> dict[str, Any]:
    if not data:
        raise DocumentImportError("The selected file is empty.")
    if len(data) > MAX_BYTES:
        raise DocumentImportError("File is larger than 20 MB. Please upload a smaller file.")
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        text, pages = _pdf_text(data)
        source = "PDF text/OCR"
    elif ext in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}:
        text = "\n".join(_ocr_lines(data))
        pages = 1
        source = "Local image OCR"
    else:
        raise DocumentImportError("Supported formats are CSV, PDF, PNG, JPG, JPEG, WEBP, BMP, TIF, and TIFF.")
    candidates, ignored = parse_transaction_lines(text)
    return {"filename": Path(filename).name, "source": source, "pages": pages, "candidates": candidates, "ignored_lines": ignored, "extracted_text": text[:30000], "needs_review": True}