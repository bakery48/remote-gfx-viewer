#!/usr/bin/env python3
"""
生成設定プリセットの保存/読み出し
================================
スマホからの画像生成画面で使う設定一式（プロンプト・サイズ・steps 等）を、
PC 側の JSON ファイルに最大 10 個まで保存する。既定（デフォルト）スロットを
指定でき、生成画面を開いたときに自動で読み込まれる。

標準ライブラリのみで動作。ThreadingHTTPServer から呼ばれるためロックで保護。
"""

from __future__ import annotations

import json
import os
import threading

MAX_SLOTS = 10

# 保存対象として許可する設定キー
ALLOWED_KEYS = (
    "prompt",
    "negative_prompt",
    "width",
    "height",
    "n_iter",
    "steps",
    "cfg_scale",
    "seed",
    "sampler_name",
)

_lock = threading.Lock()
_path = "rgv_presets.json"


def configure(path: str):
    global _path
    _path = path


def sanitize(settings: dict) -> dict:
    """許可キーだけを取り出して保存用に整える。"""
    if not isinstance(settings, dict):
        return {}
    return {k: settings[k] for k in ALLOWED_KEYS if k in settings}


def _empty() -> dict:
    return {"default": None, "slots": [None] * MAX_SLOTS}


def _load_unlocked() -> dict:
    try:
        with open(_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _empty()
    slots = data.get("slots") or []
    if not isinstance(slots, list):
        slots = []
    slots = (slots + [None] * MAX_SLOTS)[:MAX_SLOTS]
    d = data.get("default")
    if not isinstance(d, int) or not (0 <= d < MAX_SLOTS) or slots[d] is None:
        d = None
    return {"default": d, "slots": slots}


def _save_unlocked(data: dict):
    tmp = _path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _path)


def _check_slot(slot):
    if not isinstance(slot, int) or not (0 <= slot < MAX_SLOTS):
        raise ValueError("slot が不正です")


def load() -> dict:
    with _lock:
        return _load_unlocked()


def save_slot(slot: int, name: str, settings: dict) -> dict:
    with _lock:
        _check_slot(slot)
        data = _load_unlocked()
        data["slots"][slot] = {
            "name": (name or f"スロット{slot + 1}").strip()[:40],
            "settings": sanitize(settings),
        }
        _save_unlocked(data)
        return data


def delete_slot(slot: int) -> dict:
    with _lock:
        _check_slot(slot)
        data = _load_unlocked()
        data["slots"][slot] = None
        if data["default"] == slot:
            data["default"] = None
        _save_unlocked(data)
        return data


def set_default(slot) -> dict:
    with _lock:
        data = _load_unlocked()
        if slot is None:
            data["default"] = None
        else:
            _check_slot(slot)
            if data["slots"][slot] is None:
                raise ValueError("空のスロットは既定にできません")
            data["default"] = slot
        _save_unlocked(data)
        return data
