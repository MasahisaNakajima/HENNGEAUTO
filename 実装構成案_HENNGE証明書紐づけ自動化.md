# HENNGE証明書紐づけ自動化 実装構成案

## 1. 目的

Pythonで実装する際に、保守しやすく、拡張しやすく、EXE化しやすい構成にする。

## 2. 推奨ディレクトリ構成

```text
HENNGE_Automation/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── excel_reader.py
│   ├── hennge_handler.py
│   ├── smsm_handler.py
│   ├── file_handler.py
│   ├── logger.py
│   └── utils.py
├── config/
│   └── settings.json
├── logs/
├── screenshots/
├── downloads/
├── requirements.txt
├── README.md
└── build_exe.py
```

## 3. 各ファイルの役割

### app/main.py
- 全体の処理フローを制御する
- 各モジュールを呼び出して、1件ずつ処理を実行する

### app/config.py
- 設定ファイルの読み込みを担当する
- HENNGE/SMSMの認証情報や実行設定を管理する

### app/excel_reader.py
- Excelファイルを読み込む
- 指定シート・指定列の情報を取得する
- IMEIの空白除去などの整形処理を行う

### app/hennge_handler.py
- HENNGEのログイン
- ユーザー検索
- 証明書のダウンロード処理

### app/smsm_handler.py
- SMSMのログイン
- 証明書アップロード
- 端末検索
- IMEI紐づけ処理

### app/file_handler.py
- ダウンロードされた証明書のファイル名変更
- 既存ファイルの削除
- 必要な一時ファイル管理

### app/logger.py
- ログ出力
- エラー時のログファイル作成
- スクリーンショット保存の呼び出し

### app/utils.py
- 共通的な補助関数を配置する
- 例：日時生成、文字列整形、待機処理など

## 4. 実行フロー

1. main.py が起動する
2. config.py で設定ファイルを読み込む
3. excel_reader.py でExcel情報を取得する
4. 各対象行に対して、hennge_handler.py と smsm_handler.py を順に実行する
5. file_handler.py で証明書ファイルを管理する
6. logger.py でログ・スクリーンショットを保存する

## 5. 設定ファイルのイメージ

```json
{
  "hennge": {
    "username": "hacadmin",
    "password": "password"
  },
  "smsm": {
    "username": "",
    "password": ""
  },
  "excel": {
    "url": "https://example.sharepoint.com/...
  },
  "paths": {
    "downloads": "downloads",
    "logs": "logs",
    "screenshots": "screenshots"
  }
}
```

## 6. 実装時の注意点

- HENNGE/SMSMの画面構造が変わる可能性があるため、要素セレクタはできるだけ柔軟にする
- ログにはパスワード等の機密情報を残さない
- 実行中にページが読み込まれる時間差を考慮して待機処理を入れる
- エラー時はスクリーンショットを保存して原因を追跡できるようにする
- EXE化を見据えて、外部依存ファイル（設定ファイル・ログ出力先）を分けておく

## 7. EXE化を見据えた考慮事項

- PyInstallerを利用する想定
- 実行時に設定ファイルが参照できるようにする
- 相対パスで動作するように設計する
- 実行フォルダ配下の logs / screenshots / downloads が作成されるようにする
