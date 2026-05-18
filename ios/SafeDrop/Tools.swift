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

func buildDefaultRegistry() -> ToolRegistry {
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
