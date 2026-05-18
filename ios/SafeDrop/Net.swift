import Darwin
import Foundation

// MARK: - Constants

let kSafeDropVersion = "1.0"
let kSafeDropDiscoveryPort: UInt16 = 47890
let kSafeDropDefaultTCPPort: UInt16 = 47891
let kSafeDropBroadcastIntervalSec: TimeInterval = 3.0
let kSafeDropPeerTTLSec: TimeInterval = 10.0
let kSafeDropChunkSize = 64 * 1024
let kSafeDropMaxFrame = 64 * 1024 * 1024

// MARK: - Frame protocol

/// Length-prefixed JSON frames matching Python's `safedrop/protocol.py`:
///   [4-byte big-endian length N][N bytes payload]
/// Payload is plaintext JSON during handshake, Fernet-encrypted JSON after.
enum FrameProtocol {
    enum FrameError: Error {
        case eof, oversize(Int), badJson(String)
    }

    static func sendFrame(_ fd: Int32, _ payload: Data) throws {
        precondition(payload.count <= kSafeDropMaxFrame)
        var header = Data(count: 4)
        let n = UInt32(payload.count)
        header[0] = UInt8((n >> 24) & 0xff)
        header[1] = UInt8((n >> 16) & 0xff)
        header[2] = UInt8((n >> 8) & 0xff)
        header[3] = UInt8(n & 0xff)
        try writeAll(fd, header)
        try writeAll(fd, payload)
    }

    static func recvFrame(_ fd: Int32) throws -> Data {
        let header = try readExact(fd, 4)
        let n = (UInt32(header[0]) << 24) | (UInt32(header[1]) << 16)
              | (UInt32(header[2]) << 8) | UInt32(header[3])
        guard n <= UInt32(kSafeDropMaxFrame) else { throw FrameError.oversize(Int(n)) }
        return try readExact(fd, Int(n))
    }

    static func sendJSON(_ fd: Int32, _ obj: [String: Any], encrypt: ((Data) throws -> Data)? = nil) throws {
        let raw = try JSONSerialization.data(withJSONObject: obj, options: [])
        let payload = try (encrypt?(raw)) ?? raw
        try sendFrame(fd, payload)
    }

    static func recvJSON(_ fd: Int32, decrypt: ((Data) throws -> Data)? = nil) throws -> [String: Any] {
        let raw = try recvFrame(fd)
        let decoded = try (decrypt?(raw)) ?? raw
        do {
            let obj = try JSONSerialization.jsonObject(with: decoded)
            guard let dict = obj as? [String: Any] else {
                let preview = String(data: decoded.prefix(200), encoding: .utf8) ?? "\(decoded.prefix(60).map { String(format: "%02x", $0) }.joined())"
                NSLog("[SafeDrop] decrypted but not a dict (%lu bytes): %@", decoded.count, preview)
                throw FrameError.badJson("not an object: \(type(of: obj))")
            }
            return dict
        } catch let e {
            let preview = String(data: decoded.prefix(200), encoding: .utf8) ?? "\(decoded.prefix(60).map { String(format: "%02x", $0) }.joined())"
            NSLog("[SafeDrop] JSON parse failed (%lu bytes): %@  err=%@", decoded.count, preview, "\(e)")
            throw FrameError.badJson("\(e)")
        }
    }

    private static func writeAll(_ fd: Int32, _ data: Data) throws {
        var remaining = data
        while !remaining.isEmpty {
            let n = remaining.withUnsafeBytes { buf in
                Darwin.send(fd, buf.baseAddress, remaining.count, 0)
            }
            if n < 0 { throw FrameError.eof }
            remaining.removeFirst(n)
        }
    }

    private static func readExact(_ fd: Int32, _ count: Int) throws -> Data {
        var buf = Data(count: count)
        var read = 0
        while read < count {
            let n = buf.withUnsafeMutableBytes { ptr -> Int in
                let base = ptr.baseAddress!.advanced(by: read)
                return Darwin.recv(fd, base, count - read, 0)
            }
            if n <= 0 { throw FrameError.eof }
            read += n
        }
        return buf
    }
}

