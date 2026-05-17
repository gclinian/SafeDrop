package com.safedrop.android.net

import android.content.Context
import android.net.Uri
import android.os.Environment
import android.provider.OpenableColumns
import android.util.Log
import com.safedrop.android.crypto.Identity
import com.safedrop.android.crypto.Session
import com.safedrop.android.crypto.deriveSession
import java.io.File
import java.io.IOException
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.security.MessageDigest
import java.util.Base64
import java.util.UUID
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONObject

private const val TAG = "SafeDrop/Transfer"

const val TCP_PORT = 47891
const val CHUNK_SIZE = 64 * 1024
const val VERSION = "1.0"

enum class TransferDirection { Send, Recv }
enum class TransferKind { File, Clipboard }
enum class TransferStatus { Pending, Transferring, Done, Failed, Rejected }

data class TransferState(
    val transferId: String,
    val direction: TransferDirection,
    val kind: TransferKind,
    val peerName: String,
    val name: String,
    val size: Long,
    val pairCode: String = "",
    val bytesDone: Long = 0,
    val startedAtMs: Long = System.currentTimeMillis(),
    val status: TransferStatus = TransferStatus.Pending,
    val error: String? = null,
    val savePath: String? = null,
    val clipboardContent: String? = null,
    val clipboardContentType: String? = null,
) {
    val speedBps: Double
        get() {
            val elapsed = (System.currentTimeMillis() - startedAtMs).coerceAtLeast(1) / 1000.0
            return bytesDone / elapsed
        }
}

data class IncomingRequest(
    val transferId: String,
    val peerName: String,
    val peerIp: String,
    val pairCode: String,
    val kind: TransferKind,
    val name: String,
    val size: Long,
    val contentType: String? = null,
    val preview: String? = null,
) {
    private val decision = kotlinx.coroutines.CompletableDeferred<Boolean>()
    fun accept() { decision.complete(true) }
    fun reject() { decision.complete(false) }
    suspend fun await(timeoutMs: Long): Boolean =
        withTimeoutOrNull(timeoutMs) { decision.await() } ?: false
}

data class ClipboardPayload(
    val transferId: String,
    val peerName: String,
    val contentType: String,
    val content: String,
)

/** Inbound CALL_TOOL — handed to the UI to render an Allow/Deny dialog. */
data class ToolCallRequest(
    val requestId: String,
    val peerName: String,
    val peerIp: String,
    val peerDeviceId: String,
    val pairCode: String,
    val toolName: String,
    val arguments: org.json.JSONObject,
) {
    private val decision = kotlinx.coroutines.CompletableDeferred<Pair<Boolean, Boolean>>()
    /** allow + (persist?) */
    fun respond(allow: Boolean, persist: Boolean = false) { decision.complete(allow to persist) }
    suspend fun await(timeoutMs: Long): Pair<Boolean, Boolean> =
        kotlinx.coroutines.withTimeoutOrNull(timeoutMs) { decision.await() } ?: (false to false)
}

/** One row of the cross-device tool-call audit log. */
data class ToolCallAuditEntry(
    val timestampMs: Long,
    val direction: String,           // "inbound" | "outbound"
    val peerName: String,
    val peerIp: String,
    val toolName: String,
    val arguments: String,           // JSON string for compactness
    val decision: String,            // "allowed" | "denied" | "error"
    val resultSummary: String?,
    val error: String?,
)

