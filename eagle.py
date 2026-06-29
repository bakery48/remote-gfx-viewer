#!/usr/bin/env python3
"""
Eagle 連携モジュール（方式A）
============================
PC 上で起動している Eagle アプリのローカル API（既定 http://localhost:41595）に
問い合わせ、スマートフォルダの一覧とその中身を取得するためのヘルパー群です。

Eagle の制約:
  スマートフォルダは「実体フォルダ」ではなく「保存された検索条件」なので、
  /api/item/list に “スマートフォルダID” を渡して中身を取ることはできません。
  そこで本モジュールは
    1. /api/library/info でスマートフォルダの条件(conditions)を取得し、
    2. /api/item/list で全アイテムを取得して、
    3. 条件を Python 側で評価して絞り込む
  という流れで中身を再現します。

  方式A では、よく使う条件
  （名前 / タグ / メモ / URL / 拡張子 / 評価(★) / 幅・高さ・サイズ / 向き / フォルダ）
  に対応します。色・日付などの未対応条件は「無視」されるため、
  Eagle 本体より結果が広くなることがあります（partial=True で通知）。

すべて標準ライブラリのみで動作します。
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

DEFAULT_API = "http://localhost:41595"

# Eagle 本体より結果が広く/狭くなり得る（=正確に再現できない）プロパティ。
# これらの条件を含むスマートフォルダは partial 扱いにする。
UNSUPPORTED_PROPERTIES = {"color", "colors", "date", "createTime", "modifyTime", "btime", "mtime"}


class EagleError(Exception):
    pass


def _get(api_base: str, path: str, params: dict | None = None, timeout: float = 8.0):
    url = api_base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:  # 接続不可など
        raise EagleError(
            f"Eagle API へ接続できません ({url})。Eagle アプリが起動しているか確認してください。 [{e}]"
        )
    except (ValueError, OSError) as e:
        raise EagleError(f"Eagle API の応答を解釈できません ({url}): {e}")
    if isinstance(payload, dict) and payload.get("status") not in (None, "success"):
        raise EagleError(f"Eagle API がエラーを返しました ({url}): {payload}")
    return payload.get("data") if isinstance(payload, dict) else payload


# --------------------------------------------------------------------------- #
# 取得系
# --------------------------------------------------------------------------- #
def get_smart_folders(api_base: str = DEFAULT_API) -> list[dict]:
    """スマートフォルダの一覧を（ネストを展開して）返す。

    各要素: {"id", "name", "conditions", "partial"(bool)}
    """
    data = _get(api_base, "/api/library/info") or {}
    smart = data.get("smartFolders") or []
    result: list[dict] = []

    def walk(nodes, prefix=""):
        for n in nodes:
            name = n.get("name", "(無名)")
            full = f"{prefix} / {name}" if prefix else name
            conditions = n.get("conditions") or []
            result.append(
                {
                    "id": n.get("id", ""),
                    "name": name,
                    "path": full,
                    "conditions": conditions,
                    "partial": _has_unsupported(conditions),
                }
            )
            children = n.get("children") or []
            if children:
                walk(children, full)

    walk(smart)
    return result


def get_all_items(api_base: str = DEFAULT_API, limit: int = 100000) -> list[dict]:
    """ライブラリ内の（ゴミ箱を除く）全アイテムを返す。"""
    items = _get(api_base, "/api/item/list", {"limit": limit}) or []
    return [it for it in items if not it.get("isDeleted")]


def get_thumbnail_path(item_id: str, api_base: str = DEFAULT_API) -> str | None:
    """アイテムのサムネイル（無ければ原本）のディスク上の絶対パスを返す。"""
    try:
        path = _get(api_base, "/api/item/thumbnail", {"id": item_id})
    except EagleError:
        return None
    return path if isinstance(path, str) and path else None


# --------------------------------------------------------------------------- #
# 書き込み系（--allow-edit のときのみ server から呼ばれる）
# --------------------------------------------------------------------------- #
def _post(api_base: str, path: str, body: dict, timeout: float = 8.0):
    url = api_base.rstrip("/") + path
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise EagleError(
            f"Eagle API へ接続できません ({url})。Eagle アプリが起動しているか確認してください。 [{e}]"
        )
    except (ValueError, OSError) as e:
        raise EagleError(f"Eagle API の応答を解釈できません ({url}): {e}")
    if isinstance(payload, dict) and payload.get("status") not in (None, "success"):
        raise EagleError(f"Eagle API がエラーを返しました ({url}): {payload}")
    return payload.get("data") if isinstance(payload, dict) else payload


def set_star(item_id: str, star: int, api_base: str = DEFAULT_API):
    """アイテムの★評価を変更する（0〜5）。お気に入り=★5 として使う。"""
    star = max(0, min(5, int(star)))
    return _post(api_base, "/api/item/update", {"id": item_id, "star": star})


def move_to_trash(item_id: str, api_base: str = DEFAULT_API):
    """アイテムを Eagle のゴミ箱へ移動する（復元可能）。"""
    return _post(api_base, "/api/item/moveToTrash", {"itemIds": [item_id]})


def add_from_path(path: str, name: str, annotation: str = "",
                  tags: list | None = None, api_base: str = DEFAULT_API):
    """ローカルのファイルパスから Eagle ライブラリへ画像を取り込む。"""
    body = {"path": path, "name": name}
    if annotation:
        body["annotation"] = annotation
    if tags:
        body["tags"] = tags
    return _post(api_base, "/api/item/addFromPath", body)


# --------------------------------------------------------------------------- #
# スマートフォルダ条件の評価（方式A）
# --------------------------------------------------------------------------- #
def _has_unsupported(conditions) -> bool:
    for group in conditions or []:
        for rule in group.get("rules", []) or []:
            if rule.get("property") in UNSUPPORTED_PROPERTIES:
                return True
    return False


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _str_match(method: str, target: str, values: list) -> bool:
    target = (target or "").lower()
    vals = [str(v).lower() for v in values] or [""]
    if method in ("contain", "include", "contains"):
        return any(v in target for v in vals)
    if method in ("notContain", "exclude"):
        return all(v not in target for v in vals)
    if method in ("is", "equal", "equals"):
        return any(v == target for v in vals)
    if method in ("isNot", "notEqual"):
        return all(v != target for v in vals)
    if method in ("startWith", "startsWith"):
        return any(target.startswith(v) for v in vals)
    if method in ("endWith", "endsWith"):
        return any(target.endswith(v) for v in vals)
    # 既定は contain 扱い
    return any(v in target for v in vals)


def _set_match(method: str, target_set: set, values: list) -> bool:
    tset = {str(t).lower() for t in target_set}
    vals = {str(v).lower() for v in values}
    if not vals:
        return True
    # Eagle のタグ条件:
    #   intersection = 指定タグを「全部」含む (ALL)
    #   union        = 指定タグの「いずれか」を含む (ANY)
    #   difference   = いずれも含まない (NONE)
    if method in ("intersection", "containAll", "all"):
        return vals <= tset
    if method in ("notContain", "exclude", "isNot", "difference"):
        return not (tset & vals)
    # union / contain / include / is など → いずれか一致 (ANY)
    return bool(tset & vals)


def _num_match(method: str, target, values: list) -> bool:
    try:
        target = float(target)
        vals = [float(v) for v in values]
    except (TypeError, ValueError):
        return True  # 数値化できなければ判定不能 → 通す
    if not vals:
        return True
    if method in ("greater", "greaterThan", ">"):
        return target > vals[0]
    if method in ("greaterEqual", ">="):
        return target >= vals[0]
    if method in ("less", "lessThan", "<"):
        return target < vals[0]
    if method in ("lessEqual", "<="):
        return target <= vals[0]
    if method in ("is", "equal", "=="):
        return target in vals
    if method in ("between",) and len(vals) >= 2:
        lo, hi = sorted(vals[:2])
        return lo <= target <= hi
    return True


def _eval_rule(rule: dict, item: dict):
    """1ルールを評価。判定不能(=未対応)なら None を返す。"""
    prop = rule.get("property")
    method = rule.get("method", "contain")
    values = _as_list(rule.get("value"))

    if prop in ("name", "title"):
        return _str_match(method, item.get("name", ""), values)
    if prop in ("annotation", "note", "memo"):
        return _str_match(method, item.get("annotation", ""), values)
    if prop in ("url", "link"):
        return _str_match(method, item.get("url", ""), values)
    if prop in ("ext", "extension", "type", "filetype"):
        return _set_match(method, {item.get("ext", "")}, values)
    if prop in ("tag", "tags"):
        return _set_match(method, set(item.get("tags", []) or []), values)
    if prop in ("folder", "folders"):
        return _set_match(method, set(item.get("folders", []) or []), values)
    if prop in ("rating", "star", "stars"):
        return _num_match(method, item.get("star", 0), values)
    if prop in ("width",):
        return _num_match(method, item.get("width", 0), values)
    if prop in ("height",):
        return _num_match(method, item.get("height", 0), values)
    if prop in ("size", "filesize"):
        return _num_match(method, item.get("size", 0), values)
    if prop in ("shape", "orientation"):
        w, h = item.get("width", 0) or 0, item.get("height", 0) or 0
        if w == h:
            shape = "square"
        elif w > h:
            shape = "horizontal"
        else:
            shape = "vertical"
        return _set_match(method, {shape}, values)

    # color / date など未対応
    return None


def _eval_group(group: dict, item: dict) -> bool:
    match = (group.get("match") or "AND").upper()
    results = []
    for rule in group.get("rules", []) or []:
        r = _eval_rule(rule, item)
        if r is not None:
            results.append(r)
    if not results:
        # 全ルールが未対応 → 絞り込めないので通す
        return True
    if match == "OR":
        return any(results)
    return all(results)


def item_matches(conditions, item: dict) -> bool:
    """スマートフォルダ条件にアイテムが合致するか（複数グループは AND 結合）。"""
    groups = conditions or []
    if not groups:
        return True
    return all(_eval_group(g, item) for g in groups)


def filter_items(conditions, items: list[dict]) -> list[dict]:
    return [it for it in items if item_matches(conditions, it)]


# こちらで評価できるプロパティ（_eval_rule が扱うもの）
SUPPORTED_PROPERTIES = {
    "name", "title", "annotation", "note", "memo", "url", "link",
    "ext", "extension", "type", "filetype", "tag", "tags", "folder", "folders",
    "rating", "star", "stars", "width", "height", "size", "filesize",
    "shape", "orientation",
}


def _conditions_evaluable(conditions) -> bool:
    """少なくとも1つ、こちらで評価できるルールを含むか。"""
    for group in conditions or []:
        for rule in group.get("rules", []) or []:
            if rule.get("property") in SUPPORTED_PROPERTIES:
                return True
    return False


def filter_no_smart_folder(smart_folders: list[dict], items: list[dict]) -> list[dict]:
    """どのスマートフォルダの条件にも該当しないアイテムを返す。

    色・日付など丸ごと未対応のスマートフォルダ（評価できないもの）は、
    全件該当扱いになって結果を潰してしまうため、判定対象から除外する。
    """
    conds = [
        sf["conditions"]
        for sf in smart_folders
        if sf.get("conditions") and _conditions_evaluable(sf["conditions"])
    ]
    return [it for it in items if not any(item_matches(c, it) for c in conds)]
