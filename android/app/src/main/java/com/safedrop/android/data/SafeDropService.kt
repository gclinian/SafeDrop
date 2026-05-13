package com.safedrop.android.data

import android.content.Context
import android.os.Build
import com.safedrop.android.crypto.Identity
import com.safedrop.android.net.Discovery
import com.safedrop.android.net.Peer
import com.safedrop.android.net.TCP_PORT
import com.safedrop.android.net.TransferManager
import com.safedrop.android.net.VERSION
import java.util.UUID
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * Process-scoped owner of one [Identity], one [Discovery], one
 * [TransferManager], plus a list of manually-entered peers (for emulator
 * use, where UDP broadcast can't escape NAT to the host LAN).
 */
class SafeDropService(context: Context) {
    val identity: Identity = Identity.generate()
    val deviceId: String = UUID.randomUUID().toString()
    val deviceName: String = "${Build.MODEL} (Android ${Build.VERSION.RELEASE})"

    val discovery: Discovery = Discovery(
        context = context,
        deviceId = deviceId,
        deviceName = deviceName,
        platformName = "Android",
        tcpPort = TCP_PORT,
        publicKeyBase64 = identity.publicKeyBase64(),
        version = VERSION,
    )

    val transfer: TransferManager = TransferManager(
        context = context,
        identity = identity,
        deviceId = deviceId,
        deviceName = deviceName,
        tcpPort = TCP_PORT,
    )

    private val _manualPeers = MutableStateFlow<List<Peer>>(emptyList())
    val manualPeers: StateFlow<List<Peer>> = _manualPeers

    fun addManualPeer(name: String, ip: String, port: Int, pubKey: String) {
        val displayName = name.ifBlank { "$ip:$port" }
        val peer = Peer(
            deviceId = "manual:$ip:$port",
            name = displayName,
            platform = "manual",
            ip = ip,
            tcpPort = port,
            pubKeyBase64 = pubKey,
            lastSeenMs = Long.MAX_VALUE,
        )
        _manualPeers.value = _manualPeers.value.filterNot { it.deviceId == peer.deviceId } + peer
    }

    fun removeManualPeer(deviceId: String) {
        _manualPeers.value = _manualPeers.value.filterNot { it.deviceId == deviceId }
    }

    fun start() {
        transfer.start()
        discovery.start()
    }

    fun stop() {
        discovery.stop()
        transfer.stop()
    }
}
