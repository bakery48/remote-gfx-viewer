#!/usr/bin/env python3
"""
Stable Diffusion (AUTOMATIC1111 / Forge) 連携モジュール
=====================================================
PC 上で起動している stable-diffusion-webui 系の REST API を呼び、
txt2img（プロンプトからの画像生成）を行うためのヘルパーです。

webui は起動時に `--api` を付けておく必要があります（既定 http://127.0.0.1:7860）。
すべて標準ライブラリのみで動作します。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_API = "http://127.0.0.1:7860"

# サンプラー取得に失敗したときのフォールバック
FALLBACK_SAMPLERS = [
    "Euler a",
    "Euler",
    "DPM++ 2M",
    "DPM++ 2M Karras",
    "DPM++ SDE Karras",
    "DDIM",
]


class SDError(Exception):
    pass


def _get(api_base: str, path: str, timeout: float = 10.0):
    url = api_base.rstrip("/") + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise SDError(
            f"Stable Diffusion API へ接続できません ({url})。"
            f"webui を --api 付きで起動しているか確認してください。 [{e}]"
        )
    except (ValueError, OSError) as e:
        raise SDError(f"SD API の応答を解釈できません ({url}): {e}")


def _post(api_base: str, path: str, body: dict, timeout: float = 600.0):
    url = api_base.rstrip("/") + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:500]
        except Exception:
            pass
        raise SDError(f"SD API がエラーを返しました ({url}): HTTP {e.code} {detail}")
    except urllib.error.URLError as e:
        raise SDError(
            f"Stable Diffusion API へ接続できません ({url})。"
            f"webui を --api 付きで起動しているか確認してください。 [{e}]"
        )
    except (ValueError, OSError) as e:
        raise SDError(f"SD API の応答を解釈できません ({url}): {e}")


def get_samplers(api_base: str = DEFAULT_API) -> list[str]:
    try:
        data = _get(api_base, "/sdapi/v1/samplers")
        names = [s.get("name") for s in data if s.get("name")]
        return names or list(FALLBACK_SAMPLERS)
    except SDError:
        return list(FALLBACK_SAMPLERS)


def get_progress(api_base: str = DEFAULT_API) -> dict:
    """生成の進捗（0〜1）と ETA を返す。失敗時は progress=0。"""
    try:
        data = _get(api_base, "/sdapi/v1/progress?skip_current_image=true", timeout=5.0)
        return {
            "progress": float(data.get("progress", 0) or 0),
            "eta": float(data.get("eta_relative", 0) or 0),
        }
    except (SDError, ValueError, TypeError):
        return {"progress": 0.0, "eta": 0.0}


def interrupt(api_base: str = DEFAULT_API):
    """実行中の生成を中断する（A1111 /sdapi/v1/interrupt）。"""
    return _post(api_base, "/sdapi/v1/interrupt", {}, timeout=10.0)


def txt2img(params: dict, api_base: str = DEFAULT_API) -> dict:
    """txt2img を実行し、{"images": [base64...], "info": {...}} を返す。"""
    payload = build_payload(params)
    data = _post(api_base, "/sdapi/v1/txt2img", payload)
    images = data.get("images") or []
    if not images:
        raise SDError("画像が生成されませんでした（モデル未ロードの可能性）。")
    # info は JSON 文字列で返るのでパースを試みる
    info = {}
    try:
        info = json.loads(data.get("info", "{}"))
    except (ValueError, TypeError):
        pass
    return {"images": images, "info": info}


def build_payload(p: dict) -> dict:
    """フロントからのパラメータを A1111 の txt2img ペイロードへ変換・クランプ。"""

    def clamp(v, lo, hi, default):
        try:
            v = type(default)(v)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, v))

    return {
        "prompt": str(p.get("prompt", ""))[:4000],
        "negative_prompt": str(p.get("negative_prompt", ""))[:4000],
        "width": clamp(p.get("width", 1024), 64, 2048, 1024),
        "height": clamp(p.get("height", 1024), 64, 2048, 1024),
        "steps": clamp(p.get("steps", 30), 1, 150, 30),
        "cfg_scale": clamp(p.get("cfg_scale", 7.0), 1.0, 30.0, 7.0),
        "n_iter": clamp(p.get("n_iter", 1), 1, 8, 1),
        "batch_size": 1,
        "seed": clamp(p.get("seed", -1), -1, 2**31 - 1, -1),
        "sampler_name": str(p.get("sampler_name", "Euler a"))[:64],
        "send_images": True,
        "save_images": False,
    }
