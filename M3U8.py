import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import asyncio
import aiohttp
import os
import re
import sys
import time
from urllib.parse import urljoin, urlparse
from Cryptodome.Cipher import AES
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type

# ==================== 常量配置 ====================

APP_TITLE = "M3U8视频下载器"

DEFAULT_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36')

TS_TIMEOUT = 60                 # 单个分片请求总超时（秒）
CONNECT_TIMEOUT = 15            # 建立连接超时（秒）
SOCK_READ_TIMEOUT = 30          # 读取数据超时（秒）

UI_REFRESH_INTERVAL = 0.1       # 状态栏最小刷新间隔（秒），防止刷爆主线程
SPEED_WINDOW = 1.0              # 速度统计窗口（秒）
MERGING_NOTIFY_STEP = 50        # 合并时每处理多少个分片刷新一次状态

MERGE_CHUNK = 4 * 1024 * 1024   # 合并时单次读取块大小（4MB）
MAX_LOG_LINES = 2000            # 日志区最大保留行数，超出后裁剪旧内容

MAX_VARIANT_DEPTH = 3           # master playlist 最大下钻层数

INVALID_CHARS_PATTERN = r'[\\/:*?"<>|\r\n\t]'

HEADERS = {'User-Agent': DEFAULT_UA}

# 常见分片/媒体扩展名（不含 m3u8 与字幕，避免把嵌套列表、字幕当成分片）
MEDIA_EXT_PATTERN = (
    r'\.(?:ts|m2ts|m4s|mp4|m4v|mov|mkv|webm|flv|avi|3gp|mpg|mpeg|vob|'
    r'm4a|aac|ac3|eac3|mp3|mp2|wav|ogg|oga|opus|flac|'
    r'jpg|jpeg|png|gif|webp|bmp|avif)(?:[?#]|$)'
)


def sanitize_filename(name, default="output"):
    """过滤 Windows 非法文件名字符，避免写文件时抛异常"""
    name = re.sub(INVALID_CHARS_PATTERN, "_", (name or "").strip()).strip().rstrip(".")
    return name or default


def resource_path(relative):
    """兼容源码运行与 PyInstaller 打包，定位随包资源"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


def human_size(num_bytes):
    """把字节数格式化为易读字符串"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024


