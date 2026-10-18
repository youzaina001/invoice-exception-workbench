"""One bounded pypdf adapter; offsets refer to LF-normalized page text."""

from __future__ import annotations

import hashlib
import io
import json
import re
import signal
import subprocess
import sys
from typing import Any, Literal

from ledgerlens.extraction import Failure, StrictModel

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PAGES = 8
MAX_EXPANDED_BYTES = 1024 * 1024
MAX_TEXT_BYTES = 64 * 1024
MEMORY_BYTES = 256 * 1024 * 1024
CPU_SECONDS = 3
WALL_SECONDS = 5
ADAPTER: dict[str, Any] = {
    "pypdf": "6.21.1",
    "strict": True,
    "extraction_mode": "plain",
    "orientations": [0, 90, 180, 270],
    "space_width": 200.0,
    "line_endings": "LF",
    "page_tags": "[page N] followed by LF",
    "max_pdf_bytes": MAX_PDF_BYTES,
    "max_pages": MAX_PAGES,
    "max_expanded_bytes": MAX_EXPANDED_BYTES,
    "max_text_bytes": MAX_TEXT_BYTES,
    "memory_bytes": MEMORY_BYTES,
    "cpu_seconds": CPU_SECONDS,
    "wall_seconds": WALL_SECONDS,
}


class PDFText(StrictModel):
    version: Literal["1"]
    status: Literal["text"]
    pages: list[str]
    text: str
    text_sha256: str
    adapter: dict[str, Any]


def refused(code: str) -> Failure:
    return Failure(version="1", status="failure", stage="pdf", code=code)


def read_pdf(data: bytes) -> PDFText | Failure:
    if len(data) > MAX_PDF_BYTES:
        return refused("input_limit")
    try:
        child = subprocess.run(
            [sys.executable, "-m", "ledgerlens.pdf_text"],
            input=data,
            capture_output=True,
            timeout=WALL_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return refused("time_limit")
    if child.returncode in {-signal.SIGKILL, -signal.SIGXCPU}:
        return refused("resource_limit")
    if child.returncode:
        try:
            return Failure.model_validate_json(child.stdout)
        except ValueError:
            return refused("corrupt")
    raw = json.loads(child.stdout)
    if raw.get("status") == "failure":
        return Failure.model_validate(raw)
    return PDFText.model_validate(raw)


def worker() -> PDFText | Failure:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    except (ImportError, OSError, ValueError):
        return refused("bounds_unavailable")
    try:
        import pypdf

        if pypdf.__version__ != ADAPTER["pypdf"]:
            return refused("adapter_version")
        data = sys.stdin.buffer.read(MAX_PDF_BYTES + 1)
        if len(data) > MAX_PDF_BYTES:
            return refused("input_limit")
        reader = pypdf.PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            return refused("encrypted")
        if not 1 <= len(reader.pages) <= MAX_PAGES:
            return refused("page_limit")
        pages = []
        expanded = 0
        counted_xobjects: set[int] = set()
        for page in reader.pages:
            contents = page.get_contents()
            expanded += len(contents.get_data()) if contents is not None else 0
            if expanded > MAX_EXPANDED_BYTES:
                return refused("expanded_text_limit")
            resources = [page.get("/Resources")]
            seen_on_page: set[int] = set()
            has_image = False
            while resources:
                page_resources = resources.pop()
                if page_resources is None:
                    continue
                xobjects = page_resources.get_object().get("/XObject")
                if xobjects is None:
                    continue
                for reference in xobjects.get_object().values():
                    obj = reference.get_object()
                    identity = id(obj)
                    if identity in seen_on_page:
                        continue
                    seen_on_page.add(identity)
                    if obj.get("/Subtype") == "/Image":
                        has_image = True
                    if identity not in counted_xobjects:
                        counted_xobjects.add(identity)
                        expanded += len(obj.get_data())
                        if expanded > MAX_EXPANDED_BYTES:
                            return refused("expanded_text_limit")
                    if obj.get("/Subtype") == "/Form":
                        resources.append(obj.get("/Resources"))
            text = (
                page.extract_text(
                    orientations=(0, 90, 180, 270),
                    space_width=200.0,
                    extraction_mode="plain",
                )
                .replace("\r\n", "\n")
                .replace("\r", "\n")
            )
            # shortcut: empty/folio text marks scans; widen with scan fixtures.
            if has_image and (
                not text.strip() or re.fullmatch(r"[0-9]{1,4}", text.strip())
            ):
                return refused("no_digital_text")
            pages.append(text if text.endswith("\n") else text + "\n")
        if not any(text.strip() for text in pages):
            return refused("no_digital_text")
        tagged = "".join(f"[page {i}]\n{text}" for i, text in enumerate(pages, 1))
        if len(tagged.encode("utf-8")) > MAX_TEXT_BYTES:
            return refused("text_limit")
        invoice_id = re.compile(
            r"(?im)^[ \t]*invoice[ \t]+(?:id|number|no\.?|#)"
            r"[ \t]*:[ \t]*([^\n]+)"
        )
        invoice_ids = {value.strip() for value in invoice_id.findall(tagged)}
        if len(invoice_ids) > 1:
            return refused("multiple_invoices")
        # shortcut: same-ID page headers imply continuation; widen with fixtures.
        for page_text in pages:
            sections = re.split(
                r"(?im)^[ \t]*(?:(?:synthetic|tax|commercial)[ \t]+)*invoice[ \t]*$",
                page_text,
            )
            if sum(bool(invoice_id.search(section)) for section in sections) > 1:
                return refused("multiple_invoices")
        return PDFText(
            version="1",
            status="text",
            pages=pages,
            text=tagged,
            text_sha256=hashlib.sha256(tagged.encode("utf-8")).hexdigest(),
            adapter=ADAPTER,
        )
    except MemoryError:
        return refused("memory_limit")
    except Exception:
        return refused("corrupt")


if __name__ == "__main__":
    print(worker().model_dump_json())
