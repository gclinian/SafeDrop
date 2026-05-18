package com.safedrop.android.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import com.safedrop.android.data.SafeDropService
import com.safedrop.android.data.TrustStore
import com.safedrop.android.net.ClipboardPayload
import com.safedrop.android.net.IncomingRequest
import com.safedrop.android.net.Peer
import com.safedrop.android.net.TCP_PORT
import com.safedrop.android.net.ToolCallAuditEntry
import com.safedrop.android.net.ToolCallRequest
import com.safedrop.android.net.TransferKind
import com.safedrop.android.net.TransferState
import com.safedrop.android.net.TransferStatus
import kotlin.math.min
import kotlinx.coroutines.flow.collect

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HomeScreen(service: SafeDropService) {
    val context = LocalContext.current

    val discoveredPeers by service.discovery.peers.collectAsState()
    val manualPeers by service.manualPeers.collectAsState()
    val transfers by service.transfer.transfers.collectAsState()

    val allPeers: List<Peer> = remember(discoveredPeers, manualPeers) {
        (manualPeers + discoveredPeers.values).sortedBy { it.name.lowercase() }
    }

    var selectedPeerId by remember { mutableStateOf<String?>(null) }
    val selectedPeer = allPeers.firstOrNull { it.deviceId == selectedPeerId }

    var pendingRequest by remember { mutableStateOf<IncomingRequest?>(null) }
    var clipboardPayload by remember { mutableStateOf<ClipboardPayload?>(null) }
    var pendingToolCall by remember { mutableStateOf<ToolCallRequest?>(null) }
    var showAddDialog by remember { mutableStateOf(false) }
    var showTrustDialog by remember { mutableStateOf(false) }

    val auditEntries by service.transfer.audit.collectAsState()

    LaunchedEffect(Unit) {
        service.transfer.incoming.collect { req -> pendingRequest = req }
    }
    LaunchedEffect(Unit) {
        service.transfer.clipboardReceived.collect { payload -> clipboardPayload = payload }
    }
    LaunchedEffect(Unit) {
        service.transfer.toolCallPrompts.collect { req -> pendingToolCall = req }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text("SafeDrop", fontWeight = FontWeight.Bold)
                        Text(
                            "${service.deviceName} · ${service.discovery.localIp}:${TCP_PORT}",
                            fontSize = 12.sp,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                },
                actions = {
                    TextButton(onClick = { showTrustDialog = true }) {
                        Text("🔒 Trust")
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.surfaceVariant
                ),
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .padding(padding)
                .verticalScroll(rememberScrollState())
                .padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            PeerListCard(
                peers = allPeers,
                selectedId = selectedPeerId,
                onSelect = { selectedPeerId = it },
                onAddManual = { showAddDialog = true },
                onRemoveManual = { service.removeManualPeer(it) },
            )

            SendCard(
                peer = selectedPeer,
                onSendFile = { uri ->
                    val peer = selectedPeer
                    if (peer != null) {
                        // Persist read permission so we can stream the file later.
                        try {
                            context.contentResolver.takePersistableUriPermission(
                                uri, Intent.FLAG_GRANT_READ_URI_PERMISSION
                            )
                        } catch (_: SecurityException) {}
                        service.transfer.sendFile(peer, uri)
                    }
                },
                onSendClipboard = { text, kind ->
                    val peer = selectedPeer
                    if (peer != null && text.isNotEmpty()) {
                        service.transfer.sendClipboard(peer, text, kind)
                    }
                },
            )

            TransfersCard(transfers.values.sortedByDescending { it.startedAtMs })

            AuditCard(auditEntries.reversed().take(20))
        }
    }

    if (showAddDialog) {
        AddManualPeerDialog(
            onDismiss = { showAddDialog = false },
            onAdd = { name, ip, port, pub ->
                service.addManualPeer(name, ip, port, pub)
                showAddDialog = false
            },
        )
    }

    if (showTrustDialog) {
        TrustDialog(
            trustStore = service.trustStore,
            onDismiss = { showTrustDialog = false },
        )
    }

    pendingRequest?.let { req ->
        IncomingRequestDialog(
            request = req,
            onAccept = { req.accept(); pendingRequest = null },
            onReject = { req.reject(); pendingRequest = null },
        )
    }

    clipboardPayload?.let { payload ->
        ClipboardReceivedDialog(
            payload = payload,
            onDismiss = { clipboardPayload = null },
            onCopy = { copyToClipboard(context, payload.content); clipboardPayload = null },
            onOpenUrl = { openUrl(context, payload.content); clipboardPayload = null },
        )
    }

    pendingToolCall?.let { req ->
        ToolCallDialog(
            request = req,
            onRespond = { allow, persist ->
                req.respond(allow, persist)
                pendingToolCall = null
            },
        )
    }
}


