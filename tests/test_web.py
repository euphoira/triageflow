from html.parser import HTMLParser

from fastapi.testclient import TestClient

from triageflow.api import create_app
from triageflow.config import Settings


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and attributes.get("src"):
            self.urls.append(attributes["src"])
        if tag == "link" and attributes.get("rel") == "stylesheet":
            self.urls.append(attributes["href"])


def test_dashboard_and_all_assets_are_served_locally(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "TriageFlow" in response.text
        parser = Assets()
        parser.feed(response.text)
        assert len(parser.urls) >= 2
        for url in parser.urls:
            assert url.startswith("/static/")
            asset = client.get(url)
            assert asset.status_code == 200, url
            assert len(asset.content) > 100


def test_openapi_includes_public_workflow_endpoints(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        document = client.get("/openapi.json").json()
        assert "/api/runs" in document["paths"]
        assert "/api/runs/{run_id}/resume" in document["paths"]
        assert "/api/runs/{run_id}/report" in document["paths"]
