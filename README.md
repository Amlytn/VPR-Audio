# VPR Audio

Windows 下的加密音频解密与转码工具，可将酷狗、网易云、QQ 音乐下载的加密文件还原并转码为 MP3 / FLAC / OGG。全程离线运行，文件只在本机处理。

## 支持格式

| 平台 | 输入格式 | 说明 |
| --- | --- | --- |
| 酷狗 | `.kgg` | 需配合 `kgg-dec.exe` 与本机 `KGMusicV3.db` |
| 酷狗 | `.kgm` `.kgma` `.vpr`（含 `.kgm.flac` `.vpr.flac`） | 直接转换 |
| 网易云 | `.ncm` | 直接转换，保留歌曲标签与封面 |
| QQ 音乐 | `.qmc0` `.qmc2` `.qmc3` `.qmc4` `.qmc6` `.qmc8` `.qmcflac` `.qmcogg` `.tkm` `.mflac` `.mgg` 等（含 `.mflac.cache` `.mgg.cache`） | 直接转换；无内置密钥的新版文件需导入 QQ 密钥库或手动填写密钥 |

输出支持 **MP3（320k）/ FLAC / OGG**，普通 MP3 / FLAC 不支持作为输入。

## 使用方法

### 准备工作

环境要求：Windows 10 / 11（64 位）。

1. 从 [Releases](../../releases) 下载 `VPR Audio.exe`。
2. 前往 FFmpeg 官网获取 Windows 构建包，解压出其中的 `ffmpeg.exe`，放到 `VPR Audio.exe` 同目录。
3. 如需转换 `.kgg`，另需准备 `kgg-dec.exe` 放在同目录，并确保本机酷狗已生成 `KGMusicV3.db` 密钥库（其中包含该歌曲的密钥）。

### 操作步骤

1. 打开 `VPR Audio.exe`。
2. 如需调整默认输出格式或并发数，可在左侧「设置中心」修改。
3. 回到「转换工作台」，将文件或文件夹拖入窗口，或点击「添加文件」/「添加文件夹」，按需修改「输出目录」。
4. 点击「开始」进行转换，过程中可随时点击「取消」。
5. 完成后点击「打开目录」查看输出文件；历史批次可在「任务记录」中查看（保留最近 200 条）。

## 常见问题

- **`.kgg` 提示找不到密钥或转换失败**
  先用本机酷狗客户端完整播放一遍该歌曲，让密钥写入 `KGMusicV3.db`，确认「酷狗密钥库」状态显示已连接后再试。
- **`.kgm` / `.kgma` / `.vpr` 提示解密结果异常**
  内置解密仅支持酷狗旧版 V2 格式，较新的加密版本（v3 / v5）暂不支持。
- **新版 QQ 音乐文件（`.mflac` / `.mgg`）提示不含密钥**
  在工作台「密钥库」切换至 QQ 并选择密钥库文件（`.db` / `.mmkv`），或在「设置中心 → QQ 音乐密钥」手动粘贴密钥。
- **文件拖不进窗口**
  从压缩包或网盘客户端里直接拖入通常拿不到本地路径，请先解压或下载到本地文件夹后再拖入。
- **点击关闭后找不到窗口**
  开启「关闭时最小化到托盘」后，关闭按钮会将窗口隐藏到右下角系统托盘，点击托盘图标即可重新打开，也可在「设置中心」关闭该选项。

## 源码运行与打包

需要 Python 3.10+（推荐 3.12）：

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
# 运行前将 ffmpeg.exe（及可选的 kgg-dec.exe）放到项目根目录
.venv\Scripts\python -m vpr.app
```

打包为单文件 exe（输出至 `dist/VPR Audio.exe`）：

```powershell
.venv\Scripts\python scripts\build.py
```

## 许可与说明

基于 MIT 协议开源，详见 `LICENSE`；内嵌算法常量、字体与外部工具的许可说明见 `THIRD-PARTY-NOTICES.md`。

本软件仅用于处理个人合法下载的本地音频文件，不提供任何绕过付费或版权限制的功能。
