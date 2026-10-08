# しおりツール – スキャンPDF補正

[한국어](README.md) · [English](README.en.md) · [中文](README.zh-CN.md)

黄ばんで薄いスキャンPDFの背景を白く、文字を濃くして、電子書籍リーダーの白黒画面でも読みやすくします。サーバー不要、インストール不要、元ファイルは一切変更しません。

![補正前後](docs/before_after.png)

## ダウンロード

- **実行ファイル**: [Releases](https://github.com/microhan1/scan-pdf-cleanup/releases) から `scan-pdf-cleanup.exe` を取得してダブルクリック。インストールは不要です。
- **ソースから実行**:

```bash
pip install -r requirements.txt
python main.py
```

## 使い方

1. PDFファイルまたはフォルダをウィンドウにドロップします。
2. プレビューを見ながらオプションを調整します（背景を白く · 文字を濃く · カラーモード · 傾き補正 · 解像度）。
3. **実行**を押すと、元ファイルの隣に `<元の名前>_clean.pdf` が作られます。

コマンドラインでも使えます。

```bash
python main.py input.pdf --contrast mid --mode gray --dpi 200 --deskew
```

`python main.py --help` はOSの言語（한국어 · English · 中文 · 日本語）でオプションを表示します。

## しないこと

- OCRはしません。出力は画像PDFです。
- 余白カットと見開き分割は別ツールです。このツールは画質だけを扱います。
- カラー写真中心の雑誌スキャンは対象外です。

## シリーズ

- しおりツール: [余白カット (TrimPDF)](https://github.com/microhan1/TrimPDF) · [見開き分割 (spread-split)](https://github.com/microhan1/spread-split) · [目次しおり (pdf-toc-add)](https://github.com/microhan1/pdf-toc-add)
- [しおりライブラリ（Chaekgalpi Library）](https://chaekgalpi.co.kr/?utm_source=github&utm_medium=referral&utm_campaign=tool_cta&utm_content=scanpdfcleanup) — 読んだ本と読書記録を残すウェブサービス（韓国語のみ）

## ライセンス

ソースコードは MIT です。[LICENSE](LICENSE) を参照。

Releases の exe には [PyMuPDF](https://github.com/pymupdf/PyMuPDF)（AGPL-3.0）が同梱されているため、exe 全体は AGPL-3.0 の条件で配布されます。対応するソースはこのリポジトリと PyMuPDF のリポジトリです。 exe に同梱されたコンポーネントとライセンス全文は [THIRD_PARTY_LICENSES.txt](THIRD_PARTY_LICENSES.txt) にあります。
