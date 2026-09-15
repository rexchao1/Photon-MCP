from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest

from photon_mcp import api, server
from photon_mcp.api import (
    Credentials,
    PhotonAPIError,
    PhotonClient,
    PhotonConfigError,
    resolve_allowed_dirs,
    resolve_base_url,
    strip_bulky_fields,
)

CREDS = Credentials(
    client_id="cid", secret_key="skey", username="u@e.com", api_key="akey", password="pw"
)

ENV_VARS = (
    "PHOTON_CLIENT_ID", "PHOTON_SECRET_KEY", "PHOTON_USERNAME",
    "PHOTON_API_KEY", "PHOTON_PASSWORD",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS + ("PHOTON_ENV", "PHOTON_BASE_URL", "PHOTON_ALLOWED_DIRS"):
        monkeypatch.delenv(name, raising=False)
    server._client = None


def build_client(handler, **kwargs):
    client = PhotonClient(credentials=CREDS, base_url="https://api.test", **kwargs)
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def json_route(payload, status=200):
    def handler(request):
        handler.last = request
        return httpx.Response(status, json=payload)
    handler.last = None
    return handler


class TestCredentials:
    def test_missing_all_names_every_variable(self):
        with pytest.raises(PhotonConfigError) as excinfo:
            Credentials.from_env()
        for name in ENV_VARS:
            assert name in str(excinfo.value)

    def test_blank_value_counts_as_missing(self, monkeypatch):
        for name in ENV_VARS:
            monkeypatch.setenv(name, "x")
        monkeypatch.setenv("PHOTON_API_KEY", "   ")
        with pytest.raises(PhotonConfigError, match="PHOTON_API_KEY"):
            Credentials.from_env()

    def test_headers_match_the_documented_scheme(self):
        assert CREDS.headers() == {
            "CLIENT-ID": "cid",
            "SECRET-KEY": "skey",
            "AUTHORIZATION": "apikey u@e.com:akey",
            "PASSWORD": "pw",
        }


class TestConfiguration:
    def test_defaults_to_sandbox(self):
        assert resolve_base_url() == api.SANDBOX_BASE_URL

    @pytest.mark.parametrize("value", ["production", "prod", "live"])
    def test_production_aliases(self, monkeypatch, value):
        monkeypatch.setenv("PHOTON_ENV", value)
        assert resolve_base_url() == api.PRODUCTION_BASE_URL

    def test_base_url_override_wins_and_strips_slash(self, monkeypatch):
        monkeypatch.setenv("PHOTON_ENV", "production")
        monkeypatch.setenv("PHOTON_BASE_URL", "https://local.test/")
        assert resolve_base_url() == "https://local.test"

    def test_unknown_environment_is_rejected(self, monkeypatch):
        monkeypatch.setenv("PHOTON_ENV", "staging")
        with pytest.raises(PhotonConfigError, match="sandbox"):
            resolve_base_url()

    def test_allowed_dirs_unset_is_empty(self):
        assert resolve_allowed_dirs() == []

    def test_allowed_dirs_splits_and_resolves(self, monkeypatch, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir(), b.mkdir()
        monkeypatch.setenv("PHOTON_ALLOWED_DIRS", f"{a}{os.pathsep}{b}")
        assert resolve_allowed_dirs() == [a.resolve(), b.resolve()]

    def test_allowed_dirs_does_not_split_a_windows_drive_letter(self, monkeypatch):
        monkeypatch.setattr(os, "pathsep", ";")
        monkeypatch.setenv("PHOTON_ALLOWED_DIRS", r"C:\docs;D:\scans")
        assert len(resolve_allowed_dirs()) == 2


class TestStripBulkyFields:
    def test_removes_raw_text_at_any_depth(self):
        payload = {
            "Total": 1,
            "Raw_Text": "x" * 5000,
            "Line_Items": [{"SKU": "1", "raw_text": "y"}],
            "nested": {"RawText": "z", "keep": True},
        }
        assert strip_bulky_fields(payload) == {
            "Total": 1,
            "Line_Items": [{"SKU": "1"}],
            "nested": {"keep": True},
        }

    def test_preserves_lookalike_field_names(self):
        payload = {"Vendor_Raw_Name": "ACME", "Raw_Text": "drop"}
        assert strip_bulky_fields(payload) == {"Vendor_Raw_Name": "ACME"}

    def test_passes_scalars_through(self):
        assert strip_bulky_fields("s") == "s"
        assert strip_bulky_fields(7) == 7


class TestUploadValidation:
    def make_file(self, tmp_path, name="doc.pdf", size=2048):
        path = tmp_path / name
        path.write_bytes(b"x" * size)
        return path

    def test_accepts_a_valid_file(self, tmp_path):
        path = self.make_file(tmp_path)
        assert build_client(json_route({})).resolve_upload(str(path)) == path.resolve()

    def test_rejects_missing_file(self, tmp_path):
        with pytest.raises(PhotonConfigError, match="No file found"):
            build_client(json_route({})).resolve_upload(str(tmp_path / "nope.pdf"))

    def test_rejects_file_below_minimum_size(self, tmp_path):
        path = self.make_file(tmp_path, size=10)
        with pytest.raises(PhotonConfigError, match="under"):
            build_client(json_route({})).resolve_upload(str(path))

    def test_rejects_file_above_maximum_size(self, tmp_path, monkeypatch):
        monkeypatch.setattr(api, "MAX_FILE_BYTES", 4096)
        path = self.make_file(tmp_path, size=8192)
        with pytest.raises(PhotonConfigError, match="over"):
            build_client(json_route({})).resolve_upload(str(path))

    def test_rejects_unsupported_extension(self, tmp_path):
        path = self.make_file(tmp_path, name="payload.exe")
        with pytest.raises(PhotonConfigError, match="not supported"):
            build_client(json_route({})).resolve_upload(str(path))

    def test_rejects_path_outside_allowed_dirs(self, tmp_path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = self.make_file(tmp_path, name="outside.pdf")
        client = build_client(json_route({}), allowed_dirs=[allowed.resolve()])
        with pytest.raises(PhotonConfigError, match="outside"):
            client.resolve_upload(str(outside))

    def test_allows_nested_path_inside_allowed_dirs(self, tmp_path):
        allowed = tmp_path / "allowed"
        (allowed / "deep").mkdir(parents=True)
        inside = self.make_file(allowed / "deep", name="ok.pdf")
        client = build_client(json_route({}), allowed_dirs=[allowed.resolve()])
        assert client.resolve_upload(str(inside)) == inside.resolve()

    def test_allowlist_is_checked_before_size_and_type(self, tmp_path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = self.make_file(tmp_path, name="tiny.exe", size=1)
        client = build_client(json_route({}), allowed_dirs=[allowed.resolve()])
        with pytest.raises(PhotonConfigError, match="outside"):
            client.resolve_upload(str(outside))


class TestExtractRequest:
    def test_requires_exactly_one_source(self):
        client = build_client(json_route({}))
        with pytest.raises(PhotonConfigError, match="exactly one"):
            client.extract()
        with pytest.raises(PhotonConfigError, match="exactly one"):
            client.extract(url="https://e.test/a.pdf", file_path="/tmp/a.pdf")

    def test_rejects_unknown_doctype(self):
        with pytest.raises(PhotonConfigError, match="Unknown doctype"):
            build_client(json_route({})).extract(url="https://e.test/a.pdf", doctype="spaceship")

    def test_posts_to_the_pro_endpoint_with_expected_params(self):
        handler = json_route({"data": {"Total": 1}})
        client = build_client(handler)
        client.extract(
            url="https://e.test/a.pdf", doctype="statement", page_start=2, page_end=5,
            subaccount="cust-1", reference_id="ref-9",
        )
        request = handler.last
        assert request.method == "POST"
        assert request.url.path == "/api/pro"
        assert dict(request.url.params) == {
            "url": "https://e.test/a.pdf", "doctype": "statement",
            "page_start": "2", "page_end": "5", "subaccount": "cust-1", "ID": "ref-9",
        }

    def test_omits_unset_parameters(self):
        handler = json_route({"data": {}})
        build_client(handler).extract(url="https://e.test/a.pdf")
        assert dict(handler.last.url.params) == {"url": "https://e.test/a.pdf"}

    def test_sends_authentication_headers(self):
        handler = json_route({"data": {}})
        build_client(handler).extract(url="https://e.test/a.pdf")
        assert handler.last.headers["CLIENT-ID"] == "cid"
        assert handler.last.headers["AUTHORIZATION"] == "apikey u@e.com:akey"


class TestErrorHandling:
    def test_maps_api_error_message(self):
        handler = json_route(
            {"message": "Authentication failed. Please check your credentials"}, 401
        )
        with pytest.raises(PhotonAPIError) as excinfo:
            build_client(handler).get_json("k")
        assert excinfo.value.status_code == 401
        assert "Authentication failed" in str(excinfo.value)

    def test_non_json_body_raises_rather_than_returning_none(self):
        def handler(request):
            return httpx.Response(200, text="<html>gateway</html>")
        with pytest.raises(PhotonAPIError, match="non-JSON"):
            build_client(handler).get_json("k")

    def test_transport_failure_is_wrapped(self):
        def handler(request):
            raise httpx.ConnectError("refused")
        with pytest.raises(PhotonAPIError, match="Could not reach"):
            build_client(handler).health()


class TestExtractionResultShape:
    def test_reports_extracted_when_data_present(self):
        result = server._extraction_result({"data": {"Total": 5, "photon_key": "k1"}})
        assert result["ok"] is True
        assert result["status"] == "extracted"
        assert result["photon_key"] == "k1"
        assert result["extraction"] == {"Total": 5, "photon_key": "k1"}

    def test_strips_raw_text_from_extraction(self):
        result = server._extraction_result({"data": {"Total": 5, "Raw_Text": "big"}})
        assert "Raw_Text" not in result["extraction"]

    def test_notes_when_no_photon_key_is_returned(self):
        result = server._extraction_result({"data": {"Total": 5, "photon_key": ""}})
        assert "photon_key" not in result
        assert "cannot be retrieved" in result["note"]

    def test_reports_queued_when_only_a_key_comes_back(self):
        result = server._extraction_result({"photon_key": "k2", "status": "success"})
        assert result["status"] == "queued"
        assert result["photon_key"] == "k2"
        assert "get_extraction" in result["note"]

    def test_surfaces_doc_path_for_save_original_document(self):
        result = server._extraction_result({"photon_key": "k", "doc_path": "data/u/x.pdf"})
        assert result["doc_path"] == "data/u/x.pdf"


class TestQueuedFollowUp:
    def test_queued_document_is_upgraded_when_extraction_is_ready(self, monkeypatch):
        for name in ENV_VARS:
            monkeypatch.setenv(name, "x")
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/api/pro":
                return httpx.Response(200, json={"photon_key": "k", "doc_path": "d.pdf"})
            return httpx.Response(200, json={"data": {"Total": 7, "Raw_Text": "big"}})

        server._client = build_client(handler)
        result = server.process_document(url="https://e.test/a.pdf")
        assert calls == ["/api/pro", "/api/v4/json"]
        assert result["status"] == "extracted"
        assert result["extraction"] == {"Total": 7}
        assert "note" not in result

    def test_still_queued_when_extraction_is_not_ready(self, monkeypatch):
        for name in ENV_VARS:
            monkeypatch.setenv(name, "x")

        def handler(request):
            if request.url.path == "/api/pro":
                return httpx.Response(200, json={"photon_key": "k"})
            return httpx.Response(200, json={"data": None})

        server._client = build_client(handler)
        result = server.process_document(url="https://e.test/a.pdf")
        assert result["status"] == "queued"
        assert "get_extraction" in result["note"]

    def test_follow_up_failure_does_not_break_the_result(self, monkeypatch):
        for name in ENV_VARS:
            monkeypatch.setenv(name, "x")

        def handler(request):
            if request.url.path == "/api/pro":
                return httpx.Response(200, json={"photon_key": "k"})
            return httpx.Response(500, json={"message": "boom"})

        server._client = build_client(handler)
        result = server.process_document(url="https://e.test/a.pdf")
        assert result["ok"] is True
        assert result["status"] == "queued"


class TestClassify:
    def test_normalizes_detected_type_to_a_doctype_key(self, monkeypatch):
        for name in ENV_VARS:
            monkeypatch.setenv(name, "x")
        server._client = build_client(json_route({"data": {"document_type": "Invoice"}}))
        result = server.classify_document(file_path=None, url="https://e.test/a.pdf")
        assert result["document_type"] == "Invoice"
        assert result["suggested_doctype"] == "invoice"

    def test_detection_is_case_insensitive(self):
        assert api.suggested_doctype("receipt") == "receipt"
        assert api.suggested_doctype("RECEIPT") == "receipt"
        assert api.suggested_doctype("Invoice-Commercial") == "invoice-commercial"
        assert api.suggested_doctype("ShippingLabel") == "shippinglabel"

    def test_unmappable_type_is_flagged_rather_than_guessed(self, monkeypatch):
        for name in ENV_VARS:
            monkeypatch.setenv(name, "x")
        server._client = build_client(
            json_route({"data": {"document_type": "passport"}})
        )
        result = server.classify_document(url="https://e.test/a.pdf")
        assert "suggested_doctype" not in result
        assert "not a recognized doctype" in result["note"]

    def test_url_failure_suggests_file_path(self, monkeypatch):
        for name in ENV_VARS:
            monkeypatch.setenv(name, "x")
        server._client = build_client(
            json_route({"message": "Request could not be completed"}, 400)
        )
        result = server.classify_document(url="https://e.test/a.pdf")
        assert result["ok"] is False
        assert "file_path" in result["hint"]


class TestCorrectionErrorHandling:
    @pytest.fixture(autouse=True)
    def stub(self, monkeypatch):
        for name in ENV_VARS:
            monkeypatch.setenv(name, "x")
        server._client = build_client(
            json_route({"message": "Request could not be completed"}, 400)
        )

    @pytest.mark.parametrize(
        "call",
        [
            lambda: server.correct_fields("k", {"Total": 1}),
            lambda: server.add_line_item("k", {"Amount": 1}),
            lambda: server.correct_line_item("k", "1", {"SKU": "x"}),
            lambda: server.delete_line_item("k", "1"),
        ],
    )
    def test_correction_errors_carry_account_guidance(self, call):
        result = call()
        assert result["ok"] is False
        assert result["hint"] == server.CORRECTION_UNAVAILABLE_HINT

    def test_the_hint_discourages_retrying(self):
        hint = server.CORRECTION_UNAVAILABLE_HINT
        assert "support@photoncommerce.com" in hint
        assert "not change the outcome" in hint

    def test_add_line_item_uses_the_update_namespace(self):
        handler = json_route({"status": "success"})
        server._client = build_client(handler)
        server.add_line_item("k", {"Amount": 1})
        assert handler.last.url.path == "/api/v4/update/line-items"


class TestToolLayer:
    def test_missing_credentials_returns_error_not_exception(self):
        result = server.process_document(url="https://e.test/a.pdf")
        assert result["ok"] is False
        assert "PHOTON_CLIENT_ID" in result["error"]

    def test_check_connection_reports_missing_credentials(self):
        result = server.check_connection()
        assert result["ok"] is False
        assert result["credentials"] == "missing"

    def test_save_original_document_survives_missing_credentials(self, tmp_path):
        result = server.save_original_document("some/path.pdf", str(tmp_path))
        assert result["ok"] is False

    def test_auth_failure_carries_a_hint(self, monkeypatch):
        for name in ENV_VARS:
            monkeypatch.setenv(name, "x")
        handler = json_route({"message": "Authentication failed"}, 401)
        server._client = build_client(handler)
        result = server.get_extraction("k")
        assert result["ok"] is False
        assert "PHOTON_CLIENT_ID" in result["hint"]

    def test_quota_failure_carries_a_hint(self, monkeypatch):
        for name in ENV_VARS:
            monkeypatch.setenv(name, "x")
        handler = json_route({"message": "The free trial includes 20 pages"}, 403)
        server._client = build_client(handler)
        result = server.get_extraction("k")
        assert "api@photoncommerce.com" in result["hint"]

    def test_corrections_require_fields(self):
        assert server.correct_fields("k", {})["ok"] is False
        assert server.add_line_item("k", {})["ok"] is False
        assert server.correct_line_item("k", "1", {})["ok"] is False

    def test_list_document_types_needs_no_credentials(self):
        result = server.list_document_types()
        assert result["ok"] is True
        assert "invoice" in result["doctypes"]
        assert result["max_file_bytes"] == api.MAX_FILE_BYTES


@pytest.fixture(scope="module")
def tools():
    return {t.name: t for t in asyncio.run(server.server.list_tools())}


class TestToolRegistration:
    def test_every_documented_tool_is_registered(self, tools):
        assert set(tools) == {
            "list_document_types", "process_document", "get_extraction",
            "classify_document", "split_document", "correct_fields",
            "add_line_item", "correct_line_item", "delete_line_item",
            "save_original_document", "get_usage", "check_connection",
        }

    def test_deleting_a_line_item_is_marked_destructive(self, tools):
        assert tools["delete_line_item"].annotations.destructive_hint is True

    @pytest.mark.parametrize(
        "name", ["get_extraction", "get_usage", "check_connection", "list_document_types"]
    )
    def test_read_only_tools_are_marked(self, tools, name):
        assert tools[name].annotations.read_only_hint is True

    def test_no_other_tool_is_marked_destructive(self, tools):
        destructive = [n for n, t in tools.items() if t.annotations.destructive_hint]
        assert destructive == ["delete_line_item"]

    def test_every_tool_has_a_description(self, tools):
        assert all(t.description and len(t.description) > 40 for t in tools.values())

    def test_process_document_exposes_only_supported_parameters(self, tools):
        properties = set(tools["process_document"].input_schema["properties"])
        assert properties == {
            "file_path", "url", "doctype", "page_start",
            "page_end", "subaccount", "reference_id",
        }

    def test_tool_results_are_json_serialisable(self, tools):
        json.dumps(server.list_document_types())
