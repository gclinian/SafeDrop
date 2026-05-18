import Combine
import Darwin
import Foundation

// MARK: - Inbound CALL_TOOL state

/// Surfaced to the UI when an inbound CALL_TOOL needs a user decision.
/// The Allow/Deny dialog collects from `TransferManager.toolPrompts` and
/// calls `respond(allow:persist:)` to resume the dispatcher thread.
final class ToolCallRequest: ObservableObject, Identifiable {
    let id = UUID()
    let requestId: String
    let peerName: String
    let peerIp: String
    let peerDeviceId: String
    let pairCode: String
    let toolName: String
    let arguments: [String: Any]

    private let sema = DispatchSemaphore(value: 0)
    private(set) var allow: Bool = false
    private(set) var persist: Bool = false

    init(requestId: String, peerName: String, peerIp: String, peerDeviceId: String,
         pairCode: String, toolName: String, arguments: [String: Any]) {
        self.requestId = requestId; self.peerName = peerName; self.peerIp = peerIp
        self.peerDeviceId = peerDeviceId; self.pairCode = pairCode
        self.toolName = toolName; self.arguments = arguments
    }

    func respond(allow: Bool, persist: Bool) {
        self.allow = allow
        self.persist = persist
        sema.signal()
    }

    /// Blocking wait — must be called off the main thread. Returns
    /// `(false, false)` on timeout.
    func waitSync(timeout: TimeInterval) -> (Bool, Bool) {
        if sema.wait(timeout: .now() + timeout) == .timedOut {
            return (false, false)
        }
        return (allow, persist)
    }
}

// MARK: - Audit entries

struct ToolCallAuditEntry: Identifiable {
    let id = UUID()
    let timestamp: Date
    let direction: String   // "inbound" | "outbound"
    let peerName: String
    let peerIp: String
    let toolName: String
    let arguments: String
    let decision: String    // "allowed" | "denied" | "error"
    let resultSummary: String?
    let error: String?
}

// MARK: - TransferManager

/// iOS port of `safedrop/transfer.py` + Android `TransferManager.kt`.
/// All blocking POSIX socket calls live on a dedicated concurrent GCD
/// queue (`ioQueue`) so they don't starve Swift's cooperative thread
/// pool. UI state (@Published) is mutated from the main thread.
final class TransferManager: ObservableObject {
    let identity: Identity
    let deviceId: String
    let deviceName: String
    var tcpPort: UInt16

    let toolRegistry: ToolRegistry
    let trustStore: TrustStore

    @Published var audit: [ToolCallAuditEntry] = []
    @Published var toolPrompts: [ToolCallRequest] = []
    @Published var lastReceivedClipboard: (peerName: String, contentType: String, content: String)? = nil

    private var serverFd: Int32 = -1
    private var stopRequested = false
    private let ioQueue = DispatchQueue(label: "safedrop.tcp",
                                        qos: .userInitiated,
                                        attributes: .concurrent)

    init(identity: Identity, deviceId: String, deviceName: String,
         tcpPort: UInt16 = kSafeDropDefaultTCPPort,
         toolRegistry: ToolRegistry, trustStore: TrustStore) {
        self.identity = identity
        self.deviceId = deviceId
        self.deviceName = deviceName
        self.tcpPort = tcpPort
        self.toolRegistry = toolRegistry
        self.trustStore = trustStore
    }

    // ---- lifecycle ----

