# SafeDrop

> CNL Group 5 Final Project — Secure Zero-Config LAN File & Clipboard Sharing Tool
> 郭至恩、吳健賓、顏愷威、林冠辰、謝承憲、林詠宸

SafeDrop 是一個跨平台、零設定的區網檔案 / 剪貼簿分享工具。
位於同一個 Wi-Fi / LAN 下的兩台裝置可以**自動發現對方**，不經由雲端、
不需帳號，即可**安全（端對端加密）**地傳送：

- 任意大小的檔案（chunked，顯示進度與速度）
- 文字、URL、程式碼片段（接收端可一鍵複製 / 開啟連結）

完整設計請見 [`spec.md`](spec.md)。

## 1. Architecture at a glance

```
┌─────────────────────────────────────────────┐
│  User Interface  (tkinter)                  │
├─────────────────────────────────────────────┤
│  Device Discovery  (UDP broadcast)          │  HELLO / BYE
├─────────────────────────────────────────────┤
│  Control Protocol  (JSON over TCP)          │  REQUEST / ACCEPT / CHUNK / …
├─────────────────────────────────────────────┤
│  Data Transfer  (TCP socket, chunked)       │
├─────────────────────────────────────────────┤
│  Security  (X25519 ECDH + Fernet AES)       │  pairing code
└─────────────────────────────────────────────┘
```

| 檔案 | 內容 |
| --- | --- |
| `safedrop/config.py` | port 號、chunk size、預設下載資料夾 |
| `safedrop/protocol.py` | TCP 訊息 framing（4-byte length + JSON / 加密 JSON）|
| `safedrop/crypto.py` | X25519 keypair、ECDH、Fernet session、pair code |
| `safedrop/discovery.py` | UDP 廣播 + 接收，維護 nearby peers 表 |
| `safedrop/transfer.py` | TCP server / client、handshake、REQUEST/ACCEPT、檔案 & 剪貼簿傳輸 |
| `safedrop/gui.py` | tkinter 主介面、Accept/Reject 對話框、進度顯示 |
| `safedrop/__main__.py` | `python -m safedrop` 入口 |
| `run.py` | 直接執行的方便 launcher |

## 2. Requirements

