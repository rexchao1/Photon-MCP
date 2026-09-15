from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from photon_mcp import api
from photon_mcp.api import (
    DOCTYPES,
    MAX_FILE_BYTES,
    MIN_FILE_BYTES,
    SUPPORTED_SUFFIXES,
    PhotonAPIError,
    PhotonClient,
    PhotonConfigError,
    strip_bulky_fields,
)

server = MCPServer(
    name="photon-commerce",
    title="Photon Commerce",
    version="0.1.0",
    instructions=(
        "Extract structured data from financial and logistics documents through the "
        "Photon Commerce API. Documents are submitted either as a local file path or a "
        "publicly reachable URL, and every result carries a photon_key that addresses "
        "it for later retrieval and correction. When a user names a document type, map "
        "it to one of the doctype keys from list_document_types before calling "
        "process_document."
    ),
)

_client: PhotonClient | None = None

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)
LOCAL_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)


def get_client() -> PhotonClient:
    global _client
    if _client is None:
        _client = PhotonClient()
    return _client


def _fail(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, PhotonAPIError):
        result: dict[str, Any] = {"ok": False, "error": str(exc)}
        if exc.status_code is not None:
            result["status_code"] = exc.status_code
        if exc.status_code == 401:
            result["hint"] = (
                "Authentication failed. Check PHOTON_CLIENT_ID, PHOTON_SECRET_KEY, "
                "PHOTON_USERNAME, PHOTON_API_KEY and PHOTON_PASSWORD, and confirm "
                "PHOTON_ENV matches the account those credentials belong to."
            )
        elif exc.status_code == 403:
            result["hint"] = (
                "The account is out of quota. Sandbox accounts include 20 documents "
                "over 15 days, and after the first 3 documents, the account has to be "
                "verified. If the user has not verified yet, point them at the link "
                "Photon emailed them. Otherwise ask whether they would like a production "
                "account, and if so tell them to email api@photoncommerce.com with their "
                "registered email. There is no self-serve upgrade yet."
            )
        return result
    return {"ok": False, "error": str(exc)}


CORRECTION_UNAVAILABLE_HINT = (
    "Editing extractions is enabled per account. Ask the user to contact "
    "support@photoncommerce.com to have it turned on. Retrying, or reformatting the "
    "photon_key, will not change the outcome."
)


def _correction_fail(exc: Exception) -> dict[str, Any]:
    result = _fail(exc)
    result.setdefault("hint", CORRECTION_UNAVAILABLE_HINT)
    return result


def _extraction_result(payload: Any) -> dict[str, Any]:
    body = payload.get("data") if isinstance(payload, dict) else None
    photon_key = None
    doc_path = None
    if isinstance(payload, dict):
        photon_key = payload.get("photon_key")
        doc_path = payload.get("doc_path")
    if isinstance(body, dict) and not photon_key:
        photon_key = body.get("photon_key") or body.get("Photon_Key")

    result: dict[str, Any] = {"ok": True}
    if photon_key:
        result["photon_key"] = photon_key
    if doc_path:
        result["doc_path"] = doc_path
    if body:
        result["status"] = "extracted"
        result["extraction"] = strip_bulky_fields(body)
        if not photon_key:
            result["note"] = (
                "The API returned no photon_key for this document, so it cannot be "
                "retrieved or corrected later. The extraction below is the only copy."
            )
    else:
        result["status"] = "queued"
        result["note"] = (
            "The document was accepted for asynchronous processing. Call "
            "get_extraction with the photon_key once it has been processed."
        )
        if isinstance(payload, dict):
            result["response"] = strip_bulky_fields(payload)
    if isinstance(payload, dict) and payload.get("message"):
        result["message"] = payload["message"]
    return result


@server.tool(
    name="list_document_types",
    title="List document types",
    description=(
        "List the document type keys the Photon Commerce API accepts for both "
        "extraction and classification, the supported file formats, and the file size "
        "limits. Call this before process_document when unsure which doctype to pass. "
        "Makes no API call and needs no credentials."
    ),
    annotations=LOCAL_ONLY,
)
def list_document_types() -> dict[str, Any]:
    return {
        "ok": True,
        "doctypes": DOCTYPES,
        "default_doctype": "invoice",
        "supported_file_types": sorted(SUPPORTED_SUFFIXES),
        "min_file_bytes": MIN_FILE_BYTES,
        "max_file_bytes": MAX_FILE_BYTES,
        "note": (
            "If no doctype is passed, the document is treated as an invoice. "
            "classify_document reports types from this same set, so its "
            "suggested_doctype can be passed straight to process_document."
        ),
    }


