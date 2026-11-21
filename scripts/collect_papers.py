"""Collect papers newest-first. Keep a paper only after a valid PDF is saved."""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

import findpapers
from findpapers.core.paper import Paper
from findpapers.query.parser import QueryParser

# Archive root. Each paper is stored by publication date: YYYY-MM/DD/
OUTPUT_ROOT = Path(r"D:\20260929_Power")
SINCE = datetime.date(2010, 1, 1)
SEARCH_DATABASES = ["arxiv", "pubmed", "semantic_scholar"]
# Reject empty files and HTML error pages saved with a PDF content type.
MIN_PDF_BYTES = 1024
_STEM_RE = re.compile(r"[dt][0-9a-f]{16}\Z")
_DOI_URL_RE = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.IGNORECASE)
_DOI_LABEL_RE = re.compile(r"^doi:\s*", re.IGNORECASE)

# Each entry is one technology-purpose pair. Phrases stay at three words or fewer
# so PubMed still matches them. Do not merge these into a single OR query.
# Collected on 2026-09-30. main() does not search these, so that run's failures are not retried.
FINISHED_SEARCHES: dict[str, str] = {
    "hydro_efficiency": "[hydropower] AND [efficiency improvement]",
    "hydro_output": "[hydropower] AND [power output]",
    "hydro_turbine": "[hydropower] AND [turbine] AND [optimization]",
    "hydro_uprating": "[hydropower] AND [uprating]",
    "hydro_rehabilitation": "[hydropower] AND [rehabilitation]",
    "hydro_modernization": "[hydropower] AND [modernization]",
    "hydro_retrofit": "[hydropower] AND [retrofit]",
    "hydro_operation": "[hydropower] AND [optimal operation]",
    "hydro_maintenance": "[hydropower] AND [predictive maintenance]",
    "hydro_digital_twin": "[hydropower] AND [digital twin]",
    "hydraulic_turbine": "[hydraulic turbine] AND [efficiency]",
    "francis_turbine": "[francis turbine] AND [efficiency]",
    "kaplan_turbine": "[kaplan turbine] AND [efficiency]",
    "pelton_turbine": "[pelton turbine] AND [efficiency]",
    "thermal_efficiency": "[thermal power plant] AND [efficiency improvement]",
    "thermal_output": "[thermal power plant] AND [power output]",
    "thermal_heat_rate": "[thermal power plant] AND [heat rate]",
    "thermal_boiler": "[thermal power plant] AND [boiler] AND [optimization]",
    "thermal_steam_turbine": "[thermal power plant] AND [steam turbine] AND [optimization]",
    "thermal_condenser": "[thermal power plant] AND [condenser] AND [optimization]",
    "thermal_waste_heat": "[thermal power plant] AND [waste heat recovery]",
    "thermal_retrofit": "[thermal power plant] AND [retrofit]",
    "thermal_modernization": "[thermal power plant] AND [modernization]",
    "thermal_performance": "[thermal power plant] AND [performance] AND [optimization]",
}

# Vocabulary that was not in FINISHED_SEARCHES. Same DOI still resolves to the saved PDF.
SEARCHES: dict[str, str] = {
    "hydroelectric_efficiency": "[hydroelectric] AND [efficiency]",
    "hydroelectric_output": "[hydroelectric] AND [power output]",
    "hydroelectric_plant": "[hydroelectric plant] AND [efficiency]",
    "pumped_storage_efficiency": "[pumped storage] AND [efficiency]",
    "pumped_storage_optimization": "[pumped storage] AND [optimization]",
    "pump_turbine": "[pump turbine] AND [efficiency]",
    "water_turbine": "[water turbine] AND [optimization]",
    "hydro_optimisation": "[hydropower] AND [optimisation]",
    "runner_blade": "[runner blade] AND [hydropower]",
    "draft_tube": "[draft tube] AND [efficiency]",
    "guide_vane": "[guide vane] AND [turbine]",
    "combined_cycle_efficiency": "[combined cycle] AND [efficiency]",
    "combined_cycle_plant": "[combined cycle] AND [power plant]",
    "coal_fired_efficiency": "[coal fired] AND [efficiency]",
    "coal_plant": "[coal fired] AND [power plant]",
    "gas_turbine_plant": "[gas turbine] AND [power plant]",
    "steam_turbine_efficiency": "[steam turbine] AND [efficiency]",
    "heat_rate_optimization": "[heat rate] AND [optimization]",
    "supercritical_plant": "[supercritical] AND [power plant]",
    "feedwater_optimization": "[feedwater] AND [power plant]",
    "thermal_optimisation": "[thermal power plant] AND [optimisation]",
    "plant_modernisation": "[power plant] AND [modernisation]",
}


