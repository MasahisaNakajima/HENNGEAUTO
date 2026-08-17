# HENNGE証明書紐づけ自動化

## 実行手順

1. Python 3.11 をインストールする
2. `pip install -r requirements.txt` を実行する
3. `config/settings.json` に必要な設定を入力する
4. 必要に応じて環境変数を設定する
	- `HENNGE_USERNAME`
	- `HENNGE_PASSWORD`
	- `SMSM_USERNAME`
	- `SMSM_PASSWORD`
	環境変数が設定されている場合は `settings.json` より優先されます。
5. Edge の管理ポリシーを設定する
	- 設定先: `HKEY_CURRENT_USER\\SOFTWARE\\Policies\\Microsoft\\Edge\\AutoSelectCertificateForUrls`
	- 値名: `1`
	- 種類: `REG_SZ`
	- 例:
	  `{"pattern":"https://ap.ssso.hdems.com","filter":{"ISSUER":{"CN":"Cybertrust DeviceiD Public CA G3h"}}}`
6. すべての Edge を終了して再起動し、`edge://policy` で `AutoSelectCertificateForUrls` が表示されることを確認する
7. `python -m app.main` を実行する

## 注意点

- Windows レジストリを Python コードから自動変更しません
- パスワードや秘密情報をログ出力しない設計にしています
