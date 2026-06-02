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

/// UDP-broadcast peer discovery.
///
/// **Why this is a `final class` on a dedicated `DispatchQueue`, not an
/// `actor`:** the listen loop calls a *blocking* `recvfrom` (1 s
/// `SO_RCVTIMEO`). Running blocking POSIX syscalls on Swift's cooperative
/// thread pool — which is what `actor` methods and `Task.detached` use —
/// starves the pool and the loops stall on real hardware. This is the
/// same lesson `TransferManager` already encodes (see CLAUDE.md). All
/// blocking socket work happens on `ioQueue`; peer state is guarded by a
/// plain lock.
final class Discovery {
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

    private let lock = NSLock()
    private var peers: [String: Peer] = [:]
    private var peersChanged: (([Peer]) -> Void)?

    // Dedicated queue for blocking socket I/O — never the cooperative pool.
    private let ioQueue = DispatchQueue(label: "safedrop.discovery",
                                        qos: .utility,
                                        attributes: .concurrent)

    init(deviceId: String, deviceName: String, platformName: String,
         tcpPort: UInt16, pubKey: String,
         capabilities: [String] = ["safedrop.transfer", "safedrop.tools"],
         version: String = kSafeDropVersion) {
        self.deviceId = deviceId; self.deviceName = deviceName
        self.platformName = platformName; self.tcpPort = tcpPort
        self.pubKey = pubKey; self.capabilities = capabilities; self.version = version
    }

    func setObserver(_ cb: @escaping ([Peer]) -> Void) {
        lock.lock(); peersChanged = cb; lock.unlock()
    }

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
        addr.sin_addr.s_addr = in_addr_t(0)   // INADDR_ANY
        let addrSize = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(recvFd, $0, addrSize)
            }
        }
        var tv = timeval(tv_sec: 1, tv_usec: 0)
        setsockopt(recvFd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))

        ioQueue.async { [weak self] in self?.broadcastLoopSync() }
        ioQueue.async { [weak self] in self?.listenLoopSync() }
        ioQueue.async { [weak self] in self?.reaperLoopSync() }
    }

    func stop() {
        stopRequested = true
        broadcast(byePayload())
        if sendFd >= 0 { close(sendFd); sendFd = -1 }
        if recvFd >= 0 { close(recvFd); recvFd = -1 }
    }

    func snapshot() -> [Peer] {
        lock.lock(); defer { lock.unlock() }
        return Array(peers.values)
    }

    // ---- payloads ----

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

    /// IPv4 destinations to fan each HELLO out to. Mirrors the Python
    /// fix: loopback (same-machine / simulator), every active interface's
    /// subnet-directed broadcast (what actually traverses real Wi-Fi),
    /// and the global broadcast as a fallback. Sending to `255.255.255.255`
    /// alone does NOT reach peers on most real networks.
    private func broadcastTargets() -> [in_addr_t] {
        var targets: [in_addr_t] = [inet_addr("127.0.0.1")]
        var ifaddr: UnsafeMutablePointer<ifaddrs>?
        if getifaddrs(&ifaddr) == 0, let first = ifaddr {
            var cur: UnsafeMutablePointer<ifaddrs>? = first
            while let ptr = cur {
                defer { cur = ptr.pointee.ifa_next }
                let flags = Int32(ptr.pointee.ifa_flags)
                guard (flags & (IFF_UP | IFF_RUNNING)) == (IFF_UP | IFF_RUNNING),
                      (flags & IFF_LOOPBACK) == 0,
                      (flags & IFF_BROADCAST) != 0,
                      let sa = ptr.pointee.ifa_addr,
                      sa.pointee.sa_family == sa_family_t(AF_INET) else { continue }
                let name = String(cString: ptr.pointee.ifa_name)
                guard name.hasPrefix("en") || name.hasPrefix("br") else { continue }
                // For IFF_BROADCAST links, ifa_dstaddr holds the broadcast
                // address (computed by the OS from addr + netmask).
                if let bcastSa = ptr.pointee.ifa_dstaddr {
                    let b = bcastSa.withMemoryRebound(to: sockaddr_in.self, capacity: 1) {
                        $0.pointee.sin_addr.s_addr
                    }
                    if b != 0 { targets.append(b) }
                }
            }
            freeifaddrs(ifaddr)
        }
        targets.append(inet_addr("255.255.255.255"))
        var seen = Set<in_addr_t>()
        return targets.filter { seen.insert($0).inserted }
    }

    private func broadcast(_ payload: Data) {
        guard sendFd >= 0 else { return }
        let addrSize = socklen_t(MemoryLayout<sockaddr_in>.size)
        for target in broadcastTargets() {
            var addr = sockaddr_in()
            addr.sin_family = sa_family_t(AF_INET)
            addr.sin_port = kSafeDropDiscoveryPort.bigEndian
            addr.sin_addr.s_addr = target
            _ = payload.withUnsafeBytes { buf in
                withUnsafePointer(to: &addr) {
                    $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                        Darwin.sendto(sendFd, buf.baseAddress, payload.count, 0, $0, addrSize)
                    }
                }
            }
        }
    }

    // ---- loops (run on ioQueue, blocking) ----

    private func broadcastLoopSync() {
        let payload = helloPayload()
        while !stopRequested {
            broadcast(payload)
            Thread.sleep(forTimeInterval: kSafeDropBroadcastIntervalSec)
        }
    }

    private func listenLoopSync() {
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
            // recvfrom returns <= 0 on the 1 s timeout (EAGAIN) — just loop
            // and re-check stopRequested. No Task.sleep (we're not on the
            // cooperative pool here).
            if received <= 0 { continue }
            let data = Data(buf.prefix(received))
            let ipStr = String(cString: inet_ntoa(addr.sin_addr))
            handleDatagram(data, sender: ipStr)
        }
    }

    private func handleDatagram(_ data: Data, sender: String) {
        guard let msg = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let kind = msg["type"] as? String,
              let id = msg["device_id"] as? String,
              id != deviceId else { return }

        var snapshot: [Peer]? = nil
        var observer: (([Peer]) -> Void)? = nil

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
            lock.lock()
            peers[id] = peer
            snapshot = Array(peers.values)
            observer = peersChanged
            lock.unlock()
        } else if kind == "BYE" {
            lock.lock()
            if peers.removeValue(forKey: id) != nil {
                snapshot = Array(peers.values)
                observer = peersChanged
            }
            lock.unlock()
        }

        if let snap = snapshot { observer?(snap) }
    }

    private func reaperLoopSync() {
        while !stopRequested {
            Thread.sleep(forTimeInterval: 1.0)
            let cutoff = Date().addingTimeInterval(-kSafeDropPeerTTLSec)
            var snapshot: [Peer]? = nil
            var observer: (([Peer]) -> Void)? = nil
            lock.lock()
            let before = peers.count
            peers = peers.filter { $0.value.lastSeen >= cutoff }
            if peers.count != before {
                snapshot = Array(peers.values)
                observer = peersChanged
            }
            lock.unlock()
            if let snap = snapshot { observer?(snap) }
        }
    }
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
