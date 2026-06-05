from auto_ai_cr.config import AppConfig, ToolConfig


def test_config_round_trip_preserves_command_tool():
    config = AppConfig(
        tool="command",
        tools={"command": ToolConfig(type="command", command="cat")},
    )

    restored = AppConfig.from_mapping(config.to_mapping())

    assert restored.tool == "command"
    assert restored.tools["command"].command == "cat"
