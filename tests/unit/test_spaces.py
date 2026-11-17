import re

import pytest
from fastapi.testclient import TestClient

from bidbox import web

client = TestClient(web.app)

ROUTES = {
    "app": ["/app", "/app/review/case-1", "/app/records/ocds-x/2026/1", "/app/approvals", "/app/approvals/case-1/tok",
            "/app/reports", "/app/reports/case-1", "/app/data"],
    "connect": ["/connect", "/connect/new", "/connect/ocds_source", "/connect/status", "/connect/architecture"],
    "developers": ["/developers", "/developers/manifest", "/developers/cli", "/developers/api", "/developers/samples"],
}


def nav(html: str) -> str:
    return re.search(r'<nav id="(?:main|space)-nav".*?</nav>', html, re.S).group(0)


@pytest.mark.parametrize("space,path", [(space, path) for space, paths in ROUTES.items() for path in paths])
def test_every_route_returns_its_shell(space, path):
    response = client.get(path)
    assert response.status_code == 200
    assert f'data-space="{space}"' in response.text


def test_auditor_nav_is_review_only():
    html = client.get("/app").text
    labels = re.findall(r">([^<]+)</a>", nav(html))
    assert labels == ["Review", "Approvals", "Reports", "Data"]
    for word in ("connector", "integration", "economics", "architecture", "yaml", "view-switch"):
        assert word not in html.lower()


def test_connect_nav_has_no_review_screens():
    html = client.get("/connect").text
    assert re.findall(r">([^<]+)</a>", nav(html)) == ["Connections", "Add a system", "Status", "Architecture"]
    assert "/app/review" not in html and "approval" not in html.lower()


def test_titles_are_distinct():
    titles = {re.search(r"<title>(.*?)</title>", client.get(path).text).group(1) for path in ("/", "/app", "/connect", "/developers")}
    assert len(titles) == 4


def test_unknown_pages_and_api_404():
    page = client.get("/app/nope/deeper/x", headers={"accept": "text/html"})
    assert page.status_code == 404 and "This page does not exist." in page.text
    assert client.get("/nowhere", headers={"accept": "text/html"}).status_code == 404
    api = client.get("/api/nope")
    assert api.status_code == 404 and api.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("old,new", [("/connectors", "/connect"), ("/connectors/new", "/connect/new"),
                                     ("/architecture", "/connect/architecture"), ("/app/admin/economics", "/connect/status"),
                                     ("/app/admin/replays", "/app/reports")])
def test_old_paths_redirect_permanently(old, new):
    response = client.get(old, follow_redirects=False)
    assert response.status_code == 301 and response.headers["location"] == new