// MARK: - Peer model

struct Peer: Equatable, Identifiable {
    var id: String { deviceId }
    let deviceId: String
    let name: String
    let platform: String
    let ip: String
    let tcpPort: UInt16
    let pubKeyBase64: String
    let capabilities: [String]
    var lastSeen: Date

    func hasCapability(_ cap: String) -> Bool { capabilities.contains(cap) }
}

// MARK: - Discovery (UDP broadcast)

actor Discovery {
    let deviceId: String
    let deviceName: String
    let platformName: String
    let tcpPort: UInt16
    let pubKey: String
    let capabilities: [String]
    let version: String

    private var sendFd: Int32 = -1
    private var recvFd: Int32 = -1
    private var stopRequested = false
    private(set) var peers: [String: Peer] = [:]
    var peersChanged: (([Peer]) -> Void)?

    init(deviceId: String, deviceName: String, platformName: String,
         tcpPort: UInt16, pubKey: String,
         capabilities: [String] = ["safedrop.transfer", "safedrop.tools"],
         version: String = kSafeDropVersion) {
        self.deviceId = deviceId; self.deviceName = deviceName
        self.platformName = platformName; self.tcpPort = tcpPort
        self.pubKey = pubKey; self.capabilities = capabilities; self.version = version
    }

    func setObserver(_ cb: @escaping ([Peer]) -> Void) { self.peersChanged = cb }

    func start() {
        sendFd = socket(AF_INET, SOCK_DGRAM, 0)
        var bcast: Int32 = 1
        setsockopt(sendFd, SOL_SOCKET, SO_BROADCAST, &bcast, socklen_t(MemoryLayout<Int32>.size))

        recvFd = socket(AF_INET, SOCK_DGRAM, 0)
        var reuse: Int32 = 1
        setsockopt(recvFd, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout<Int32>.size))
        setsockopt(recvFd, SOL_SOCKET, SO_REUSEPORT, &reuse, socklen_t(MemoryLayout<Int32>.size))
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = kSafeDropDiscoveryPort.bigEndian
        addr.sin_addr.s_addr = INADDR_ANY.bigEndian
        let addrSize = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(recvFd, $0, addrSize)
            }
        }
        var tv = timeval(tv_sec: 1, tv_usec: 0)
        setsockopt(recvFd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))

        Task.detached { [weak self] in await self?.broadcastLoop() }
        Task.detached { [weak self] in await self?.listenLoop() }
        Task.detached { [weak self] in await self?.reaperLoop() }
    }

    func stop() {
        stopRequested = true
        try? sendBye()
        if sendFd >= 0 { close(sendFd); sendFd = -1 }
        if recvFd >= 0 { close(recvFd); recvFd = -1 }
    }

    private func helloPayload() -> Data {
        let dict: [String: Any] = [
            "type": "HELLO",
            "device_id": deviceId,
            "name": deviceName,
            "platform": platformName,
            "tcp_port": tcpPort,
            "pubkey": pubKey,
            "capabilities": capabilities,
            "version": version,
        ]
        return (try? JSONSerialization.data(withJSONObject: dict)) ?? Data()
    }

    private func byePayload() -> Data {
        let dict: [String: Any] = ["type": "BYE", "device_id": deviceId]
        return (try? JSONSerialization.data(withJSONObject: dict)) ?? Data()
    }

    private func broadcast(_ payload: Data) {
        guard sendFd >= 0 else { return }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = kSafeDropDiscoveryPort.bigEndian
        addr.sin_addr.s_addr = inet_addr("255.255.255.255")
        let addrSize = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = payload.withUnsafeBytes { buf in
            withUnsafePointer(to: &addr) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    Darwin.sendto(sendFd, buf.baseAddress, payload.count, 0, $0, addrSize)
                }
            }
        }
    }

    private func sendBye() throws { broadcast(byePayload()) }

    private func broadcastLoop() async {
        let payload = helloPayload()
        while !stopRequested {
            broadcast(payload)
            try? await Task.sleep(nanoseconds: UInt64(kSafeDropBroadcastIntervalSec * 1_000_000_000))
        }
    }

    private func listenLoop() async {
        var buf = [UInt8](repeating: 0, count: 8192)
        var addr = sockaddr_in()
        var addrLen = socklen_t(MemoryLayout<sockaddr_in>.size)
        while !stopRequested {
            let received = buf.withUnsafeMutableBufferPointer { (ptr: inout UnsafeMutableBufferPointer<UInt8>) -> Int in
                withUnsafeMutablePointer(to: &addr) { addrPtr in
                    addrPtr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockPtr in
                        Darwin.recvfrom(recvFd, ptr.baseAddress, ptr.count, 0, sockPtr, &addrLen)
                    }
                }
            }
            if received <= 0 {
                try? await Task.sleep(nanoseconds: 100_000_000)
                continue
            }
            let data = Data(buf.prefix(received))
            let ipStr = String(cString: inet_ntoa(addr.sin_addr))
            await handleDatagram(data, sender: ipStr)
        }
    }

    private func handleDatagram(_ data: Data, sender: String) async {
        guard let msg = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let kind = msg["type"] as? String,
              let id = msg["device_id"] as? String,
              id != deviceId else { return }
        if kind == "HELLO" {
            guard let port = msg["tcp_port"] as? Int,
                  let pub = msg["pubkey"] as? String,
                  port > 0, !pub.isEmpty else { return }
            let caps = (msg["capabilities"] as? [String]) ?? []
            let peer = Peer(
                deviceId: id,
                name: (msg["name"] as? String) ?? "unknown",
                platform: (msg["platform"] as? String) ?? "?",
                ip: sender,
                tcpPort: UInt16(port),
                pubKeyBase64: pub,
                capabilities: caps,
                lastSeen: Date()
            )
            peers[id] = peer
            peersChanged?(Array(peers.values))
        } else if kind == "BYE" {
            if peers.removeValue(forKey: id) != nil {
                peersChanged?(Array(peers.values))
            }
        }
    }

    private func reaperLoop() async {
        while !stopRequested {
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            let cutoff = Date().addingTimeInterval(-kSafeDropPeerTTLSec)
            let before = peers.count
            peers = peers.filter { $0.value.lastSeen >= cutoff }
            if peers.count != before {
                peersChanged?(Array(peers.values))
            }
        }
    }

    func snapshot() -> [Peer] { Array(peers.values) }
}

