"""The Claude Code plugin and the marketplace that lists it agree with each other."""

import json
import re
import tomllib
from pathlib import Path

from desk.server import TOOLS

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "mcp__plugin_trading-desk_desk__"


def _json(*parts: str) -> dict:
    return json.loads(ROOT.joinpath(*parts).read_text())


def test_the_plugin_and_its_marketplace_agree():
    market = _json(".claude-plugin", "marketplace.json")
    plugin = _json("plugin", ".claude-plugin", "plugin.json")
    assert market["name"] == "trading-desk"
    assert market["owner"]["name"]
    assert len(market["plugins"]) == 1
    entry = market["plugins"][0]
    assert entry["name"] == plugin["name"] == "trading-desk"
    assert entry["source"] == "./plugin"
    assert _json("plugin", ".mcp.json") == {
        "mcpServers": {"desk": {"command": "trading-desk", "args": ["mcp"]}}
    }
    assert "version" not in plugin
    assert len(TOOLS) == 16
    assert all(len(PREFIX + name) <= 128 for name in TOOLS)
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    assert "trading-desk" in scripts


def test_yfinance_is_a_dependency_not_an_extra():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert any(d.startswith("yfinance") for d in project["dependencies"])
    assert "optional-dependencies" not in project


def test_the_readme_gives_the_commands_that_work():
    readme = (ROOT / "README.md").read_text()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    repo = project["urls"]["Repository"].removeprefix("https://github.com/")
    market = _json(".claude-plugin", "marketplace.json")["name"]
    plugin = _json("plugin", ".claude-plugin", "plugin.json")["name"]
    assert f"uv tool install {project['name']}\n" in readme and "git+https" not in readme
    assert f"/plugin marketplace add {repo}" in readme
    assert f"/plugin install {plugin}@{market}" in readme
    assert f"{PREFIX}*" in readme
    assert all(f"`{name}`" in readme for name in TOOLS)


def test_the_readme_links_work_on_pypi():
    readme = (ROOT / "README.md").read_text()
    targets = re.findall(r"\]\(([^)\s]+)\)", readme)
    assert targets and all(t.startswith(("https://", "mailto:")) for t in targets), targets
    prose = re.sub(r"^```.*?^```", "", readme, flags=re.M | re.S)   # a fence's `## x` is not one
    anchors = {re.sub(r"[^\w -]", "", h.lower()).replace(" ", "-")   # GitHub keeps `_`
               for h in re.findall(r"^#+ (.+)$", prose, re.M)}
    page = "https://github.com/IngTian/trading-desk#"
    assert {t.removeprefix(page) for t in targets if t.startswith(page)} <= anchors
