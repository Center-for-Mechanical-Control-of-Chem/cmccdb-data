"""Read-only PDF indexing and visual grounding through Poppler.

No text, caption, or embedded PDF instruction is executed. Original files are
hashed and copied to the run so later edits cannot silently change citations.
"""

import hashlib
import math
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        h = hashlib.file_digest(stream, "sha256")
    return h.hexdigest()


def executable(name: str, directory: str | None) -> str:
    path = str(Path(directory) / name) if directory else shutil.which(name)
    if not path or not Path(path).is_file():
        raise ValueError(f"Poppler {name} is required; configure --poppler-bin")
    return path


def command(args, *, timeout=120):
    proc = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    if proc.returncode:
        raise ValueError(f"Document processing failed: {proc.stderr.decode(errors='replace')[:1500]}")
    return proc.stdout


def index_pdf(path: Path, poppler_bin: str | None = None) -> dict:
    sha = digest(path)
    source_id = "src-" + sha[:24]
    xml = command([executable("pdftotext", poppler_bin), "-bbox-layout", "-enc", "UTF-8", str(path), "-"])
    # Some PDF fonts yield control glyphs forbidden by XML 1.0. Preserve their
    # positions as replacement characters and report every affected codepoint;
    # never guess the missing scientific symbol. The archived PDF remains intact.
    text_xml = xml.decode("utf-8", errors="strict")
    invalid = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
    replacements = {}
    for match in invalid.finditer(text_xml):
        code = f"U+{ord(match.group()):04X}"
        replacements[code] = replacements.get(code, 0) + 1
    root = ET.fromstring(invalid.sub("\ufffd", text_xml))
    pages, lines = [], []
    for number, page in enumerate(root.iter("{http://www.w3.org/1999/xhtml}page"), 1):
        pid = f"{source_id}:p{number:04d}"
        width, height = float(page.attrib["width"]), float(page.attrib["height"])
        count = 0
        for line in page.iter("{http://www.w3.org/1999/xhtml}line"):
            text = " ".join("".join(w.itertext()) for w in line if w.tag.endswith("word"))
            if not text.strip():
                continue
            count += 1
            bbox = [float(line.attrib[k]) for k in ["xMin", "yMin", "xMax", "yMax"]]
            lines.append(dict(evidence_id=f"{pid}:l{count:04d}", source_id=source_id,
                              page=number, text=text, bbox=bbox, kind="text"))
        pages.append(dict(evidence_id=pid, source_id=source_id, page=number,
                          width=width, height=height, kind="page", text="",
                          text_lines=count, needs_visual_reading=count == 0))
    if not pages:
        raise ValueError("PDF contains no readable page inventory")
    text = "\n".join(l["text"] for l in lines)
    doi = re.search(r"\b10\.\d{4,9}/[A-Za-z0-9._;()/:-]+", text)
    return dict(source_id=source_id, sha256=sha, filename=path.name, pages=pages,
                lines=lines, doi_hint=doi.group().rstrip(".,;)") if doi else None,
                index_version="poppler-bbox-v2", coordinate_system="PDF points; top-left origin",
                text_substitutions=replacements,
                warnings=["Unmapped PDF control glyphs were replaced with U+FFFD; inspect page pixels"]
                         if replacements else [])


