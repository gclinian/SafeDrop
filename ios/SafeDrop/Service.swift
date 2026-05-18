import Combine
import Foundation
import UIKit

import SwiftUI

/// Holds the singleton state for the SafeDrop iOS app (identity,
/// discovery, transfer manager, tool registry, trust store). Mirrors
/// the Python `headless.py` + Android `SafeDropService.kt`.
@MainActor
final class SafeDropService: ObservableObject {
    let identity: Identity
    let deviceId: String
    let deviceName: String

    let toolRegistry: ToolRegistry
    let trustStore: TrustStore
    let transfer: TransferManager
    private(set) var discovery: Discovery?

    @Published var peers: [Peer] = []
    @Published var manualPeers: [Peer] = []
    private var transferSink: AnyCancellable?

    init() {
        self.identity = Identity()
        self.deviceId = UUID().uuidString
        let dev = UIDevice.current
        self.deviceName = "\(dev.name) (iOS \(dev.systemVersion))"
        self.toolRegistry = buildDefaultRegistry()
        self.trustStore = TrustStore()
        self.transfer = TransferManager(
            identity: identity,
            deviceId: deviceId,
            deviceName: deviceName,
            tcpPort: 0,    // dynamic port — coexists with any other SafeDrop on the same machine
            toolRegistry: toolRegistry,
            trustStore: trustStore
        )
    }

    func start() {
        transfer.start()
        // Forward transfer's @Published changes up so views observing
        // `service` re-render when e.g. `service.transfer.toolPrompts`
        // or `service.transfer.audit` change.
        transferSink = transfer.objectWillChange.sink { [weak self] _ in
            DispatchQueue.main.async { self?.objectWillChange.send() }
        }
        // Build discovery now that TransferManager has bound a real port.
        let disc = Discovery(
            deviceId: deviceId,
            deviceName: deviceName,
            platformName: "iOS",
            tcpPort: transfer.tcpPort,
            pubKey: identity.publicKeyBase64()
        )
        self.discovery = disc
        Task {
            await disc.setObserver { [weak self] peers in
                Task { @MainActor in self?.peers = peers }
            }
            await disc.start()
        }
    }

    func stop() {
        if let d = discovery { Task { await d.stop() } }
        transfer.stop()
    }

    func addManualPeer(name: String, ip: String, port: UInt16, pubKey: String) {
        let p = Peer(
            deviceId: "manual:\(ip):\(port)",
            name: name.isEmpty ? "\(ip):\(port)" : name,
            platform: "manual",
            ip: ip, tcpPort: port,
            pubKeyBase64: pubKey,
            capabilities: ["safedrop.transfer", "safedrop.tools"],
            lastSeen: Date.distantFuture
        )
        manualPeers.removeAll { $0.deviceId == p.deviceId }
        manualPeers.append(p)
    }

    func removeManualPeer(deviceId: String) {
        manualPeers.removeAll { $0.deviceId == deviceId }
    }

    var localIp: String { detectLocalIP() }
    var tcpPort: UInt16 { transfer.tcpPort }
}
