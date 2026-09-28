"""The interactive docs are part of the product: /docs is useless if the OpenAPI document can't be built."""

from tests.conftest import make_client


def test_openapi_document_builds(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200, r.text
    spec = r.json()
    assert spec["openapi"].startswith("3.1")
    assert {"/v1/auth/token", "/v1/catalog/jobs", "/v1/catalog/jobs/{job_id}/download"} <= set(spec["paths"])


def test_docs_page_loads(client):
    r = client.get("/docs")
    assert r.status_code == 200 and "swagger-ui" in r.text


def test_upload_fields_render_as_file_pickers(client):
    # Swagger UI only shows a file picker for OpenAPI 3.1 string schemas with contentMediaType.
    props = (client.get("/openapi.json").json()["paths"]["/v1/catalog/jobs"]["post"]["requestBody"]
             ["content"]["multipart/form-data"]["schema"]["properties"])
    for field in ("file", "master_data"):
        assert props[field]["contentMediaType"] == "application/octet-stream"
        assert "format" not in props[field]


def test_download_documents_an_xlsx_200(client):
    op = client.get("/openapi.json").json()["paths"]["/v1/catalog/jobs/{job_id}/download"]["get"]
    assert "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in op["responses"]["200"]["content"]


def test_docs_can_be_disabled(tmp_path):
    with make_client(tmp_path, enable_docs=False) as c:
        assert c.get("/docs").status_code == 404
        assert c.get("/openapi.json").status_code == 404
