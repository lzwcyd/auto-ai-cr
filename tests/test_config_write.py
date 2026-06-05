from auto_ai_cr.config import AppConfig, load_config, write_config


def test_write_config_round_trip(tmp_path):
    config = AppConfig(scope="staged", base_branch="main")

    write_config(tmp_path, config)
    restored = load_config(tmp_path)

    assert restored.scope == "staged"
    assert restored.base_branch == "main"