@server.tool(
    name="process_document",
    title="Process a document",
    description=(
        "Send a document to Photon Commerce for data extraction and return the "
        "structured JSON. Supply exactly one of file_path (a file on this machine) or "
        "url (a publicly reachable link). Pass doctype when the type is known — it "
        "defaults to invoice otherwise. page_start and page_end limit processing to a "
        "page range. Depending on how the account is configured, this either returns "
        "the extraction directly or queues the document and returns a photon_key to "
        "poll with get_extraction."
    ),
    annotations=WRITES,
)
def process_document(
    file_path: str | None = None,
    url: str | None = None,
    doctype: str | None = None,
    page_start: int | None = None,
    page_end: int | None = None,
    subaccount: str | None = None,
    reference_id: str | None = None,
) -> dict[str, Any]:
    try:
        client = get_client()
        payload = client.extract(
            file_path=file_path,
            url=url,
            doctype=doctype,
            page_start=page_start,
            page_end=page_end,
            subaccount=subaccount,
            reference_id=reference_id,
        )
    except (PhotonAPIError, PhotonConfigError) as exc:
        return _fail(exc)
    result = _extraction_result(payload)
    if result.get("status") == "queued" and result.get("photon_key"):
        _attach_ready_extraction(client, result)
    return result


def _attach_ready_extraction(client: PhotonClient, result: dict[str, Any]) -> None:
    try:
        follow_up = client.get_json(result["photon_key"])
    except (PhotonAPIError, PhotonConfigError):
        return
    body = follow_up.get("data") if isinstance(follow_up, dict) else None
    if not body:
        return
    result["status"] = "extracted"
    result["extraction"] = strip_bulky_fields(body)
    result.pop("note", None)
    result.pop("response", None)


@server.tool(
    name="get_extraction",
    title="Get an extraction by photon_key",
    description=(
        "Retrieve the extracted JSON for a document already submitted to Photon "
        "Commerce, addressed by its photon_key. Use this to poll a document that was "
        "queued for asynchronous processing, or to re-read one processed earlier."
    ),
    annotations=READ_ONLY,
)
def get_extraction(photon_key: str) -> dict[str, Any]:
    try:
        payload = get_client().get_json(photon_key)
    except (PhotonAPIError, PhotonConfigError) as exc:
        return _fail(exc)
    body = payload.get("data") if isinstance(payload, dict) else None
    if not body:
        return {
            "ok": True,
            "photon_key": photon_key,
            "status": "pending",
            "note": "No extraction is available yet. Try again shortly.",
            "response": strip_bulky_fields(payload),
        }
    return {
        "ok": True,
        "photon_key": photon_key,
        "status": "extracted",
        "extraction": strip_bulky_fields(body),
    }


@server.tool(
    name="classify_document",
    title="Detect a document's type",
    description=(
        "Detect what kind of document a file is before extracting it. Prefer file_path, "
        "which is the most reliable input for this endpoint. Returns the detected type "
        "plus suggested_doctype, the key to pass to process_document."
    ),
    annotations=WRITES,
)
def classify_document(file_path: str | None = None, url: str | None = None) -> dict[str, Any]:
    try:
        payload = get_client().classify(file_path=file_path, url=url)
    except (PhotonAPIError, PhotonConfigError) as exc:
        failure = _fail(exc)
        if url and not file_path:
            failure["hint"] = (
                "Classification works most reliably from a local file. Download the "
                "document and pass file_path instead."
            )
        return failure
    data = payload.get("data") if isinstance(payload, dict) else {}
    detected = (data or {}).get("document_type")
    result: dict[str, Any] = {"ok": True, "document_type": detected}
    suggestion = api.suggested_doctype(detected)
    if suggestion:
        result["suggested_doctype"] = suggestion
    else:
        result["note"] = (
            f"{detected!r} is not a recognized doctype; process_document will "
            "treat the document as an invoice unless you pass another doctype."
        )
    if (data or {}).get("photon_key"):
        result["photon_key"] = data["photon_key"]
    return result


@server.tool(
    name="split_document",
    title="Find document boundaries in a multi-document file",
    description=(
        "Detect where each document starts in a PDF that contains several documents "
        "stacked together. Supply exactly one of file_path or url. Returns the page "
        "numbers each embedded document begins on."
    ),
    annotations=WRITES,
)
def split_document(file_path: str | None = None, url: str | None = None) -> dict[str, Any]:
    try:
        payload = get_client().split(file_path=file_path, url=url)
    except (PhotonAPIError, PhotonConfigError) as exc:
        return _fail(exc)
    data = payload.get("data") if isinstance(payload, dict) else None
    return {"ok": True, "result": data if data else payload}


@server.tool(
    name="correct_fields",
    title="Correct header fields on an extraction",
    description=(
        "Overwrite one or more top-level fields on a stored extraction, addressed by "
        "photon_key. Pass fields as an object of field name to new value, for example "
        '{"Total": 1878.8, "Vendor_Name": "Einsteam"}. Only the supplied fields change. '
        "Use this to correct an extraction the user says is wrong."
    ),
    annotations=WRITES,
)
def correct_fields(photon_key: str, fields: dict[str, Any]) -> dict[str, Any]:
    if not fields:
        return {"ok": False, "error": "Pass at least one field to change."}
    try:
        payload = get_client().update_fields(photon_key, fields)
    except (PhotonAPIError, PhotonConfigError) as exc:
        return _correction_fail(exc)
    return {"ok": True, "photon_key": photon_key, "updated": sorted(fields), "response": payload}


