"""Catalog endpoints: upload -> process -> one-time download (files deleted afterwards)."""

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

from app.auth.deps import CurrentPrincipal
from app.catalog.reader import InputFormatError
from app.catalog.service import CatalogInput, generate_catalog
from app.core.errors import ApiError
from app.core.logging import audit
from app.jobs.response import OneTimeFileResponse
from app.jobs.store import OUTPUT, JobMeta, JobStore

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/catalog", tags=["wage type catalog"])

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CHUNK = 1024 * 1024


# ---------- schemas ----------

class JobSummary(BaseModel):
    payslips: int
    line_items: int
    wage_types: int
    provisional_codes: int
    molgas: list[str]
    currencies: dict[str, str]
    code_sources: dict[str, int] = Field(
        default_factory=dict,
        description="Line items by where their wage code came from: input column, mapping file, provisional")


class InputFile(BaseModel):
    name: str
    bytes: int


class Job(BaseModel):
    job_id: str
    status: str = "ready"
    created_at: datetime
    expires_at: datetime
    download_url: str
    input_files: list[InputFile]
    output_filename: str
    output_bytes: int
    summary: JobSummary
    warnings: list[str]


def _job(meta: JobMeta, request: Request) -> Job:
    return Job(
        job_id=meta.job_id,
        created_at=datetime.fromtimestamp(meta.created_at, UTC),
        expires_at=datetime.fromtimestamp(meta.expires_at, UTC),
        download_url=str(request.url_for("download_job", job_id=meta.job_id)),
        input_files=meta.input_files,
        output_filename=meta.output_file,
        output_bytes=meta.output_bytes,
        summary=JobSummary(**meta.summary),
        warnings=meta.warnings,
    )


def _job_not_found() -> ApiError:
    return ApiError(404, "job_not_found", "Job not found, expired, or already downloaded")


# ---------- upload helpers ----------

UPLOAD_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {"multipart/form-data": {"schema": {
            "type": "object",
            "required": ["file"],
            "properties": {
                "file": {"type": "string", "contentMediaType": "application/octet-stream",
                         "description": "Payslip Master Data .xlsx, or the Pay Line Items .csv"},
                "master_data": {"type": "string", "contentMediaType": "application/octet-stream",
                                "description": "Master Data .csv (CSV mode only; omit if `file` is pre-joined)"},
            },
        }}},
    }
}


