"""Paths and settings shared by every part of the demo."""

from __future__ import annotations

import json
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "cau-hinh"
MODEL_DIR = ROOT / "mo-hinh"
WEB_DIR = ROOT / "web"
LOG_DIR = ROOT / "nhat-ky"


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def network() -> dict:
    return load_json(CONFIG_DIR / "mang.json")


def signals_config() -> dict:
    return load_json(CONFIG_DIR / "tin-hieu.json")


def model() -> dict:
    return load_json(MODEL_DIR / "ban-thu.json")


def setup_logging(name: str) -> None:
    logging.basicConfig(level=logging.INFO,
                        format=f"%(asctime)s {name:<9} %(message)s", datefmt="%H:%M:%S")
