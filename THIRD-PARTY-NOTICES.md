# 第三方组件与数据来源

本项目以 MIT 协议开源（见 `LICENSE`）。以下列出仓库内涉及的公开协议参考、内嵌数据及外部依赖来源，完整许可条款以原项目为准。

## 算法常量与协议参考

| 内容 | 位置 | 来源与许可 |
| --- | --- | --- |
| KGM / KGMA / VPR 掩码表（`_TABLE1` / `_TABLE2` / `_TABLE_V2` / `_VPR_KEY`） | `vpr/decrypt/kgm.py` | 公开数据，取自 unlock-music / kugou-crypto（MIT），仅作常量表使用；实现为本项目独立编写 |
| QMC 静态表、Map / RC4 算法与 footer 结构 | `vpr/decrypt/qmc.py` | 参考 unlock-music 公开实现（MIT） |
| Tencent TEA（Tars-CBC） | `vpr/decrypt/tc_tea.py` | 对齐 `tc_tea`（MIT） |
| NCM 容器格式（AES-ECB + 变体 RC4） | `vpr/decrypt/ncm.py` | 基于社区公开格式资料实现 |

## 前端与字体

- `vpr/frontend/`：本项目前端界面构建产物。
- `vpr/frontend/assets/noto-sans-sc-*.woff2`：Noto Sans SC 字体子集，SIL Open Font License 1.1（<https://scripts.sil.org/OFL>）。

## Python 运行时依赖（不随仓库分发）

`requirements.txt` 中的第三方库遵循各自开源协议：pycryptodome（BSD-2-Clause / Public Domain）、pywebview（BSD-3-Clause）、pythonnet（MIT）、pystray（LGPL-3.0）、Pillow（HPND）。打包工具 PyInstaller 为 GPL-2.0-or-later（含 bootloader 例外）。

## 外部可执行文件（不随仓库分发）

- `ffmpeg.exe`：FFmpeg（按构建选项为 LGPL-2.1-or-later 或 GPL-2.0-or-later），需用户自行获取并置于程序目录。
- `kgg-dec.exe`：第三方 KGG 解密工具，需用户自行获取，本仓库不提供或分发。

本仓库不包含任何音频样本、密钥库（`KGMusicV3.db`）或用户数据。
