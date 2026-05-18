import SwiftUI
import UIKit

@main
struct SafeDropApp: App {
    @StateObject private var service = SafeDropService()

    var body: some Scene {
        WindowGroup {
            HomeView()
                .environmentObject(service)
                .onAppear { service.start() }
        }
    }
}

// MARK: - HomeView

struct HomeView: View {
    @EnvironmentObject var service: SafeDropService
    @State private var selectedPeer: Peer?
    @State private var text: String = ""
    @State private var contentType: String = "text"
    @State private var showAddPeer = false
    @State private var statusMessage: String?

    var allPeers: [Peer] {
        (service.manualPeers + service.peers).sorted { $0.name < $1.name }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    headerSection
                    peersSection
                    sendSection
                    auditSection
                    Spacer(minLength: 24)
                }
                .padding()
            }
            .navigationTitle("SafeDrop")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    NavigationLink(destination: TrustView()) { Image(systemName: "lock.shield") }
                }
            }
            .sheet(isPresented: $showAddPeer) {
                AddManualPeerSheet()
            }
            .overlay { receivedClipboardBanner }
            .overlay { toolPromptOverlay }
        }
    }

    private var headerSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(service.deviceName).font(.headline)
            Text("\(service.localIp):\(service.tcpPort)")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private var peersSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Nearby devices").font(.subheadline.weight(.semibold))
                Spacer()
                Button("+ Add manually") { showAddPeer = true }
                    .font(.caption)
            }
            if allPeers.isEmpty {
                Text("No devices yet. Other SafeDrop peers on the same Wi-Fi will appear automatically.")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                ForEach(allPeers) { peer in
                    PeerRow(peer: peer, selected: selectedPeer?.id == peer.id) {
                        selectedPeer = peer
                    }
                }
            }
        }
        .padding()
        .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 10))
    }

    private var sendSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(selectedPeer.map { "Send to \($0.name)" } ?? "Send (select a device first)")
                .font(.subheadline.weight(.semibold))
            Picker("Content type", selection: $contentType) {
                Text("text").tag("text")
                Text("url").tag("url")
                Text("code").tag("code")
            }.pickerStyle(.segmented)
            TextEditor(text: $text)
                .frame(minHeight: 100)
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(.secondary.opacity(0.3)))
            HStack {
                Button("Paste") {
                    if let s = UIPasteboard.general.string {
                        text = s
                    }
                }
                Button("Clear") { text = "" }
                Spacer()
                Button("Send clipboard") { sendText() }
                    .buttonStyle(.borderedProminent)
                    .disabled(selectedPeer == nil || text.isEmpty)
            }
            if let m = statusMessage {
                Text(m).font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding()
        .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 10))
    }

    private var auditSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Cross-device tool audit").font(.subheadline.weight(.semibold))
            if service.transfer.audit.isEmpty {
                Text("No cross-device tool calls yet.")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                ForEach(service.transfer.audit.reversed().prefix(15)) { entry in
                    AuditRow(entry: entry)
                }
            }
        }
        .padding()
        .background(Color(uiColor: .secondarySystemBackground), in: RoundedRectangle(cornerRadius: 10))
    }

    @ViewBuilder
    private var receivedClipboardBanner: some View {
        if let (peerName, ctype, content) = service.transfer.lastReceivedClipboard {
            VStack {
                Spacer()
                ClipboardReceivedBanner(peerName: peerName, contentType: ctype, content: content) {
                    service.transfer.lastReceivedClipboard = nil
                }
                .padding()
            }
        }
    }

    @ViewBuilder
    private var toolPromptOverlay: some View {
        if let req = service.transfer.toolPrompts.first {
            ToolCallSheet(request: req)
                .id(req.id)
        }
    }

    private func sendText() {
        guard let peer = selectedPeer else { return }
        statusMessage = "Sending…"
        let txt = text
        let ctype = contentType
        Task {
            do {
                let r = try await service.transfer.sendText(peer: peer, content: txt, contentType: ctype)
                statusMessage = "→ \(r["status"] ?? "?")  pair=\(r["pair_code"] ?? "?")"
            } catch {
                statusMessage = "error: \(error.localizedDescription)"
            }
        }
    }
}

// MARK: - Peer row

private struct PeerRow: View {
    let peer: Peer
    let selected: Bool
    let onTap: () -> Void

