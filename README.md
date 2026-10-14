<div align="center">

# M3U8 视频下载器

把 m3u8 地址粘进去，得到可直接播放的 MP4。

![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows-0078D4?logo=windows&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

</div>

## 简介

基于 **Tkinter（界面）+ asyncio / aiohttp（下载内核）** 的 Windows 桌面工具：自动解析分片列表、并发下载、AES-128 解密、按序合并，输出一个可直接播放的 `.mp4`。无需 FFmpeg，无需命令行，双击即用。

## 特性

- **自动选最高码率** — 遇 master playlist 沿 `#EXT-X-STREAM-INF` 下钻（≤3 层），取 `BANDWIDTH` 最大的子列表
- **AES-128 解密** — 支持 `#EXT-X-KEY`（AES-128-CBC）与分段换密钥；`IV` 缺失时按 RFC 8216 用媒体序列号推导
- **高并发下载** — asyncio + aiohttp，并发数 1~100 可调（默认 40），连接池随之收敛
- **断点续传** — 已下载分片自动跳过，中断后重新点「开始下载」即继续
- **原子写盘** — 分片先写 `.part` 再 `os.replace`，中断不会留下半截文件
- **失败重试** — 分片重试 3 次，列表与密钥重试 5 次并缓存（tenacity）
- **界面不卡** — 下载跑在独立线程事件循环，合并走线程池，日志自动裁剪（≤2000 行）
- **可中断** — 随时停止，未完成的分片任务会被真正取消

## 快速开始

```bash
git clone https://github.com/xufrp/M3U8.git
cd M3U8

python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

python M3U8.py
```

打包为单文件 EXE（可选，产物 `dist/M3U8.exe`，双击运行，无需 Python）：

```bash
pyinstaller M3U8.spec
```

## 使用

1. 「M3U8 地址」粘贴播放列表链接（点「粘贴」可直接取剪贴板，按回车也能开始）
2. 「视频标题」填输出文件名（无需写 `.mp4`，非法字符自动过滤）
3. 拖动滑块设置并发数（网络差就调小，建议 8~40）
4. 点「开始下载」，日志出现 `✅ 合并完成` 即成功

**输出**：运行目录下 `<视频标题>.mp4`；临时分片在 `TS/`，中断时会保留以便续传。

## 已知限制

- 不支持需要 `Referer` / `Cookie` / 签名鉴权的站点（需自行修改 `HEADERS`）
- 仅实现 `AES-128` 与 `METHOD=NONE`，不支持 SAMPLE-AES / Widevine 等
- 仅支持 http(s) 直链，不解析任何站点的专有协议，也不绕过鉴权与地域限制
- 输出目录 = 程序运行目录，建议单独放在一个空文件夹中运行
- 并发数过高易触发目标站风控，建议不超过 40

## 免责声明

本项目仅供**个人学习、技术研究与合法授权内容**的下载使用。请勿用于下载、传播受版权保护的付费内容，或任何违反目标站点服务条款及所在地法律法规的场景。使用后果由使用者自行承担。

## 许可证

[MIT](LICENSE) © 2026 xufrp