def _provided(value):
    """None for a form field the user left empty. Browsers and Swagger UI send an untouched file input as an
    empty string or as a part with no filename, rather than omitting it."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, UploadFile) and not value.filename:
        return None
    return value


def _extension(upload: UploadFile) -> str:
    ext = Path(upload.filename or "").suffix.lower()
    if ext == ".xls":
        raise ApiError(415, "unsupported_file_type", "Legacy .xls is not supported; save the workbook as .xlsx")
    if ext not in (".xlsx", ".csv"):
        raise ApiError(415, "unsupported_file_type",
                       f"'{upload.filename}': only .xlsx or .csv files are accepted")
    return ext[1:]


async def _save(upload: UploadFile, dest: Path, max_bytes: int) -> int:
    size = 0
    with dest.open("wb") as out:
        while chunk := await upload.read(CHUNK):
            size += len(chunk)
            if size > max_bytes:
                raise ApiError(413, "file_too_large",
                               f"'{upload.filename}' exceeds the {max_bytes // (1024 * 1024)} MB per-file limit")
            out.write(chunk)
    if size == 0:
        raise ApiError(400, "empty_file", f"'{upload.filename}' is empty")
    return size


@asynccontextmanager
async def _processing_slot(request: Request):
    slots: asyncio.Semaphore = request.app.state.processing_slots
    timeout = request.app.state.settings.processing_queue_timeout_seconds
    try:
        await asyncio.wait_for(slots.acquire(), timeout=timeout)
    except TimeoutError:
        raise ApiError(503, "busy", "The service is at capacity; retry shortly",
                       headers={"Retry-After": "10"}) from None
    try:
        yield
    finally:
        slots.release()


def _safe_stem(filename: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).stem).strip("._")
    return stem[:60] or "upload"


# ---------- endpoints ----------

@router.post(
    "/jobs", response_model=Job, status_code=status.HTTP_201_CREATED, openapi_extra=UPLOAD_OPENAPI,
    summary="Upload payslip data and generate a wage type catalog",
    description=(
        "Accepts **either** the Payslip Master Data workbook (`.xlsx` with sheets *Master Data* and "
        "*Pay Line Items*), **or** CSV: `file` = Pay Line Items and `master_data` = Master Data "
        "(or one pre-joined line-items CSV carrying `molga_country_grouping` and `employment_group`).\n\n"
        "Pay Line Items should carry a `wage_code` column: the catalog has one row per wage code per molga. "
        "Files without it (or blank cells) fall back to the wage-code mapping by wage-type name. "
        "Other sheets in the workbook (e.g. an existing *Wage Type Catalog*) are ignored.\n\n"
        "The uploaded and generated files are kept until the catalog is downloaded once, or until the job expires."
    ),
    responses={413: {"description": "Upload too large"}, 415: {"description": "Not .xlsx/.csv"},
               422: {"description": "File does not match the template; `errors` lists the rows"},
               429: {"description": "Too many pending jobs"}, 503: {"description": "Server busy"}},
)
async def create_job(request: Request, response: Response, principal: CurrentPrincipal) -> Job:
    # No body parameters are declared, so FastAPI hasn't read the body yet: authentication, quota and
    # capacity are all settled before a single upload byte is accepted.
    settings = request.app.state.settings
    store: JobStore = request.app.state.job_store

    if store.count_active(principal.username) >= settings.max_pending_jobs_per_user:
        raise ApiError(429, "too_many_pending_jobs",
                       f"You already have {settings.max_pending_jobs_per_user} jobs waiting; "
                       "download or delete one first")

    meta, job_path = None, None
    try:
        async with request.form(max_files=2, max_fields=2) as form:
            file, master = _provided(form.get("file")), _provided(form.get("master_data"))
            if not isinstance(file, UploadFile):
                raise ApiError(422, "missing_file", "Multipart field 'file' (an .xlsx or .csv) is required")
            if master is not None and not isinstance(master, UploadFile):
                raise ApiError(422, "invalid_input", "'master_data' must be a file")
            kind = _extension(file)
            if master is not None and (kind != "csv" or _extension(master) != "csv"):
                raise ApiError(422, "invalid_input",
                               "'master_data' is only used with a CSV line-items file, and must be .csv itself")

            meta, job_path = store.create(owner=principal.username)
            primary = job_path / f"input.{kind}"
            meta.input_files.append({"name": file.filename or primary.name,
                                     "bytes": await _save(file, primary, settings.max_upload_bytes)})
            master_path = None
            if master is not None:
                master_path = job_path / "input_master.csv"
                meta.input_files.append({"name": master.filename or master_path.name,
                                         "bytes": await _save(master, master_path, settings.max_upload_bytes)})

        codes = request.app.state.wage_codes.get()
        # Bound CPU-heavy work; the slot is held for processing only, never while a client is uploading.
        async with _processing_slot(request):
            result = await run_in_threadpool(
                generate_catalog, CatalogInput(kind, primary, master_path), job_path / OUTPUT, codes)
    except InputFormatError as e:
        if job_path:
            store.delete(job_path)
        audit("job_rejected", username=principal.username, reason=str(e), error_count=len(e.errors))
        raise ApiError(422, "invalid_input", str(e), errors=e.errors) from None
    except BaseException:
        if job_path:
            store.delete(job_path)
        raise

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    meta.output_file = f"Wage_Type_Catalog_{_safe_stem(meta.input_files[0]['name'])}_{stamp}.xlsx"
    meta.output_bytes = (job_path / OUTPUT).stat().st_size
    meta.summary = {
        "payslips": result.payslip_count, "line_items": result.line_count, "wage_types": len(result.rows),
        "provisional_codes": result.provisional_count,
        "molgas": sorted({r.molga for r in result.rows}), "currencies": result.currencies,
        "code_sources": result.code_sources,
    }
    meta.warnings = result.warnings
    store.commit(meta)

    audit("job_created", username=principal.username, job_id=meta.job_id, auth=principal.method,
          inputs=[f["name"] for f in meta.input_files], line_items=result.line_count,
          wage_types=len(result.rows), provisional_codes=result.provisional_count)
    job = _job(meta, request)
    response.headers["Location"] = str(request.url_for("get_job", job_id=meta.job_id))
    return job


@router.get("/jobs", response_model=list[Job], summary="List your jobs that are still waiting to be downloaded")
def list_jobs(request: Request, principal: CurrentPrincipal) -> list[Job]:
    return [_job(m, request) for m in request.app.state.job_store.list(principal.username)]


@router.get("/jobs/{job_id}", name="get_job", response_model=Job, summary="Job details, summary and warnings")
def get_job(job_id: str, request: Request, principal: CurrentPrincipal) -> Job:
    meta = request.app.state.job_store.get(job_id, principal.username)
    if meta is None:
        raise _job_not_found()
    return _job(meta, request)


@router.get(
    "/jobs/{job_id}/download", name="download_job", response_class=OneTimeFileResponse,
    # Explicit: FastAPI otherwise infers it from the response class's __init__, which has no status_code
    # parameter, and OpenAPI generation (and with it /docs) fails.
    status_code=status.HTTP_200_OK,
    summary="Download the catalog — one time only; the uploaded and generated files are then deleted",
    description="If the transfer is interrupted the job is kept, so the download can be retried until it expires. "
                "Range requests are not supported.",
    responses={200: {"content": {XLSX_MIME: {}}, "description": "The Wage Type Catalog workbook"},
               404: {"description": "Unknown, expired or already downloaded"}},
)
def download_job(job_id: str, request: Request, principal: CurrentPrincipal) -> OneTimeFileResponse:
    store: JobStore = request.app.state.job_store
    claimed = store.claim(job_id, principal.username)
    if claimed is None:
        raise _job_not_found()
    meta, path = claimed

    def completed() -> None:
        store.delete(path)
        audit("job_downloaded", username=principal.username, job_id=job_id, bytes=meta.output_bytes)

    def aborted() -> None:
        kept = store.release(meta, path)
        if request.method != "HEAD":
            audit("job_download_interrupted", username=principal.username, job_id=job_id, kept=kept)

    try:
        return OneTimeFileResponse(path / OUTPUT, filename=meta.output_file, media_type=XLSX_MIME,
                                   on_complete=completed, on_abort=aborted)
    except OSError:
        store.delete(path)
        log.exception("Job %s output missing", job_id)
        raise _job_not_found() from None


@router.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Discard a job and its files without downloading")
def discard_job(job_id: str, request: Request, principal: CurrentPrincipal) -> None:
    if not request.app.state.job_store.discard(job_id, principal.username):
        raise _job_not_found()
    audit("job_discarded", username=principal.username, job_id=job_id)
