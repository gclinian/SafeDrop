import SwiftUI

/// v1.6 — manage a remote peer's capability tokens over SafeDrop.
///
/// This view *does not* manage local iPhone tokens — the phone is not
/// an MCP host. Instead it calls the cross-device peer tools
/// (`tokens_list`, `tokens_mint`, `tokens_revoke`) on a selected
/// SafeDrop peer (typically your desktop running `safedrop-mcp`), and
/// renders the results.
///
/// The first time a paired peer accepts a `tokens_*` call, the user on
/// the other end sees an Allow / Deny dialog — the same trust flow as
/// any other peer tool. Once "Always allow"d, this view operates with
/// no further prompts on either side.
struct TokenAdminView: View {
    @EnvironmentObject var service: SafeDropService
    let peer: Peer

    @State private var rows: [TokenRow] = []
    @State private var loading = false
    @State private var errorMessage: String?
    @State private var newLabel = ""
    @State private var newScopeCSV = ""
    @State private var newTTL = "86400"
    @State private var minted: MintedSecret?

    struct TokenRow: Identifiable {
        let id = UUID()
        let label: String
        let suffix: String
        let scope: [String]
        let createdAt: Double
        let expiresAt: Double?
        let isExpired: Bool
    }

    struct MintedSecret: Identifiable {
        let id = UUID()
        let token: String
        let label: String
    }

    var body: some View {
        Form {
            Section(header: Text("On \(peer.name)")) {
                if let msg = errorMessage {
                    Text(msg).font(.caption).foregroundStyle(.red)
                }
                if loading && rows.isEmpty {
                    HStack {
                        ProgressView()
                        Text("Loading tokens…").foregroundStyle(.secondary)
                    }
                } else if rows.isEmpty {
                    Text("No capability tokens on this device yet. Use the form below to mint one.")
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    ForEach(rows) { row in
                        TokenRowView(row: row) {
                            revoke(suffix: row.suffix)
                        }
                    }
                }
                Button {
                    refresh()
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(loading)
            }

            Section(header: Text("Mint new")) {
                TextField("Label (e.g. cloud-bot)", text: $newLabel)
                    .autocapitalization(.none)
                TextField("Scope (comma-separated globs, blank = allow-all)", text: $newScopeCSV)
                    .autocapitalization(.none)
                TextField("TTL seconds (blank = no expiry)", text: $newTTL)
                    .keyboardType(.numberPad)
                Button {
                    mint()
                } label: {
                    Label("Mint token", systemImage: "key.fill")
                }
                .disabled(newLabel.trimmingCharacters(in: .whitespaces).isEmpty || loading)
            }
        }
        .navigationTitle("Tokens")
        .onAppear { if rows.isEmpty { refresh() } }
        .sheet(item: $minted) { secret in
            MintedSecretSheet(secret: secret)
        }
    }

    // ---- networking -------------------------------------------------

    private func refresh() {
        loading = true
        errorMessage = nil
        Task {
            do {
                let r = try await service.transfer.callRemoteTool(
                    peer: peer, name: "tokens_list"
                )
                if let err = r["error"] as? String {
                    errorMessage = err
                    rows = []
                } else {
                    rows = decodeTokensList(r["result"])
                }
            } catch {
                errorMessage = "\(error.localizedDescription)"
            }
            loading = false
        }
    }

    private func mint() {
        loading = true
        errorMessage = nil
        let label = newLabel.trimmingCharacters(in: .whitespaces)
        let scope = newScopeCSV
            .split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        var args: [String: Any] = ["label": label, "scope": scope]
        if let ttl = Double(newTTL.trimmingCharacters(in: .whitespaces)), ttl > 0 {
            args["ttl_seconds"] = ttl
        }
        Task {
            do {
                let r = try await service.transfer.callRemoteTool(
                    peer: peer, name: "tokens_mint", arguments: args
                )
                if let err = r["error"] as? String {
                    errorMessage = err
                } else if let result = r["result"] as? [String: Any],
                          let tok = result["token"] as? String,
                          let lbl = result["label"] as? String {
                    minted = MintedSecret(token: tok, label: lbl)
                    newLabel = ""; newScopeCSV = ""; newTTL = "86400"
                    refresh()
                }
            } catch {
                errorMessage = "\(error.localizedDescription)"
            }
            loading = false
        }
    }

    private func revoke(suffix: String) {
        loading = true
        errorMessage = nil
        Task {
            do {
                let r = try await service.transfer.callRemoteTool(
                    peer: peer, name: "tokens_revoke", arguments: ["token": suffix]
                )
                if let err = r["error"] as? String {
                    errorMessage = err
                }
                refresh()
            } catch {
                errorMessage = "\(error.localizedDescription)"
                loading = false
            }
        }
    }

    private func decodeTokensList(_ raw: Any?) -> [TokenRow] {
        guard let dict = raw as? [String: Any],
              let tokensRaw = dict["tokens"] as? [[String: Any]] else {
            return []
        }
        return tokensRaw.map { row in
            TokenRow(
                label: (row["label"] as? String) ?? "(unnamed)",
                suffix: (row["token_suffix"] as? String) ?? "",
                scope: (row["scope"] as? [String]) ?? [],
                createdAt: (row["created_at"] as? Double) ?? 0,
                expiresAt: row["expires_at"] as? Double,
                isExpired: (row["is_expired"] as? Bool) ?? false
            )
        }
    }
}

// MARK: - Subviews

private struct TokenRowView: View {
    let row: TokenAdminView.TokenRow
    let onRevoke: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(row.label).font(.body.weight(.medium))
                Spacer()
                Text("…" + row.suffix)
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
                if row.isExpired {
                    Text("expired").font(.caption2)
                        .padding(.horizontal, 4)
                        .background(Color.red.opacity(0.2),
                                    in: RoundedRectangle(cornerRadius: 3))
                }
            }
            if !row.scope.isEmpty {
                Text("scope: \(row.scope.joined(separator: ", "))")
                    .font(.caption).foregroundStyle(.secondary).lineLimit(2)
            } else {
                Text("scope: (allow-all)")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let exp = row.expiresAt {
                Text("expires \(Date(timeIntervalSince1970: exp).formatted())")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            Button(role: .destructive, action: onRevoke) {
                Label("Revoke", systemImage: "trash")
                    .font(.caption)
            }
        }
        .padding(.vertical, 4)
    }
}

private struct MintedSecretSheet: View {
    let secret: TokenAdminView.MintedSecret
    @Environment(\.dismiss) private var dismiss
    @State private var copied = false

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 14) {
                Text("Copy this token NOW — it will not be shown again.")
                    .font(.callout.weight(.semibold))
                    .foregroundStyle(.red)
                Text("label: \(secret.label)")
                    .font(.caption).foregroundStyle(.secondary)
                ScrollView(.horizontal, showsIndicators: false) {
                    Text(secret.token)
                        .font(.system(.body, design: .monospaced))
                        .padding(8)
                        .background(Color(uiColor: .secondarySystemBackground),
                                    in: RoundedRectangle(cornerRadius: 6))
                        .textSelection(.enabled)
                }
                HStack {
                    Button {
                        UIPasteboard.general.string = secret.token
                        copied = true
                    } label: {
                        Label(copied ? "Copied!" : "Copy to clipboard",
                              systemImage: "doc.on.doc")
                    }
                    .buttonStyle(.borderedProminent)
                    Spacer()
                    Button("Close") { dismiss() }
                }
                Spacer()
            }
            .padding()
            .navigationTitle("Token minted")
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}