    func start() {
        serverFd = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        var reuse: Int32 = 1
        setsockopt(serverFd, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout<Int32>.size))
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = tcpPort.bigEndian
        addr.sin_addr.s_addr = INADDR_ANY.bigEndian
        let addrSize = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(serverFd, $0, addrSize)
            }
        }
        if tcpPort == 0 {
            var bound = sockaddr_in()
            var boundLen = socklen_t(MemoryLayout<sockaddr_in>.size)
            withUnsafeMutablePointer(to: &bound) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    _ = getsockname(serverFd, $0, &boundLen)
                }
            }
            tcpPort = UInt16(bigEndian: bound.sin_port)
        }
        listen(serverFd, 8)
        ioQueue.async { [weak self] in self?.acceptLoopSync() }
    }

    func stop() {
        stopRequested = true
        if serverFd >= 0 { close(serverFd); serverFd = -1 }
    }

    // ---- inbound (synchronous, runs on ioQueue) ----

    private func acceptLoopSync() {
        while !stopRequested {
            var clientAddr = sockaddr_in()
            var clientLen = socklen_t(MemoryLayout<sockaddr_in>.size)
            let clientFd = withUnsafeMutablePointer(to: &clientAddr) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockPtr in
                    Darwin.accept(serverFd, sockPtr, &clientLen)
                }
            }
            if clientFd < 0 {
                if stopRequested { break }
                continue
            }
            let ip = String(cString: inet_ntoa(clientAddr.sin_addr))
            ioQueue.async { [weak self] in
                self?.handleInboundSync(clientFd: clientFd, peerIp: ip)
            }
        }
    }

    private func handleInboundSync(clientFd: Int32, peerIp: String) {
        defer { close(clientFd) }
        do {
            let hello = try FrameProtocol.recvJSON(clientFd)
            guard (hello["type"] as? String) == "HELLO" else {
                throw NSError(domain: "SafeDrop", code: 1,
                              userInfo: [NSLocalizedDescriptionKey: "expected HELLO"])
            }
            let peerName = (hello["name"] as? String) ?? "unknown"
            let peerDeviceId = (hello["device_id"] as? String) ?? ""
            let peerPub = (hello["pubkey"] as? String) ?? ""
            let session = try deriveSession(identity: identity, peerPublicKeyBase64: peerPub)

            try FrameProtocol.sendJSON(clientFd, [
                "type": "HELLO_ACK",
                "device_id": deviceId,
                "name": deviceName,
                "platform": "iOS",
                "pubkey": identity.publicKeyBase64(),
                "version": kSafeDropVersion,
                "pair_code": session.pairCode,
            ])

            let msg = try FrameProtocol.recvJSON(clientFd, decrypt: { try session.decrypt($0) })
            switch msg["type"] as? String {
            case "LIST_TOOLS":
                try handleListTools(clientFd: clientFd, session: session, msg: msg)
            case "CALL_TOOL":
                handleCallTool(clientFd: clientFd, session: session, msg: msg,
                               peerName: peerName, peerIp: peerIp, peerDeviceId: peerDeviceId)
            case "REQUEST":
                handleRequest(clientFd: clientFd, session: session, msg: msg, peerName: peerName)
            default:
                throw NSError(domain: "SafeDrop", code: 2,
                              userInfo: [NSLocalizedDescriptionKey: "unknown encrypted msg type"])
            }
        } catch {
            NSLog("[SafeDrop] inbound failed: %@", error.localizedDescription)
        }
    }

    private func handleListTools(clientFd: Int32, session: Session, msg: [String: Any]) throws {
        let requestId = (msg["request_id"] as? String) ?? ""
        let tools = toolRegistry.listManifests()
        try FrameProtocol.sendJSON(clientFd,
            ["type": "TOOLS_LIST", "request_id": requestId, "tools": tools],
            encrypt: { try session.encrypt($0) })
    }

    private func handleCallTool(clientFd: Int32, session: Session, msg: [String: Any],
                                peerName: String, peerIp: String, peerDeviceId: String) {
        let requestId = (msg["request_id"] as? String) ?? ""
        let name = (msg["name"] as? String) ?? ""
        let args = (msg["arguments"] as? [String: Any]) ?? [:]

        if !toolRegistry.has(name) {
            let err = "tool not available: '\(name)'"
            try? FrameProtocol.sendJSON(clientFd, [
                "type": "CALL_TOOL_RESULT", "request_id": requestId, "error": err,
            ], encrypt: { try session.encrypt($0) })
            recordAudit("inbound", peerName: peerName, peerIp: peerIp, toolName: name,
                        arguments: args, decision: "denied", resultSummary: nil, error: err)
            return
        }

        let decision = trustStore.check(peerDeviceId: peerDeviceId, toolName: name)
        var allowed = true
        if decision == TrustStore.DECISION_ALLOW {
            allowed = true
        } else if decision == TrustStore.DECISION_DENY {
            allowed = false
        } else {
            // Pop dialog; block this I/O thread until the UI responds.
            let req = ToolCallRequest(
                requestId: requestId, peerName: peerName, peerIp: peerIp,
                peerDeviceId: peerDeviceId, pairCode: session.pairCode,
                toolName: name, arguments: args)
            DispatchQueue.main.async { self.toolPrompts.append(req) }
            let (allow, persist) = req.waitSync(timeout: 120)
            DispatchQueue.main.async {
                self.toolPrompts.removeAll(where: { $0.id == req.id })
            }
            allowed = allow
            if persist {
                trustStore.set(peerDeviceId: peerDeviceId, toolName: name,
                               decision: allow ? TrustStore.DECISION_ALLOW : TrustStore.DECISION_DENY)
            }
        }

        if !allowed {
            try? FrameProtocol.sendJSON(clientFd, [
                "type": "CALL_TOOL_RESULT", "request_id": requestId,
                "error": "denied by authorizer",
            ], encrypt: { try session.encrypt($0) })
            recordAudit("inbound", peerName: peerName, peerIp: peerIp, toolName: name,
                        arguments: args, decision: "denied", resultSummary: nil, error: nil)
            return
        }

        do {
            let result = try toolRegistry.call(name, arguments: args)
            try FrameProtocol.sendJSON(clientFd, [
                "type": "CALL_TOOL_RESULT", "request_id": requestId, "result": result,
            ], encrypt: { try session.encrypt($0) })
            let summary = String(describing: result).prefix(120)
            recordAudit("inbound", peerName: peerName, peerIp: peerIp, toolName: name,
                        arguments: args, decision: "allowed",
                        resultSummary: String(summary), error: nil)
        } catch {
            let err = "\(type(of: error)): \(error.localizedDescription)"
            try? FrameProtocol.sendJSON(clientFd, [
                "type": "CALL_TOOL_RESULT", "request_id": requestId, "error": err,
            ], encrypt: { try session.encrypt($0) })
            recordAudit("inbound", peerName: peerName, peerIp: peerIp, toolName: name,
                        arguments: args, decision: "error", resultSummary: nil, error: err)
        }
    }

    private func handleRequest(clientFd: Int32, session: Session, msg: [String: Any],
                               peerName: String) {
        let transferId = (msg["transfer_id"] as? String) ?? UUID().uuidString
        let kind = (msg["kind"] as? String) ?? ""
        if kind == "clipboard" {
            try? FrameProtocol.sendJSON(clientFd, [
                "type": "ACCEPT", "transfer_id": transferId,
            ], encrypt: { try session.encrypt($0) })
            if let payload = try? FrameProtocol.recvJSON(clientFd, decrypt: { try session.decrypt($0) }),
               (payload["type"] as? String) == "CLIPBOARD" {
                let content = (payload["content"] as? String) ?? ""
                let ctype = (payload["content_type"] as? String) ?? "text"
                DispatchQueue.main.async {
                    self.lastReceivedClipboard = (peerName, ctype, content)
                }
            }
        } else {
            try? FrameProtocol.sendJSON(clientFd, [
                "type": "REJECT", "transfer_id": transferId,
                "reason": "iOS Phase 1 doesn't accept files yet",
            ], encrypt: { try session.encrypt($0) })
        }
    }

    // ---- outbound (sync, runs on caller's queue) ----

    private func openSession(peer: Peer) throws -> (Int32, Session) {
        let fd = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = peer.tcpPort.bigEndian
        addr.sin_addr.s_addr = inet_addr(peer.ip)
        let addrSize = socklen_t(MemoryLayout<sockaddr_in>.size)
        let connectStatus = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.connect(fd, $0, addrSize)
            }
        }
        if connectStatus < 0 {
            close(fd)
            throw NSError(domain: "SafeDrop", code: 3,
                          userInfo: [NSLocalizedDescriptionKey: "connect failed"])
        }
        try FrameProtocol.sendJSON(fd, [
            "type": "HELLO",
            "device_id": deviceId,
            "name": deviceName,
            "platform": "iOS",
            "pubkey": identity.publicKeyBase64(),
            "version": kSafeDropVersion,
        ])
        let ack = try FrameProtocol.recvJSON(fd)
        guard (ack["type"] as? String) == "HELLO_ACK",
              let pub = ack["pubkey"] as? String else {
            close(fd)
            throw NSError(domain: "SafeDrop", code: 4,
                          userInfo: [NSLocalizedDescriptionKey: "bad HELLO_ACK"])
        }
        let session = try deriveSession(identity: identity, peerPublicKeyBase64: pub)
        return (fd, session)
    }

    func sendText(peer: Peer, content: String, contentType: String = "text") async throws -> [String: Any] {
        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<[String: Any], Error>) in
            ioQueue.async { [weak self] in
                guard let self else { cont.resume(throwing: NSError(domain: "SafeDrop", code: 99)); return }
                do {
                    let (fd, session) = try self.openSession(peer: peer)
                    defer { close(fd) }
                    let transferId = UUID().uuidString
                    let preview = String(content.prefix(200))
                    try FrameProtocol.sendJSON(fd, [
                        "type": "REQUEST", "transfer_id": transferId,
                        "kind": "clipboard", "content_type": contentType,
                        "preview": preview, "length": content.utf8.count,
                    ], encrypt: { try session.encrypt($0) })
                    let decision = try FrameProtocol.recvJSON(fd, decrypt: { try session.decrypt($0) })
                    guard (decision["type"] as? String) == "ACCEPT" else {
                        cont.resume(returning: ["status": "rejected", "pair_code": session.pairCode]); return
                    }
                    try FrameProtocol.sendJSON(fd, [
                        "type": "CLIPBOARD", "transfer_id": transferId,
                        "content_type": contentType, "content": content,
                    ], encrypt: { try session.encrypt($0) })
                    cont.resume(returning: ["status": "done", "pair_code": session.pairCode])
                } catch { cont.resume(throwing: error) }
            }
        }
    }

    func listRemoteTools(peer: Peer) async throws -> [[String: Any]] {
        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<[[String: Any]], Error>) in
            ioQueue.async { [weak self] in
                guard let self else { cont.resume(throwing: NSError(domain: "SafeDrop", code: 99)); return }
                do {
                    let (fd, session) = try self.openSession(peer: peer)
                    defer { close(fd) }
                    let requestId = UUID().uuidString
                    try FrameProtocol.sendJSON(fd, [
                        "type": "LIST_TOOLS", "request_id": requestId,
                    ], encrypt: { try session.encrypt($0) })
                    let resp = try FrameProtocol.recvJSON(fd, decrypt: { try session.decrypt($0) })
                    cont.resume(returning: (resp["tools"] as? [[String: Any]]) ?? [])
                } catch { cont.resume(throwing: error) }
            }
        }
    }

    func callRemoteTool(peer: Peer, name: String, arguments: [String: Any] = [:]) async throws -> [String: Any] {
        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<[String: Any], Error>) in
            ioQueue.async { [weak self] in
                guard let self else { cont.resume(throwing: NSError(domain: "SafeDrop", code: 99)); return }
                do {
                    let (fd, session) = try self.openSession(peer: peer)
                    defer { close(fd) }
                    let requestId = UUID().uuidString
                    try FrameProtocol.sendJSON(fd, [
                        "type": "CALL_TOOL", "request_id": requestId,
                        "name": name, "arguments": arguments,
                    ], encrypt: { try session.encrypt($0) })
                    let resp = try FrameProtocol.recvJSON(fd, decrypt: { try session.decrypt($0) })
                    var out: [String: Any] = [:]
                    if let r = resp["result"] { out["result"] = r }
                    if let e = resp["error"] { out["error"] = e }
                    let decisionTxt = out["error"] != nil ? "error" : "allowed"
                    let summary: String? = {
                        if let r = out["result"] { return String(describing: r).prefix(120).description }
                        return nil
                    }()
                    self.recordAudit("outbound", peerName: peer.name, peerIp: peer.ip,
                                     toolName: name, arguments: arguments,
                                     decision: decisionTxt, resultSummary: summary,
                                     error: out["error"] as? String)
                    cont.resume(returning: out)
                } catch { cont.resume(throwing: error) }
            }
        }
    }

    // ---- audit ----

    private func recordAudit(_ direction: String, peerName: String, peerIp: String,
                             toolName: String, arguments: [String: Any],
                             decision: String, resultSummary: String?, error: String?) {
        let argsJson = (try? JSONSerialization.data(withJSONObject: arguments))
            .flatMap { String(data: $0, encoding: .utf8) } ?? "{}"
        let entry = ToolCallAuditEntry(
            timestamp: Date(), direction: direction,
            peerName: peerName, peerIp: peerIp,
            toolName: toolName, arguments: argsJson,
            decision: decision, resultSummary: resultSummary, error: error
        )
        DispatchQueue.main.async {
            self.audit.append(entry)
            if self.audit.count > 200 {
                self.audit.removeFirst(self.audit.count - 200)
            }
        }
    }
}