def log(message: str) -> None:
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    line = f"[{stamp}] {message}"
    print(line, flush=True)


def normalize_doi(doi: str | None) -> str:
    """Return a lowercase DOI with a resolver URL or 'doi:' label removed."""
    text = (doi or "").strip()
    text = _DOI_URL_RE.sub("", text)
    text = _DOI_LABEL_RE.sub("", text).strip()
    return text.lower()


def file_stem(paper: Paper) -> str:
    """Stable stem from the normalised DOI, or from the title when DOI is missing.

    The stem is a hash, so DOIs that differ only by punctuation cannot share a filename.
    """
    doi = normalize_doi(paper.doi)
    if doi:
        digest = hashlib.sha256(doi.encode("utf-8")).hexdigest()[:16]
        return f"d{digest}"
    title = paper.title or "paper"
    digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:16]
    return f"t{digest}"


def legacy_file_stem(paper: Paper) -> str:
    """Previous filename stem, kept so already downloaded PDFs are recognised."""
    doi = (paper.doi or "").strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:", "", doi, flags=re.IGNORECASE).strip()
    if doi:
        stem = re.sub(r"[^\w.-]", "_", doi).strip("._")
        if not stem:
            return ""
        if len(stem) > 80:
            digest = hashlib.sha256(doi.encode("utf-8")).hexdigest()[:8]
            stem = f"{stem[:70]}_{digest}"
        return stem
    title = paper.title or "paper"
    return "t" + hashlib.sha256(title.encode("utf-8")).hexdigest()[:12]


def candidate_stems(paper: Paper) -> list[str]:
    stems = [file_stem(paper)]
    legacy = legacy_file_stem(paper)
    if legacy and legacy not in stems:
        stems.append(legacy)
    return stems


def is_valid_pdf(path: Path) -> bool:
    """True when the file exists, is large enough, and starts with a PDF header."""
    try:
        if not path.is_file() or path.stat().st_size < MIN_PDF_BYTES:
            return False
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def paper_dir(paper: Paper) -> Path:
    """Return OUTPUT_ROOT/YYYY-MM/DD for the paper's publication date."""
    published = paper.publication_date
    if published is None:
        return OUTPUT_ROOT / "unknown" / "unknown"
    month_dir = f"{published.year:04d}-{published.month:02d}"
    day_dir = f"{published.day:02d}"
    return OUTPUT_ROOT / month_dir / day_dir


