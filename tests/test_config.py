from auto_ai_cr.config import AppConfig, ToolConfig


def test_config_round_trip_preserves_command_tool():
    config = AppConfig(
        tool="command",
        tools={"command": ToolConfig(type="command", command="cat")},
    )

    restored = AppConfig.from_mapping(config.to_mapping())

    assert restored.tool == "command"
    assert restored.tools["command"].command == "cat"


def test_config_merges_default_ai_tool_presets():
    restored = AppConfig.from_mapping({"tools": {"command": {"type": "command", "command": "x"}}})

    assert restored.tools["codex"].command == "codex review -"
    assert restored.tools["claude"].command.startswith("claude -p")
    assert restored.tools["command"].command == "x"