- Python 3.10+，**而且需要附帶 Tk 8.6 的 framework Python**
  （macOS 內建 `/usr/bin/python3` 也可，但版本較舊；建議用
  [python.org 的官方安裝包](https://www.python.org/downloads/) 或 `brew install python-tk`。）
- 套件：`cryptography`, `pyperclip`（標準庫 `tkinter` 已內建）

```bash
# 建議：用 framework Python 建一個 venv
/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 3. Run

兩台筆電都裝好之後，**連到同一個 Wi-Fi / LAN**，各自啟動：

```bash
.venv/bin/python run.py
# 或
.venv/bin/python -m safedrop
```

兩邊的「Nearby devices」清單會在 3-10 秒內自動列出對方。

### 傳檔

1. 在 Sender 端的清單上點選對方
2. 按 **Choose…** 選檔，按 **Send file**
3. Receiver 跳出對話框，顯示對方名稱、檔案、**pair code**
   - 兩台機器的 pair code 必須一致；如果不一致代表有 MITM
4. Receiver 按 **Accept**，開始加密傳輸（進度條 + 速率）
5. 完成後檔案會存到 `~/Downloads/SafeDrop/`
   雙擊傳輸列可以在 Finder 中 reveal

### 傳剪貼簿 / URL / 程式碼

1. 點選對方
2. 把內容貼到下方文字框（或直接按 **Paste from clipboard**）
3. 選擇 Text / URL / Code，按 **Send clipboard**
4. Receiver 按 **Accept** 之後跳出視窗，可以一鍵 **Copy to clipboard**
   或 **Open URL**（URL 模式時）

## 4. Security model

1. **Discovery 廣播是明文** — 只攜帶名稱與公鑰，不洩漏內容
2. **TCP 握手** — 雙方各自送一次 plaintext HELLO（含 X25519 公鑰）
3. **ECDH** — 用對方公鑰 + 自己私鑰算 shared secret
4. **HKDF-SHA256** → 32-byte Fernet 金鑰（AES-128-CBC + HMAC-SHA256）
   → 另外導 4 位數字作為 **pair code** 供使用者目視核對
5. **此後一切訊息（含 chunk）皆 Fernet 加密**
6. **接收端 Accept** 之前不會有任何檔案資料外流；剪貼簿同理（先 preview）

可以用 Wireshark 觀察 `tcp.port == 47891` 的封包，會看到 handshake 之後
完全是加密內容。

## 5. Protocol summary

| 步驟 | 方向 | type | 加密 |
| --- | --- | --- | --- |
| Discovery | UDP broadcast | `HELLO` / `BYE` | 否 |
| Handshake | sender → recv | `HELLO` (pubkey) | 否 |
| Handshake | recv → sender | `HELLO_ACK` (pubkey + pair_code) | 否 |
| Request  | sender → recv | `REQUEST` | 是 |
| Decision | recv → sender | `ACCEPT` / `REJECT` | 是 |
| File     | sender → recv | `CHUNK` × N (last 帶 `"final": true`) | 是 |
| Clip     | sender → recv | `CLIPBOARD` (一次) | 是 |

所有 TCP frame 都是 `[4-byte big-endian length][payload]`。

## 6. Benchmarking transfer speed

`bench.py` 用 SafeDrop 自己的傳輸引擎跑各種大小檔案，並驗證 sha256。

### 本機 loopback（最快路徑，純 CPU bound：JSON + base64 + Fernet）

```bash
.venv/bin/python bench.py                                    # 預設 1KB / 100KB / 1MB / 10MB / 100MB
.venv/bin/python bench.py local --sizes 1KB 1MB 100MB 500MB  # 自訂
```

輸出範例（M1 Mac、loopback）：

```
      size   wall (s)   xfer (s)      MB/s  ok
--------------------------------------------------
     1.0KB      0.013      0.013      0.08  ✓
   100.0KB      0.013      0.013      7.28  ✓
     1.0MB      0.030      0.013     79.80  ✓
    10.0MB      0.148      0.135     74.24  ✓
    50.0MB      0.715      0.702     71.21  ✓
```

> `wall` 含 handshake (ECDH + HELLO 來回 + Accept)，`xfer` 只算 chunk 流。
> 小檔吞吐量被握手成本拉低；大檔大約在 70-80 MB/s（loopback、純加密成本）。

### 跨機器（真正的 Wi-Fi / LAN 速度）

接收端：

```bash
.venv/bin/python bench.py receive --port 47891
# 會印出一行 base64 pubkey — 複製起來
```

發送端（同網段另一台）：

```bash
.venv/bin/python bench.py send <接收端 IP> --port 47891 \
    --peer-pubkey <貼上接收端的 pubkey> \
    --sizes 1MB 10MB 100MB
```

跨機器要傳 pubkey 是因為 bench 模式繞過 UDP discovery，純粹點對點測 TCP 吞吐量。
（GUI 模式不用，因為 HELLO 廣播裡就帶了 pubkey。）

## 7. Troubleshooting

| 現象 | 解法 |
| --- | --- |
| 看不到對方 | 確認都在同一 Wi-Fi；macOS 第一次跑會跳「允許接受連線」，按允許；公司 / 學校網路 (eduroam) 可能擋 UDP 廣播，目前需手動同網段 / 個人 hotspot 測試 |
| `ModuleNotFoundError: _tkinter` | Python 沒有附 Tk；改用 framework Python 或安裝 `python-tk` |
| Port 已被占用 | 改 `safedrop/config.py` 裡 `DISCOVERY_PORT` / `TCP_PORT` |

## 8. AI agents / MCP / CLI

SafeDrop 兩條 programmatic 介面，讓 AI agent 或自動化變成 LAN 上一個 SafeDrop peer。

### 8.1 安裝

```bash
.venv/bin/pip install -e .[mcp]
# 安裝兩個 entry point：
#   safedrop      — CLI
#   safedrop-mcp  — MCP stdio server
```

### 8.2 MCP server

啟動時自行生 Identity / Discovery / TransferManager，TCP 用動態 port，跟 GUI 完全解耦。
同台機器 GUI + MCP 並存沒問題，會看到兩個 peer（`Mac (Darwin)` 與 `Mac (Darwin, MCP)`）。

4 個 tools：

| Tool | 用途 |
| --- | --- |
| `list_devices()` | 列當前 LAN 看到的 SafeDrop peer |
| `send_file(device, path, timeout_seconds?)` | 發檔，blocking 直到 done/rejected/failed |
| `send_text(device, content, content_type?, timeout_seconds?)` | 發 text/url/code |
| `wait_for_drop(timeout_seconds?)` | 阻塞直到別的裝置 push 進來 |

**接收端裝置的人類仍要按 Accept** — AI agent 只把「按下 Send」這一步換成「Claude 幫你呼叫」，trust model 沒退化。

### 8.3 接到 Claude Code / Claude Desktop / Cursor

**Claude Code：**

```bash
claude mcp add safedrop -- /Users/you/path/to/.venv/bin/safedrop-mcp
```

**Claude Desktop：** 編輯 `~/Library/Application Support/Claude/claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "safedrop": {
      "command": "/Users/you/path/to/.venv/bin/safedrop-mcp"
    }
  }
}
```

**Cursor：** 類似 Claude Desktop，編輯 Cursor 的 MCP settings 加同樣 entry。

之後在 agent 視窗講「列出附近裝置」「把這份檔傳到我手機」等指令即可。

### 8.4 CLI (給 bash agent / 自動化)

```bash
safedrop ls                                  # 列 peer
safedrop send-file <device> <path>           # 發檔
safedrop send-text <device> "hello"          # 發文字
safedrop send-text <device> "https://..." --type url
cat snippet.py | safedrop send-text <device> --stdin --type code
safedrop wait --timeout 120                  # 等別人 push 過來
```

每次 invocation 跑一個 ephemeral peer 然後關掉。加 `--json` 取得結構化輸出。
`device` 可填 peer 名稱（substring 即可）或完整 device id。

### 8.5 Cross-device tools (Phase 2)

每個 SafeDrop peer 自帶一個 ToolRegistry，別的 peer 可以透過已加密的 TCP channel
**動態探詢並呼叫**。Master agent（Claude Code / Cursor / …）把所有 trusted peer 的
tools 收集起來當延伸臂用。

**內建 tools**（每個 Python peer 都有）：

| Tool | 行為 |
| --- | --- |
| `system_info` | hostname / OS / Python 版本 |
| `read_clipboard` | 讀本機剪貼簿 |
| `write_clipboard` | 寫本機剪貼簿（`{"content": "..."}`） |
| `run_shell` | 跑 shell 指令 —— **預設關**，peer 端設 `SAFEDROP_ALLOW_SHELL=1` 才開 |

**自訂 tool** — 把 `HeadlessSafeDrop` 換成自己的 `ToolRegistry` 即可：

```python
from safedrop.tools import ToolRegistry, register_default_tools, ToolSpec
from safedrop.headless import HeadlessSafeDrop

