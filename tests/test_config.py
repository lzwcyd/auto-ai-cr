from auto_ai_cr.config import AppConfig, ToolConfig


def test_config_round_trip_preserves_command_tool():
    config = AppConfig(
        tool="command",
        tools={"command": ToolConfig(type="command", command="cat")},
        fix_tool="command",
        fix_tools={"command": ToolConfig(type="command", command="cat > /tmp/fix")},
    )

    restored = AppConfig.from_mapping(config.to_mapping())

    assert restored.tool == "command"
    assert restored.tools["command"].command == "cat"
    assert restored.fix_tool == "command"
    assert restored.fix_tools["command"].command == "cat > /tmp/fix"


def test_config_merges_default_ai_tool_presets():
    restored = AppConfig.from_mapping({"tools": {"command": {"type": "command", "command": "x"}}})

    assert restored.tools["codex"].command == "codex review -"
    assert restored.tools["claude"].command.startswith("claude -p")
    assert restored.tools["cursor"].command == "cursor-agent -p --output-format text"
    assert restored.fix_tools["codex"].command.startswith("codex exec")
    assert restored.fix_tools["cursor"].command.startswith("cursor-agent -p")
    assert restored.tools["command"].command == "x"
