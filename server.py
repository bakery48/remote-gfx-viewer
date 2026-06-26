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

        if path == "/raw":
            return self.serve_file(qs.get("p", [""])[0])
        if path == "/thumb":
            return self.serve_thumb(qs.get("p", [""])[0])
        # それ以外はギャラリーページ（path がフォルダの相対パス）
        return self.serve_gallery(path)

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
        cells = []
        for name in images:
            child = f"{rel}/{name}" if rel else name
            p = urllib.parse.quote(child)
            thumb = f"/thumb?p={p}"
            full = f"/raw?p={p}"
            img_data.append({"name": name, "full": full})
            cells.append(
                f'<div class="cell">'
                f'<img loading="lazy" decoding="async" src="{thumb}" alt="">'
                f"</div>"
            )
        if cells:
            grid_html = f'<div class="grid">{"".join(cells)}</div>'
        elif not folder_items:
            grid_html = '<div class="empty">画像がありません</div>'
        else:
            grid_html = ""

        title = os.path.basename(abs_dir) or "画像ビューア"
        count = f"フォルダ {len(dirs)} / 画像 {len(images)}"

        page = PAGE_TEMPLATE.format(
            title=html.escape(title),
            crumbs=crumbs_html,
            count=count,
            folders_html=folders_html,
            grid_html=grid_html,
            images_json=json.dumps(img_data, ensure_ascii=False),
        )
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
    global ROOT_DIR, THUMB_SIZE

    parser = argparse.ArgumentParser(
        description="同じLAN上のスマホからローカル画像を閲覧する軽量サーバー"
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="公開する画像フォルダ（既定: カレントディレクトリ）",
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
    args = parser.parse_args()

    root = os.path.abspath(args.directory)
    if not os.path.isdir(root):
        print(f"エラー: フォルダが見つかりません: {root}", file=sys.stderr)
        sys.exit(1)

    ROOT_DIR = root
    THUMB_SIZE = args.thumb_size

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)

    ip = local_ip()
    print("=" * 56)
    print("  remote-gfx-viewer を起動しました")
    print("=" * 56)
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