// ============================================================ Peer list ====

@Composable
private fun PeerListCard(
    peers: List<Peer>,
    selectedId: String?,
    onSelect: (String) -> Unit,
    onAddManual: () -> Unit,
    onRemoveManual: (String) -> Unit,
) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Nearby devices", fontWeight = FontWeight.SemiBold)
                Spacer(Modifier.weight(1f))
                OutlinedButton(onClick = onAddManual) { Text("+ Add manually") }
            }
            Spacer(Modifier.height(8.dp))
            if (peers.isEmpty()) {
                Text(
                    "No devices yet. Other SafeDrop instances on the same Wi-Fi will appear here automatically.",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            } else {
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    for (peer in peers) {
                        PeerRow(
                            peer = peer,
                            selected = peer.deviceId == selectedId,
                            onClick = { onSelect(peer.deviceId) },
                            onRemove = if (peer.platform == "manual") {
                                { onRemoveManual(peer.deviceId) }
                            } else null,
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun PeerRow(peer: Peer, selected: Boolean, onClick: () -> Unit, onRemove: (() -> Unit)?) {
    val bg = if (selected) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.surface
    Surface(
        color = bg,
        shape = RoundedCornerShape(8.dp),
        modifier = Modifier
            .fillMaxWidth()
            .border(1.dp, MaterialTheme.colorScheme.outlineVariant, RoundedCornerShape(8.dp)),
        onClick = onClick,
    ) {
        Row(
            modifier = Modifier.padding(horizontal = 12.dp, vertical = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                Text(peer.name, fontWeight = FontWeight.Medium)
                Text(
                    "${peer.ip}:${peer.tcpPort} · ${peer.platform}",
                    fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (onRemove != null) {
                TextButton(onClick = onRemove) { Text("Remove") }
            }
        }
    }
}


// ============================================================ Send card ====

@Composable
private fun SendCard(
    peer: Peer?,
    onSendFile: (Uri) -> Unit,
    onSendClipboard: (String, String) -> Unit,
) {
    var clipText by remember { mutableStateOf("") }
    var contentType by remember { mutableStateOf("text") }

    val filePicker = rememberLauncherForActivityResult(
        contract = ActivityResultContracts.OpenDocument()
    ) { uri: Uri? ->
        uri?.let(onSendFile)
    }

    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(
                peer?.let { "Send to ${it.name}" } ?: "Send (select a device first)",
                fontWeight = FontWeight.SemiBold,
            )

            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("File:", modifier = Modifier.width(48.dp))
                Button(
                    enabled = peer != null,
                    onClick = { filePicker.launch(arrayOf("*/*")) },
                ) { Text("Pick & send file") }
            }

            HorizontalDivider()

            Text("Clipboard / text", fontWeight = FontWeight.Medium)
            Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                for (option in listOf("text", "url", "code")) {
                    FilterChip(
                        selected = contentType == option,
                        onClick = { contentType = option },
                        label = { Text(option) },
                    )
                }
            }
            OutlinedTextField(
                value = clipText,
                onValueChange = { clipText = it },
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(min = 90.dp),
                maxLines = 6,
                placeholder = { Text("Paste or type something to send…") },
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                val ctx = LocalContext.current
                OutlinedButton(onClick = {
                    val cm = ContextCompat.getSystemService(ctx, ClipboardManager::class.java)
                    val clip = cm?.primaryClip
                    if (clip != null && clip.itemCount > 0) {
                        clipText = clip.getItemAt(0).coerceToText(ctx).toString()
                        val trimmed = clipText.trim()
                        contentType = when {
                            (trimmed.startsWith("http://") || trimmed.startsWith("https://"))
                                && !trimmed.contains('\n') -> "url"
                            trimmed.contains('\n') || trimmed.any { it in "{};=" } -> "code"
                            else -> "text"
                        }
                    }
                }) { Text("Paste") }
                OutlinedButton(onClick = { clipText = "" }) { Text("Clear") }
                Spacer(Modifier.weight(1f))
                Button(
                    enabled = peer != null && clipText.isNotEmpty(),
                    onClick = { onSendClipboard(clipText, contentType) },
                ) { Text("Send clipboard") }
            }
        }
    }
}


// ============================================================ Transfers ====

@Composable
private fun TransfersCard(transfers: List<TransferState>) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp)) {
            Text("Transfers", fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.height(8.dp))
            if (transfers.isEmpty()) {
                Text(
                    "No transfers yet.",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            } else {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    for (t in transfers) TransferRow(t)
                }
            }
        }
    }
}

