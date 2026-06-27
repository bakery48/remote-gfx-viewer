# 出先（外出先）から見る — Cloudflare Tunnel ガイド

`remote-gfx-viewer` を **同じLANの外（出先）** から見るための手順です。
ここでは **Cloudflare Tunnel** を使います。ルーターのポート開放は不要で、
HTTPS も自動で付きます。

> ⚠️ **重要**: 公開URLは「URLを知っていれば誰でもアクセスできる」状態です。
> 必ずアプリ側の **パスワード認証（`--password`）を有効にして**から公開してください。

---

## 全体像

```
スマホ(出先) ──https──> Cloudflare ──暗号化トンネル──> あなたのPC(cloudflared) ──> remote-gfx-viewer(localhost)
```

PC では「アプリ本体」と「cloudflared（トンネル）」の2つを動かします。

---

## 手順

### 1. アプリを認証付きで起動する

まず `remote-gfx-viewer` を **必ず `--password` 付き** で起動します。

```bash
# 例: Eagle連携 + 編集許可 + パスワード認証
python3 server.py --eagle --allow-edit --password "ここに長めのパスワード"
```

- パスワードは環境変数でも渡せます: `RGV_PASSWORD=... python3 server.py --eagle --password ...`
- 起動ログに `認証: 有効（パスワード）` と出ることを確認してください。
- ローカルでの待ち受けは `localhost:8000` のままで構いません
  （トンネルが `localhost` に中継します）。

### 2. cloudflared をインストール

| OS | コマンド |
| --- | --- |
| macOS | `brew install cloudflared` |
| Windows | `winget install --id Cloudflare.cloudflared` |
| Linux | [公式の手順](https://developers.cloudflare.com/tunnel/setup/) を参照 |

### 3. トンネルを張る（お試し: Quick Tunnel）

アカウント不要で、すぐ試せる方法です。

```bash
cloudflared tunnel --url http://localhost:8000
```

実行すると、次のような **ランダムな公開URL** が表示されます。

```
https://xxxx-yyyy-zzzz.trycloudflare.com
```

この URL をスマホのブラウザで開くと、まず**ログイン画面**が出ます。
手順1で設定したパスワードを入れれば閲覧できます。

> Quick Tunnel の注意点:
> - URL は **起動するたびに変わります**（固定されません）。
> - 同時接続数などに制限があり、**お試し・一時利用向け**です。
> - 常用したい場合は次の「固定URL」をどうぞ。

### 4. （任意）固定URL + さらに堅牢な認証（Named Tunnel + Access）

常用するなら、無料の Cloudflare アカウントで **固定のサブドメイン**を持てます。
さらに **Cloudflare Access** を併用すると、Cloudflare 側でメールOTP認証などを
かけられます（アプリのパスワードと二重で守れる）。

おおまかな流れ:

1. `cloudflared tunnel login`（ブラウザでドメインを認可）
2. `cloudflared tunnel create gfx` でトンネル作成
3. 設定ファイル（`config.yml`）で `hostname` とローカルの `service: http://localhost:8000` を紐付け
4. `cloudflared tunnel route dns gfx gfx.example.com`
5. `cloudflared tunnel run gfx`
6. （推奨）Zero Trust → Access → Applications で `gfx.example.com` に
   メールOTP等のポリシーを設定

詳細は公式ドキュメントを参照してください:
- Tunnel セットアップ: https://developers.cloudflare.com/tunnel/setup/
- Cloudflare Access（認証）: https://developers.cloudflare.com/cloudflare-one/

---

## セキュリティのポイント

- ✅ **必ず `--password` を付ける**（特に `--allow-edit` 使用時は削除もできるため必須）。
- ✅ パスワードは**長く・推測されにくいもの**にする。
- ✅ 常用するなら **Cloudflare Access** でさらに認証を重ねる。
- ⚠️ Quick Tunnel の URL でも、知られれば誰でもアクセスを試せます。認証が最後の砦です。
- ⚠️ ルーターのポート開放（直公開）は推奨しません。

---

## うまくいかないときは

- スマホで開けない → PCで「アプリ本体」と「cloudflared」の**両方**が動いているか確認。
- ログインできない → 起動ログに `認証: 有効` が出ているか、パスワードが合っているか確認。
- 画像が出ない（Eagle連携）→ Eagle アプリが起動しているか確認。
- URL が毎回変わる → Quick Tunnel の仕様です。固定したい場合は上の「固定URL」へ。