@server.tool(
    name="add_line_item",
    title="Add a line item to an extraction",
    description=(
        "Append a line item to a stored extraction, addressed by photon_key. Pass "
        "fields as an object matching the document's line item shape, for example "
        '{"SKU": "003", "Description": "MacBook", "Price": 1299, "Amount": 1299}.'
    ),
    annotations=WRITES,
)
def add_line_item(photon_key: str, fields: dict[str, Any]) -> dict[str, Any]:
    if not fields:
        return {"ok": False, "error": "Pass the line item fields to add."}
    try:
        payload = get_client().add_line_item(photon_key, fields)
    except (PhotonAPIError, PhotonConfigError) as exc:
        return _correction_fail(exc)
    return {"ok": True, "photon_key": photon_key, "response": payload}


@server.tool(
    name="correct_line_item",
    title="Correct a line item on an extraction",
    description=(
        "Overwrite fields on one line item of a stored extraction. line_item_id is the "
        "position of the line item as returned in the extraction. Only the supplied "
        "fields change."
    ),
    annotations=WRITES,
)
def correct_line_item(
    photon_key: str, line_item_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    if not fields:
        return {"ok": False, "error": "Pass at least one field to change."}
    try:
        payload = get_client().update_line_item(photon_key, line_item_id, fields)
    except (PhotonAPIError, PhotonConfigError) as exc:
        return _correction_fail(exc)
    return {
        "ok": True,
        "photon_key": photon_key,
        "line_item_id": line_item_id,
        "updated": sorted(fields),
        "response": payload,
    }


@server.tool(
    name="delete_line_item",
    title="Delete a line item from an extraction",
    description=(
        "Permanently remove one line item from a stored extraction. line_item_id is "
        "the position of the line item as returned in the extraction. This cannot be "
        "undone — confirm with the user before calling it."
    ),
    annotations=DESTRUCTIVE,
)
def delete_line_item(photon_key: str, line_item_id: str) -> dict[str, Any]:
    try:
        payload = get_client().delete_line_item(photon_key, line_item_id)
    except (PhotonAPIError, PhotonConfigError) as exc:
        return _correction_fail(exc)
    return {
        "ok": True,
        "photon_key": photon_key,
        "line_item_id": line_item_id,
        "response": payload,
    }


@server.tool(
    name="save_original_document",
    title="Save a processed document to disk",
    description=(
        "Download the original file for a processed document and write it into "
        "directory. doc_path is returned by process_document alongside the "
        "photon_key. Returns the path written. The directory must already exist."
    ),
    annotations=WRITES,
)
def save_original_document(doc_path: str, directory: str) -> dict[str, Any]:
    try:
        client = get_client()
    except PhotonConfigError as exc:
        return _fail(exc)
    target_dir = Path(directory).expanduser().resolve()
    if not target_dir.is_dir():
        return {"ok": False, "error": f"{target_dir} is not an existing directory."}
    if client.allowed_dirs and not any(
        target_dir == root or root in target_dir.parents for root in client.allowed_dirs
    ):
        allowed = ", ".join(str(root) for root in client.allowed_dirs)
        return {
            "ok": False,
            "error": (
                f"{target_dir} is outside the directories this server may write to "
                f"({allowed})."
            ),
        }
    name = Path(doc_path).name or "document"
    destination = target_dir / name
    try:
        content = client.download(doc_path)
    except (PhotonAPIError, PhotonConfigError) as exc:
        return _fail(exc)
    destination.write_bytes(content)
    return {"ok": True, "path": str(destination), "bytes": len(content)}


@server.tool(
    name="get_usage",
    title="Get API usage for the account",
    description=(
        "Report how many API calls and pages this account has consumed. Optionally "
        "narrow to a year and month, or break the figures out per subaccount."
    ),
    annotations=READ_ONLY,
)
def get_usage(
    year: int | None = None, month: int | None = None, per_subaccount: bool = False
) -> dict[str, Any]:
    try:
        payload = get_client().balance(year=year, month=month, subaccount=per_subaccount)
    except (PhotonAPIError, PhotonConfigError) as exc:
        return _fail(exc)
    data = payload.get("data") if isinstance(payload, dict) else None
    return {"ok": True, "usage": data if data is not None else payload}


@server.tool(
    name="check_connection",
    title="Check API connectivity and configuration",
    description=(
        "Verify that the Photon Commerce API is reachable and that this server has "
        "credentials configured. Call this first when another tool fails, to tell a "
        "configuration problem apart from an API outage."
    ),
    annotations=READ_ONLY,
)
def check_connection() -> dict[str, Any]:
    try:
        client = get_client()
    except PhotonConfigError as exc:
        return {"ok": False, "credentials": "missing", "error": str(exc)}
    result: dict[str, Any] = {
        "ok": True,
        "credentials": "configured",
        "username": client.credentials.username,
        "base_url": client.base_url,
        "allowed_dirs": [str(p) for p in client.allowed_dirs] or "unrestricted",
    }
    try:
        result["health"] = client.health()
    except (PhotonAPIError, PhotonConfigError) as exc:
        result["ok"] = False
        result["health"] = _fail(exc)
    return result


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("PHOTON_LOG_LEVEL", "WARNING").upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