@Composable
private fun TransferRow(state: TransferState) {
    val arrow = if (state.direction.name == "Send") "↑" else "↓"
    val pct = if (state.size > 0) {
        min(1f, state.bytesDone.toFloat() / state.size.toFloat())
    } else if (state.status == TransferStatus.Done) 1f else 0f

    Column {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("$arrow  ${state.peerName}", fontWeight = FontWeight.Medium)
            Spacer(Modifier.weight(1f))
            Text(
                state.status.name.lowercase() +
                    (state.error?.let { ": ${it.take(40)}" } ?: ""),
                fontSize = 12.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Text(state.name, fontSize = 13.sp)
        if (state.size > 0) {
            LinearProgressIndicator(
                progress = { pct },
                modifier = Modifier.fillMaxWidth().padding(top = 4.dp),
            )
            Text(
                "${humanSize(state.bytesDone)} / ${humanSize(state.size)}" +
                    (if (state.status == TransferStatus.Transferring) "  ·  ${humanSpeed(state.speedBps)}" else ""),
                fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        if (state.savePath != null && state.kind == TransferKind.File && state.status == TransferStatus.Done) {
            Text(
                "Saved to ${state.savePath}",
                fontSize = 11.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}


// ============================================================ Dialogs ====

@Composable
private fun IncomingRequestDialog(
    request: IncomingRequest,
    onAccept: () -> Unit,
    onReject: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = onReject,
        title = { Text("${request.peerName} wants to send you something") },
        text = {
            Column {
                Text(
                    "from ${request.peerIp}",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    fontSize = 12.sp,
                )
                Spacer(Modifier.height(8.dp))
                if (request.kind == TransferKind.File) {
                    Text("📄  ${request.name}")
                    Text("Size: ${humanSize(request.size)}", fontSize = 13.sp)
                } else {
                    val ct = request.contentType ?: "text"
                    Text("📋  Clipboard — $ct")
                    Spacer(Modifier.height(4.dp))
                    Surface(
                        color = MaterialTheme.colorScheme.surfaceVariant,
                        shape = RoundedCornerShape(6.dp),
                    ) {
                        Text(
                            text = request.preview.orEmpty(),
                            modifier = Modifier.padding(8.dp),
                            fontSize = 13.sp,
                        )
                    }
                }
                Spacer(Modifier.height(12.dp))
                Text("Pair code (verify visually):", fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                Text(
                    request.pairCode,
                    fontSize = 28.sp,
                    fontWeight = FontWeight.Bold,
                    fontFamily = FontFamily.Monospace,
                )
            }
        },
        confirmButton = { Button(onClick = onAccept) { Text("Accept") } },
        dismissButton = { OutlinedButton(onClick = onReject) { Text("Reject") } },
    )
}

@Composable
private fun ClipboardReceivedDialog(
    payload: ClipboardPayload,
    onDismiss: () -> Unit,
    onCopy: () -> Unit,
    onOpenUrl: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Clipboard from ${payload.peerName}") },
        text = {
            Column {
                Text("Type: ${payload.contentType}",
                    color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp)
                Spacer(Modifier.height(8.dp))
                Surface(
                    color = MaterialTheme.colorScheme.surfaceVariant,
                    shape = RoundedCornerShape(6.dp),
                ) {
                    Text(payload.content, modifier = Modifier.padding(8.dp))
                }
            }
        },
        confirmButton = { Button(onClick = onCopy) { Text("Copy") } },
        dismissButton = {
            if (payload.contentType == "url") {
                Row {
                    OutlinedButton(onClick = onOpenUrl) { Text("Open URL") }
                    Spacer(Modifier.width(8.dp))
                    TextButton(onClick = onDismiss) { Text("Close") }
                }
            } else {
                TextButton(onClick = onDismiss) { Text("Close") }
            }
        },
    )
}

@Composable
private fun ToolCallDialog(
    request: ToolCallRequest,
    onRespond: (allow: Boolean, persist: Boolean) -> Unit,
) {
    AlertDialog(
        onDismissRequest = { onRespond(false, false) },
        title = { Text("${request.peerName} wants to call a tool") },
        text = {
            Column {
                Text("from ${request.peerIp}",
                    color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp)
                Spacer(Modifier.height(8.dp))
                Text("🔧 ${request.toolName}", fontWeight = FontWeight.SemiBold)
                Spacer(Modifier.height(4.dp))
                val argsPreview = if (request.arguments.length() == 0) "(no arguments)"
                else request.arguments.toString(2).take(300)
                Surface(
                    color = MaterialTheme.colorScheme.surfaceVariant,
                    shape = RoundedCornerShape(6.dp),
                ) {
                    Text(argsPreview, modifier = Modifier.padding(8.dp),
                         fontSize = 12.sp, fontFamily = FontFamily.Monospace)
                }
                Spacer(Modifier.height(10.dp))
                Text("Pair code:", fontSize = 12.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                Text(
                    request.pairCode,
                    fontSize = 22.sp,
                    fontWeight = FontWeight.Bold,
                    fontFamily = FontFamily.Monospace,
                )
            }
        },
        confirmButton = {
            Row {
                OutlinedButton(onClick = { onRespond(true, false) }) { Text("Allow once") }
                Spacer(Modifier.width(6.dp))
                Button(onClick = { onRespond(true, true) }) { Text("Always allow") }
            }
        },
        dismissButton = {
            Row {
                OutlinedButton(onClick = { onRespond(false, false) }) { Text("Deny") }
                Spacer(Modifier.width(6.dp))
                TextButton(onClick = { onRespond(false, true) }) { Text("Always deny") }
            }
        },
    )
}

@Composable
private fun AuditCard(entries: List<ToolCallAuditEntry>) {
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp)) {
            Text("Cross-device tool audit", fontWeight = FontWeight.SemiBold)
            Spacer(Modifier.height(8.dp))
            if (entries.isEmpty()) {
                Text("No cross-device tool calls yet.",
                     color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 13.sp)
            } else {
                Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    for (e in entries) {
                        val arrow = if (e.direction == "inbound") "↓" else "↑"
                        val summary = e.error ?: e.resultSummary ?: ""
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text("$arrow ${e.peerName}",
                                 fontSize = 12.sp,
                                 fontWeight = FontWeight.Medium,
                                 modifier = Modifier.weight(1.4f))
                            Text(e.toolName, fontSize = 12.sp, modifier = Modifier.weight(1.2f))
                            Text(e.decision, fontSize = 12.sp,
                                 color = when (e.decision) {
                                    "allowed" -> MaterialTheme.colorScheme.primary
                                    "denied" -> MaterialTheme.colorScheme.error
                                    else -> MaterialTheme.colorScheme.onSurfaceVariant
                                 },
                                 modifier = Modifier.weight(0.8f))
                            Text(summary.take(28), fontSize = 11.sp,
                                 color = MaterialTheme.colorScheme.onSurfaceVariant,
                                 modifier = Modifier.weight(2f))
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun TrustDialog(
    trustStore: com.safedrop.android.data.TrustStore,
    onDismiss: () -> Unit,
) {
    var snapshot by remember { mutableStateOf(trustStore.snapshot()) }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Trusted devices") },
        text = {
            Column {
                Text(
                    "Per-(peer, tool) decisions saved by 'Always allow' / 'Always deny'. " +
                        "Tap × to revoke an entry; future calls will ask again.",
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.height(8.dp))
                if (snapshot.isEmpty()) {
                    Text("(empty)", color = MaterialTheme.colorScheme.onSurfaceVariant)
                } else {
                    Column(
                        modifier = Modifier.heightIn(max = 360.dp),
                        verticalArrangement = Arrangement.spacedBy(10.dp),
                    ) {
                        for ((peerId, tools) in snapshot.entries.sortedBy { it.key }) {
                            Column(
                                modifier = Modifier.fillMaxWidth(),
                                verticalArrangement = Arrangement.spacedBy(3.dp),
                            ) {
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    Text(
                                        peerId.take(36),
                                        fontFamily = FontFamily.Monospace,
                                        fontSize = 11.sp,
                                        modifier = Modifier.weight(1f),
                                    )
                                    TextButton(onClick = {
                                        trustStore.clearPeer(peerId)
                                        snapshot = trustStore.snapshot()
                                    }) {
                                        Text("Revoke all", fontSize = 11.sp)
                                    }
                                }
                                for ((tool, decision) in tools.entries.sortedBy { it.key }) {
                                    Row(verticalAlignment = Alignment.CenterVertically) {
                                        Text(tool, fontSize = 13.sp,
                                             modifier = Modifier.weight(1f).padding(start = 12.dp))
                                        Text(
                                            decision,
                                            fontSize = 12.sp,
                                            color = if (decision == "allow") Color(0xFF1B7F2F)
                                                    else MaterialTheme.colorScheme.error,
                                            modifier = Modifier.padding(end = 8.dp),
                                        )
                                        TextButton(onClick = {
                                            trustStore.clear(peerId, tool)
                                            snapshot = trustStore.snapshot()
                                        }) { Text("×", fontWeight = FontWeight.Bold) }
                                    }
                                }
                            }
                            HorizontalDivider()
                        }
                    }
                }
            }
        },
        confirmButton = { Button(onClick = onDismiss) { Text("Close") } },
        dismissButton = if (snapshot.isNotEmpty()) {
            {
                TextButton(onClick = {
                    trustStore.clearAll()
                    snapshot = trustStore.snapshot()
                }) { Text("Clear all") }
            }
        } else null,
    )
}

@Composable
private fun AddManualPeerDialog(
    onDismiss: () -> Unit,
    onAdd: (name: String, ip: String, port: Int, pubKey: String) -> Unit,
) {
    var name by remember { mutableStateOf("") }
    var ip by remember { mutableStateOf("") }
    var portStr by remember { mutableStateOf(TCP_PORT.toString()) }
    var pubKey by remember { mutableStateOf("") }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Add device manually") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                OutlinedTextField(value = name, onValueChange = { name = it },
                    label = { Text("Name (optional)") }, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(value = ip, onValueChange = { ip = it },
                    label = { Text("IP address") }, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(value = portStr, onValueChange = { portStr = it.filter { c -> c.isDigit() } },
                    label = { Text("Port") }, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(value = pubKey, onValueChange = { pubKey = it },
                    label = { Text("Peer pubkey (base64)") }, modifier = Modifier.fillMaxWidth(),
                    maxLines = 3)
                Text(
                    "Tip — when testing on the emulator use 10.0.2.2 as the host IP, and run " +
                        "`adb reverse tcp:47891 tcp:47891`. The Python `bench.py receive` mode prints the pubkey.",
                    fontSize = 11.sp,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        },
        confirmButton = {
            Button(
                enabled = ip.isNotBlank() && pubKey.isNotBlank() && portStr.toIntOrNull() != null,
                onClick = { onAdd(name, ip.trim(), portStr.toInt(), pubKey.trim()) },
            ) { Text("Add") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}


// ============================================================ Helpers ====

private fun copyToClipboard(context: Context, text: String) {
    val cm = ContextCompat.getSystemService(context, ClipboardManager::class.java)
    cm?.setPrimaryClip(ClipData.newPlainText("SafeDrop", text))
}

private fun openUrl(context: Context, url: String) {
    val trimmed = url.trim()
    if (trimmed.isEmpty()) return
    val intent = Intent(Intent.ACTION_VIEW, Uri.parse(trimmed))
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    runCatching { context.startActivity(intent) }
}
