#!/usr/bin/env python3
"""
remote-gfx-viewer
=================
同じLAN上のPCにあるローカル画像を、スマホのブラウザから閲覧するための
軽量な画像ビューアサーバーです。

特徴:
  - 標準ライブラリのみで動作（追加インストール不要）
  - サムネイルのグリッド一覧
  - フォルダ階層をたどって閲覧
  - 拡大表示中に左右スワイプ / タップで前後の画像へ
  - Pillow がインストールされていれば、軽量なサムネイルを自動生成

使い方:
  python3 server.py /path/to/images
  python3 server.py /path/to/images --port 8000 --host 0.0.0.0

その後、スマホのブラウザから  http://<PCのIPアドレス>:8000/  を開きます。
PCのIPアドレスは起動時にコンソールへ表示されます。
"""

from __future__ import annotations

import argparse
import base64
import hmac
import html
import io
import json
import mimetypes
import os
import secrets
import socket
import sys
import tempfile
import urllib.parse
from http import cookies as http_cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import eagle
import presets
import sd

# Pillow は任意。あればサムネイル生成に使う。
try:
    from PIL import Image  # type: ignore

    HAS_PIL = True
except Exception:  # pragma: no cover - 環境依存
    HAS_PIL = False

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".avif",
}

# 設定（main で上書きされる）
ROOT_DIR = os.getcwd()
THUMB_SIZE = 400  # サムネイルの最大辺(px)
VIEW_SIZE = 2048  # 拡大表示プレビューの最大辺(px)。原寸は保存時のみ取得
EAGLE_MODE = False  # Eagle 連携を有効にするか
EAGLE_API = eagle.DEFAULT_API  # Eagle ローカル API のベースURL
EDIT_ENABLED = False  # スマホからの編集（★/削除）を許可するか
PASSWORD = None  # ログインパスワード（None なら認証なし）
AUTH_ENABLED = False  # 認証を有効にするか
SESSIONS = set()  # 有効なセッショントークン（メモリ保持）
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # クッキー有効期間（30日）
SD_MODE = False  # Stable Diffusion 連携（生成）を有効にするか
SD_API = sd.DEFAULT_API  # SD webui の API ベースURL