reg = ToolRegistry()
register_default_tools(reg)
reg.register(ToolSpec(
    name="add",
    description="Return a + b",
    input_schema={"type": "object",
                  "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                  "required": ["a", "b"]},
    handler=lambda args: {"sum": args["a"] + args["b"]},
))
HeadlessSafeDrop(tool_registry=reg).start()
```

**從 MCP / CLI 呼叫**：

```bash
# 從另一台 peer 看 RECV 端的 tool 清單
.venv/bin/safedrop tools RECV
# Tools on Mac (Darwin, RECV):
#   system_info               Return basic info about this device...
#   read_clipboard            Read the local clipboard...
#   ...

# 遠端執行
.venv/bin/safedrop call RECV system_info
# {"hostname": "Mac", "platform": "Darwin", ...}

.venv/bin/safedrop call RECV write_clipboard --args '{"content":"hello"}'
# {"status": "ok", "wrote_chars": 5}
```

從 Claude Code：

```
> what's on my other Mac's clipboard?
[uses list_remote_tools then call_remote_tool("Other Mac", "read_clipboard")]

> set my Pi's clipboard to "hello"
[call_remote_tool("Pi", "write_clipboard", {"content": "hello"})]
```

**Trust + Audit**：`TransferManager.on_tool_call` callback 決定 allow/deny；每次
cross-device call（兩端）都寫進 `audit_log`，可從 MCP tool `audit_log()` 或 CLI
`safedrop audit` 拉。Phase 2.0 預設 allow-all，Phase 2.1 會接到 GUI dialog。

完整協定設計 → [spec.md §16](spec.md#16-cross-device-tools-phase-2--implemented)。

## 9. Android client

`android/` 是原生 Kotlin / Jetpack Compose 實作的 SafeDrop client，講同一套
JSON-over-TCP + X25519 + Fernet 協定（spec.md §5）。同一個 Wi-Fi 下手機跟桌面
端互看是自動發現，跨機器 / 跨平台都可以收檔、傳剪貼簿。

### Build

需要 macOS / Linux + JDK 17 + Android SDK platform 36 + build-tools 36+。

```bash
cd android
./gradlew assembleDebug
# APK 落在 app/build/outputs/apk/debug/app-debug.apk
```

第一次 build 約 1-2 分鐘（要下載 AGP、Compose 等依賴）。之後增量 build ~10s。

### Install + run

```bash
# 任一已開的 AVD 或實體裝置
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell am start -n com.safedrop.android/.MainActivity
```

### Real Wi-Fi pairing (手機 ↔ 桌面)

1. 兩台連到同一個 Wi-Fi，桌面端 `python run.py`，手機開 SafeDrop
2. 雙方在 *Nearby devices* 自動看到對方（UDP HELLO 廣播）
3. 桌面或手機任一邊發起傳送，對方按 *Accept*
4. Pair code 雙方顯示同一組 4 位數字（從 ECDH shared secret 衍生）

### Emulator pairing (`adb forward` trick)

Android Emulator 用 NAT 對外，UDP 廣播跨不出 host，所以 *Nearby devices* 不會自動列出 host 上的桌面端。
兩種繞道：

**Direction A：Android → Desktop（最簡單）**

```bash
# 桌面端開接收：
.venv/bin/python bench.py receive --port 47891
# 印出 Receiver pubkey: <base64>...
```

在 emulator 的 SafeDrop：
- 按 **+ Add manually**
- IP `10.0.2.2`（emulator 對 host 的別名）、port `47891`
- 貼上 pubkey → Add
- 選此 peer，丟檔 / 剪貼簿過去

**Direction B：Desktop → Android**

```bash
# 把 host port 48050 轉發到 emulator 的 47891：
adb forward tcp:48050 tcp:47891
# 桌面端 SafeDrop GUI 看不到 emulator，所以用 bench.py send：
.venv/bin/python bench.py send 127.0.0.1 --port 48050 \
    --peer-pubkey '<paste-android-pubkey>' \
    --sizes 1MB
```

但 Android 那邊不會自動 accept；要在手機 UI 上手動按 *Accept*。Android 的 pubkey 印在哪？
目前是 dev-time 才需要，可以從 `adb logcat | grep -i safedrop` 拉，或日後加一個 *"Show my pubkey"* 按鈕。

### Crypto interop check

```bash
adb forward tcp:48050 tcp:47891
.venv/bin/python tests/test_android_interop.py 127.0.0.1 48050
```
跑完會印兩邊各自從 ECDH 算出的 pair code，相同即代表 X25519 + HKDF + base64 + JSON framing 完全對齊。

### Source layout

| 路徑 | 內容 |
| --- | --- |
| `android/app/src/main/java/com/safedrop/android/crypto/` | `Hkdf`, `Fernet`, `Identity`, `Session`（BouncyCastle X25519、自寫 Fernet）|
| `…/net/Protocol.kt` | TCP frame I/O，與 Python `protocol.py` 對齊 |
| `…/net/Discovery.kt` | UDP broadcast 探測，附 `WifiManager.MulticastLock` |
| `…/net/TransferManager.kt` | TCP server + 收發 file/clipboard，coroutine driven |
| `…/data/SafeDropService.kt` | 流程 singleton：identity + discovery + transfer + manual peers |
| `…/ui/HomeScreen.kt` | Compose 主畫面、incoming dialog、clipboard dialog、add-manual dialog |

## 10. Development timeline (已完成)

- **W1** UDP discovery + plaintext TCP prototype
- **W2** tkinter GUI + clipboard sharing
- **W3** Pairing + X25519/Fernet encryption
- **W4** Integration testing + demo
