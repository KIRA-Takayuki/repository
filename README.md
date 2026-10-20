# 学術誌145誌・新着論文ウォッチ（メタデータ収集）

指定された法哲学・政治哲学・倫理学等の学術誌**145誌**をGitHub Actionsで日次照会する。最初の144誌に、2026年10月21日からCambridge University Pressの *Philosophy* を追加した（J145、[公式FirstView](https://www.cambridge.org/core/journals/philosophy/firstview)、ISSN 0031-8191、eISSN 1469-817X）。原則として日本時間の毎朝に収集を開始するが、GitHub Actionsのスケジュール実行には遅延がある。

## 監視台帳

- `data/journals.json`：145誌の名称、分類、公式URL、出版社、誌名略称等。
- `state/registry.json`：誌別ISSN、OpenAlex ID、識別結果。J145はCambridge Core記載のISSNを登録済み。
- 主要7出版社 **90誌を先に照会**：Springer Nature 23誌、Cambridge University Press 24誌、Wiley 14誌、Oxford University Press 11誌、SAGE 8誌、Taylor & Francis 8誌、University of Chicago Press 2誌（*Ethics*、*Polity*）。他の55誌も毎回照会を試みる。

## 実際に動作する処理

1. `.github/workflows/journal-digest.yml` が `monitor.py` を実行する（`workflow_dispatch` による手動実行も可能）。
2. CrossrefのISSN別論文メタデータを照会し、利用可能な場合はOpenAlexの情報も補助に使用する。誌名・識別子が曖昧なら他誌のデータと混同せず未解決として記録する。
3. `state/latest-report.json`：照会済み・未解決・失敗の各誌の状況、主要出版社の集計、`journal_total` を保存する。
4. `state/latest-candidates.json`：候補の原題、著者、DOI、公開日メタデータ、取得できたAbstractを保存する。
5. `state/latest-digest.txt`：機械的に得られた候補一覧を保存する。**これは日本語要約済みの完成配信ではない。**

**GitHub側はメタデータ収集専用であり、OpenAI APIの呼び出し、Gmail送信、SMTP送信を一切実施しない。** ChatGPTの定期タスクが出版社一次資料の初回オンライン公開日やAbstractを確認して、日本語訳題名・要約をChatGPT内に掲載する。メールは原則としてChatGPT標準のタスク通知にとどめる。

## 注意点

- GitHubの `checked` はAPI照会成功であって、出版社公式サイトで全件確認できたという意味ではない。`unresolved`・`failed` も併記する。
- 初回公開日は出版社のFirstView、Online First、Early View等を優先し、後日の巻号収録・メタデータ更新を新着として再掲載しない。
- 書評、訂正、editorial、撤回等が候補に混入する可能性がある。出版社ページで文献種別と本文・Abstractを確認してから研究論文として紹介する。
- API照会用の `CROSSREF_MAILTO` と `OPENALEX_API_KEY` は任意の設定であり、**OpenAI APIの契約・キー・追加課金は不要**。
- 収集結果の正常動作は[GitHub Actions](https://github.com/KIRA-Takayuki/repository/actions)で確認できる。新しい145誌の設定が成功したかは次回の実行ログで検証する。

## ローカルでの実行

```bash
python -m pip install -r requirements.txt
python monitor.py --initialize-ids
python monitor.py --dry-run
python monitor.py
```

`--dry-run` の有無にかかわらず、現在の `monitor.py` はメールを送らない。