def is_image(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in IMAGE_EXTENSIONS


def safe_join(root: str, rel: str) -> str | None:
    """root 配下に収まる絶対パスを返す。ディレクトリトラバーサルを防ぐ。"""
    rel = rel.lstrip("/")
    target = os.path.normpath(os.path.join(root, rel))
    root_abs = os.path.abspath(root)
    target_abs = os.path.abspath(target)
    if target_abs == root_abs or target_abs.startswith(root_abs + os.sep):
        return target_abs
    return None


def local_ip() -> str:
    """LAN 上の自分の IP アドレスを推定する。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 外部へ送信はしない。ルーティング先を調べるだけ。
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def list_dir(abs_dir: str):
    """ディレクトリ内のサブフォルダと画像を名前順で返す。"""
    dirs, images = [], []
    try:
        entries = sorted(os.scandir(abs_dir), key=lambda e: e.name.lower())
    except OSError:
        return dirs, images
    for e in entries:
        if e.name.startswith("."):
            continue
        if e.is_dir():
            dirs.append(e.name)
        elif e.is_file() and is_image(e.name):
            images.append(e.name)
    return dirs, images


# --------------------------------------------------------------------------- #
# HTML テンプレート
# --------------------------------------------------------------------------- #
PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title}</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #121212;
    color: #e8e8e8;
    -webkit-tap-highlight-color: transparent;
  }}
  header {{
    position: sticky;
    top: 0;
    z-index: 10;
    background: rgba(20,20,20,.95);
    backdrop-filter: blur(8px);
    padding: 12px 14px;
    padding-top: max(12px, env(safe-area-inset-top));
    border-bottom: 1px solid #2a2a2a;
  }}
  .crumbs {{ font-size: 14px; word-break: break-all; line-height: 1.5; }}
  .crumbs a {{ color: #7cc4ff; text-decoration: none; }}
  .crumbs a:active {{ opacity: .6; }}
  .count {{ color: #888; font-size: 12px; margin-top: 4px; }}
  .hlinks {{
    position: absolute; top: max(12px, env(safe-area-inset-top)); right: 14px;
    display: flex; gap: 14px;
  }}
  .hlinks a {{ color: #9ad; font-size: 13px; text-decoration: none; }}
  .hlinks a:active {{ color: #cce; }}
  main {{ padding: 10px; }}
  .folders {{ display: flex; flex-direction: column; gap: 8px; margin-bottom: 14px; }}
  .folder {{
    display: flex; align-items: center; gap: 10px;
    padding: 14px 14px; background: #1e1e1e; border-radius: 12px;
    color: #e8e8e8; text-decoration: none; font-size: 16px;
  }}
  .folder:active {{ background: #2a2a2a; }}
  .folder .ico {{ font-size: 20px; }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(110px, 1fr));
    gap: 6px;
  }}
  .cell {{
    position: relative;
    aspect-ratio: 1 / 1;
    background: #1e1e1e;
    border-radius: 8px;
    overflow: hidden;
  }}
  .cell img {{
    width: 100%; height: 100%; object-fit: cover; display: block;
  }}
  .empty {{ color: #777; text-align: center; padding: 40px 10px; }}

  /* ライトボックス */
  #lb {{
    position: fixed; inset: 0; z-index: 100;
    background: #000; display: none;
    touch-action: pan-y;
  }}
  #lb.open {{ display: block; }}
  #lb .stage {{
    position: absolute; inset: 0;
    display: flex; align-items: center; justify-content: center;
  }}
  #lb img {{
    max-width: 100%; max-height: 100%;
    object-fit: contain; user-select: none; -webkit-user-drag: none;
  }}
  #lb .bar {{
    position: absolute; top: 0; left: 0; right: 0; z-index: 20;
    padding: 14px; padding-top: max(14px, env(safe-area-inset-top));
    display: flex; justify-content: space-between; align-items: center;
    background: linear-gradient(rgba(0,0,0,.55), transparent);
    font-size: 14px;
    transition: opacity .2s ease;
  }}
  #lb .baractions {{ display: flex; gap: 10px; align-items: center; }}
  #lb .iconbtn, #lb .close {{
    background: rgba(255,255,255,.15); border: none; color: #fff;
    width: 38px; height: 38px; border-radius: 50%; font-size: 19px;
    display: flex; align-items: center; justify-content: center;
    text-decoration: none; cursor: pointer;
  }}
  #lb .iconbtn:active, #lb .close:active {{ background: rgba(255,255,255,.3); }}
  #lb .nav {{
    position: absolute; top: 0; bottom: 0; width: 33%; z-index: 5;
    display: flex; align-items: center; opacity: 0;
  }}
  #lb .nav.prev {{ left: 0; justify-content: flex-start; }}
  #lb .nav.next {{ right: 0; justify-content: flex-end; }}
  #lb .pos {{ color: #ddd; }}
  #lb .toolbar {{
    position: absolute; left: 0; right: 0; bottom: 0; z-index: 20;
    padding: 16px 14px; padding-bottom: max(16px, env(safe-area-inset-bottom));
    display: none; align-items: center; justify-content: space-between;
    background: linear-gradient(transparent, rgba(0,0,0,.65));
    transition: opacity .2s ease;
  }}
  /* 編集可能なアイテムのときだけツールバーを配置 */
  #lb.editable .toolbar {{ display: flex; }}
  /* 中央タップでコントロール(chrome)を半透明トグル。非表示時はフェードアウト */
  #lb:not(.chrome) .bar,
  #lb:not(.chrome) .toolbar {{ opacity: 0; pointer-events: none; }}
  #lb .stars {{ display: flex; gap: 4px; }}
  #lb .stars .st {{
    font-size: 30px; line-height: 1; color: #666;
    padding: 4px; cursor: pointer; user-select: none;
  }}
  #lb .stars .st.on {{ color: #ffce3d; }}
  #lb .trash {{
    background: rgba(220,60,60,.85); border: none; color: #fff;
    padding: 10px 16px; border-radius: 10px; font-size: 15px;
  }}
  #lb .trash:active {{ background: rgba(180,40,40,.9); }}
  .toast {{
    position: fixed; left: 50%; bottom: 90px; transform: translateX(-50%);
    background: rgba(40,40,40,.95); color: #fff; padding: 10px 16px;
    border-radius: 8px; font-size: 14px; z-index: 200; opacity: 0;
    transition: opacity .2s; pointer-events: none;
  }}
  .toast.show {{ opacity: 1; }}
</style>
</head>
<body>
<header>
  <div class="crumbs">{crumbs}</div>
  <div class="count">{count}</div>
  <div class="hlinks">{header_links}</div>
</header>
<main>
  {folders_html}
  {grid_html}
</main>

<div id="lb">
  <div class="stage"><img id="lbimg" alt=""></div>
  <div class="bar">
    <span class="pos" id="lbpos"></span>
    <div class="baractions">
      <button class="iconbtn" id="lbsave" aria-label="保存">⬇</button>
      <button class="close" id="lbclose" aria-label="閉じる">&times;</button>
    </div>
  </div>
  <div class="nav prev" id="lbprev"></div>
  <div class="nav next" id="lbnext"></div>
  <div class="toolbar" id="lbtools">
    <div class="stars" id="lbstars"></div>
    <button class="trash" id="lbtrash">🗑 削除</button>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
const IMAGES = {images_json};
const EDIT = {edit_js};
let idx = -1;
const lb = document.getElementById('lb');
const lbimg = document.getElementById('lbimg');
const lbpos = document.getElementById('lbpos');
const lbtools = document.getElementById('lbtools');
const lbstars = document.getElementById('lbstars');
const lbtrash = document.getElementById('lbtrash');
const toastEl = document.getElementById('toast');
let toastTimer = null;

function toast(msg) {{
  toastEl.textContent = msg;
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), 1800);
}}

function open(i) {{
  idx = i;
  show();
  lb.classList.add('open');
  lb.classList.add('chrome'); // 開いた直後はコントロールを表示
  document.body.style.overflow = 'hidden';
}}
function close() {{
  lb.classList.remove('open');
  document.body.style.overflow = '';
  lbimg.src = '';
}}
function show() {{
  if (idx < 0 || idx >= IMAGES.length) return;
  const it = IMAGES[idx];
  lbimg.onerror = () => {{ toast('画像を読み込めませんでした'); }};
  lbimg.src = it.full;
  lbpos.textContent = (idx + 1) + ' / ' + IMAGES.length + '  ' + it.name;
  // 編集ツールバー（Eagleアイテム かつ --allow-edit のときのみ）
  if (EDIT && it.id) {{
    renderStars(it.star || 0);
    lb.classList.add('editable');
  }} else {{
    lb.classList.remove('editable');
  }}
}}
// 画像中央のタップでコントロール表示/非表示をトグル
function toggleChrome() {{ lb.classList.toggle('chrome'); }}

// 画像を端末に保存（ダウンロード）
async function download() {{
  const it = IMAGES[idx];
  if (!it) return;
  const fname = (it.name || 'image').replace(/[\\\\/:*?"<>|]/g, '_');
  const srcUrl = it.dl || it.full;  // 保存は原寸（dl）優先
  try {{
    const r = await fetch(srcUrl);
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = fname;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast('保存しました');
  }} catch (e) {{
    // フォールバック: 直接リンクを開く（端末側で長押し保存）
    window.open(srcUrl, '_blank');
  }}
}}
function next() {{ if (idx < IMAGES.length - 1) {{ idx++; show(); }} }}
function prev() {{ if (idx > 0) {{ idx--; show(); }} }}

function renderStars(star) {{
  lbstars.innerHTML = '';
  for (let n = 1; n <= 5; n++) {{
    const s = document.createElement('span');
    s.className = 'st' + (n <= star ? ' on' : '');
    s.textContent = '★';
    s.addEventListener('click', () => setStar(n));
    lbstars.appendChild(s);
  }}
}}

async function setStar(n) {{
  const it = IMAGES[idx];
  // 同じ★を再タップで解除（0に）
  const value = (it.star === n) ? 0 : n;
  try {{
    const r = await fetch('/eagle/star', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{id: it.id, star: value}})
    }});
    if (!r.ok) throw new Error(await r.text());
    it.star = value;
    renderStars(value);
    toast(value === 5 ? '⭐ お気に入りに登録' : (value === 0 ? '評価を解除' : '★' + value + ' に変更'));
  }} catch (e) {{
    toast('変更に失敗: ' + e.message);
  }}
}}

async function trash() {{
  const it = IMAGES[idx];
  if (!confirm('「' + it.name + '」をEagleのゴミ箱へ移動しますか？')) return;
  try {{
    const r = await fetch('/eagle/trash', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{id: it.id}})
    }});
    if (!r.ok) throw new Error(await r.text());
    IMAGES.splice(idx, 1);
    // 対応するサムネイルセルも消す
    const cell = document.querySelectorAll('.cell')[idx];
    if (cell) cell.remove();
    toast('🗑 ゴミ箱へ移動しました');
    if (IMAGES.length === 0) {{ close(); return; }}
    if (idx >= IMAGES.length) idx = IMAGES.length - 1;
    show();
  }} catch (e) {{
    toast('削除に失敗: ' + e.message);
  }}
}}

document.querySelectorAll('.cell').forEach((c) => {{
  // 削除でセルが減ってもズレないよう、クリック時に現在位置を求める
  c.addEventListener('click', () => {{
    const i = Array.from(document.querySelectorAll('.cell')).indexOf(c);
    if (i >= 0) open(i);
  }});
}});
document.getElementById('lbclose').addEventListener('click', close);
document.getElementById('lbnext').addEventListener('click', next);
document.getElementById('lbprev').addEventListener('click', prev);
document.getElementById('lbsave').addEventListener('click', download);
lbtrash.addEventListener('click', trash);
// 画像（中央）のタップでコントロールを表示/非表示（スワイプ時は無視）
document.querySelector('#lb .stage').addEventListener('click', () => {{
  if (!moved) toggleChrome();
}});

// キーボード操作（PC でも使えるように）
document.addEventListener('keydown', (e) => {{
  if (!lb.classList.contains('open')) return;
  if (e.key === 'ArrowRight') next();
  else if (e.key === 'ArrowLeft') prev();
  else if (e.key === 'Escape') close();
}});

// スワイプ操作
let sx = 0, sy = 0, moved = false;
lb.addEventListener('touchstart', (e) => {{
  if (e.touches.length !== 1) return;
  sx = e.touches[0].clientX; sy = e.touches[0].clientY; moved = false;
}}, {{passive: true}});
lb.addEventListener('touchmove', (e) => {{ moved = true; }}, {{passive: true}});
lb.addEventListener('touchend', (e) => {{
  const dx = e.changedTouches[0].clientX - sx;
  const dy = e.changedTouches[0].clientY - sy;
  if (!moved) return;
  if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy)) {{
    if (dx < 0) next(); else prev();
  }} else if (dy > 80 && Math.abs(dy) > Math.abs(dx)) {{
    close(); // 下スワイプで閉じる
  }}
}}, {{passive: true}});
</script>
</body>
</html>
"""


LOGIN_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>ログイン</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center;
    justify-content: center; background: #121212; color: #e8e8e8;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  form {{
    width: min(90vw, 340px); background: #1e1e1e; padding: 28px 24px;
    border-radius: 16px; display: flex; flex-direction: column; gap: 14px;
  }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  input {{
    font-size: 16px; padding: 12px 14px; border-radius: 10px;
    border: 1px solid #3a3a3a; background: #121212; color: #e8e8e8;
  }}
  button {{
    font-size: 16px; padding: 12px; border: none; border-radius: 10px;
    background: #2d7dd2; color: #fff;
  }}
  button:active {{ background: #2568b0; }}
  .err {{ color: #ff8a8a; font-size: 14px; }}
</style>
</head>
<body>
<form method="POST" action="/login">
  <h1>🔒 ログイン</h1>
  {error}
  <input type="password" name="password" placeholder="パスワード"
         autofocus autocomplete="current-password" inputmode="text">
  <button type="submit">開く</button>
</form>
</body>
</html>
"""


GENERATE_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>画像生成</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: #121212; color: #e8e8e8;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  header {{
    position: sticky; top: 0; z-index: 10;
    background: rgba(20,20,20,.95); backdrop-filter: blur(8px);
    padding: 12px 14px; padding-top: max(12px, env(safe-area-inset-top));
    border-bottom: 1px solid #2a2a2a; display: flex;
    justify-content: space-between; align-items: center;
  }}
  header a {{ color: #7cc4ff; text-decoration: none; font-size: 14px; }}
  header .title {{ font-size: 16px; font-weight: 600; }}
  main {{ padding: 12px; max-width: 720px; margin: 0 auto; }}
  label {{ display: block; font-size: 13px; color: #aaa; margin: 12px 0 4px; }}
  textarea, input, select {{
    width: 100%; font-size: 16px; padding: 10px 12px; border-radius: 10px;
    border: 1px solid #3a3a3a; background: #1a1a1a; color: #e8e8e8;
  }}
  textarea {{ resize: vertical; min-height: 70px; line-height: 1.5; }}
  #prompt {{ min-height: 200px; }}
  #negative {{ min-height: 110px; }}
  .row {{ display: flex; gap: 10px; }}
  .row > div {{ flex: 1; }}
  details {{ margin-top: 10px; background: #1a1a1a; border-radius: 10px; padding: 0 12px; }}
  summary {{ padding: 12px 0; cursor: pointer; color: #ccc; }}
  .gen {{
    width: 100%; margin-top: 16px; padding: 15px; border: none; border-radius: 12px;
    background: #2d7dd2; color: #fff; font-size: 17px; font-weight: 600;
  }}
  .gen:disabled {{ background: #444; }}
  .stop {{
    width: 100%; margin-top: 10px; padding: 14px; border: none; border-radius: 12px;
    background: #c0392b; color: #fff; font-size: 16px; font-weight: 600; display: none;
  }}
  .stop:active {{ background: #a93226; }}
  .stop.show {{ display: block; }}
  .check {{ display: flex; align-items: center; gap: 8px; margin-top: 14px; }}
  .check input {{ width: auto; }}
  #status {{ margin-top: 14px; font-size: 14px; color: #9ad; min-height: 20px; }}
  .barwrap {{ height: 6px; background: #222; border-radius: 3px; overflow: hidden; margin-top: 8px; display: none; }}
  .barwrap.show {{ display: block; }}
  #bar {{ height: 100%; width: 0%; background: #2d7dd2; transition: width .3s; }}
  .results {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px; margin-top: 16px; }}
  .results img {{ width: 100%; border-radius: 8px; display: block; }}
  .hint {{ color: #777; font-size: 12px; }}
  .presets {{
    display: flex; gap: 6px; align-items: center; margin-bottom: 6px;
    background: #1a1a1a; border-radius: 10px; padding: 8px;
  }}
  .presets select {{ flex: 1; padding: 8px 10px; }}
  .pbtn {{
    border: none; border-radius: 8px; background: #333; color: #e8e8e8;
    padding: 9px 10px; font-size: 13px; white-space: nowrap;
  }}
  .pbtn:active {{ background: #444; }}
  .pbtn.def {{ background: #2d7dd2; color: #fff; }}
</style>
</head>
<body>
<header>
  <span class="title">✨ 画像生成 (SDXL)</span>
  <span>{header_links}</span>
</header>
<main>
  <div class="presets">
    <select id="slot"></select>
    <button class="pbtn" id="loadBtn">読込</button>
    <button class="pbtn" id="saveBtn">保存</button>
    <button class="pbtn def" id="defBtn">既定に</button>
  </div>

  <label>プロンプト</label>
  <textarea id="prompt" placeholder="例: a cat astronaut, highly detailed, cinematic lighting"></textarea>

  <label>ネガティブプロンプト</label>
  <textarea id="negative" placeholder="例: lowres, bad anatomy, worst quality"></textarea>

  <div class="row">
    <div><label>幅</label><input id="width" type="number" value="1024" min="64" max="2048" step="64"></div>
    <div><label>高さ</label><input id="height" type="number" value="1024" min="64" max="2048" step="64"></div>
    <div><label>枚数</label><input id="count" type="number" value="1" min="1" max="8"></div>
  </div>

  <details>
    <summary>詳細設定（steps / CFG / seed / サンプラー）</summary>
    <div class="row">
      <div><label>Steps</label><input id="steps" type="number" value="30" min="1" max="150"></div>
      <div><label>CFG</label><input id="cfg" type="number" value="7" min="1" max="30" step="0.5"></div>
    </div>
    <label>Seed（-1 でランダム）</label>
    <input id="seed" type="number" value="-1">
    <label>サンプラー</label>
    <select id="sampler">{sampler_options}</select>
    <div style="height:12px"></div>
  </details>

  {eagle_save_html}

  <button class="gen" id="genbtn">生成する</button>
  <button class="stop" id="stopbtn">■ 生成を中止</button>
  <div id="status"></div>
  <div class="barwrap" id="barwrap"><div id="bar"></div></div>
  <div class="results" id="results"></div>
</main>

<script>
const EAGLE = {eagle_js};
const btn = document.getElementById('genbtn');
const statusEl = document.getElementById('status');
const barwrap = document.getElementById('barwrap');
const bar = document.getElementById('bar');
const results = document.getElementById('results');
let polling = null;

function val(id) {{ return document.getElementById(id).value; }}

// フォームの入力欄 ←→ 設定キー の対応
const FIELDS = [
  ['prompt', 'prompt', 's'], ['negative', 'negative_prompt', 's'],
  ['width', 'width', 'n'], ['height', 'height', 'n'], ['count', 'n_iter', 'n'],
  ['steps', 'steps', 'n'], ['cfg', 'cfg_scale', 'n'], ['seed', 'seed', 'n'],
  ['sampler', 'sampler_name', 's'],
];
function getSettings() {{
  const s = {{}};
  for (const [id, key, t] of FIELDS) {{
    const v = val(id);
    s[key] = (t === 'n') ? (+v) : v;
  }}
  return s;
}}
function applySettings(s) {{
  if (!s) return;
  for (const [id, key] of FIELDS) {{
    if (s[key] === undefined || s[key] === null) continue;
    const el = document.getElementById(id);
    if (el.tagName === 'SELECT') {{
      // 候補に無いサンプラーは無視
      if ([...el.options].some(o => o.value === String(s[key]))) el.value = s[key];
    }} else {{
      el.value = s[key];
    }}
  }}
}}

async function poll() {{
  try {{
    const r = await fetch('/sd/progress');
    if (!r.ok) return;
    const d = await r.json();
    const pct = Math.round((d.progress || 0) * 100);
    bar.style.width = pct + '%';
    if (pct > 0) statusEl.textContent = '生成中... ' + pct + '%';
  }} catch (e) {{}}
}}

async function generate() {{
  const save = EAGLE && document.getElementById('saveEagle') && document.getElementById('saveEagle').checked;
  const body = Object.assign(getSettings(), {{ save_to_eagle: !!save }});
  btn.disabled = true;
  stopBtn.classList.add('show');
  results.innerHTML = '';
  statusEl.textContent = '生成を開始しました...';
  barwrap.classList.add('show'); bar.style.width = '0%';
  polling = setInterval(poll, 1000);
  try {{
    const r = await fetch('/generate/run', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(body)
    }});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
    (d.images || []).forEach(src => {{
      const img = document.createElement('img');
      img.src = src; img.loading = 'lazy';
      results.appendChild(img);
    }});
    let msg = (d.images || []).length + ' 枚を生成しました';
    if (d.saved) msg += ' / Eagleに ' + d.saved + ' 枚保存';
    statusEl.textContent = msg;
  }} catch (e) {{
    statusEl.textContent = 'エラー: ' + e.message;
  }} finally {{
    clearInterval(polling);
    bar.style.width = '100%';
    setTimeout(() => barwrap.classList.remove('show'), 600);
    btn.disabled = false;
    stopBtn.classList.remove('show');
  }}
}}

const stopBtn = document.getElementById('stopbtn');
stopBtn.addEventListener('click', async () => {{
  stopBtn.disabled = true;
  statusEl.textContent = '中止しています...';
  try {{
    const r = await fetch('/sd/interrupt', {{ method: 'POST' }});
    if (!r.ok) {{ const d = await r.json().catch(() => ({{}})); throw new Error(d.error || ('HTTP ' + r.status)); }}
    statusEl.textContent = '中止をリクエストしました';
  }} catch (e) {{
    statusEl.textContent = '中止に失敗: ' + e.message;
  }} finally {{
    stopBtn.disabled = false;
  }}
}});
btn.addEventListener('click', generate);

// ---- プリセット（最大10個・既定設定）----
const slotSel = document.getElementById('slot');
let PRESETS = {{ default: null, slots: [] }};

function renderSlots() {{
  slotSel.innerHTML = '';
  for (let i = 0; i < 10; i++) {{
    const sl = PRESETS.slots[i];
    const isDef = PRESETS.default === i;
    const label = (i + 1) + ': ' + (sl ? sl.name : '（空）') + (isDef ? ' ★既定' : '');
    const opt = document.createElement('option');
    opt.value = i; opt.textContent = label;
    slotSel.appendChild(opt);
  }}
}}
function selectedSlot() {{ return +slotSel.value; }}

async function loadPresets(applyDefault) {{
  try {{
    const r = await fetch('/sd/presets');
    if (!r.ok) return;
    PRESETS = await r.json();
    renderSlots();
    if (applyDefault && PRESETS.default !== null && PRESETS.slots[PRESETS.default]) {{
      applySettings(PRESETS.slots[PRESETS.default].settings);
      slotSel.value = PRESETS.default;
    }}
  }} catch (e) {{}}
}}
async function presetPost(path, body) {{
  const r = await fetch(path, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(body)
  }});
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
  PRESETS = d; renderSlots();
}}

document.getElementById('loadBtn').addEventListener('click', () => {{
  const sl = PRESETS.slots[selectedSlot()];
  if (!sl) {{ statusEl.textContent = 'そのスロットは空です'; return; }}
  applySettings(sl.settings);
  statusEl.textContent = '「' + sl.name + '」を読み込みました';
}});
document.getElementById('saveBtn').addEventListener('click', async () => {{
  const i = selectedSlot();
  const cur = PRESETS.slots[i];
  const name = window.prompt('プリセット名', cur ? cur.name : ('スロット' + (i + 1)));
  if (name === null) return;
  try {{
    await presetPost('/sd/presets/save', {{ slot: i, name: name, settings: getSettings() }});
    slotSel.value = i;
    statusEl.textContent = '保存しました: ' + (name || ('スロット' + (i + 1)));
  }} catch (e) {{ statusEl.textContent = '保存失敗: ' + e.message; }}
}});
document.getElementById('defBtn').addEventListener('click', async () => {{
  const i = selectedSlot();
  if (!PRESETS.slots[i]) {{ statusEl.textContent = '空スロットは既定にできません'; return; }}
  try {{
    await presetPost('/sd/presets/default', {{ slot: i }});
    slotSel.value = i;
    statusEl.textContent = 'スロット' + (i + 1) + ' を既定にしました';
  }} catch (e) {{ statusEl.textContent = '設定失敗: ' + e.message; }}
}});

loadPresets(true);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "RemoteGfxViewer/1.0"

    # 既定のアクセスログは静かめに
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)

        # ---- 認証ゲート ----
        if AUTH_ENABLED:
            if path == "/login":
                return self.serve_login()
            if path == "/logout":
                return self.handle_logout()
            if not self.is_authed():
                return self.redirect("/login")

        # ---- Stable Diffusion 生成ルート ----
        if SD_MODE and path == "/generate":
            return self.serve_generate_page()
        if SD_MODE and path == "/sd/progress":
            return self._send_json(sd.get_progress(SD_API))
        if SD_MODE and path == "/sd/presets":
            return self._send_json(presets.load())
        # ギャラリーが無く生成だけの構成なら / は生成画面へ
        if SD_MODE and path == "/" and not EAGLE_MODE and ROOT_DIR is None:
            return self.redirect("/generate")

        # ---- Eagle 連携ルート ----
        if path == "/eagle/thumb":
            return self.serve_eagle_image(qs.get("id", [""])[0], "thumb")
        if path == "/eagle/view":
            return self.serve_eagle_image(qs.get("id", [""])[0], "view")
        if path == "/eagle/raw":
            return self.serve_eagle_image(qs.get("id", [""])[0], "raw")
        if path == "/eagle/smart":
            return self.serve_eagle_smart(qs.get("id", [""])[0])
        if EAGLE_MODE and path == "/":
            return self.serve_eagle_home()

        # ---- ファイルシステムのルート ----
        if path == "/raw":
            return self.serve_file(qs.get("p", [""])[0])
        if path == "/thumb":
            return self.serve_thumb(qs.get("p", [""])[0])
        if ROOT_DIR is None:
            self.send_error(404, "Not Found")
            return
        # それ以外はギャラリーページ（path がフォルダの相対パス）
        return self.serve_gallery(path)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if AUTH_ENABLED and path == "/login":
            return self.handle_login()
        if AUTH_ENABLED and not self.is_authed():
            return self._json_error(401, "認証が必要です")
        if path == "/generate/run":
            return self.handle_generate()
        if path == "/sd/interrupt":
            return self.handle_interrupt()
        if path.startswith("/sd/presets/"):
            return self.handle_presets(path)
        if path not in ("/eagle/star", "/eagle/trash"):
            self.send_error(404, "Not Found")
            return
        if not (EAGLE_MODE and EDIT_ENABLED):
            return self._json_error(403, "編集は無効です（--allow-edit で起動してください）")
        body = self._read_json()
        if body is None:
            return self._json_error(400, "不正なリクエストです")
        item_id = body.get("id")
        if not item_id:
            return self._json_error(400, "id がありません")
        try:
            if path == "/eagle/star":
                eagle.set_star(item_id, int(body.get("star", 0)), EAGLE_API)
            else:  # /eagle/trash
                eagle.move_to_trash(item_id, EAGLE_API)
        except (eagle.EagleError, ValueError) as e:
            return self._json_error(502, str(e))
        self._json_ok()

    # ----------------------------------------------------------------- #
    # 認証
    # ----------------------------------------------------------------- #
    def _cookie_token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = http_cookies.SimpleCookie(raw)
        except http_cookies.CookieError:
            return None
        m = jar.get("session")
        return m.value if m else None

    def is_authed(self):
        if not AUTH_ENABLED:
            return True
        token = self._cookie_token()
        return bool(token and token in SESSIONS)

    def redirect(self, location, cookie=None):
        self.send_response(303)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def serve_login(self, error=""):
        err_html = f'<div class="err">{html.escape(error)}</div>' if error else ""
        body = LOGIN_TEMPLATE.format(error=err_html).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_login(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            form = urllib.parse.parse_qs(raw.decode("utf-8"))
        except (ValueError, OSError):
            form = {}
        pw = form.get("password", [""])[0]
        if PASSWORD is not None and hmac.compare_digest(pw, PASSWORD):
            token = secrets.token_urlsafe(32)
            SESSIONS.add(token)
            cookie = (
                f"session={token}; HttpOnly; SameSite=Lax; Path=/; "
                f"Max-Age={SESSION_MAX_AGE}"
            )
            return self.redirect("/", cookie=cookie)
        # 失敗
        self.serve_login(error="パスワードが違います")

    def handle_logout(self):
        token = self._cookie_token()
        if token:
            SESSIONS.discard(token)
        self.redirect("/login", cookie="session=; Path=/; Max-Age=0")

    # ----------------------------------------------------------------- #
    # 共通ヘッダーのリンク
    # ----------------------------------------------------------------- #
    def header_links(self, on_generate=False):
        links = []
        if SD_MODE and not on_generate:
            links.append('<a href="/generate">✨ 生成</a>')
        if on_generate:
            links.append('<a href="/">← 戻る</a>')
        if AUTH_ENABLED:
            links.append('<a href="/logout">ログアウト</a>')
        return "".join(links)

    # ----------------------------------------------------------------- #
    # Stable Diffusion 生成
    # ----------------------------------------------------------------- #
    def serve_generate_page(self):
        samplers = sd.get_samplers(SD_API)
        opts = "".join(
            f'<option value="{html.escape(s)}">{html.escape(s)}</option>'
            for s in samplers
        )
        if EAGLE_MODE:
            eagle_save = (
                '<label class="check"><input type="checkbox" id="saveEagle" checked>'
                "生成結果を Eagle に保存する</label>"
            )
        else:
            eagle_save = '<p class="hint">Eagle 連携(--eagle)時は結果をライブラリへ保存できます。</p>'
        page = GENERATE_TEMPLATE.format(
            sampler_options=opts,
            eagle_save_html=eagle_save,
            eagle_js=("true" if EAGLE_MODE else "false"),
            header_links=self.header_links(on_generate=True),
        )
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_presets(self, path):
        """生成設定プリセットの保存/削除/既定設定。"""
        if not SD_MODE:
            return self._json_error(403, "生成は無効です（--sd で起動してください）")
        body = self._read_json()
        if body is None:
            return self._json_error(400, "不正なリクエストです")
        try:
            if path == "/sd/presets/save":
                slot = body.get("slot")
                data = presets.save_slot(
                    int(slot), body.get("name", ""), body.get("settings", {})
                )
            elif path == "/sd/presets/delete":
                data = presets.delete_slot(int(body.get("slot")))
            elif path == "/sd/presets/default":
                slot = body.get("slot")
                data = presets.set_default(None if slot is None else int(slot))
            else:
                self.send_error(404, "Not Found")
                return
        except (ValueError, TypeError) as e:
            return self._json_error(400, str(e))
        except OSError as e:
            return self._json_error(500, f"保存に失敗しました: {e}")
        self._send_json(data)

    def handle_interrupt(self):
        if not SD_MODE:
            return self._json_error(403, "生成は無効です（--sd で起動してください）")
        try:
            sd.interrupt(SD_API)
        except sd.SDError as e:
            return self._json_error(502, str(e))
        self._json_ok()

    def handle_generate(self):
        if not SD_MODE:
            return self._json_error(403, "生成は無効です（--sd で起動してください）")
        params = self._read_json()
        if params is None:
            return self._json_error(400, "不正なリクエストです")
        if not str(params.get("prompt", "")).strip():
            return self._json_error(400, "プロンプトを入力してください")
        try:
            result = sd.txt2img(params, SD_API)
        except sd.SDError as e:
            return self._json_error(502, str(e))

        b64_list = result["images"]
        data_urls = [
            "data:image/png;base64," + b for b in b64_list if isinstance(b, str)
        ]

        saved = 0
        if params.get("save_to_eagle") and EAGLE_MODE:
            saved = self._save_to_eagle(b64_list, params, result.get("info", {}))

        self._send_json({"images": data_urls, "saved": saved})

    def _save_to_eagle(self, b64_list, params, info):
        """生成画像を一時ファイルに書き出し、Eagle へ取り込む。保存できた枚数を返す。"""
        outdir = os.path.join(tempfile.gettempdir(), "rgv-generated")
        try:
            os.makedirs(outdir, exist_ok=True)
        except OSError:
            return 0
        prompt = str(params.get("prompt", "")).strip()
        base_name = (prompt[:40] or "sdxl").replace("\n", " ")
        annotation = prompt
        seed = info.get("seed", params.get("seed"))
        saved = 0
        for i, b in enumerate(b64_list):
            try:
                raw = base64.b64decode(b)
            except (ValueError, TypeError):
                continue
            token = secrets.token_hex(4)
            fpath = os.path.join(outdir, f"sdxl_{token}_{i}.png")
            try:
                with open(fpath, "wb") as f:
                    f.write(raw)
                name = f"{base_name} ({seed})" if seed is not None else base_name
                eagle.add_from_path(fpath, name, annotation=annotation,
                                    tags=["SDXL"], api_base=EAGLE_API)
                saved += 1
            except (OSError, eagle.EagleError):
                continue
        return saved

    # ----------------------------------------------------------------- #
    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, OSError):
            return None

    def _json_ok(self):
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_error(self, code, msg):
        body = json.dumps({"error": msg}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ----------------------------------------------------------------- #
    def render_page(self, title, crumbs_html, count, folders_html, img_data,
                    extra_grid=""):
        """共通のギャラリーHTMLを生成して送信する。"""
        cells = []
        for d in img_data:
            cells.append(
                f'<div class="cell">'
                f'<img loading="lazy" decoding="async" src="{d["thumb"]}" alt="">'
                f"</div>"
            )
        if cells:
            grid_html = f'<div class="grid">{"".join(cells)}</div>'
        elif not folders_html:
            grid_html = extra_grid or '<div class="empty">画像がありません</div>'
        else:
            grid_html = extra_grid

        # IMAGES 用には name/full と（Eagleなら）id/star を渡す
        images_payload = []
        for d in img_data:
            entry = {"name": d["name"], "full": d["full"]}
            if "dl" in d:
                entry["dl"] = d["dl"]
            if "id" in d:
                entry["id"] = d["id"]
                entry["star"] = d.get("star", 0)
            images_payload.append(entry)
        images_json = json.dumps(images_payload, ensure_ascii=False)
        edit_js = "true" if (EDIT_ENABLED and EAGLE_MODE) else "false"
        page = PAGE_TEMPLATE.format(
            title=html.escape(title),
            crumbs=crumbs_html,
            count=count,
            folders_html=folders_html,
            grid_html=grid_html,
            images_json=images_json,
            edit_js=edit_js,
            header_links=self.header_links(),
        )
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ----------------------------------------------------------------- #
    # Eagle 連携
    # ----------------------------------------------------------------- #
    def serve_eagle_home(self):
        """組み込みビュー（すべて/未分類）とスマートフォルダの一覧を表示する。"""
        try:
            folders = eagle.get_smart_folders(EAGLE_API)
        except eagle.EagleError as e:
            return self.send_eagle_error(e)

        crumbs_html = "🦅 Eagle"
        items_html = []
        # 組み込みの特別ビュー
        for vid, icon, label in (
            ("__all__", "🗂️", "すべて"),
            ("__uncategorized__", "📭", "未分類"),
            ("__no_smart_folder__", "🧩", "スマートフォルダ未該当"),
        ):
            href = "/eagle/smart?id=" + vid
            items_html.append(
                f'<a class="folder" href="{href}">'
                f'<span class="ico">{icon}</span>{label}</a>'
            )
        for f in folders:
            href = "/eagle/smart?id=" + urllib.parse.quote(f["id"])
            badge = (
                ' <span style="color:#caa84a;font-size:12px">条件一部のみ対応</span>'
                if f["partial"]
                else ""
            )
            items_html.append(
                f'<a class="folder" href="{href}">'
                f'<span class="ico">🔍</span>{html.escape(f["name"])}{badge}</a>'
            )
        folders_html = f'<div class="folders">{"".join(items_html)}</div>'
        count = f"スマートフォルダ {len(folders)} 件 ＋ すべて / 未分類"
        self.render_page("Eagle", crumbs_html, count, folders_html, [])

    def serve_eagle_smart(self, sid: str):
        """指定スマートフォルダの中身（条件に合致する画像）を表示する。"""
        if not sid:
            self.send_error(404, "Not Found")
            return
        try:
            items = eagle.get_all_items(EAGLE_API)
            if sid == "__all__":
                target = {"name": "すべて", "partial": False}
                matched = items
            elif sid == "__uncategorized__":
                target = {"name": "未分類", "partial": False}
                matched = [it for it in items if not it.get("folders")]
            elif sid == "__no_smart_folder__":
                folders = eagle.get_smart_folders(EAGLE_API)
                # 色・日付など丸ごと未対応のSFがあると精度が落ちるため partial 表示
                partial = any(f["partial"] for f in folders)
                target = {"name": "スマートフォルダ未該当", "partial": partial}
                matched = eagle.filter_no_smart_folder(folders, items)
            else:
                folders = eagle.get_smart_folders(EAGLE_API)
                target = next((f for f in folders if f["id"] == sid), None)
                if target is None:
                    self.send_error(404, "Smart folder not found")
                    return
                matched = eagle.filter_items(target["conditions"], items)
        except eagle.EagleError as e:
            return self.send_eagle_error(e)

        # 画像のみ対象（動画等は除外）
        matched = [it for it in matched if is_image("x." + (it.get("ext") or ""))]

        img_data = []
        for it in matched:
            raw_id = it.get("id", "")
            iid = urllib.parse.quote(raw_id)
            name = it.get("name", "") + "." + (it.get("ext") or "")
            img_data.append(
                {
                    "name": name,
                    "thumb": f"/eagle/thumb?id={iid}",
                    "full": f"/eagle/view?id={iid}",  # 拡大は軽量プレビュー
                    "dl": f"/eagle/raw?id={iid}",      # 保存は原寸
                    "id": raw_id,
                    "star": it.get("star", 0) or 0,
                }
            )

        crumbs = (
            '<a href="/">🦅 Eagle</a>'
            ' <span style="color:#555">/</span> '
            f'{html.escape(target["name"])}'
        )
        note = ""
        if target["partial"]:
            note = (
                ' <span style="color:#caa84a">'
                "※ 色・日付などの条件は未対応のため、Eagle本体より多く表示される場合があります</span>"
            )
        count = f"画像 {len(img_data)} 件{note}"
        self.render_page(target["name"], crumbs, count, "", img_data)

    def serve_eagle_image(self, item_id: str, kind: str):
        """Eagle ライブラリ内の画像を配信する。

        kind:
          thumb … 一覧用サムネイル（最大 THUMB_SIZE に縮小）
          view  … 拡大表示用プレビュー（最大 VIEW_SIZE に縮小。トンネル越しでも軽い）
          raw   … 原寸そのまま（保存/ダウンロード用）
        """
        if not item_id:
            self.send_error(404, "Not Found")
            return
        thumb_path = eagle.get_thumbnail_path(item_id, EAGLE_API)
        if not thumb_path:
            self.log_message("eagle thumbnail path 取得失敗 id=%s", item_id)
            self.send_error(404, "Not Found")
            return

        original = self._eagle_original_path(thumb_path)
        if kind == "thumb":
            # サムネイル要求: _thumbnail.png があればそれ、無ければ原本。
            candidates = [thumb_path, original]
        else:
            # view / raw は原本を優先（プレビューは原本から縮小、保存は原本）
            candidates = [original, thumb_path]
        path = next((p for p in candidates if p and os.path.isfile(p)), None)
        if path is None:
            self.log_message(
                "eagle 実ファイルが見つかりません id=%s kind=%s path=%s",
                item_id, kind, thumb_path,
            )
            self.send_error(404, "Not Found")
            return

        if kind == "thumb" and self._try_send_scaled(path, THUMB_SIZE, 80):
            return
        if kind == "view" and self._try_send_scaled(path, VIEW_SIZE, 85):
            return
        # raw、または Pillow が無い場合は原寸をそのまま配信
        self._serve_disk_file(path, require_info=True)

    @staticmethod
    def _eagle_original_path(thumb_path: str) -> str:
        """サムネイルのパスから原本ファイルのパスを推定する。

        Eagle がサムネ名と原本名を別に付ける場合（生成画像など名前に記号が
        入ると顕著）があるため、`.info` フォルダ内を走査して実画像を確実に拾う:
          1) `<stem>_thumbnail.png` と同名の原本があればそれ
          2) 無ければ metadata/サムネ以外で最初の画像ファイル
        """
        d = os.path.dirname(thumb_path)
        base = os.path.basename(thumb_path)
        stem, _ = os.path.splitext(base)
        original_stem = (
            stem[: -len("_thumbnail")] if stem.endswith("_thumbnail") else stem
        )
        try:
            files = os.listdir(d)
        except OSError:
            return thumb_path

        def is_candidate(f):
            return (
                f != "metadata.json"
                and not f.endswith("_thumbnail.png")
                and is_image(f)
            )

        candidates = [f for f in files if is_candidate(f)]
        # 1) 名前一致を優先
        for f in candidates:
            if os.path.splitext(f)[0] == original_stem:
                return os.path.join(d, f)
        # 2) それ以外は最初の画像ファイル
        if candidates:
            return os.path.join(d, candidates[0])
        return thumb_path  # サムネイル＝原本のケース

    def send_eagle_error(self, err: Exception):
        msg = html.escape(str(err))
        body = (
            f'<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>Eagle 接続エラー</title>"
            f'<style>body{{font-family:sans-serif;background:#121212;color:#e8e8e8;'
            f"padding:24px;line-height:1.7}}.box{{background:#2a1f1f;border:1px solid "
            f"#5a3030;border-radius:10px;padding:16px}}a{{color:#7cc4ff}}</style></head>"
            f'<body><h2>🦅 Eagle に接続できませんでした</h2>'
            f'<div class="box">{msg}</div>'
            f"<p>確認してください:</p><ul>"
            f"<li>Eagle アプリが起動していますか？</li>"
            f"<li>API のURLは正しいですか？（既定 http://localhost:41595）</li>"
            f"</ul><p><a href=\"/\">← 再読み込み</a></p></body></html>"
        ).encode("utf-8")
        self.send_response(503)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_disk_file(self, abs_path, require_info=False):
        """ディスク上のファイルを配信する（Eagle 用）。

        パスは Eagle API 由来（信頼できる）なので拡張子の許可リストでは弾かず、
        .info フォルダ配下であることだけ確認する。未知拡張子はブラウザ側の
        コンテンツ判定に任せる。
        """
        if (
            not abs_path
            or not os.path.isfile(abs_path)
            or (require_info and ".info" not in abs_path)
        ):
            self.send_error(404, "Not Found")
            return
        ctype = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
        try:
            size = os.path.getsize(abs_path)
            with open(abs_path, "rb") as f:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self._copy(f)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError:
            self.send_error(500, "Read error")

    # ----------------------------------------------------------------- #
    def serve_gallery(self, rel_path: str):
        abs_dir = safe_join(ROOT_DIR, rel_path)
        if abs_dir is None or not os.path.isdir(abs_dir):
            self.send_error(404, "Not Found")
            return

        rel = os.path.relpath(abs_dir, ROOT_DIR)
        rel = "" if rel == "." else rel
        dirs, images = list_dir(abs_dir)

        # パンくずリスト
        crumbs = ['<a href="/">🏠 ホーム</a>']
        acc = ""
        if rel:
            for part in rel.split(os.sep):
                acc = f"{acc}/{part}" if acc else part
                href = "/" + urllib.parse.quote(acc)
                crumbs.append(f'<a href="{href}">{html.escape(part)}</a>')
        crumbs_html = ' <span style="color:#555">/</span> '.join(crumbs)

        # フォルダ一覧
        folder_items = []
        for d in dirs:
            child = f"{rel}/{d}" if rel else d
            href = "/" + urllib.parse.quote(child)
            folder_items.append(
                f'<a class="folder" href="{href}">'
                f'<span class="ico">📁</span>{html.escape(d)}</a>'
            )
        folders_html = (
            f'<div class="folders">{"".join(folder_items)}</div>'
            if folder_items
            else ""
        )

        # 画像グリッド
        img_data = []
        for name in images:
            child = f"{rel}/{name}" if rel else name
            p = urllib.parse.quote(child)
            img_data.append(
                {"name": name, "thumb": f"/thumb?p={p}", "full": f"/raw?p={p}"}
            )

        title = os.path.basename(abs_dir) or "画像ビューア"
        count = f"フォルダ {len(dirs)} / 画像 {len(images)}"
        self.render_page(title, crumbs_html, count, folders_html, img_data)

    # ----------------------------------------------------------------- #
    def serve_file(self, rel: str):
        abs_path = safe_join(ROOT_DIR, urllib.parse.unquote(rel))
        if abs_path is None or not os.path.isfile(abs_path) or not is_image(abs_path):
            self.send_error(404, "Not Found")
            return
        ctype = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
        try:
            size = os.path.getsize(abs_path)
            with open(abs_path, "rb") as f:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self._copy(f)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError:
            self.send_error(500, "Read error")

    # ----------------------------------------------------------------- #
    def serve_thumb(self, rel: str):
        abs_path = safe_join(ROOT_DIR, urllib.parse.unquote(rel))
        if abs_path is None or not os.path.isfile(abs_path) or not is_image(abs_path):
            self.send_error(404, "Not Found")
            return
        if self._try_send_scaled(abs_path, THUMB_SIZE):
            return
        # Pillow が無い／生成失敗時は元画像をそのまま返す
        self.serve_file(rel)

    def _try_send_scaled(self, abs_path, max_size, quality=80) -> bool:
        """Pillow で max_size 以下に縮小した JPEG を送る。送れたら True。"""
        if not HAS_PIL:
            return False
        try:
            with Image.open(abs_path) as im:
                im.draft("RGB", (max_size, max_size))
                im = im.convert("RGB")
                im.thumbnail((max_size, max_size))
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=quality)
                data = buf.getvalue()
        except (BrokenPipeError, ConnectionResetError):
            return True  # 送信途中で切断: これ以上何もしない
        except Exception:
            return False
        try:
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return True

    # ----------------------------------------------------------------- #
    def _copy(self, f):
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            self.wfile.write(chunk)


def main():
    global ROOT_DIR, THUMB_SIZE, EAGLE_MODE, EAGLE_API, EDIT_ENABLED
    global PASSWORD, AUTH_ENABLED, SD_MODE, SD_API

    parser = argparse.ArgumentParser(
        description="同じLAN上のスマホからローカル画像を閲覧する軽量サーバー"
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=None,
        help="公開する画像フォルダ（--eagle 時は省略可）",
    )
    parser.add_argument(
        "--port", "-p", type=int, default=8000, help="待ち受けポート（既定: 8000）"
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="待ち受けホスト（既定: 0.0.0.0 = LAN全体に公開）",
    )
    parser.add_argument(
        "--thumb-size",
        type=int,
        default=400,
        help="サムネイルの最大辺px（Pillow使用時のみ・既定: 400）",
    )
    parser.add_argument(
        "--eagle",
        action="store_true",
        help="Eagle 連携モード。スマートフォルダをスマホから閲覧できる（Eagle 起動が必要）",
    )
    parser.add_argument(
        "--eagle-api",
        default=eagle.DEFAULT_API,
        help=f"Eagle ローカル API のURL（既定: {eagle.DEFAULT_API}）",
    )
    parser.add_argument(
        "--allow-edit",
        action="store_true",
        help="スマホからの★評価変更・削除（ゴミ箱へ）を許可する（--eagle 時のみ）",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("RGV_PASSWORD"),
        help="ログインパスワード（指定すると認証必須。環境変数 RGV_PASSWORD でも可）",
    )
    parser.add_argument(
        "--sd",
        action="store_true",
        help="Stable Diffusion 連携。スマホから txt2img 生成ができる（webui を --api で起動）",
    )
    parser.add_argument(
        "--sd-api",
        default=sd.DEFAULT_API,
        help=f"SD webui の API URL（既定: {sd.DEFAULT_API}）",
    )
    parser.add_argument(
        "--presets-file",
        default="rgv_presets.json",
        help="生成プリセットの保存先JSON（既定: rgv_presets.json）",
    )
    args = parser.parse_args()

    presets.configure(os.path.abspath(args.presets_file))

    EAGLE_MODE = args.eagle
    EAGLE_API = args.eagle_api
    EDIT_ENABLED = args.allow_edit
    THUMB_SIZE = args.thumb_size
    PASSWORD = args.password if args.password else None
    AUTH_ENABLED = PASSWORD is not None
    SD_MODE = args.sd
    SD_API = args.sd_api

    # ディレクトリ: 指定があれば検証。--eagle 単独なら省略可。
    root = None
    if args.directory is not None:
        root = os.path.abspath(args.directory)
        if not os.path.isdir(root):
            print(f"エラー: フォルダが見つかりません: {root}", file=sys.stderr)
            sys.exit(1)
    elif not EAGLE_MODE and not SD_MODE:
        root = os.path.abspath(".")  # 既定はカレントディレクトリ
    ROOT_DIR = root

    if EAGLE_MODE:
        print("  Eagle 連携の確認中...", file=sys.stderr)
        try:
            sf = eagle.get_smart_folders(EAGLE_API)
            print(f"  → Eagle に接続成功（スマートフォルダ {len(sf)} 件）", file=sys.stderr)
        except eagle.EagleError as e:
            print(f"  警告: 今は Eagle に接続できません: {e}", file=sys.stderr)
            print("        Eagle アプリを起動すれば、ページ再読み込みで使えます。", file=sys.stderr)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)

    ip = local_ip()
    print("=" * 56)
    print("  remote-gfx-viewer を起動しました")
    print("=" * 56)
    if EAGLE_MODE:
        print(f"  モード       : Eagle 連携（{EAGLE_API}）")
        edit_label = "有効（★変更・削除可）" if EDIT_ENABLED else "無効（閲覧専用）"
        print(f"  編集         : {edit_label}")
    if root:
        print(f"  公開フォルダ : {root}")
    if SD_MODE:
        print(f"  画像生成     : 有効（SD API: {SD_API}）")
    print(f"  認証         : {'有効（パスワード）' if AUTH_ENABLED else '無効（誰でもアクセス可）'}")
    print(f"  サムネイル   : {'Pillowで生成' if HAS_PIL else '元画像を縮小表示 (Pillow未導入)'}")
    if EDIT_ENABLED and not AUTH_ENABLED:
        print()
        print("  ⚠️  編集が有効ですが認証がありません。出先公開時は必ず --password を設定してください。")
    print()
    print("  スマホのブラウザで以下を開いてください:")
    print(f"    http://{ip}:{args.port}/")
    if args.host in ("0.0.0.0", "::"):
        print(f"  （同じPC上なら http://localhost:{args.port}/ ）")
    print()
    print("  停止するには Ctrl+C")
    print("=" * 56)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました。")
        httpd.shutdown()


if __name__ == "__main__":
    main()