class TransferManager(
    private val context: Context,
    private val identity: Identity,
    private val deviceId: String,
    private val deviceName: String,
    private val tcpPort: Int = TCP_PORT,
    val toolRegistry: ToolRegistry? = null,
    private val trustStore: com.safedrop.android.data.TrustStore? = null,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var serverSocket: ServerSocket? = null

    private val _transfers = MutableStateFlow<Map<String, TransferState>>(emptyMap())
    val transfers: StateFlow<Map<String, TransferState>> = _transfers

    private val _incoming = MutableSharedFlow<IncomingRequest>(extraBufferCapacity = 8)
    val incoming: SharedFlow<IncomingRequest> = _incoming

    private val _clipboardReceived = MutableSharedFlow<ClipboardPayload>(extraBufferCapacity = 8)
    val clipboardReceived: SharedFlow<ClipboardPayload> = _clipboardReceived

    /** Inbound CALL_TOOL that needs a user decision (after policy check). */
    private val _toolCallPrompts = MutableSharedFlow<ToolCallRequest>(extraBufferCapacity = 8)
    val toolCallPrompts: SharedFlow<ToolCallRequest> = _toolCallPrompts

    /** Cross-device tool-call audit log (most recent appended last). */
    private val _audit = MutableStateFlow<List<ToolCallAuditEntry>>(emptyList())
    val audit: StateFlow<List<ToolCallAuditEntry>> = _audit

    val downloadDir: File =
        File(context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS), "SafeDrop").apply { mkdirs() }

    fun start() {
        val sock = ServerSocket().apply {
            reuseAddress = true
            bind(InetSocketAddress(tcpPort))
            soTimeout = 1000
        }
        serverSocket = sock
        scope.launch { acceptLoop(sock) }
    }

    fun stop() {
        try { serverSocket?.close() } catch (_: Exception) {}
        scope.cancel()
    }

    // ----- state helpers ----------------------------------------------

    private fun upsert(state: TransferState) {
        _transfers.value = _transfers.value + (state.transferId to state)
    }

    private fun update(id: String, transform: (TransferState) -> TransferState) {
        val current = _transfers.value[id] ?: return
        _transfers.value = _transfers.value + (id to transform(current))
    }

    // ----- inbound -----------------------------------------------------

    private suspend fun acceptLoop(sock: ServerSocket) {
        while (scope.isActive) {
            val client = try {
                sock.accept()
            } catch (_: java.net.SocketTimeoutException) {
                continue
            } catch (e: Exception) {
                if (!scope.isActive) break
                Log.w(TAG, "accept error: ${e.message}")
                continue
            }
            scope.launch { handleInbound(client) }
        }
    }

    private suspend fun handleInbound(client: Socket) {
        var stateId: String? = null
        try {
            client.use { s ->
                s.soTimeout = 60_000
                val input = s.getInputStream()
                val out = s.getOutputStream()

                // ---- plaintext handshake ----
                val hello = Protocol.recvJson(input)
                require(hello.optString("type") == "HELLO") { "expected HELLO" }
                val peerName = hello.optString("name", "unknown")
                val peerDeviceId = hello.optString("device_id", "")
                val peerPubKey = hello.optString("pubkey")
                require(peerPubKey.isNotEmpty()) { "missing pubkey" }

                val session = deriveSession(identity, peerPubKey)

                val ack = JSONObject().apply {
                    put("type", "HELLO_ACK")
                    put("device_id", deviceId)
                    put("name", deviceName)
                    put("platform", "Android")
                    put("pubkey", identity.publicKeyBase64())
                    put("version", VERSION)
                    put("pair_code", session.pairCode)
                }
                Protocol.sendJson(out, ack)

                // ---- dispatch on first encrypted message ----
                val msg = Protocol.recvJson(input, decrypt = { session.decrypt(it) })
                when (msg.optString("type")) {
                    "LIST_TOOLS" -> {
                        handleInboundListTools(out, session, msg)
                        return@use
                    }
                    "CALL_TOOL" -> {
                        handleInboundCallTool(out, session, msg, peerName, client.inetAddress?.hostAddress ?: "?", peerDeviceId)
                        return@use
                    }
                    "REQUEST" -> { /* fall through to existing flow */ }
                    else -> throw IOException("unknown encrypted message type: ${msg.optString("type")}")
                }

                // ---- REQUEST (encrypted) ----
                val req = msg
                val transferId = req.optString("transfer_id", UUID.randomUUID().toString())
                stateId = transferId
                val kindStr = req.optString("kind")

                val state: TransferState
                val incoming: IncomingRequest
                when (kindStr) {
                    "file" -> {
                        val name = req.optString("name", "file")
                        val size = req.optLong("size", 0)
                        state = TransferState(
                            transferId = transferId,
                            direction = TransferDirection.Recv,
                            kind = TransferKind.File,
                            peerName = peerName,
                            name = name,
                            size = size,
                            pairCode = session.pairCode,
                        )
                        incoming = IncomingRequest(
                            transferId = transferId,
                            peerName = peerName,
                            peerIp = client.inetAddress?.hostAddress ?: "?",
                            pairCode = session.pairCode,
                            kind = TransferKind.File,
                            name = name,
                            size = size,
                        )
                    }
                    "clipboard" -> {
                        val contentType = req.optString("content_type", "text")
                        val preview = req.optString("preview", "")
                        val length = req.optLong("length", 0)
                        val displayName = "Clipboard ($contentType)"
                        state = TransferState(
                            transferId = transferId,
                            direction = TransferDirection.Recv,
                            kind = TransferKind.Clipboard,
                            peerName = peerName,
                            name = displayName,
                            size = length,
                            pairCode = session.pairCode,
                        )
                        incoming = IncomingRequest(
                            transferId = transferId,
                            peerName = peerName,
                            peerIp = client.inetAddress?.hostAddress ?: "?",
                            pairCode = session.pairCode,
                            kind = TransferKind.Clipboard,
                            name = displayName,
                            size = length,
                            contentType = contentType,
                            preview = preview,
                        )
                    }
                    else -> throw IOException("unknown kind: $kindStr")
                }
                upsert(state)
                _incoming.emit(incoming)

                val accepted = incoming.await(120_000)
                if (!accepted) {
                    Protocol.sendJson(out, JSONObject().apply {
                        put("type", "REJECT")
                        put("transfer_id", transferId)
                        put("reason", "user")
                    }, encrypt = { session.encrypt(it) })
                    update(transferId) { it.copy(status = TransferStatus.Rejected) }
                    return@use
                }

                Protocol.sendJson(out, JSONObject().apply {
                    put("type", "ACCEPT")
                    put("transfer_id", transferId)
                }, encrypt = { session.encrypt(it) })
                update(transferId) {
                    it.copy(status = TransferStatus.Transferring, startedAtMs = System.currentTimeMillis())
                }

                if (incoming.kind == TransferKind.Clipboard) {
                    recvClipboard(input, session, transferId, incoming.contentType ?: "text", peerName)
                } else {
                    recvFile(input, session, transferId, incoming.name)
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "inbound failed: ${e.message}", e)
            stateId?.let { id ->
                update(id) { it.copy(status = TransferStatus.Failed, error = e.message ?: "error") }
            }
        }
    }

    private suspend fun recvClipboard(
        input: java.io.InputStream,
        session: Session,
        transferId: String,
        contentType: String,
        peerName: String,
    ) {
        val msg = Protocol.recvJson(input, decrypt = { session.decrypt(it) })
        require(msg.optString("type") == "CLIPBOARD" && msg.optString("transfer_id") == transferId)
        val content = msg.optString("content")
        val bytes = content.toByteArray(Charsets.UTF_8).size.toLong()
        update(transferId) {
            it.copy(
                status = TransferStatus.Done,
                bytesDone = bytes,
                size = bytes,
                clipboardContent = content,
                clipboardContentType = contentType,
            )
        }
        _clipboardReceived.emit(ClipboardPayload(transferId, peerName, contentType, content))
    }

    private fun recvFile(
        input: java.io.InputStream,
        session: Session,
        transferId: String,
        suggestedName: String,
    ) {
        val dest = chooseSavePath(suggestedName)
        update(transferId) { it.copy(savePath = dest.absolutePath) }
        val hasher = MessageDigest.getInstance("SHA-256")
        dest.outputStream().use { fos ->
            while (true) {
                val msg = Protocol.recvJson(input, decrypt = { session.decrypt(it) })
                require(msg.optString("type") == "CHUNK") { "expected CHUNK" }
                val b64 = msg.optString("data_b64")
                val data = if (b64.isEmpty()) ByteArray(0)
                else Base64.getDecoder().decode(b64)
                fos.write(data)
                hasher.update(data)
                update(transferId) { it.copy(bytesDone = it.bytesDone + data.size) }
                if (msg.optBoolean("final", false)) break
            }
        }
        update(transferId) { it.copy(status = TransferStatus.Done) }
    }

    private fun chooseSavePath(suggested: String): File {
        // Strip directory components for safety.
        val safe = suggested.substringAfterLast('/').substringAfterLast('\\').ifBlank { "received.bin" }
        var candidate = File(downloadDir, safe)
        if (!candidate.exists()) return candidate
        val dot = safe.lastIndexOf('.')
        val base = if (dot > 0) safe.substring(0, dot) else safe
        val ext = if (dot > 0) safe.substring(dot) else ""
        var i = 1
        while (true) {
            candidate = File(downloadDir, "${base}_$i$ext")
            if (!candidate.exists()) return candidate
            i++
        }
    }

    // ----- inbound: cross-device tools (Phase 2.2) --------------------

    private fun handleInboundListTools(
        out: java.io.OutputStream,
        session: Session,
        msg: JSONObject,
    ) {
        val requestId = msg.optString("request_id", "")
        val tools = toolRegistry?.listManifests() ?: org.json.JSONArray()
        Protocol.sendJson(out, JSONObject().apply {
            put("type", "TOOLS_LIST")
            put("request_id", requestId)
            put("tools", tools)
        }, encrypt = { session.encrypt(it) })
    }

    private suspend fun handleInboundCallTool(
        out: java.io.OutputStream,
        session: Session,
        msg: JSONObject,
        peerName: String,
        peerIp: String,
        peerDeviceId: String,
    ) {
        val requestId = msg.optString("request_id", "")
        val name = msg.optString("name", "")
        val args = msg.optJSONObject("arguments") ?: JSONObject()

        // Unknown tool — short-circuit.
        if (toolRegistry == null || !toolRegistry.has(name)) {
            Protocol.sendJson(out, JSONObject().apply {
                put("type", "CALL_TOOL_RESULT")
                put("request_id", requestId)
                put("error", "tool not available: '$name'")
            }, encrypt = { session.encrypt(it) })
            recordAudit("inbound", peerName, peerIp, name, args.toString().take(200),
                        "denied", null, "tool not available")
            return
        }

        // Step 1: persisted policy short-circuit.
        val ts = trustStore
        var allowed = true
        var consultedUi = false
        if (ts != null) {
            when (ts.check(peerDeviceId, name)) {
                com.safedrop.android.data.TrustStore.ALLOW -> allowed = true
                com.safedrop.android.data.TrustStore.DENY -> allowed = false
                else -> consultedUi = true
            }
        } else {
            consultedUi = true
        }

        // Step 2: UI prompt.
        if (consultedUi) {
            val prompt = ToolCallRequest(
                requestId = requestId,
                peerName = peerName,
                peerIp = peerIp,
                peerDeviceId = peerDeviceId,
                pairCode = session.pairCode,
                toolName = name,
                arguments = args,
            )
            _toolCallPrompts.emit(prompt)
            val (allow, persist) = prompt.await(120_000)
            allowed = allow
            if (persist && ts != null) {
                ts.set(peerDeviceId, name,
                       if (allow) com.safedrop.android.data.TrustStore.ALLOW
                       else com.safedrop.android.data.TrustStore.DENY)
            }
        }

        if (!allowed) {
            Protocol.sendJson(out, JSONObject().apply {
                put("type", "CALL_TOOL_RESULT")
                put("request_id", requestId)
                put("error", "denied by authorizer")
            }, encrypt = { session.encrypt(it) })
            recordAudit("inbound", peerName, peerIp, name, args.toString().take(200),
                        "denied", null, null)
            return
        }

        // Step 3: execute on the IO dispatcher (handlers may do clipboard/etc).
        try {
            val result = withContext(Dispatchers.Default) { toolRegistry.call(name, args) }
            val resultJson: Any = result ?: JSONObject.NULL
            Protocol.sendJson(out, JSONObject().apply {
                put("type", "CALL_TOOL_RESULT")
                put("request_id", requestId)
                put("result", resultJson)
            }, encrypt = { session.encrypt(it) })
            val summary = if (result is String) result.take(120) else result.toString().take(120)
            recordAudit("inbound", peerName, peerIp, name, args.toString().take(200),
                        "allowed", summary, null)
        } catch (e: Exception) {
            val err = "${e.javaClass.simpleName}: ${e.message ?: ""}"
            Protocol.sendJson(out, JSONObject().apply {
                put("type", "CALL_TOOL_RESULT")
                put("request_id", requestId)
                put("error", err)
            }, encrypt = { session.encrypt(it) })
            recordAudit("inbound", peerName, peerIp, name, args.toString().take(200),
                        "error", null, err)
        }
    }

    private fun recordAudit(
        direction: String,
        peerName: String,
        peerIp: String,
        toolName: String,
        arguments: String,
        decision: String,
        resultSummary: String?,
        error: String?,
    ) {
        val entry = ToolCallAuditEntry(
            timestampMs = System.currentTimeMillis(),
            direction = direction,
            peerName = peerName,
            peerIp = peerIp,
            toolName = toolName,
            arguments = arguments,
            decision = decision,
            resultSummary = resultSummary,
            error = error,
        )
        // Cap at 200 rows in memory; oldest first.
        _audit.value = (_audit.value + entry).takeLast(200)
    }

    // ----- outbound ----------------------------------------------------

    fun sendFile(peer: Peer, uri: Uri): TransferState {
        val info = queryFileInfo(uri) ?: ("file" to 0L)
        val state = TransferState(
            transferId = UUID.randomUUID().toString(),
            direction = TransferDirection.Send,
            kind = TransferKind.File,
            peerName = peer.name,
            name = info.first,
            size = info.second,
        )
        upsert(state)
        scope.launch { runCatching { doSendFile(peer, uri, state) }
            .onFailure { e -> update(state.transferId) {
                it.copy(status = TransferStatus.Failed, error = e.message ?: "error")
            } }
        }
        return state
    }

    fun sendClipboard(peer: Peer, content: String, contentType: String): TransferState {
        val type = if (contentType in setOf("text", "url", "code")) contentType else "text"
        val sizeBytes = content.toByteArray(Charsets.UTF_8).size.toLong()
        val state = TransferState(
            transferId = UUID.randomUUID().toString(),
            direction = TransferDirection.Send,
            kind = TransferKind.Clipboard,
            peerName = peer.name,
            name = "Clipboard ($type)",
            size = sizeBytes,
            clipboardContent = content,
            clipboardContentType = type,
        )
        upsert(state)
        scope.launch { runCatching { doSendClipboard(peer, content, type, state) }
            .onFailure { e -> update(state.transferId) {
                it.copy(status = TransferStatus.Failed, error = e.message ?: "error")
            } }
        }
        return state
    }

    private fun openSession(peer: Peer): Pair<Socket, Session> {
        val sock = Socket()
        sock.connect(InetSocketAddress(peer.ip, peer.tcpPort), 15_000)
        sock.soTimeout = 60_000
        Protocol.sendJson(sock.getOutputStream(), JSONObject().apply {
            put("type", "HELLO")
            put("device_id", deviceId)
            put("name", deviceName)
            put("platform", "Android")
            put("pubkey", identity.publicKeyBase64())
            put("version", VERSION)
        })
        val ack = Protocol.recvJson(sock.getInputStream())
        require(ack.optString("type") == "HELLO_ACK") { "expected HELLO_ACK" }
        val peerPub = ack.optString("pubkey")
        require(peerPub.isNotEmpty()) { "missing peer pubkey" }
        return sock to deriveSession(identity, peerPub)
    }

    private fun connectAndHandshake(peer: Peer, state: TransferState): Pair<Socket, Session> {
        val (sock, session) = openSession(peer)
        update(state.transferId) { it.copy(pairCode = session.pairCode) }
        return sock to session
    }

    // ----- outbound: cross-device tools -------------------------------

    /**
     * Ask [peer] for its tool manifest. Returns a JSONArray of
     * `{name, description, inputSchema}` entries.
     */
    suspend fun listRemoteTools(peer: Peer, timeoutMs: Long = 10_000): org.json.JSONArray =
        withContext(Dispatchers.IO) {
            val (sock, session) = openSession(peer)
            sock.use { s ->
                s.soTimeout = timeoutMs.toInt()
                val requestId = UUID.randomUUID().toString()
                Protocol.sendJson(s.getOutputStream(), JSONObject().apply {
                    put("type", "LIST_TOOLS")
                    put("request_id", requestId)
                }, encrypt = { session.encrypt(it) })
                val resp = Protocol.recvJson(s.getInputStream(), decrypt = { session.decrypt(it) })
                require(resp.optString("type") == "TOOLS_LIST") { "expected TOOLS_LIST" }
                resp.optJSONArray("tools") ?: org.json.JSONArray()
            }
        }

    /**
     * Invoke [name] on [peer]. Returns a JSONObject with either a
     * `result` field or an `error` field.
     */
    suspend fun callRemoteTool(
        peer: Peer,
        name: String,
        arguments: JSONObject = JSONObject(),
        timeoutMs: Long = 60_000,
    ): JSONObject = withContext(Dispatchers.IO) {
        val (sock, session) = openSession(peer)
        sock.use { s ->
            s.soTimeout = timeoutMs.toInt()
            val requestId = UUID.randomUUID().toString()
            Protocol.sendJson(s.getOutputStream(), JSONObject().apply {
                put("type", "CALL_TOOL")
                put("request_id", requestId)
                put("name", name)
                put("arguments", arguments)
            }, encrypt = { session.encrypt(it) })
            val resp = Protocol.recvJson(s.getInputStream(), decrypt = { session.decrypt(it) })
            require(resp.optString("type") == "CALL_TOOL_RESULT") { "expected CALL_TOOL_RESULT" }

            val decision = if (resp.has("error")) "error" else "allowed"
            val resultSummary: String? = if (resp.has("result")) {
                val r = resp.opt("result")
                (if (r is String) r else r.toString()).take(120)
            } else null
            val errMsg = if (resp.has("error")) resp.optString("error") else null
            recordAudit(
                "outbound", peer.name, peer.ip, name, arguments.toString().take(200),
                decision, resultSummary, errMsg,
            )

            JSONObject().apply {
                if (resp.has("result")) put("result", resp.opt("result"))
                if (resp.has("error")) put("error", resp.optString("error"))
            }
        }
    }

    private fun awaitDecision(sock: Socket, session: Session, transferId: String): String {
        sock.soTimeout = 180_000
        val resp = Protocol.recvJson(sock.getInputStream(), decrypt = { session.decrypt(it) })
        sock.soTimeout = 60_000
        val t = resp.optString("type")
        require(t in setOf("ACCEPT", "REJECT") && resp.optString("transfer_id") == transferId) {
            "expected ACCEPT/REJECT, got $t"
        }
        return t
    }

    private fun doSendFile(peer: Peer, uri: Uri, state: TransferState) {
        val (sock, session) = connectAndHandshake(peer, state)
        sock.use { s ->
            val out = s.getOutputStream()

            Protocol.sendJson(out, JSONObject().apply {
                put("type", "REQUEST")
                put("transfer_id", state.transferId)
                put("kind", "file")
                put("name", state.name)
                put("size", state.size)
            }, encrypt = { session.encrypt(it) })

            val decision = awaitDecision(s, session, state.transferId)
            if (decision == "REJECT") {
                update(state.transferId) { it.copy(status = TransferStatus.Rejected) }
                return@use
            }
            update(state.transferId) {
                it.copy(status = TransferStatus.Transferring, startedAtMs = System.currentTimeMillis())
            }

            val resolver = context.contentResolver
            var seq = 0
            var sent = 0L
            val inp = resolver.openInputStream(uri)
                ?: throw IOException("cannot open URI: $uri")
            inp.use { stream ->
                val buf = ByteArray(CHUNK_SIZE)
                while (true) {
                    val n = stream.read(buf)
                    val read = if (n < 0) 0 else n
                    val isFinal = read < buf.size
                    val data = if (read == buf.size) buf else buf.copyOfRange(0, read)
                    val msg = JSONObject().apply {
                        put("type", "CHUNK")
                        put("transfer_id", state.transferId)
                        put("seq", seq)
                        put("data_b64", if (data.isEmpty()) "" else Base64.getEncoder().encodeToString(data))
                        put("final", isFinal)
                    }
                    Protocol.sendJson(out, msg, encrypt = { session.encrypt(it) })
                    sent += read
                    update(state.transferId) { it.copy(bytesDone = sent) }
                    seq++
                    if (isFinal) break
                }
            }
            update(state.transferId) { it.copy(status = TransferStatus.Done) }
        }
    }

    private fun doSendClipboard(peer: Peer, content: String, contentType: String, state: TransferState) {
        val (sock, session) = connectAndHandshake(peer, state)
        sock.use { s ->
            val out = s.getOutputStream()
            val preview = if (content.length > 200) content.substring(0, 200) else content
            Protocol.sendJson(out, JSONObject().apply {
                put("type", "REQUEST")
                put("transfer_id", state.transferId)
                put("kind", "clipboard")
                put("content_type", contentType)
                put("preview", preview)
                put("length", state.size)
            }, encrypt = { session.encrypt(it) })

            val decision = awaitDecision(s, session, state.transferId)
            if (decision == "REJECT") {
                update(state.transferId) { it.copy(status = TransferStatus.Rejected) }
                return@use
            }
            update(state.transferId) {
                it.copy(status = TransferStatus.Transferring, startedAtMs = System.currentTimeMillis())
            }
            Protocol.sendJson(out, JSONObject().apply {
                put("type", "CLIPBOARD")
                put("transfer_id", state.transferId)
                put("content_type", contentType)
                put("content", content)
            }, encrypt = { session.encrypt(it) })
            update(state.transferId) { it.copy(bytesDone = state.size, status = TransferStatus.Done) }
        }
    }

    private fun queryFileInfo(uri: Uri): Pair<String, Long>? {
        return context.contentResolver
            .query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)
            ?.use { c ->
                if (c.moveToFirst()) {
                    val name = c.getString(0) ?: "file"
                    val size = c.getLong(1)
                    name to size
                } else null
            }
    }
}