def index_source(path: Path, poppler_bin: str | None = None) -> dict:
    """Index original SI cells/lines without evaluating formulas or TeX commands.

    Non-PDF locations carry sheet/cell or file-line coordinates, never fictional
    PDF coordinates. Charts still require an independently reviewed image source.
    """
    extension = path.suffix.lower()
    if extension == ".pdf":
        result = index_pdf(path, poppler_bin)
        result["source_format"] = "pdf"
        return result
    sha = digest(path)
    sid = "src-" + sha[:24]
    pages, lines, warnings = [], [], []
    if extension == ".tex":
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("TeX text exceeds the 20 MiB indexing limit")
        for number, value in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if value.strip():
                lines.append(dict(evidence_id=f"{sid}:p0001:l{number:06d}", source_id=sid,
                    page=1, text=value, bbox=None, kind="text", file_line=number))
        pages.append(dict(evidence_id=sid+":p0001", source_id=sid, page=1,
            kind="text_file", text="", text_lines=len(lines), needs_visual_reading=False))
        coordinate_system = "Original UTF-8 file line numbers; TeX is not executed"
        version = "utf8-lines-v1"
    elif extension == ".xlsx":
        from openpyxl import load_workbook
        # Bound decompressed input before handing XML to a workbook reader.
        with zipfile.ZipFile(path) as archive:
            if sum(x.file_size for x in archive.infolist()) > 512 * 1024 * 1024:
                raise ValueError("Workbook exceeds the 512 MiB decompressed SI budget")
        original = load_workbook(path, read_only=True, data_only=False, keep_links=False)
        cached = load_workbook(path, read_only=True, data_only=True, keep_links=False)
        try:
            if len(original.worksheets) > 200:
                raise ValueError("SI workbook exceeds 200 worksheets")
            total = 0
            for number, sheet in enumerate(original.worksheets, 1):
                if sheet.max_row * sheet.max_column > 2_000_000:
                    raise ValueError("SI worksheet exceeds the 2-million-cell indexing budget")
                pid = f"{sid}:p{number:04d}"
                count = 0
                cached_rows = cached[sheet.title].iter_rows()
                for row_number, (row, cached_row) in enumerate(zip(sheet.iter_rows(), cached_rows), 1):
                    entries = []
                    populated = []
                    for cell, cached_cell in zip(row, cached_row):
                        if cell.value is None:
                            continue
                        total += 1
                        if total > 2_000_000:
                            raise ValueError("SI workbook exceeds 2 million populated cells")
                        value = cell.value.isoformat() if hasattr(cell.value, "isoformat") else str(cell.value)
                        entry = sheet.title+"!"+cell.coordinate+" = "+value
                        if cell.data_type == "f":
                            cache = cached_cell.value
                            cache = cache.isoformat() if hasattr(cache, "isoformat") else cache
                            entry += " [cached result: " + (str(cache) if cache is not None else "missing") + "]"
                        entries.append(entry)
                        populated.append(cell.coordinate)
                    if entries:
                        count += 1
                        # Index a source row once rather than repeating metadata
                        # for each cell in large model grids. Every cell address,
                        # original value/formula and cached result is retained.
                        lines.append(dict(evidence_id=pid+f":r{row_number:06d}", source_id=sid,
                            page=number, text=" | ".join(entries), bbox=None, kind="text",
                            sheet=sheet.title, row=row_number,
                            cell_range=populated[0]+":"+populated[-1]))
                pages.append(dict(evidence_id=pid, source_id=sid, page=number,
                    kind="worksheet", sheet=sheet.title, text="", text_lines=count,
                    needs_visual_reading=False))
        finally:
            original.close()
            cached.close()
        coordinate_system = "Original worksheet name and cell address; formulas are not executed"
        version = "xlsx-rows-v1"
        warnings.append("Formula caches are source data, not recalculated results; charts and images are not indexed as numerical measurements")
    else:
        raise ValueError("Supported source formats are PDF, XLSX supporting information, and UTF-8 TeX")
    return dict(source_id=sid, sha256=sha, filename=path.name, pages=pages, lines=lines,
        doi_hint=None, index_version=version, source_format=extension[1:],
        coordinate_system=coordinate_system, text_substitutions={}, warnings=warnings)


def check_bbox(bbox, page):
    if len(bbox) != 4 or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                 and math.isfinite(v) for v in bbox):
        raise ValueError("bbox requires four finite coordinates")
    x0, y0, x1, y1 = bbox
    if not (0 <= x0 < x1 <= page["width"] and 0 <= y0 < y1 <= page["height"]):
        raise ValueError("bbox is outside the source page")


def render_page(path, page, output, poppler_bin=None, bbox=None, dpi=110):
    if not 50 <= dpi <= 180:
        raise ValueError("dpi must be between 50 and 180")
    args = [executable("pdftoppm", poppler_bin), "-f", str(page["page"]), "-l", str(page["page"]),
            "-singlefile", "-r", str(dpi), "-png"]
    if bbox is not None:
        check_bbox(bbox, page)
        # Cover the whole requested box despite fractional pixel coordinates.
        scale = dpi / 72
        x, y = math.floor(bbox[0] * scale), math.floor(bbox[1] * scale)
        w, h = math.ceil(bbox[2] * scale) - x, math.ceil(bbox[3] * scale) - y
        args += ["-x", str(x), "-y", str(y), "-W", str(w), "-H", str(h)]
    args += [str(path), str(output.with_suffix(""))]
    command(args)
    if not output.is_file():
        raise ValueError("Page rendering did not produce an image")
    return output