// MARK: - Local IP detection

func detectLocalIP() -> String {
    var address = "0.0.0.0"
    var ifaddr: UnsafeMutablePointer<ifaddrs>?
    guard getifaddrs(&ifaddr) == 0, let first = ifaddr else { return address }
    var cur: UnsafeMutablePointer<ifaddrs>? = first
    while let ptr = cur {
        defer { cur = ptr.pointee.ifa_next }
        let flags = Int32(ptr.pointee.ifa_flags)
        guard (flags & (IFF_UP | IFF_RUNNING)) == (IFF_UP | IFF_RUNNING),
              (flags & IFF_LOOPBACK) == 0,
              let addr = ptr.pointee.ifa_addr,
              addr.pointee.sa_family == sa_family_t(AF_INET) else { continue }
        let nameStr = String(cString: ptr.pointee.ifa_name)
        // en0 is Wi-Fi on iOS; pdp_ip0 is cellular (avoid).
        guard nameStr.hasPrefix("en") || nameStr.hasPrefix("br") else { continue }
        var hostBuf = [CChar](repeating: 0, count: Int(NI_MAXHOST))
        getnameinfo(addr, socklen_t(ptr.pointee.ifa_addr.pointee.sa_len),
                    &hostBuf, socklen_t(hostBuf.count),
                    nil, 0, NI_NUMERICHOST)
        address = String(cString: hostBuf)
        break
    }
    freeifaddrs(ifaddr)
    return address
}
