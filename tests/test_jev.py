from types import SimpleNamespace

from bot.ai.jev import Jev, parse_answers


def test_parse_noul_choice_and_score():
    payload = {
        "answers": {
            "is_urgent": {"type": "noul", "noul": 0.92},
            "category": {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": 0.87, "bug": 0.09, "other": 0.04},
                "confidence": 0.87,
            },
            "anger": {
                "type": "score",
                "score": 1.4,
                "confidence": 0.7,
                "probabilities": {"0": 0.1, "1": 0.4, "2": 0.5},
            },
        }
    }
    answers = parse_answers(payload)
    assert answers["is_urgent"].noul == 0.92
    assert answers["is_urgent"].probability == 0.92
    assert answers["category"].choice == "billing"
    assert answers["category"].confidence == 0.87
    assert answers["anger"].score == 1.4


def test_parse_skips_malformed_entries():
    payload = {
        "answers": {
            "bad_noul": {"type": "noul"},
            "bad_choice": {"type": "choice"},
            "unknown": {"type": "wat"},
            "not_a_dict": "nope",
            "good": {"type": "noul", "noul": 0.4},
        }
    }
    answers = parse_answers(payload)
    assert set(answers) == {"good"}


def test_parse_handles_garbage():
    assert parse_answers(None) == {}
    assert parse_answers({}) == {}
    assert parse_answers({"answers": []}) == {}


def test_endpoint_defaults_to_gateway_path():
    settings = SimpleNamespace(
        ai_jev_url="", ai_base_url="https://ai.hackclub.com/proxy/v1"
    )
    assert Jev(settings).url == "https://ai.hackclub.com/proxy/v1/jev/systemone"


def test_endpoint_can_be_overridden():
    settings = SimpleNamespace(ai_jev_url="https://example.com/jev/", ai_base_url="ignored")
    assert Jev(settings).url == "https://example.com/jev"
