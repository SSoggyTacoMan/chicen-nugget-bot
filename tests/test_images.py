from types import SimpleNamespace

from bot.ai import images
from bot.db import Database


def _settings(**overrides):
    values = {
        "imagine_model": "google/gemini-3.1-flash-image",
        "imagine_fallback_model": "google/gemini-2.5-flash-image",
        "ai_image_cost_usd": 0.04,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_imagine_config_uses_env_defaults():
    cfg = images.imagine_config(_settings())
    assert cfg.model == "google/gemini-3.1-flash-image"
    assert cfg.fallback == "google/gemini-2.5-flash-image"
    assert cfg.cost_usd == 0.04


def test_imagine_config_prefers_db_and_can_disable_fallback(tmp_path):
    db = Database(tmp_path / "images.db")
    db.init()
    db.kv_set("config:imagine_model", "google/gemini-nano-banana-2.1")
    db.kv_set("config:imagine_fallback_model", "none")
    db.kv_set("config:imagine_cost_usd", "0.08")

    cfg = images.imagine_config(_settings(), db)
    assert cfg.model == "google/gemini-nano-banana-2.1"
    assert cfg.fallback is None
    assert cfg.cost_usd == 0.08


def test_model_options_include_the_nano_banana_family():
    values = {value for value, _ in images.MODEL_OPTIONS}
    assert {
        "google/gemini-3.1-flash-image",
        "google/gemini-nano-banana-2.1",
        "google/gemini-3-pro-image",
        "google/gemini-3.1-flash-lite-image",
    } <= values
