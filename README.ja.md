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

- しおりツール: [余白カット](https://github.com/microhan1/scan-pdf-crop) · [見開き分割](https://github.com/microhan1/scan-pdf-split)
- [しおりライブラリ](https://github.com/microhan1/chaekgalpi)

## ライセンス

MIT。[LICENSE](LICENSE) を参照。
