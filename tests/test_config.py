from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from procure.config import CustomerConfig, load_config

CONFIG = Path(__file__).resolve().parent.parent / "config" / "brunner.yaml"


def raw() -> dict:
    return yaml.safe_load(CONFIG.read_text("utf-8"))


def test_brunner_config_loads() -> None:
    cfg = load_config(CONFIG)
    assert cfg.policy.min_valid_quotes == 3
    assert (
        cfg.policy.blocked_reason("  BOHEMIA bearings s.r.o. ") == "failed supplier audit 06/2026"
    )
    assert cfg.policy.blocked_reason("Müller Wälzlager GmbH") is None


def test_weights_must_sum_to_one() -> None:
    data = raw()
    data["policy"]["weights"]["price"] = 0.7
    with pytest.raises(ValidationError, match="add up to 1"):
        CustomerConfig.model_validate(data)


def test_unknown_key_is_rejected() -> None:
    data = raw()
    data["policy"]["approval_treshold_eur"] = 1  # typo
    with pytest.raises(ValidationError):
        CustomerConfig.model_validate(data)


def test_fx_table_needs_eur() -> None:
    data = raw()
    del data["fx_to_eur"]["EUR"]
    with pytest.raises(ValidationError, match="EUR"):
        CustomerConfig.model_validate(data)
