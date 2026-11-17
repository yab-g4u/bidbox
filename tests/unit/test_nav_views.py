import re
from pathlib import Path

INDEX = Path(__file__).resolve().parents[2] / "apps" / "web" / "public" / "index.html"


def test_auditor_nav_hides_developer_views():
    nav = re.search(r'<nav id="main-nav".*?</nav>', INDEX.read_text(encoding="utf-8"), re.S).group(0)
    links = re.findall(r"<a ([^>]*)>([^<]+)</a>", nav)
    auditor = [label for attrs, label in links if 'data-role="integrator"' not in attrs]
    integrator = [label for attrs, label in links if 'data-role="integrator"' in attrs]
    assert auditor == ["Review"]
    assert integrator == ["Economics", "Integrations", "Connectors", "Architecture", "Replay archive"]
    assert "Demo mode: views, not permissions" in INDEX.read_text(encoding="utf-8")