def write_metadata(path: Path, paper: Paper, search_name: str, query: str, pdf_name: str) -> None:
    payload = {
        "search_name": search_name,
        "query": query,
        "pdf_file": pdf_name,
        "paper": paper.to_dict(),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def remove_empty_dirs(path: Path) -> None:
    """Remove empty date folders up to, but not including, the archive root."""
    current = path
    while current != OUTPUT_ROOT and current.exists() and not any(current.iterdir()):
        current.rmdir()
        current = current.parent


def discard_incomplete(pdf_path: Path, json_path: Path) -> None:
    """Remove a PDF that failed validation, and its metadata when the PDF is gone."""
    if pdf_path.exists() and not is_valid_pdf(pdf_path):
        pdf_path.unlink()
    if json_path.exists() and not is_valid_pdf(pdf_path):
        json_path.unlink()


STATE_DIR = OUTPUT_ROOT / "_state"
NO_PDF_PATH = STATE_DIR / "no_pdf.json"


def is_state_path(path: Path) -> bool:
    return "_state" in path.parts


def delete_invalid_pdfs(root: Path) -> int:
    """Delete PDFs that are empty or not PDF files, plus their metadata."""
    if not root.exists():
        return 0
    removed = 0
    for pdf_path in list(root.rglob("*.pdf")):
        if is_state_path(pdf_path) or is_valid_pdf(pdf_path):
            continue
        discard_incomplete(pdf_path, pdf_path.with_suffix(".json"))
        remove_empty_dirs(pdf_path.parent)
        removed += 1
    return removed


def delete_metadata_without_pdf(root: Path) -> int:
    """Delete paper JSON files that have no matching PDF. Leave resume state alone."""
    if not root.exists():
        return 0
    removed = 0
    for json_path in list(root.rglob("*.json")):
        if is_state_path(json_path) or is_valid_pdf(json_path.with_suffix(".pdf")):
            continue
        json_path.unlink()
        removed += 1
        remove_empty_dirs(json_path.parent)
    return removed


def prune_folders_without_documents(root: Path) -> int:
    """Delete folders that contain neither a PDF nor paper metadata."""
    if not root.exists():
        return 0
    directories = [
        path
        for path in root.rglob("*")
        if path.is_dir() and path != root and not is_state_path(path)
    ]
    directories.sort(key=lambda path: len(path.parts), reverse=True)
    removed = 0
    for directory in directories:
        if not directory.exists():
            continue
        has_document = any(
            child.is_file() and child.suffix.lower() in {".pdf", ".json"}
            for child in directory.rglob("*")
            if not is_state_path(child)
        )
        if has_document:
            continue
        shutil.rmtree(directory, ignore_errors=True)
        removed += 1
    return removed


def count_saved_pdfs(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(
        1
        for path in root.rglob("*.pdf")
        if path.is_file() and not is_state_path(path) and is_valid_pdf(path)
    )


def load_no_pdf() -> set[str]:
    if not NO_PDF_PATH.exists():
        return set()
    try:
        loaded = json.loads(NO_PDF_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log("no_pdf.json unreadable, starting with an empty skip list")
        return set()
    if not isinstance(loaded, list):
        return set()
    stems = {item for item in loaded if isinstance(item, str) and _STEM_RE.fullmatch(item)}
    dropped = len(loaded) - len(stems)
    if dropped:
        log(f"dropped {dropped} stale no_pdf entries; those papers will be tried again")
        save_no_pdf(stems)
    return stems


def save_no_pdf(stems: set[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = NO_PDF_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(sorted(stems), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(NO_PDF_PATH)


def search_cache_path(search_name: str, query: str) -> Path:
    """Cache path bound to the query, SINCE, and databases, so edits are searched again."""
    token_source = f"{query}\n{SINCE.isoformat()}\n{','.join(SEARCH_DATABASES)}"
    token = hashlib.sha256(token_source.encode("utf-8")).hexdigest()[:12]
    return STATE_DIR / f"{search_name}_{token}.json"


def legacy_search_cache_path(search_name: str) -> Path:
    return STATE_DIR / f"{search_name}.json"


def papers_from_complete_cache(path: Path, query: str) -> list[Paper] | None:
    """Return cached papers only when the query, date, and every database succeeded."""
    loaded = findpapers.load_from_json(str(path))
    papers = getattr(loaded, "papers", None)
    if papers is None:
        return None
    if getattr(loaded, "query", None) != query:
        return None
    if getattr(loaded, "since", None) != SINCE:
        return None
    if getattr(loaded, "failed_databases", []):
        return None
    cached_databases = list(getattr(loaded, "databases", []) or [])
    if set(cached_databases) != set(SEARCH_DATABASES):
        return None
    return list(papers)


def load_or_search(engine: findpapers.Engine, search_name: str, query: str) -> list[Paper]:
    """Reuse a finished search after a restart. Search again when no complete cache exists."""
    cache = search_cache_path(search_name, query)
    for path in (cache, legacy_search_cache_path(search_name)):
        if not path.exists():
            continue
        try:
            papers = papers_from_complete_cache(path, query)
        except Exception:
            log(f"search cache unreadable, searching again: {search_name}")
            path.unlink(missing_ok=True)
            continue
        if papers is None:
            path.unlink(missing_ok=True)
            continue
        log(f"resume search: {search_name} papers={len(papers)}")
        return papers

    result = engine.search(
        query,
        databases=list(SEARCH_DATABASES),
        since=SINCE,
        num_workers=3,
        enrichment_databases=["crossref"],
        show_progress=True,
    )
    papers = list(result.papers)
    if result.failed_databases:
        log(
            f"search partial, not cached: {search_name} papers={len(papers)} "
            f"failed_databases={result.failed_databases}"
        )
        return papers

    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_suffix(".json.tmp")
        findpapers.save_to_json(result, str(temporary))
        temporary.replace(cache)
    except Exception:
        log(f"search cache write failed: {search_name}")
        traceback.print_exc()
    log(f"search done: {search_name} papers={len(papers)}")
    return papers


def legacy_pdf_belongs_to(json_path: Path, paper: Paper) -> bool:
    """True when an old-style filename belongs to this paper.

    Old stems could collide. A sibling metadata file whose DOI or title differs
    belongs to another paper and must be left in place.
    """
    if not json_path.exists():
        return True
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    stored = payload.get("paper") if isinstance(payload, dict) else None
    if not isinstance(stored, dict):
        return False
    stored_doi = normalize_doi(stored.get("doi") if isinstance(stored.get("doi"), str) else None)
    paper_doi = normalize_doi(paper.doi)
    if paper_doi or stored_doi:
        return stored_doi == paper_doi
    return stored.get("title") == paper.title


def locate_saved_pdf(paper: Paper) -> tuple[Path, Path] | None:
    """Return an existing valid PDF and its metadata path, deleting invalid files."""
    destination = paper_dir(paper)
    current_stem = file_stem(paper)
    for stem in candidate_stems(paper):
        pdf_path = destination / f"{stem}.pdf"
        json_path = pdf_path.with_suffix(".json")
        if stem != current_stem and not legacy_pdf_belongs_to(json_path, paper):
            continue
        if is_valid_pdf(pdf_path):
            return pdf_path, json_path
        if pdf_path.exists() or json_path.exists():
            discard_incomplete(pdf_path, json_path)
            remove_empty_dirs(destination)
    return None


def log_says_no_urls(temp_dir: Path) -> bool:
    """True when the downloader had no URL to try. Other failures stay retryable."""
    log_path = temp_dir / "download_log.txt"
    if not log_path.exists():
        return False
    text = log_path.read_text(encoding="utf-8", errors="replace")
    return "(no URLs available)" in text


def largest_valid_pdf(temp_dir: Path) -> Path | None:
    candidates = [path for path in temp_dir.glob("*.pdf") if is_valid_pdf(path)]
    if not candidates:
        return None
    candidates.sort(key=lambda path: path.stat().st_size, reverse=True)
    return candidates[0]


def store_paper(
    engine: findpapers.Engine,
    paper: Paper,
    search_name: str,
    query: str,
    no_pdf: set[str],
) -> str:
    """Save a paper only when a valid PDF is downloaded.

    A paper with no URL is recorded and skipped later. A download that was attempted
    and failed is left unrecorded so the next run tries it again.
    """
    saved = locate_saved_pdf(paper)
    if saved is not None:
        pdf_path, json_path = saved
        if not json_path.exists():
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            write_metadata(json_path, paper, search_name, query, pdf_path.name)
        return "already"

    stem = file_stem(paper)
    if stem in no_pdf:
        return "no_pdf"

    destination = paper_dir(paper)
    pdf_path = destination / f"{stem}.pdf"
    json_path = destination / f"{stem}.json"
    no_urls = False
    try:
        with tempfile.TemporaryDirectory(prefix="findpapers-") as temp_dir:
            temp = Path(temp_dir)
            engine.download([paper], str(temp), timeout=45.0, show_progress=False)
            no_urls = log_says_no_urls(temp)
            chosen = largest_valid_pdf(temp)
            if chosen is not None:
                destination.mkdir(parents=True, exist_ok=True)
                shutil.move(str(chosen), pdf_path)
        if is_valid_pdf(pdf_path):
            write_metadata(json_path, paper, search_name, query, pdf_path.name)
            return "saved"
        discard_incomplete(pdf_path, json_path)
        remove_empty_dirs(destination)
        if no_urls:
            no_pdf.add(stem)
            save_no_pdf(no_pdf)
            return "no_pdf"
        return "retry"
    except Exception:
        discard_incomplete(pdf_path, json_path)
        remove_empty_dirs(destination)
        raise


def status_line(downloaded: int, stats: dict[str, int], search_name: str, index: int, total: int) -> str:
    return (
        f"다운로드 {downloaded}건 "
        f"(새로 {stats['saved']}, 이어받기 {stats['already']}, "
        f"PDF 없음 {stats['no_pdf']}, 다시 시도 {stats['retry']}, 실패 {stats['failed']}) "
        f"| {search_name} {index}/{total}"
    )


def main() -> int:
    parser = QueryParser()
    for name, query in SEARCHES.items():
        parser.parse(query)
        log(f"query ok: {name}")
    log(
        f"이번 실행 검색어 {len(SEARCHES)}개. "
        f"이미 수집한 검색어 {len(FINISHED_SEARCHES)}개는 건너뜁니다."
    )

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    removed_invalid = delete_invalid_pdfs(OUTPUT_ROOT)
    removed_json = delete_metadata_without_pdf(OUTPUT_ROOT)
    removed_dirs = prune_folders_without_documents(OUTPUT_ROOT)
    if removed_invalid or removed_json or removed_dirs:
        log(
            f"cleaned invalid_pdf={removed_invalid} metadata={removed_json} folders={removed_dirs}"
        )

    already_on_disk = count_saved_pdfs(OUTPUT_ROOT)
    no_pdf = load_no_pdf()
    stats = {"saved": 0, "already": 0, "no_pdf": 0, "retry": 0, "failed": 0}
    log(f"이어서 진행합니다. 이미 받은 PDF {already_on_disk}건, PDF 없음 기록 {len(no_pdf)}건")

    engine = findpapers.Engine()
    failures = 0
    search_items = list(SEARCHES.items())

    for topic_index, (name, query) in enumerate(search_items, start=1):
        log(f"search start: {name} ({topic_index}/{len(search_items)})")
        try:
            papers = load_or_search(engine, name, query)
        except Exception:
            failures += 1
            stats["failed"] += 1
            log(f"search failed: {name}")
            log(status_line(already_on_disk + stats["saved"], stats, name, 0, 0))
            traceback.print_exc()
            continue

        papers = sorted(
            papers,
            key=lambda paper: paper.publication_date or datetime.date.min,
            reverse=True,
        )
        for index, paper in enumerate(papers, start=1):
            try:
                outcome = store_paper(engine, paper, name, query, no_pdf)
            except Exception:
                failures += 1
                stats["failed"] += 1
                log(f"store failed: {name} title={paper.title}")
                traceback.print_exc()
            else:
                stats[outcome] += 1
            log(status_line(already_on_disk + stats["saved"], stats, name, index, len(papers)))

    removed_dirs = prune_folders_without_documents(OUTPUT_ROOT)
    if removed_dirs:
        log(f"cleaned folders={removed_dirs}")
    log(status_line(count_saved_pdfs(OUTPUT_ROOT), stats, "finished", len(search_items), len(search_items)))
    log(f"finished failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
