import Foundation

/// Per-(peerDeviceId, toolName) trust decisions persisted in UserDefaults.
/// "allow" / "deny" short-circuit the Allow/Deny dialog; "ask" (default
/// for missing entries) falls through to the live authorizer.
final class TrustStore {
    static let DECISION_ALLOW = "allow"
    static let DECISION_DENY = "deny"
    static let DECISION_ASK = "ask"

    private let key = "safedrop.trust.v1"
    private let queue = DispatchQueue(label: "safedrop.trust")
    private var cache: [String: [String: String]] = [:]

    init() { load() }

    private func load() {
        if let dict = UserDefaults.standard.dictionary(forKey: key) as? [String: [String: String]] {
            cache = dict
        }
    }
    private func save() {
        UserDefaults.standard.set(cache, forKey: key)
    }

    func check(peerDeviceId: String, toolName: String) -> String {
        queue.sync { cache[peerDeviceId]?[toolName] ?? Self.DECISION_ASK }
    }

    func set(peerDeviceId: String, toolName: String, decision: String) {
        queue.sync {
            if decision == Self.DECISION_ASK {
                cache[peerDeviceId]?.removeValue(forKey: toolName)
                if cache[peerDeviceId]?.isEmpty ?? false {
                    cache.removeValue(forKey: peerDeviceId)
                }
            } else {
                var sub = cache[peerDeviceId] ?? [:]
                sub[toolName] = decision
                cache[peerDeviceId] = sub
            }
            save()
        }
    }

    func clear(peerDeviceId: String, toolName: String? = nil) {
        queue.sync {
            if let t = toolName {
                cache[peerDeviceId]?.removeValue(forKey: t)
                if cache[peerDeviceId]?.isEmpty ?? false {
                    cache.removeValue(forKey: peerDeviceId)
                }
            } else {
                cache.removeValue(forKey: peerDeviceId)
            }
            save()
        }
    }

    func snapshot() -> [String: [String: String]] {
        queue.sync { cache.mapValues { $0 } }
    }
}
