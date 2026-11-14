# pidash — SSHで見るラズパイ ターミナルダッシュボード

Raspberry Pi 上で動き、PCから SSH で繋いだターミナルに表示されます。

- 🕐 大きいデジタル時計（日付・曜日・秒）
- ⛅ 天気（Open-Meteo / APIキー不要）：現在の天気・気温・体感・最高最低・降水確率・数時間先の予報
- ♫ Spotify：曲名 / アーティスト / アルバム / 進捗バー / 再生デバイス / 音量 / シャッフル・リピート
- 📰 ニュース（RSS）：最新の見出しを新しい順に表示し、10秒ごとにページを切り替え。フィードは `config.toml` の `[[news.feeds]]` で自由に追加可能
- 🍓 Pi の状態：ホスト名・IP・CPU温度・負荷・メモリ・稼働時間

```
╭──────────────────────────────────────────────╮
│          █▀▀█ █▀▀█    █▀▀▀ █▀▀▀              │
│          █  █  ▀▀█    ▀▀▀█ ▀▀▀█              │
│        2026年11月15日（日）  47秒            │
╰──────────────────────────────────────────────╯
╭──── 天気 - 東京 ────╮╭───── ♫ Spotify ───────╮
│ ⛅ 一部曇り 14.3℃   ││ ▶ 再生中               │
│ 最高/最低 17℃ / 9℃  ││ 曲名 / アーティスト    │
│ 06時 08時 10時      ││ 1:24 ━━━━━━──── 3:34   │
╰─────────────────────╯╰────────────────────────╯
```

---

## 1. Pi にインストール

```bash
# PC から
scp -r pidash pi@raspberrypi.local:~/

# Pi で
ssh pi@raspberrypi.local
cd ~/pidash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml
chmod +x run.sh
./run.sh --demo        # まずダミーデータで表示確認（Ctrl+C で終了）
```

Python 3.11 以上が必要です（Raspberry Pi OS Bookworm 以降なら標準で入っています）。

`config.toml` の `[weather]` で地点の緯度経度を設定してください。

## 2. Spotify の設定

1. https://developer.spotify.com/dashboard で **Create app**
   - Redirect URI に `http://127.0.0.1:8888/callback` を登録（`localhost` は不可。`127.0.0.1` で書く）
   - API は **Web API** にチェック
2. アプリの **Client ID** と **Client Secret** を `config.toml` の `[spotify]` に書く
3. 初回認証を Pi 上で実行：

   ```bash
   ./run.sh --auth
   ```

   表示された URL を PC のブラウザで開いて許可 → ブラウザが「このサイトにアクセスできません」になるので、
   **そのときのアドレスバーの URL（`http://127.0.0.1:8888/callback?code=...`）を丸ごとコピーしてターミナルに貼り付け** ます。
   トークンは `.spotify_token_cache` に保存され、以後は自動更新されます。

> ⚠ 2026年2〜3月の Spotify の仕様変更で、Development Mode のアプリは **アプリ所有者が Spotify Premium であること** が必要になりました
> （Premium が切れると API が動かなくなります）。自分用なら Development Mode のままで OK です。

## 3. PC から SSH で表示する

```bash
ssh -t pi@raspberrypi.local '~/pidash/run.sh'
```

`-t` を付けないと画面が描画されないので注意。PC 側にエイリアスを作っておくと楽です：

```bash
# ~/.zshrc や ~/.bashrc に
alias pidash="ssh -t pi@raspberrypi.local '~/pidash/run.sh'"
```

### (おまけ) SSH したら自動でダッシュボードを出す

`~/.ssh/config`（PC側）に専用ホストを作る方法が一番安全です：

```
Host pidash
    HostName raspberrypi.local
    User pi
    RequestTTY yes
    RemoteCommand ~/pidash/run.sh
```

→ `ssh pidash` でダッシュボード、普通に `ssh pi@raspberrypi.local` ならいつものシェル。

### tmux で常駐させて複数端末から見る

```bash
# Pi で一度だけ
tmux new -d -s dash '~/pidash/run.sh'
# PC から（何台からでも同時に見られる）
ssh -t pi@raspberrypi.local tmux attach -t dash
```

## 便利なオプション

| コマンド | 内容 |
|---|---|
| `./run.sh` | 通常起動 |
| `./run.sh --demo` | ネット不要のダミーデータ表示 |
| `./run.sh --auth` | Spotify 認証 |
| `./run.sh --once` | 1回だけ描画して終了（デバッグ用） |
| `./run.sh --3ds` | 3DS など小さい画面向け（時計・天気・Spotify・ペットだけ。更新は秒2回） |

## トラブルシュート

- **文字化け / 枠がズレる** → PC側ターミナルのフォントを等幅＋UTF-8に。絵文字の幅がズレるターミナルもあります
- **Spotify が「取得失敗: … 403」** → 所有者アカウントが Premium か、Dashboard の User Management に自分のメールが登録されているか確認
- **「未認証です」** → `./run.sh --auth` を実行。`.spotify_token_cache` を消すと認証し直し
- **天気が出ない** → Pi がネットに出られるか `curl https://api.open-meteo.com` で確認
