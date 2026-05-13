#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Configuration loader

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config.yaml"

_config_cache: dict | None = None


def load_config(reload: bool = False) -> dict:
    global _config_cache
    if _config_cache and not reload:
        return _config_cache

    cfg_path = os.environ.get("_TEST_ARXIV_META_CONFIG") or CONFIG_PATH
    cfg_path = Path(cfg_path)

    config = {"_config_path": str(cfg_path)}
    if cfg_path.exists():
        with open(cfg_path) as f:
            config.update(yaml.safe_load(f))

    # Environment variable overrides
    overrides = {
        "ARXIV_META_DB": ("db", "path"),
        "ARXIV_META_HOST": ("server", "host"),
        "ARXIV_META_PORT": ("server", "port"),
        "ARXIV_META_DATA": ("data", "dir"),
    }
    for env_key, cfg_keys in overrides.items():
        val = os.environ.get(env_key)
        if val:
            d = config
            for k in cfg_keys[:-1]:
                d = d.setdefault(k, {})
            d[cfg_keys[-1]] = val

    _config_cache = config
    return config


def get(key: str, default=None):
    """Dot-separated config access: get('db.path')"""
    cfg = load_config()
    parts = key.split(".")
    v = cfg
    for p in parts:
        if isinstance(v, dict):
            v = v.get(p)
        else:
            return default
    return v if v is not None else default
