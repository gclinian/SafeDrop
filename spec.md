# SafeDrop — Secure Zero-Config LAN File & Clipboard Sharing Tool

> CNL Group 5 Final Project
> 郭至恩、吳健賓、顏愷威、林冠辰、謝承憲、林詠宸

## 1. Overview

SafeDrop 是一個跨平台、零設定 (zero-configuration) 的區網檔案與剪貼簿分享工具。
位於同一個 Wi-Fi / LAN 下的兩台裝置可以自動互相發現，不經由雲端、不需帳號、
不需要手動輸入 IP，即可安全地傳送檔案、文字、URL 與程式碼片段。

**Goal — make nearby sharing feel like one click.**

## 2. Motivation

| 既有方案 | 痛點 |
| --- | --- |
| Cloud (Drive / iCloud) | 需要帳號、需要外網、檔案會經第三方 |
| USB | 線材、不直覺 |
| Messaging (Line / Email) | 第三方平台、無隱私 |
| 手寫 Socket | 必須查 IP、輸入 port，對使用者不友善 |

→ 在同一 Wi-Fi 內，其實可以直接透過區網高速傳輸。

## 3. Problem Definition

在同一 Wi-Fi 下，如何做到「不用設定」且「安全」分享？

核心需求：

- 自動發現附近裝置（zero-configuration discovery）。
- 直接傳送 檔案 / 文字 / URL / 程式碼片段（LAN-direct）。
- 不依賴外部雲端 server（privacy-friendly）。
- 接收端必須能 **接受** 或 **拒絕**。
- 可配對與加密，防止同網段竊聽。

## 4. System Architecture

分層設計，由上往下：

```
┌─────────────────────────────────────────────┐
│  User Interface  (tkinter GUI)              │  裝置列表、傳送面板、確認對話、進度
├─────────────────────────────────────────────┤
│  Device Discovery  (UDP broadcast)          │  HELLO / BYE 廣播，建立 peer table
├─────────────────────────────────────────────┤
│  Control Protocol  (JSON over TCP)          │  REQUEST / ACCEPT / REJECT / META
├─────────────────────────────────────────────┤
│  Data Transfer  (TCP socket, chunked)       │  檔案 chunked 傳輸 + 剪貼簿訊息
├─────────────────────────────────────────────┤
│  Security Layer  (ECDH + Fernet AES)        │  pairing code、信任裝置、加密
└─────────────────────────────────────────────┘
```

### 4.1 模組對應

| 層 | 模組 | 主要職責 |
| --- | --- | --- |
| UI | `safedrop/gui.py` | tkinter 視窗、清單、進度條、確認對話框 |
| Discovery | `safedrop/discovery.py` | UDP 廣播 + 監聽，維護附近裝置列表 |
| Protocol | `safedrop/protocol.py` | JSON 訊息結構、編碼、解碼 |
| Transfer | `safedrop/transfer.py` | TCP Server / Client、chunk 傳檔 |
| Security | `safedrop/crypto.py` | ECDH 金鑰交換、Fernet 對稱加密、pairing |
| Entry | `safedrop/__main__.py` | 啟動 discovery / server / GUI |
| Config | `safedrop/config.py` | port、chunk size、版本 |

## 5. Protocol Specification

### 5.1 Discovery (UDP, port `47890`, broadcast 到 `255.255.255.255`)

每個節點每 3 秒廣播一次 `HELLO`；離開時送一次 `BYE`。
監聽器把收到的 `HELLO` 寫進本機 peer table，並設 TTL（10 秒沒收到就移除）。

```json
{
  "type": "HELLO",
  "device_id": "9a4f...uuid",
  "name": "Alice's Laptop",
  "platform": "darwin",
  "tcp_port": 47891,
  "pubkey": "<base64 X25519 public key>",
  "version": "1.0"
}
```

```json
{ "type": "BYE", "device_id": "9a4f...uuid" }
```

### 5.2 Control Channel (TCP, default port `47891`)

連線建立後，先做一次 **handshake**：

```
sender  → receiver : { "type": "HELLO",  "device_id":..., "name":..., "pubkey":... }
receiver → sender  : { "type": "HELLO_ACK", "device_id":..., "name":..., "pubkey":..., "pair_code": "8421" }
```

雙方用對方的 X25519 public key + 自己的 private key 算出 shared secret，
再 HKDF-SHA256 → 32 bytes → Fernet 金鑰。之後 **所有訊息與檔案資料** 都用此 Fernet 金鑰加密。