    var body: some View {
        Button(action: onTap) {
            HStack {
                VStack(alignment: .leading) {
                    Text(peer.name).font(.body.weight(.medium))
                    Text("\(peer.ip):\(peer.tcpPort) · \(peer.platform)")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if peer.hasCapability("safedrop.tools") {
                    Image(systemName: "wrench.and.screwdriver")
                        .foregroundStyle(.secondary).font(.caption)
                }
                if selected {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(.blue)
                }
            }
            .padding(.vertical, 6)
            .padding(.horizontal, 10)
            .background(
                RoundedRectangle(cornerRadius: 8)
                    .stroke(selected ? Color.blue : Color.secondary.opacity(0.3))
            )
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Audit row

private struct AuditRow: View {
    let entry: ToolCallAuditEntry
    var body: some View {
        HStack(spacing: 8) {
            Text(entry.direction == "inbound" ? "↓" : "↑").bold()
            VStack(alignment: .leading) {
                Text("\(entry.peerName) — \(entry.toolName)").font(.caption.weight(.medium))
                if let s = entry.resultSummary ?? entry.error {
                    Text(s).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            Spacer()
            Text(entry.decision)
                .font(.caption2)
                .foregroundStyle(entry.decision == "allowed" ? .green :
                                 entry.decision == "denied" ? .red : .orange)
        }
    }
}

// MARK: - Allow / Deny dialog

private struct ToolCallSheet: View {
    @ObservedObject var request: ToolCallRequest

    var body: some View {
        ZStack {
            Color.black.opacity(0.45).ignoresSafeArea()
            VStack(alignment: .leading, spacing: 10) {
                Text("\(request.peerName) wants to call a tool")
                    .font(.headline)
                Text("from \(request.peerIp)").font(.caption).foregroundStyle(.secondary)
                Divider()
                Text("🔧 \(request.toolName)").font(.title3)
                Text(request.arguments.isEmpty ? "(no arguments)"
                     : (try? String(data: JSONSerialization.data(withJSONObject: request.arguments,
                                                                  options: [.prettyPrinted]),
                                    encoding: .utf8)) ?? "")
                    .font(.caption.monospaced())
                    .padding(8)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color(uiColor: .tertiarySystemBackground),
                                in: RoundedRectangle(cornerRadius: 6))
                Divider()
                Text("Pair code").font(.caption).foregroundStyle(.secondary)
                Text(request.pairCode).font(.title.weight(.bold).monospaced())
                HStack {
                    Button("Deny once") { request.respond(allow: false, persist: false) }
                    Button("Always deny") { request.respond(allow: false, persist: true) }
                    Spacer()
                    Button("Allow once") { request.respond(allow: true, persist: false) }
                    Button("Always allow") { request.respond(allow: true, persist: true) }
                        .buttonStyle(.borderedProminent)
                }
            }
            .padding(18)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
            .padding(20)
        }
    }
}

// MARK: - Received clipboard banner

private struct ClipboardReceivedBanner: View {
    let peerName: String
    let contentType: String
    let content: String
    let onDismiss: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("From \(peerName) — \(contentType)").font(.caption.weight(.semibold))
                Spacer()
                Button(action: onDismiss) { Image(systemName: "xmark.circle.fill") }
                    .foregroundStyle(.secondary)
            }
            Text(content).font(.body).lineLimit(3)
            HStack {
                Button("Copy") {
                    UIPasteboard.general.string = content
                    onDismiss()
                }
                .buttonStyle(.borderedProminent)
                if contentType == "url", let url = URL(string: content.trimmingCharacters(in: .whitespaces)) {
                    Button("Open") {
                        UIApplication.shared.open(url)
                        onDismiss()
                    }
                }
            }
        }
        .padding()
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12))
    }
}

// MARK: - Add manual peer sheet

private struct AddManualPeerSheet: View {
    @EnvironmentObject var service: SafeDropService
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var ip = ""
    @State private var port = "47891"
    @State private var pubkey = ""

    var body: some View {
        NavigationStack {
            Form {
                TextField("Name (optional)", text: $name)
                TextField("IP address", text: $ip).autocapitalization(.none)
                TextField("Port", text: $port).keyboardType(.numberPad)
                TextField("Peer pubkey (base64)", text: $pubkey, axis: .vertical)
                    .lineLimit(2...4)
                    .autocapitalization(.none)
                Text("Tip — for the desktop ↔ phone scenario, run `python bench.py receive` on the desktop and paste its pubkey here.")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            .navigationTitle("Add device manually")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Add") {
                        let p = UInt16(port) ?? 47891
                        service.addManualPeer(name: name, ip: ip, port: p, pubKey: pubkey)
                        dismiss()
                    }
                    .disabled(ip.isEmpty || pubkey.isEmpty || UInt16(port) == nil)
                }
            }
        }
    }
}

// MARK: - Trust management

private struct TrustView: View {
    @EnvironmentObject var service: SafeDropService
    @State private var snapshot: [(peerId: String, tools: [String: String])] = []

    var body: some View {
        List {
            if snapshot.isEmpty {
                Text("No trusted (peer, tool) pairs yet.\nApprove an Always allow / Always deny on a CALL_TOOL dialog and it appears here.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            ForEach(snapshot, id: \.peerId) { row in
                Section(header: Text(row.peerId).font(.caption.monospaced())) {
                    ForEach(Array(row.tools.keys.sorted()), id: \.self) { tool in
                        HStack {
                            Text(tool).font(.body.monospaced())
                            Spacer()
                            Text(row.tools[tool] ?? "")
                                .foregroundStyle(row.tools[tool] == "allow" ? .green : .red)
                            Button(action: {
                                service.trustStore.clear(peerDeviceId: row.peerId, toolName: tool)
                                reload()
                            }) {
                                Image(systemName: "minus.circle.fill").foregroundStyle(.red)
                            }
                        }
                    }
                }
            }
        }
        .navigationTitle("Trusted devices")
        .onAppear(perform: reload)
    }

    private func reload() {
        let s = service.trustStore.snapshot()
        snapshot = s.keys.sorted().map { (peerId: $0, tools: s[$0] ?? [:]) }
    }
}
