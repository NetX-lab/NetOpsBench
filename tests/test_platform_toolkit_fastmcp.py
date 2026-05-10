from types import SimpleNamespace


def test_fastmcp_server_lazy_toolkit_initialization(monkeypatch):
    import netopsbench.platform.toolkit.fastmcp_server as server
    import netopsbench.platform.toolkit.mcp.context as context

    class FakeToolkit:
        init_count = 0

        def __init__(self):
            FakeToolkit.init_count += 1

        def get_topology(self):
            return SimpleNamespace(success=True, data={"ok": True}, error=None)

    monkeypatch.setattr(context, "AgentToolkit", FakeToolkit)
    monkeypatch.setattr(context, "_toolkit", None)

    first = server.get_topology()
    second = server.get_topology()

    assert first == {"ok": True}
    assert second == {"ok": True}
    assert FakeToolkit.init_count == 1


def test_fastmcp_server_exposes_grouped_tool_registry():
    import netopsbench.platform.toolkit.fastmcp_server as server

    assert set(server.EXPOSED_TOOLS_BY_GROUP.keys()) == {
        "inventory",
        "observability",
        "connectivity",
    }

    all_tools = []
    for group_tools in server.EXPOSED_TOOLS_BY_GROUP.values():
        assert group_tools
        all_tools.extend(group_tools)

    assert set(all_tools) == set(server.EXPOSED_TOOLS)
    assert len(all_tools) == len(set(all_tools))

    for tool_name in server.EXPOSED_TOOLS:
        assert hasattr(server, tool_name)