class M3U8Downloader:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)

        # 设置窗口初始大小与居中位置
        window_width = 800
        window_height = 660
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        x = (screen_width - window_width) // 2
        y = (screen_height - window_height) // 2
        self.root.geometry(f"{window_width}x{window_height}+{x}+{y}")
        self.root.resizable(False, False)

        # 设置窗口图标（源码运行时读取仓库内 fox.ico；找不到则忽略）
        try:
            self.root.iconbitmap(resource_path("fox.ico"))
        except Exception:
            pass

        # ---- 界面状态 ----
        self.url_var = tk.StringVar()
        self.title_var = tk.StringVar(value="output")
        self.concurrent_var = tk.IntVar(value=40)

        # ---- 下载控制 ----
        self.stop_flag = False
        self.is_downloading = False
        self.download_task = None
        self.tasks = []
        self.semaphore = None
        self.session = None
        self.event_loop = None

        # ---- 统计信息 ----
        self.total = 0
        self.downloaded = 0
        self.total_bytes = 0

        # ---- UI 刷新节流与速度统计 ----
        self._last_ui_push = 0.0        # 上次刷新状态栏的时间
        self._pending_name = ""         # 待展示的当前文件名
        self._speed_time = time.monotonic()
        self._speed_bytes = 0           # 上次测速时的累计字节
        self._speed = 0.0               # 最近一次测速结果（KB/s）
        self._log_lines = 0             # 日志区当前行数

        # ---- 密钥缓存（同一 URL 的密钥只取一次） ----
        self._key_cache = {}

        self.build_ui()

    # ================= 界面 =================

    def build_ui(self):
        main_container = ttk.Frame(self.root, padding=10)
        main_container.pack(fill=tk.BOTH, expand=True)

        top_frame = ttk.Frame(main_container)
        top_frame.pack(fill=tk.X, padx=5, pady=(0, 5))

        # ===== M3U8 地址 =====
        url_frm = ttk.LabelFrame(top_frame, text="M3U8 地址")
        url_frm.pack(fill=tk.X, padx=5, pady=(0, 8))
        url_frm.columnconfigure(0, weight=1)

        self.entry = ttk.Entry(url_frm, textvariable=self.url_var)
        self.entry.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=5, pady=5)
        self.entry.bind("<Return>", lambda _e: self.start())   # 回车即开始下载

        ttk.Button(url_frm, text="粘贴", command=self.paste_url, width=8).grid(
            row=0, column=1, padx=(0, 4), pady=5)
        ttk.Button(url_frm, text="清空", command=self.clear_url, width=8).grid(
            row=0, column=2, padx=(0, 4), pady=5)

        # ===== 标题 =====
        title_frm = ttk.LabelFrame(top_frame, text="视频标题")
        title_frm.pack(fill=tk.X, padx=5, pady=(0, 8))

        ttk.Entry(title_frm, textvariable=self.title_var).pack(fill=tk.X, padx=5, pady=5)

        # ===== 并发数滑块 =====
        concur_frm = ttk.LabelFrame(top_frame, text="并发数设置")
        concur_frm.pack(fill=tk.X, padx=5, pady=(0, 8))
        concur_frm.columnconfigure(1, weight=1)

        ttk.Label(concur_frm, text="慢（1）", font=("Arial", 9)).grid(
            row=0, column=0, padx=8, pady=5, sticky=tk.W)

        self.scale = ttk.Scale(
            concur_frm,
            from_=1,
            to=100,
            orient=tk.HORIZONTAL,
            variable=self.concurrent_var,
            length=380
        )
        self.scale.grid(row=0, column=1, sticky=(tk.W, tk.E), padx=5, pady=5)

        ttk.Label(concur_frm, text="快（100）", font=("Arial", 9)).grid(
            row=0, column=2, padx=8, pady=5, sticky=tk.W)

        self.concurrent_label = ttk.Label(concur_frm, text="当前：40", font=("Arial", 9))
        self.concurrent_label.grid(row=0, column=3, padx=10, pady=5, sticky=tk.E)

        self.concurrent_var.trace_add("write", self.update_concurrent_label)

        # ===== 进度 =====
        prog_frm = ttk.LabelFrame(top_frame, text="下载进度")
        prog_frm.pack(fill=tk.X, padx=5, pady=(0, 8))

        self.progress = ttk.Progressbar(prog_frm, mode="determinate", length=760)
        self.progress.pack(fill=tk.X, padx=5, pady=3)

        self.status = ttk.Label(prog_frm, text="等待开始", font=("Arial", 9))
        self.status.pack(fill=tk.X, padx=5, pady=(0, 5))

        # ===== 日志 =====
        log_frm = ttk.LabelFrame(main_container, text="下载日志")
        log_frm.pack(fill=tk.BOTH, expand=True, padx=5, pady=(0, 8))

        self.log = scrolledtext.ScrolledText(log_frm, height=12, font=("Arial", 9))
        self.log.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # ===== 按钮 =====
        btn_frm = ttk.Frame(main_container)
        btn_frm.pack(fill=tk.X, padx=5, pady=(0, 5))

        button_container = ttk.Frame(btn_frm)
        button_container.pack(expand=True)

        self.start_btn = ttk.Button(button_container, text="开始下载",
                                    command=self.start, width=12)
        self.start_btn.pack(side=tk.LEFT, padx=5)

        self.stop_btn = ttk.Button(button_container, text="停止",
                                   command=self.stop, width=12, state="disabled")
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        ttk.Button(button_container, text="清空日志",
                   command=self.clear_log, width=12).pack(side=tk.LEFT, padx=5)

        self.entry.focus_set()

    def update_concurrent_label(self, *args):
        self.concurrent_label.config(text=f"当前：{self.concurrent_var.get()}")

    def paste_url(self):
        """从剪贴板粘贴地址，便于从浏览器直接复制"""
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            return
        if text:
            self.url_var.set(text)

    def clear_url(self):
        self.url_var.set("")

    # ================= 日志 =================

    def log_msg(self, msg):
        """线程安全地追加一行日志（可在任意线程调用）"""
        self.root.after(0, self._append_log, msg)

    def _append_log(self, msg):
        """仅在主线程执行：写入日志并裁剪超长内容，避免长时间运行内存膨胀"""
        self.log.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self._log_lines += 1
        if self._log_lines > MAX_LOG_LINES:
            trim = MAX_LOG_LINES // 4
            self.log.delete("1.0", f"{trim + 1}.0")
            self._log_lines -= trim
        self.log.see(tk.END)

    def clear_log(self):
        self.log.delete(1.0, tk.END)
        self._log_lines = 0

    # ================= 状态栏（带节流） =================

    def update_status(self, filename="", force=False):
        """由事件循环线程高频调用；内部节流，避免拖慢主线程"""
        if filename:
            self._pending_name = filename
        if self.total == 0:
            return

        now = time.monotonic()
        if not force and now - self._last_ui_push < UI_REFRESH_INTERVAL:
            return
        self._last_ui_push = now

        # 用固定时间窗口计算速度，避免瞬时数字抖动
        if now - self._speed_time >= SPEED_WINDOW:
            elapsed = now - self._speed_time
            self._speed = (self.total_bytes - self._speed_bytes) / 1024 / elapsed
            self._speed_bytes = self.total_bytes
            self._speed_time = now

        percent = self.downloaded / self.total * 100
        text = (f"进度：{percent:5.2f}% | 完成：{self.downloaded}/{self.total} | "
                f"速度：{self._speed:7.1f} KB/s | 当前：{self._pending_name}")
        self.root.after(0, self._apply_status, percent, text)

    def _apply_status(self, percent, text):
        """仅在主线程执行：刷新进度条与状态栏"""
        self.progress["value"] = percent
        self.status.config(text=text)

    # ================= 下载控制 =================

    def start(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "请填写 M3U8 地址")
            self.entry.focus_set()
            return

        if not re.match(r'^https?://', url, re.I):
            messagebox.showwarning("提示", "地址需以 http:// 或 https:// 开头")
            self.entry.focus_set()
            return

        if self.is_downloading:
            messagebox.showwarning("提示", "已经在下载中")
            return

        # 初始化状态
        self.stop_flag = False
        self.is_downloading = True
        self.downloaded = 0
        self.total = 0
        self.total_bytes = 0
        self.tasks = []
        self._pending_name = ""
        self._last_ui_push = 0.0
        self._speed = 0.0
        self._speed_bytes = 0
        self._speed_time = time.monotonic()
        self._key_cache = {}

        # 重置进度条
        self.progress["value"] = 0
        self.status.config(text="正在解析播放列表…")

        # 禁用开始按钮，启用停止按钮
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

        # 创建新的事件循环，并交给下载线程运行
        self.event_loop = asyncio.new_event_loop()

        self.download_task = threading.Thread(
            target=self.run_async, args=(self.event_loop,), daemon=True)
        self.download_task.start()

    def run_async(self, loop):
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self.download())
        except Exception as e:
            self.log_msg(f"❌ 下载出错: {e}")
        finally:
            # 取消残留任务，避免 "Task was destroyed but it is pending"
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass

            # 无论成功还是失败，都恢复按钮状态
            self.root.after(0, self.reset_buttons)

            # 关闭事件循环
            if not loop.is_closed():
                loop.close()

    def stop(self):
        if not self.is_downloading:
            return

        self.stop_flag = True
        self.log_msg("⛔ 正在停止下载…")

        # 立即禁用停止按钮
        self.stop_btn.config(state="disabled")

        # 取消尚未完成的分片任务，真正中断下载
        loop = self.event_loop
        if loop and not loop.is_closed():
            for task in list(self.tasks):
                try:
                    loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    break

        # 兜底：3 秒后若仍未结束，强制恢复按钮状态
        self.root.after(3000, self.force_reset)

    def force_reset(self):
        """兜底：3 秒后仍在下载中，则强制恢复按钮状态"""
        if self.is_downloading:
            self.reset_buttons()

    def reset_buttons(self):
        """恢复按钮状态"""
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.is_downloading = False

    # ================= 播放列表解析 =================

    @retry(stop=stop_after_attempt(5), wait=wait_fixed(2), reraise=True)
    async def fetch_playlist(self, url):
        """获取播放列表文本（失败自动重试 5 次）"""
        async with self.session.get(url) as resp:
            resp.raise_for_status()
            return await resp.text()

    @staticmethod
    def find_best_variant(text, base):
        """从 master playlist 中挑选带宽最高的子列表，避免下到低清晰度"""
        lines = [ln.strip() for ln in text.splitlines()]
        best = None          # (bandwidth, url)
        fallback = None      # 未标注 BANDWIDTH 时兜底用第一条

        for i, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF"):
                continue
            for nxt in lines[i + 1:]:
                if not nxt or nxt.startswith("#"):
                    continue
                url = nxt if nxt.startswith("http") else (urljoin(base, nxt) if base else nxt)
                if fallback is None:
                    fallback = url
                bw = re.search(r'BANDWIDTH=(\d+)', line)
                if bw:
                    bandwidth = int(bw.group(1))
                    if best is None or bandwidth > best[0]:
                        best = (bandwidth, url)
                break

        return best[1] if best else fallback

    @staticmethod
    def parse_iv(raw, seg_index, media_sequence):
        """IV 缺失时按 RFC 8216 使用媒体序列号；兼容 0x 前缀"""
        if raw:
            if raw.lower().startswith("0x"):
                raw = raw[2:]
            value = bytes.fromhex(raw)
            if len(value) != 16:
                raise ValueError(f"IV 长度异常：{len(value)} 字节（应为 16）")
            return value
        return (media_sequence + seg_index).to_bytes(16, "big")

    async def parse_key(self, line, base, seg_index, media_sequence):
        method = re.search(r'METHOD=([^,]+)', line)
        method = (method.group(1).strip() if method else "NONE").upper()
        if method == "NONE":
            self.log_msg("🔓 检测到 METHOD=NONE，该段不加密")
            return None, None
        if method != "AES-128":
            raise ValueError(f"不支持的加密方式：{method}")

        uri = re.search(r'URI="(.+?)"', line)
        if not uri:
            raise ValueError("EXT-X-KEY 中缺少 URI")
        key_url = uri.group(1)
        if not key_url.startswith("http"):
            key_url = urljoin(base, key_url) if base else key_url

        key = await self.fetch_key(key_url)
        if len(key) not in (16, 24, 32):
            raise ValueError(f"密钥长度异常：{len(key)} 字节（应为 16/24/32）")

        iv_raw = re.search(r'IV=([^,]+)', line)
        return key, self.parse_iv(iv_raw.group(1).strip() if iv_raw else None,
                                  seg_index, media_sequence)

    async def parse_segments(self, text, base):
        """解析分片；每个分片绑定其生效的 key/iv，支持分段密钥"""
        segments = []
        media_sequence = 0
        key, iv = None, None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#EXT-X-MEDIA-SEQUENCE"):
                try:
                    media_sequence = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("#EXT-X-KEY"):
                key, iv = await self.parse_key(line, base, len(segments), media_sequence)
            elif not line.startswith("#") and re.search(MEDIA_EXT_PATTERN, line, re.I):
                url = line if line.startswith("http") else (urljoin(base, line) if base else line)
                name = os.path.basename(urlparse(url).path).split("?")[0] or f"{len(segments)}.ts"
                segments.append({"url": url, "key": key, "iv": iv, "name": name})
        return segments

    async def resolve_playlist(self, url):
        """读取播放列表，自动下钻 master playlist 并选取最高码率"""
        self.log_msg(f"🌐 获取 M3U8: {url}")
        try:
            text = await self.fetch_playlist(url)
        except Exception as e:
            self.log_msg(f"❌ 获取 M3U8 失败: {e}")
            return []

        base = url.rsplit("/", 1)[0] + "/"
        for _ in range(MAX_VARIANT_DEPTH):
            variant = self.find_best_variant(text, base)
            if not variant:
                break
            self.log_msg(f"🔀 检测到多码率列表，选用最高带宽子列表: {variant}")
            try:
                text = await self.fetch_playlist(variant)
            except Exception as e:
                self.log_msg(f"❌ 获取子播放列表失败: {e}")
                return []
            base = variant.rsplit("/", 1)[0] + "/"

        return await self.parse_segments(text, base)

    # ================= 下载核心 =================

    async def download(self):
        try:
            url = self.url_var.get().strip()
            title = sanitize_filename(self.title_var.get(), "output")
            max_concurrent = max(1, min(100, self.concurrent_var.get()))

            self.log_msg("📁 创建临时目录 TS/ …")
            os.makedirs("TS", exist_ok=True)

            # 连接池按并发数限制，避免建立多余连接；DNS 结果缓存 5 分钟
            connector = aiohttp.TCPConnector(
                limit=max_concurrent,
                limit_per_host=max_concurrent,
                ttl_dns_cache=300,
            )
            timeout = aiohttp.ClientTimeout(
                total=TS_TIMEOUT, connect=CONNECT_TIMEOUT, sock_read=SOCK_READ_TIMEOUT)

            async with aiohttp.ClientSession(
                    headers=HEADERS, timeout=timeout, connector=connector) as session:
                self.session = session

                # 会话先建立：播放列表与密钥请求可复用同一连接池
                segments = await self.resolve_playlist(url)
                self.total = len(segments)
                if self.total == 0:
                    self.log_msg("❌ 未解析到任何分片，请检查 M3U8 内容")
                    return
                self.log_msg(f"✅ 发现 {self.total} 个 TS 文件（并发数：{max_concurrent}）")

                self.semaphore = asyncio.Semaphore(max_concurrent)
                indexed = []
                skipped = 0

                for i, seg in enumerate(segments):
                    if self.stop_flag:
                        break
                    ts_path = f"TS/{i}.ts"
                    if os.path.exists(ts_path):
                        skipped += 1
                        self.downloaded += 1
                        self.total_bytes += os.path.getsize(ts_path)
                        continue
                    indexed.append((i, asyncio.create_task(self.download_ts(seg, i))))

                if skipped:
                    self.log_msg(f"⏭️ 跳过已存在的分片 {skipped} 个")
                    # 跳过部分不计入测速基准，避免首次速度虚高
                    self._speed_bytes = self.total_bytes
                    self._speed_time = time.monotonic()
                    self.update_status(force=True)

                self.tasks = [task for _, task in indexed]
                if self.tasks:
                    results = await asyncio.gather(*self.tasks, return_exceptions=True)
                    failed = [i for (i, _), res in zip(indexed, results)
                              if isinstance(res, Exception)
                              and not isinstance(res, asyncio.CancelledError)]
                    if failed:
                        preview = ", ".join(map(str, failed[:10]))
                        tail = " …" if len(failed) > 10 else ""
                        self.log_msg(
                            f"❌ 有 {len(failed)} 个分片下载失败（序号 {preview}{tail}），"
                            f"已下载的分片可续传")

            if self.stop_flag:
                self.log_msg("⏹️ 下载已停止（TS 目录已保留，可重新下载续传）")
                return

            missing = [i for i in range(self.total) if not os.path.exists(f"TS/{i}.ts")]
            if missing:
                self.log_msg(
                    f"⚠️ 仍有 {len(missing)} 个分片未下载，保留 TS 目录，重新点击下载可续传")
                return

            self.update_status(force=True)
            self.log_msg("🎉 分片下载完成，开始合并…")
            # 合并期间禁用停止按钮，避免"停止"与"合并"语义冲突
            self.root.after(0, lambda: self.stop_btn.config(state="disabled"))
            # 合并放入线程池执行，避免大文件拼接时界面冻结
            await asyncio.get_running_loop().run_in_executor(None, self.merge_ts, title)

        except asyncio.CancelledError:
            self.log_msg("⏹️ 下载已取消")
        except Exception as e:
            self.log_msg(f"❌ 下载过程中出错: {e}")
        finally:
            self.tasks = []
            self.semaphore = None
            if self.session and not self.session.closed:
                await self.session.close()
            self.session = None

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(2),
           retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
           reraise=True)
    async def fetch_key(self, url):
        """获取解密密钥：复用下载会话并缓存，避免同一密钥重复请求"""
        if url in self._key_cache:
            return self._key_cache[url]

        if self.session is not None:
            async with self.session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.read()
        else:
            async with aiohttp.ClientSession(
                    headers=HEADERS,
                    timeout=aiohttp.ClientTimeout(total=CONNECT_TIMEOUT * 2)) as sess:
                async with sess.get(url) as resp:
                    resp.raise_for_status()
                    data = await resp.read()

        self._key_cache[url] = data
        return data

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(2),
           retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
           reraise=True)
    async def download_ts(self, seg, index):
        async with self.semaphore:
            if self.stop_flag:
                return

            async with self.session.get(seg["url"]) as resp:
                resp.raise_for_status()     # 404/反爬页面不再被当成正常分片写入
                data = await resp.read()

            if seg["key"]:
                data = AES.new(seg["key"], AES.MODE_CBC, seg["iv"]).decrypt(data)

            # 写盘放到线程池，避免阻塞事件循环
            await asyncio.get_running_loop().run_in_executor(
                None, self.write_ts, f"TS/{index}.ts", data)

            self.total_bytes += len(data)
            self.downloaded += 1
            self.update_status(seg["name"])

    @staticmethod
    def write_ts(path, data):
        """先写 .part 再原子替换，避免中断留下半截文件影响续传"""
        tmp = f"{path}.part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    def merge_ts(self, title):
        """在线程池中按序号拼接分片；分块读取以控制内存占用"""
        out_path = f"{title}.mp4"
        self.log_msg(f"🔄 开始合并 TS 文件 -> {out_path}")

        cleaned = 0
        with open(out_path, "wb") as out:
            for i in range(self.total):
                ts_file = f"TS/{i}.ts"
                if not os.path.exists(ts_file):
                    continue
                with open(ts_file, "rb") as f:
                    while True:
                        chunk = f.read(MERGE_CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                os.remove(ts_file)
                cleaned += 1
                if cleaned % MERGING_NOTIFY_STEP == 0:
                    self.root.after(0, self._apply_status, 100.0,
                                    f"合并中：{cleaned}/{self.total} …")

        # TS 目录为空则删除
        try:
            if os.path.exists("TS") and not os.listdir("TS"):
                os.rmdir("TS")
                self.log_msg("🗑️ 删除临时目录: TS")
        except OSError:
            pass

        size_text = human_size(os.path.getsize(out_path)) if os.path.exists(out_path) else "?"
        self.log_msg(f"🗑️ 已清理临时分片: {cleaned} 个")
        self.log_msg(f"✅ 合并完成: {out_path}（{size_text}）")
        self.root.after(0, self._apply_status, 100.0,
                        f"✅ 已完成：{out_path}（{size_text}）")


if __name__ == "__main__":
    root = tk.Tk()
    app = M3U8Downloader(root)
    root.mainloop()
