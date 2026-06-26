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
import html
import io
import json
import mimetypes
import os
import socket
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import eagle

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
EAGLE_MODE = False  # Eagle 連携を有効にするか
EAGLE_API = eagle.DEFAULT_API  # Eagle ローカル API のベースURL


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
    position: absolute; top: 0; left: 0; right: 0;
    padding: 14px; padding-top: max(14px, env(safe-area-inset-top));
    display: flex; justify-content: space-between; align-items: center;
    background: linear-gradient(rgba(0,0,0,.6), transparent);
    font-size: 14px;
  }}
  #lb .close {{
    background: rgba(255,255,255,.15); border: none; color: #fff;
    width: 38px; height: 38px; border-radius: 50%; font-size: 20px;
  }}
  #lb .nav {{
    position: absolute; top: 0; bottom: 0; width: 33%;
    display: flex; align-items: center; opacity: 0;
  }}
  #lb .nav.prev {{ left: 0; justify-content: flex-start; }}
  #lb .nav.next {{ right: 0; justify-content: flex-end; }}
  #lb .pos {{ color: #ddd; }}
</style>
</head>
<body>
<header>
  <div class="crumbs">{crumbs}</div>
  <div class="count">{count}</div>
</header>
<main>
  {folders_html}
  {grid_html}
</main>

<div id="lb">
  <div class="stage"><img id="lbimg" alt=""></div>
  <div class="bar">
    <span class="pos" id="lbpos"></span>
    <button class="close" id="lbclose" aria-label="閉じる">&times;</button>
  </div>
  <div class="nav prev" id="lbprev"></div>
  <div class="nav next" id="lbnext"></div>
</div>

<script>
const IMAGES = {images_json};
let idx = -1;
const lb = document.getElementById('lb');
const lbimg = document.getElementById('lbimg');
const lbpos = document.getElementById('lbpos');

function open(i) {{
  idx = i;
  show();
  lb.classList.add('open');
  document.body.style.overflow = 'hidden';
}}
function close() {{
  lb.classList.remove('open');
  document.body.style.overflow = '';
  lbimg.src = '';
}}
function show() {{
  if (idx < 0 || idx >= IMAGES.length) return;
  lbimg.src = IMAGES[idx].full;
  lbpos.textContent = (idx + 1) + ' / ' + IMAGES.length + '  ' + IMAGES[idx].name;
}}
function next() {{ if (idx < IMAGES.length - 1) {{ idx++; show(); }} }}
function prev() {{ if (idx > 0) {{ idx--; show(); }} }}

document.querySelectorAll('.cell').forEach((c, i) => {{
  c.addEventListener('click', () => open(i));
}});
document.getElementById('lbclose').addEventListener('click', close);
document.getElementById('lbnext').addEventListener('click', next);
document.getElementById('lbprev').addEventListener('click', prev);

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


class Handler(BaseHTTPRequestHandler):
    server_version = "RemoteGfxViewer/1.0"

    # 既定のアクセスログは静かめに
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)

        # ---- Eagle 連携ルート ----
        if path == "/eagle/thumb":
            return self.serve_eagle_image(qs.get("id", [""])[0], thumb=True)
        if path == "/eagle/raw":
            return self.serve_eagle_image(qs.get("id", [""])[0], thumb=False)
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

        # IMAGES 用には name/full のみ渡す
        images_json = json.dumps(
            [{"name": d["name"], "full": d["full"]} for d in img_data],
            ensure_ascii=False,
        )
        page = PAGE_TEMPLATE.format(
            title=html.escape(title),
            crumbs=crumbs_html,
            count=count,
            folders_html=folders_html,
            grid_html=grid_html,
            images_json=images_json,
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
            iid = urllib.parse.quote(it.get("id", ""))
            name = it.get("name", "") + "." + (it.get("ext") or "")
            img_data.append(
                {
                    "name": name,
                    "thumb": f"/eagle/thumb?id={iid}",
                    "full": f"/eagle/raw?id={iid}",
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

    def serve_eagle_image(self, item_id: str, thumb: bool):
        """Eagle ライブラリ内の実ファイル（サムネイル or 原本）を配信する。"""
        if not item_id:
            self.send_error(404, "Not Found")
            return
        thumb_path = eagle.get_thumbnail_path(item_id, EAGLE_API)
        if not thumb_path:
            self.send_error(404, "Not Found")
            return
        if thumb:
            target = thumb_path
        else:
            # 原本は同じ .info フォルダ内。サムネイルが原本そのものの場合もある。
            target = self._eagle_original_path(thumb_path)
        self._serve_disk_file(target, require_info=True)

    @staticmethod
    def _eagle_original_path(thumb_path: str) -> str:
        """`<name>_thumbnail.png` を原本ファイルパスへ変換する。"""
        d = os.path.dirname(thumb_path)
        base = os.path.basename(thumb_path)
        stem, ext = os.path.splitext(base)
        if stem.endswith("_thumbnail"):
            original_stem = stem[: -len("_thumbnail")]
            # 同フォルダ内で原本（同名・拡張子違い）を探す
            try:
                for f in os.listdir(d):
                    fstem, _ = os.path.splitext(f)
                    if fstem == original_stem and is_image(f):
                        return os.path.join(d, f)
            except OSError:
                pass
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
        """ディスク上の画像ファイルを配信する（Eagle 用）。"""
        if (
            not abs_path
            or not os.path.isfile(abs_path)
            or not is_image(abs_path)
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

        if not HAS_PIL:
            # Pillow が無ければ元画像をそのまま返す（ブラウザ側で縮小表示）
            return self.serve_file(rel)

        try:
            with Image.open(abs_path) as im:
                im.draft("RGB", (THUMB_SIZE, THUMB_SIZE))
                im = im.convert("RGB")
                im.thumbnail((THUMB_SIZE, THUMB_SIZE))
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=80)
                data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            # サムネイル生成に失敗したら元画像で代替
            self.serve_file(rel)

    # ----------------------------------------------------------------- #
    def _copy(self, f):
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            self.wfile.write(chunk)


def main():
    global ROOT_DIR, THUMB_SIZE, EAGLE_MODE, EAGLE_API

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
    args = parser.parse_args()

    EAGLE_MODE = args.eagle
    EAGLE_API = args.eagle_api
    THUMB_SIZE = args.thumb_size

    # ディレクトリ: 指定があれば検証。--eagle 単独なら省略可。
    root = None
    if args.directory is not None:
        root = os.path.abspath(args.directory)
        if not os.path.isdir(root):
            print(f"エラー: フォルダが見つかりません: {root}", file=sys.stderr)
            sys.exit(1)
    elif not EAGLE_MODE:
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
    if root:
        print(f"  公開フォルダ : {root}")
    print(f"  サムネイル   : {'Pillowで生成' if HAS_PIL else '元画像を縮小表示 (Pillow未導入)'}")
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
