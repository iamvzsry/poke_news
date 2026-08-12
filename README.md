# poke-lark

ポケモン関連情報（カード発売 / 抽選 / イベント / ゲーム）を収集して Lark webhook に通知する。
標準ライブラリのみ・依存ゼロ。GitHub Actions で 30 分毎に実行。

## セットアップ

### 1. リポジトリ作成

```bash
gh repo create poke-lark --public --source=. --push
# または GitHub UI で作ってから
git init && git add . && git commit -m "init" && git push
```

**public を推奨。** private だと Actions の無料枠 2,000 分/月に対し
30分毎 × 約1分 = 約1,440 分/月 とほぼ使い切る。public は無料枠無制限。
機密情報はコード側に一切入っていない（すべて Secrets 経由）。

### 2. Secrets 登録

`Settings > Secrets and variables > Actions > New repository secret`

| Name | 必須 | 内容 |
|---|---|---|
| `LARK_WEBHOOK_URL` | ✅ | `https://open.larksuite.com/open-apis/bot/v2/hook/xxxx` |
| `LARK_WEBHOOK_SECRET` | 任意 | bot の「署名検証」を有効にした場合のみ |
| `LARK_WEBHOOK_KEYWORD` | 任意 | bot の「キーワード」を有効にした場合のみ（例: `ポケモン`） |

Lark bot の security setting は「署名検証」推奨。キーワードだけだと URL を
知っている誰でも投稿できる。

### 3. 初回 seed（重要）

これをやらないと初回に過去記事が全部飛んでくる。

`Actions > poke-lark > Run workflow` で **seed = true** を選んで手動実行。
既読だけ記録して通知はしない。

### 4. 動作確認

`Run workflow` で **dry_run = true** → ログに送信予定の payload が出る。
問題なければ通常のスケジュール実行に任せる。

## state の持ち方

`state/poke_state.json` を毎回コミットで書き戻す方式。
`actions/cache` と違い確実に残り、いつ何を通知したかが git log で追える。
コミットノイズが嫌なら `Persist state` ステップを消して、代わりに:

```yaml
- uses: actions/cache@v4
  with:
    path: state
    key: poke-state-${{ github.run_id }}
    restore-keys: poke-state-
```

（`GITHUB_TOKEN` によるコミットは workflow を再トリガーしないので無限ループにはならない）

## ソースが取れなくなったら

日本語公式サイトは RSS がなく HTML を直接パースしている。
サイト改装で取れなくなったら:

```bash
python3 poke_lark.py --source pokecard --debug-source
```

抽出されたリンクが出るので、`poke_lark.py` の `SOURCES` 内 `href_pattern` を
実際の href に合わせて直す。他のロジックは触らなくてよい。

## ローカル実行

```bash
export LARK_WEBHOOK_URL="..."
python3 poke_lark.py --dry-run
```

## 注意

- スクレイピング対象への負荷を避けるため、実行間隔を 30 分より短くしないこと
- GitHub の schedule は繁忙時に遅延する（数分〜十数分）。分単位の精度が要るなら
  EC2 の crontab か EventBridge + Lambda に移すほうが確実
