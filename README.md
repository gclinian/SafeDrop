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

## 8. Android client

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

## 9. Development timeline (已完成)

- **W1** UDP discovery + plaintext TCP prototype
- **W2** tkinter GUI + clipboard sharing
- **W3** Pairing + X25519/Fernet encryption
- **W4** Integration testing + demo
