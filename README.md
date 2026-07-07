# prtextstyle-resolve

Premiere Proの `.prtextstyle` テキストスタイルプリセットを、DaVinci Resolve / Fusionの
Text+ `.setting` ファイル（と `.drfx` バンドル）に変換するCLIです。

## 設計方針（重要）

- **対応バージョン:** Premiere Pro 2022〜2023 の旧 FlatBuffers フォーマットのみ対応・検証済みです。Premiere Pro 24.x 以降の新フォーマット（New Dialect）には**未対応**です（パースは完走しますが、プロパティのマッピングが崩れます）。
- 本ツールは Premiere Pro から DaVinci Resolve への相互運用性の確保のみを目的とした非公式の変換ツールです。Adobe Systems 社、および Blackmagic Design 社とは一切関係ありません。

## パッケージ構成

```
prtextstyle_resolve/
    parser.py           # FlatBuffersデコーダ
    text_style.py        # 変換元データクラス（font, fill, effects, warnings）
    fusion_setting.py    # .setting ファイルの出力（MacroOperator + TextPlus1 + MediaOut1）
    drfx.py               # .drfx（zip）バンドルの構築
    cli.py                 # convert / build-drfx / list-presets コマンド
```

## 使い方

### 1. `.prtextstyle` を `.setting` に変換する

```sh
python3 -m prtextstyle_resolve convert my_presets.prtextstyle \
    --out-dir out \
    --category "MyPresets" \
    --subcategory "TextStyles" \
    --font-mapping font_mapping.json
```

出力:
```
out/
├── Edit/Titles/MyPresets/TextStyles/
│   ├── Style_1.setting
│   ├── Style_1.png        ← 自動生成されたプレビューサムネイル
│   └── ... 
└── report.json   ← プリセットごとの変換結果・警告
```

`--filename-prefix` でファイル名に接頭辞を付けられます。`--report` で
`report.json` の出力先を変更できます。
`--font-mapping` で指定したJSONファイルを用いて、未インストールフォントの自動フォールバック置換（例: MSゴシック→ヒラギノ）が可能です。
また、変換時にPillowを用いて `.setting` ごとに透過PNGのサムネイルが自動生成されます。

### 2. `.drfx` バンドルを作る（既存の `.drfx` にマージする場合）

`out/Edit/` の中身をユーザーの既存 `Custom-drfx` の `Edit/` 配下にコピーして
から再zipするか、以下でスタンドアロンの `.drfx` を作れます:

```sh
python3 -m prtextstyle_resolve build-drfx out --out out.drfx
```

`Custom-drfx.drfx` に直接マージする場合（推奨）:

```sh
cp -R out/Edit/Titles/MyPresets/TextStyles \
  "<Custom-drfxのパス>/Edit/Titles/MyPresets/TextStyles"
cd "<Custom-drfxのパス>"
zip -r Custom-drfx.drfx Edit -x "*/.DS_Store"
```

### 3. プリセット一覧を確認する

```sh
python3 -m prtextstyle_resolve list-presets my_presets.prtextstyle
```

## 出力される属性・出力されない属性

| 属性 | 出力 | 備考 |
|---|---|---|
| フォント (PSネーム) | する | `font_names[0]` を `font_mapping.json` (あれば) に照会して置換したのち `Font` に設定 |
| 塗り色 R/G/B | する | `fill_color` (0-255) を255で割って `Red1/Green1/Blue1` |
| 塗り不透明度 | する | `fill_opacity_pct` (0-100) を100で割って `Alpha1` |
| エフェクト (最大7個) | する | ストロークとシャドウを判別して出力。幅（`Thickness`）やブラー（`Softness`）も適切に変換 |
| グラデーション | する | 塗り、ストローク、シャドウそれぞれのグラデーションを解析し、`ShadingGradient` として出力。中間点（Midpoint）はダミーのカラーストップを挿入することで近似表現 |
| サムネイル画像 | する | `convert` コマンド実行時にPillowを用いてプレビュー画像を自動生成。ストローク、シャドウ、グラデーションもエミュレート |
| サイズ (pt) | 固定値 | デフォルト値として `Size = 100.0 / 1920` を出力 |
| トラッキング・行間 | しない | 未解決フィールドのため |

## 仕様ドキュメント (Docs)

`.prtextstyle` のバイナリ・スキーマ解析結果や、DaVinci Resolve の Text+ ノードへのマッピングの詳細仕様については、以下のドキュメントを参照してください。

- `docs/SCHEMA.md` - Premiere Pro テキストスタイル変換仕様書


## 参考資料

- `docs/SCHEMA.md` — Premiere Pro テキストスタイル変換仕様書
