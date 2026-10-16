# 法哲学関連144誌・新着論文メール便

このリポジトリは、提供されたHTMLの**144誌すべて**を固定台帳で管理し、毎朝8時（日本時間、GitHub Actionsの遅延あり）にCrossrefおよびOpenAlexで直近のオンライン公開論文を照会するPythonプログラムを含む。

## 実装した機能

- `data/journals.json`: 144誌の名称・分野・公式URL・一部の雑誌名略称。全144誌の識別子については初回実行時に照合する。**このファイルの `issn: null` は未検証を意味する。**
- ISSNおよびOpenAlex Source IDを**正確な誌名一致**で解決して `state/registry.json` に保存。誤一致・候補なしは「未解決」と報告し、別誌の論文を混入させない。
- Crossrefの`/journals/{ISSN}/works` とOpenAlexの出典別Works検索を照会。オンライン初回公開日を優先し、後の巻号収録による重複を回避。メタデータの曖昧な年月は補わない。
- メール送信済みDOIを `state/state.json` に保存し、再送を防止。48日程度の再確認期間（初回7日、以後45日）を設けてメタデータ登録遅延を緩和。
- OpenAI APIでAbstractに基づく日本語題名（副題込み）と簡潔な要約を作成。**Abstractが取得できなければ架空の要約を作らず、その旨を明記する。**
- Gmail SMTPアプリパスワードで配信（送信先はSecretsに保持）。完了後に取得できた誌と確認できなかった誌の件数を出力。
- `state/latest-report.json` は誌ごとの照会状況を保存する。各回のActions Artifactにも格納する。

## GitHubでの初期設定（必須）

1. GitHubリポジトリの **Settings → Secrets and variables → Actions → New repository secret** に以下を追加する。**秘密情報はコードやIssueに書かない。**
   - `OPENAI_API_KEY`: OpenAI APIのキー。**ChatGPT利用料金とは別にAPI利用料金が発生する**。
   - `GMAIL_USER`: 送信用Gmailアドレス（例：p07642@gmail.com）。
   - `GMAIL_APP_PASSWORD`: Googleアカウントの二段階認証で発行する**アプリ パスワード**（通常のログインパスワードではない）。
   - `MAIL_TO`: 送信先Gmailアドレス。
   - `CROSSREF_MAILTO`: Crossref問い合わせ用のメールアドレス（推奨）。
   - `OPENALEX_API_KEY`: OpenAlex APIキー。無料キーの取得を推奨するが、小規模照会ではキーなしでも利用可能。
2. **Actions** タブで `144-Journal Digest` を開いて `Run workflow` を選び、最初は `dry_run=true` で実行する。
3. `state/latest-report.json` を確認し、特に `unresolved` を解消する。実行時に誌名が曖昧で自動識別できなかった誌は、**正規のISSN・OpenAlex IDを `state/registry.json` に手動で記載する**。日付の検証も必要である。
4. `dry_run=false` で試験送信する。成功してから定時配信に切り替える（ワークフローは `0 23 * * *` UTC = 翌日08:00 JST）。

**重要:** メールはGmail SMTPが受理した時点で送信済みになる。状態ファイルのGitHubへのpushが送信後に失敗すると重複配信リスクが残るため、Actionsのログと `state/state.json` を確認して再実行すること。

## ローカル実行

```bash
python -m pip install -r requirements.txt
python -m pytest -q tests  # 別途pytestを導入する
python monitor.py --initialize-ids  # 識別子調査のみ
python monitor.py --dry-run         # メールを送らず本文生成
python monitor.py                   # 新着があればメール送信
```

## 未解決事項・限界を明示

- **144誌の台帳作成と、144誌への照会ロジック実装は完了。144誌の実API照会成功は未検証である。** この作成環境はCrossref/OpenAlexへのネットワーク接続が禁止されているため、モック単体テストのみ実施した。初回のGitHub Actions実行で検証する。
- Crossref/OpenAlexだけでは、DOIを付与しない雑誌、出版社がメタデータを提供しない論文、未収録文献までは保証できない。**成功とは「API照会の成功」であって、「出版社公式掲載全件の照合成功」ではない。** `unresolved`・`failed`・APIゼロ件を全誌完全確認と扱わないこと。
- 出版社側のフィードやHTMLは媒体別に形式が違うため、APIでカバーできない雑誌の全自動収集には、各誌のRSSやサイト専用アダプタ追加が必要。最初に監視台帳で未解決誌を可視化し、優先して補強する。
- GitHub Actionsの定時実行には遅延があり、分単位の時刻保証はない。公開リポジトリの場合、60日間活動がないとスケジュール実行が停止される。
- 1日に多数の論文が出た場合、既定値は1通につき40本まで。未送信論文はDOIの重複台帳に記録されず、後日拾う。保存する情報は公開DOIと論文情報に限定し、パスワード類はSecretsだけに置く。

## セキュリティ

公開リポジトリに `.env`、Gmailアプリパスワード、APIキーを置かない。Gmailアプリパスワードが使えない場合はOAuth 2.0 Gmail APIへ変更する。送信権限を安全性チェックの迂回に使わないこと。