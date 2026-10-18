"""Drive tau's real extension loader on the installed tau_search/ext/search.py.

Not a unittest (no model, no network, but it exercises the installed package
and tau's private-by-underscore loader, so it is run by hand):

    .venv/bin/python tests/check_loader.py
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

from tau_agent_core.extension_types import ExtensionAPI
from tau_agent_core.extensions.registry import ExtensionRegistry
from tau_agent_core.extensions.runner import ExtensionHandlers
from tau_agent_core.sdk import _load_one_extension

# Find the file without importing it: the loader must be the one to exec it.
spec = importlib.util.find_spec("tau_search.ext.search")
path = Path(spec.origin)
registry = ExtensionRegistry()


def api_factory(label):
    return ExtensionAPI(registry=registry, hook_handlers=ExtensionHandlers(path=label),
                        config={"limit": 3})


loaded = asyncio.run(_load_one_extension(path, api_factory))

tools = registry.get_active_tools()
assert sorted(tools) == ["web_fetch", "web_search"], sorted(tools)
for name, tool in tools.items():
    assert tool.namespace == "search", (name, tool.namespace)
    assert tool.exposure == "direct", (name, tool.exposure)
    assert tool.output_schema, name
assert tools["web_search"].parameters["properties"]["limit"]["default"] == 3
assert loaded.config_schema["title"] == "Web search and fetch"
assert [f["name"] for f in loaded.config_schema["fields"]] == [
    "backends", "searxng_url", "limit", "max_chars"]
assert "tau_search.ext.search" not in sys.modules, \
    "loader should not have imported the extension by package name"
print(f"loaded {loaded.path}")
for name, tool in sorted(tools.items()):
    print(f"  {tool.namespace}/{name} exposure={tool.exposure} "
          f"params={sorted(tool.parameters['properties'])}")
print("OK")
