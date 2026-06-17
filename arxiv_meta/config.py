#!/usr/bin/env python3
"""Configuration loader for the local arXiv metadata service."""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.yaml"

DEFAULT_DB_PATH = "/Volumes/My Book/ARXIV/arxiv_oai_title_fts.sqlite"
DEFAULT_JSONL_PATH = "/Volumes/My Book/ARXIV/arxiv-metadata-oai-snapshot.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8110

DEFAULT_CONFIG: dict[str, Any] = {
    "db": {
        "path": DEFAULT_DB_PATH,
        "read": {
            "immutable": True,
            "query_only": True,
            "mmap_size": 268435456,
            "cache_size": -80000,
        },
    },
    "server": {"host": DEFAULT_HOST, "port": DEFAULT_PORT},
    "data": {"jsonl": DEFAULT_JSONL_PATH},
}

_config_cache: dict[str, Any] | None = None


def _deep_update(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def load_config(reload: bool = False) -> dict[str, Any]:
    """Load config.yaml plus environment overrides."""
    global _config_cache
    if _config_cache is not None and not reload:
        return _config_cache

    cfg_path = Path(os.environ.get("_TEST_ARXIV_META_CONFIG") or CONFIG_PATH)
    config = deepcopy(DEFAULT_CONFIG)
    config["_config_path"] = str(cfg_path)

    if cfg_path.exists():
        with cfg_path.open(encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"Config file must contain a mapping: {cfg_path}")
        _deep_update(config, loaded)

    overrides = {
        "ARXIV_META_DB": ("db", "path"),
        "ARXIV_META_HOST": ("server", "host"),
        "ARXIV_META_PORT": ("server", "port"),
        "ARXIV_META_JSONL": ("data", "jsonl"),
        "ARXIV_META_DB_IMMUTABLE": ("db", "read", "immutable"),
        "ARXIV_META_DB_QUERY_ONLY": ("db", "read", "query_only"),
        "ARXIV_META_DB_MMAP_SIZE": ("db", "read", "mmap_size"),
        "ARXIV_META_DB_CACHE_SIZE": ("db", "read", "cache_size"),
    }
    for env_key, cfg_keys in overrides.items():
        val = os.environ.get(env_key)
        if val:
            d = config
            for key in cfg_keys[:-1]:
                d = d.setdefault(key, {})
            if env_key in {"ARXIV_META_PORT", "ARXIV_META_DB_MMAP_SIZE", "ARXIV_META_DB_CACHE_SIZE"}:
                d[cfg_keys[-1]] = int(val)
            elif env_key in {"ARXIV_META_DB_IMMUTABLE", "ARXIV_META_DB_QUERY_ONLY"}:
                d[cfg_keys[-1]] = _bool_from_env(val)
            else:
                d[cfg_keys[-1]] = val

    _config_cache = config
    return config


def _bool_from_env(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get(key: str, default: Any = None) -> Any:
    """Return a dot-separated config value."""
    value: Any = load_config()
    for part in key.split("."):
        if not isinstance(value, dict):
            return default
        value = value.get(part)
    return value if value is not None else default


def resolve_path(path: str | Path) -> Path:
    """Resolve a user/config path without placing generated data in the package."""
    path = Path(path).expanduser()
    if path.is_absolute():
        return path
    return (Path.cwd() / path).resolve()