`pair_code` 是由 shared secret 衍生出的 4 位數字，雙方介面都會顯示同一組碼，
讓使用者口頭核對防止 MITM。

### 5.3 Request / Response

```json
{
  "type": "REQUEST",
  "transfer_id": "uuid",
  "kind": "file",
  "name": "lecture.pdf",
  "size": 2048576,
  "sha256": "..."
}
```

```json
{
  "type": "REQUEST",
  "transfer_id": "uuid",
  "kind": "clipboard",
  "content_type": "url",   // "text" | "url" | "code"
  "preview": "https://github.com/example/project",
  "length": 38
}
```

```json
{ "type": "ACCEPT", "transfer_id": "uuid" }
{ "type": "REJECT", "transfer_id": "uuid", "reason": "user" }
```

### 5.4 Data Transfer

**檔案：** 以 64 KB chunk 傳送。每個 chunk 是一個加密 frame：

```
[ 4-byte big-endian length N ][ N bytes Fernet ciphertext ]
```

明文格式：

```json
{ "type": "CHUNK", "transfer_id": "...", "seq": 0, "data_b64": "..." }
```

最後一個 chunk 帶 `"final": true`。

**剪貼簿：** 一次性訊息

```json
{
  "type": "CLIPBOARD",
  "transfer_id": "...",
  "content_type": "text|url|code",
  "content": "actual content"
}
```

### 5.5 Message Framing

TCP 是 stream，每個 JSON 訊息以 `[4-byte length][payload]` 的長度前綴方式 framing。
握手完成後，`payload` 為 Fernet 加密的 JSON bytes；握手前則是明文 JSON。

## 6. Workflow

```
①  使用者開啟 SafeDrop，自動加入同一 Wi-Fi
②  本機開始廣播 HELLO，並監聽其他裝置
③  GUI 即時顯示「附近裝置」清單
④  使用者選一台裝置，挑檔案 / 貼剪貼簿，按 Send
⑤  接收端跳出確認對話 (顯示 pairing code、檔名 / 預覽)
⑥  接收端按 Accept → 開始加密 TCP 傳輸 (進度條 + 速率)
⑦  完成後檔案存到 ~/Downloads/SafeDrop/，剪貼簿可一鍵複製或開啟
```

## 7. Key Features

1. **Zero-Configuration Discovery** — UDP broadcast，自動列出附近裝置
2. **Direct LAN File Transfer** — TCP chunked，顯示進度與速度
3. **Clipboard Sharing** — Text / URL / Code，一鍵複製或開啟連結
4. **Receiver Confirmation** — 每次傳輸都需接收端按下 Accept / Reject
5. **Security Layer** — X25519 ECDH 交換金鑰 + Fernet (AES-128) 加密 + pairing code

## 8. Security Model

- **Confidentiality** — Fernet (AES-128-CBC + HMAC-SHA256) 加密所有 control 訊息與資料。
- **Authentication of channel** — 由 ECDH shared secret 衍生 pairing code，雙方目視確認以對抗 MITM。
- **Authorization** — 接收端使用者按 Accept 才會開始傳輸。
- **No persistence by default** — 程式不會儲存對方公鑰；可選擇 "Trust this device" 把對方加入信任清單，下次免確認。
- **Clipboard privacy** — 絕不在背景自動同步；發送前一定先 preview。

## 9. Technical Challenges & Solutions

| Challenge | Solution |
| --- | --- |
| UDP broadcast 被防火牆擋 | Fallback：手動輸入 IP、QR pairing、未來支援 mDNS |
| 同網段竊聽 | ECDH + Fernet 加密、receiver 確認、pairing code |
| 大檔中斷 | 64 KB chunk、`transfer_id` 可作為 resume 基礎 (未來) |
| 剪貼簿隱私 | 不背景同步、傳送前先 preview、接收端要按 Accept |
| GUI thread blocking | discovery / transfer 跑在 worker thread，用 queue 跟 GUI 溝通 |

## 10. Tech Stack

- **Python 3.10+**（使用 macOS framework Python 以取得 tkinter 支援）
- 標準函式庫：`socket`、`threading`、`queue`、`json`、`struct`、`tkinter`、`uuid`、`hashlib`、`pathlib`
- 第三方套件：
  - `cryptography` — X25519、HKDF、Fernet
  - `pyperclip` — 跨平台剪貼簿
- 跨平台：macOS / Linux / Windows

## 11. File Layout

