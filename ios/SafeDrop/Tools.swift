import Foundation
import UIKit

/// Synchronous handler signature. Tools that need MainActor (e.g.
/// UIPasteboard) bounce through DispatchQueue.main.sync internally.
typealias ToolHandler = (_ arguments: [String: Any]) throws -> Any

struct ToolSpec {
    let name: String
    let description: String
    let inputSchema: [String: Any]
    let handler: ToolHandler

    func manifest() -> [String: Any] {
        ["name": name, "description": description, "inputSchema": inputSchema]
    }
}

final class ToolRegistry {
    private var tools: [String: ToolSpec] = [:]
    private let queue = DispatchQueue(label: "safedrop.tools")

    func register(_ spec: ToolSpec) { queue.sync { tools[spec.name] = spec } }
    func has(_ name: String) -> Bool { queue.sync { tools[name] != nil } }
    func listManifests() -> [[String: Any]] { queue.sync { tools.values.map { $0.manifest() } } }
    func call(_ name: String, arguments: [String: Any]) throws -> Any {
        let spec = queue.sync { tools[name] }
        guard let s = spec else {
            throw NSError(domain: "SafeDrop", code: 404,
                          userInfo: [NSLocalizedDescriptionKey: "unknown tool: \(name)"])
        }
        return try s.handler(arguments)
    }
}

// MARK: - Default tools

func buildDefaultRegistry(photoBroker: PhotoBroker? = nil) -> ToolRegistry {
    let reg = ToolRegistry()

    reg.register(ToolSpec(
        name: "system_info",
        description: "Return basic info about this device: hostname, OS, model.",
        inputSchema: ["type": "object", "properties": [:]],
        handler: { _ in
            var out: [String: Any] = [:]
            DispatchQueue.main.sync {
                let dev = UIDevice.current
                out = [
                    "hostname": dev.name,
                    "platform": "iOS",
                    "release": dev.systemVersion,
                    "machine": modelIdentifier(),
                    "model": dev.model,
                    "manufacturer": "Apple",
                ]
            }
            return out
        }
    ))

    reg.register(ToolSpec(
        name: "read_clipboard",
        description: "Read the current clipboard contents on this device. iOS shows a banner " +
                     "every time clipboard is read by a non-foreground app — keep SafeDrop foreground.",
        inputSchema: ["type": "object", "properties": [:]],
        handler: { _ in
            var text = ""
            DispatchQueue.main.sync { text = UIPasteboard.general.string ?? "" }
            return ["content": text, "content_type": "text"]
        }
    ))

    reg.register(ToolSpec(
        name: "write_clipboard",
        description: "Set this device's clipboard to the given text.",
        inputSchema: [
            "type": "object",
            "properties": ["content": ["type": "string"]],
            "required": ["content"],
        ],
        handler: { args in
            let content = (args["content"] as? String) ?? ""
            DispatchQueue.main.sync { UIPasteboard.general.string = content }
            return ["status": "ok", "wrote_chars": content.count]
        }
    ))

    // ---- take_photo (v1.5 phase 2) ----
    if let broker = photoBroker {
        reg.register(ToolSpec(
            name: "take_photo",
            description:
                "Open the iOS camera so the user can take a photo to send back. " +
                "Blocks until the user shutters (returns the JPEG) or cancels. " +
                "Returns {mime_type, size_bytes, data_b64}.",
            inputSchema: [
                "type": "object",
                "properties": [
                    "timeout_seconds": ["type": "integer", "default": 120],
                ],
            ],
            handler: { args in
                let timeout = TimeInterval((args["timeout_seconds"] as? Int) ?? 120)
                // The trust dialog has already gated this call; we know
                // the user opted in. Now wait for them to actually shoot.
                let (data, err) = broker.capture(
                    peerName: (args["__peer_name"] as? String) ?? "remote",
                    pairCode: (args["__pair_code"] as? String) ?? "",
                    timeout: timeout
                )
                if let d = data {
                    return [
                        "mime_type": "image/jpeg",
                        "size_bytes": d.count,
                        "data_b64": d.base64EncodedString(),
                    ]
                }
                let msg = err ?? "no image"
                throw NSError(domain: "SafeDrop", code: 20,
                              userInfo: [NSLocalizedDescriptionKey: msg])
            }
        ))
    }

    return reg
}

private func modelIdentifier() -> String {
    var systemInfo = utsname()
    uname(&systemInfo)
    return withUnsafePointer(to: &systemInfo.machine) {
        $0.withMemoryRebound(to: CChar.self, capacity: Int(_SYS_NAMELEN)) {
            String(cString: $0)
        }
    }
}