```
Final_Project/
├── spec.md                 ← 本檔
├── README.md
├── requirements.txt
├── run.py                  ← 啟動腳本
└── safedrop/
    ├── __init__.py
    ├── __main__.py         ← python -m safedrop 入口
    ├── config.py
    ├── protocol.py         ← JSON 訊息 + framing
    ├── crypto.py           ← ECDH / Fernet / pairing code
    ├── discovery.py        ← UDP broadcast / listen
    ├── transfer.py         ← TCP server / client
    └── gui.py              ← tkinter UI
```

## 12. Demo Plan

兩台筆電連到同一 Wi-Fi：

1. 啟動 SafeDrop，互相自動出現在裝置清單上。
2. 傳送一份 PDF / 圖片，顯示傳輸進度與速度。
3. 傳送一段 URL / 程式碼片段，接收端按一鍵複製到剪貼簿。
4. 展示 Accept / Reject 流程。
5. 用 Wireshark 觀察 TCP 流量為加密內容（不可讀）。

## 13. Development Timeline

| 週 | 工作項目 |
| --- | --- |
| W1 | Discovery + 明文 TCP prototype |
| W2 | tkinter GUI + clipboard sharing |
| W3 | Pairing + ECDH + Fernet encryption |
| W4 | 整合測試 + final demo |

## 14. Expected Outcome

一套可運作的 LAN 共享系統，展示：

- UDP discovery
- TCP 可靠傳輸 + 進度
- 使用者確認流程
- 端對端加密的本地通訊

## 15. AI agent integration (MCP server + CLI)

SafeDrop 暴露兩條 programmatic 介面，讓 AI agent 或自動化腳本變成
LAN 上的一個 SafeDrop peer：

### 15.1 MCP server (`safedrop-mcp`)

Headless mode — 啟動時自行生一組 Identity / Discovery / TransferManager，
跟同機器上的 GUI 完全解耦，TCP 動態 port 避免衝突。
Stdio transport，可被 Claude Code / Claude Desktop / Cursor 等 MCP host 使用。

提供 4 個 tools：

| Tool | 行為 |
| --- | --- |
| `list_devices()` | 回傳目前 LAN 上看到的 peer JSON 陣列 |
| `send_file(device, path, timeout_seconds?)` | 發檔給某 peer，blocking 直到 done/rejected/failed |
| `send_text(device, content, content_type?, timeout_seconds?)` | 發 text/url/code |
| `wait_for_drop(timeout_seconds?)` | 阻塞直到別的裝置 push 進來，回傳該 drop 內容 |

對外的安全模型不變 —— **接收端裝置上的人類仍然必須按 Accept**。
MCP server 只是把 sender 那邊「按 Send」這一步換成「Claude 幫你呼叫」。

### 15.2 CLI (`safedrop`)

針對「只會 bash」的 agent / shell 自動化的 fallback，提供同樣 4 個動作：

```
safedrop ls
safedrop send-file <device> <path>
safedrop send-text <device> "<text>" [--type url|code]
cat snippet.py | safedrop send-text <device> --stdin --type code
safedrop wait
```

每次 invocation 啟一個 ephemeral peer，做事完關掉，加 `--json` 取得結構化輸出。

## 16. Cross-device tools (Phase 2, not yet implemented)

目前 MCP server 只暴露 *local* SafeDrop 功能。下個 milestone 把 **遠端裝置的能力**
動態 import 成 master agent 的 tools，每個 SafeDrop peer 可以 expose 自己的 tool registry
（手機相機、桌面 shell、Pi 上的 GPIO…）。

在現有 protocol 上加 4 條訊息（一樣走加密 TCP frame）：

```
{ "type": "LIST_TOOLS", "request_id": "..." }

{ "type": "TOOLS_LIST",  "request_id": "...",
  "tools": [
    {"name": "take_photo", "description": "...", "inputSchema": {...}},
    ...
  ]
}

{ "type": "CALL_TOOL",   "request_id": "...",
  "name": "take_photo", "arguments": {...} }

{ "type": "CALL_TOOL_RESULT", "request_id": "...",
  "result": {...} | "error": "..." }
```

HELLO 加 `"capabilities": ["safedrop.transfer", "agent.tools"]`，舊版 peer 沒這欄
就視為純傳檔 peer，向後相容。

Trust model：

- 未配對 peer 的 `LIST_TOOLS` 預設拒絕
- 配對後預設 per-call confirm（受邀裝置跳 dialog「X 想呼叫 Y，Allow / Deny」）
- 可設 per-tool always-allow / always-deny
- 全部 cross-device call 寫進本地 audit log，GUI 可瀏覽
