package com.safedrop.android.net

import android.content.Context
import android.net.wifi.WifiManager
import android.util.Log
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.NetworkInterface
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

private const val TAG = "SafeDrop/Discovery"

const val DISCOVERY_PORT = 47890
const val BROADCAST_INTERVAL_MS = 3_000L
const val PEER_TTL_MS = 10_000L

data class Peer(
    val deviceId: String,
    val name: String,
    val platform: String,
    val ip: String,
    val tcpPort: Int,
    val pubKeyBase64: String,
    val lastSeenMs: Long,
    val capabilities: List<String> = emptyList(),
) {
    fun hasCapability(cap: String): Boolean = cap in capabilities
}

/**
 * UDP-broadcast discovery for SafeDrop. Listens on DISCOVERY_PORT and
 * broadcasts HELLO to 255.255.255.255 every BROADCAST_INTERVAL_MS while
 * running. Peers that we haven't heard from in PEER_TTL_MS are dropped.
 */
class Discovery(
    private val context: Context,
    private val deviceId: String,
    private val deviceName: String,
    private val platformName: String,
    private val tcpPort: Int,
    private val publicKeyBase64: String,
    private val version: String,
    private val capabilities: List<String> = listOf("safedrop.transfer", "safedrop.tools"),
) {
    private val _peers = MutableStateFlow<Map<String, Peer>>(emptyMap())
    val peers: StateFlow<Map<String, Peer>> = _peers

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var jobs: List<Job> = emptyList()

    private var sendSocket: DatagramSocket? = null
    private var recvSocket: DatagramSocket? = null
    private var multicastLock: WifiManager.MulticastLock? = null

    var localIp: String = "0.0.0.0"
        private set

    fun start() {
        try {
            val wifi = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            multicastLock = wifi.createMulticastLock("safedrop-discovery").apply {
                setReferenceCounted(false)
                acquire()
            }
        } catch (e: Exception) {
            Log.w(TAG, "MulticastLock unavailable: ${e.message}")
        }

        sendSocket = DatagramSocket().apply { broadcast = true }
        recvSocket = DatagramSocket(null).apply {
            reuseAddress = true
            broadcast = true
            bind(InetSocketAddress(DISCOVERY_PORT))
            soTimeout = 1000
        }

        localIp = detectLocalIp() ?: "0.0.0.0"

        jobs = listOf(
            scope.launch { broadcastLoop() },
            scope.launch { listenLoop() },
            scope.launch { reaperLoop() },
        )
    }

    fun stop() {
        try { sendBye() } catch (_: Exception) {}
        jobs.forEach { it.cancel() }
        try { sendSocket?.close() } catch (_: Exception) {}
        try { recvSocket?.close() } catch (_: Exception) {}
        try { multicastLock?.release() } catch (_: Exception) {}
        scope.cancel()
    }

    private fun helloPayload(): ByteArray {
        return JSONObject().apply {
            put("type", "HELLO")
            put("device_id", deviceId)
            put("name", deviceName)
            put("platform", platformName)
            put("tcp_port", tcpPort)
            put("pubkey", publicKeyBase64)
            put("version", version)
            val caps = org.json.JSONArray()
            for (c in capabilities) caps.put(c)
            put("capabilities", caps)
        }.toString().toByteArray(Charsets.UTF_8)
    }

    private fun byePayload(): ByteArray {
        return JSONObject().apply {
            put("type", "BYE")
            put("device_id", deviceId)
        }.toString().toByteArray(Charsets.UTF_8)
    }

    private suspend fun broadcastLoop() {
        val payload = helloPayload()
        while (scope.isActive) {
            broadcastDatagram(payload)
            delay(BROADCAST_INTERVAL_MS)
        }
    }

    private fun sendBye() {
        broadcastDatagram(byePayload())
    }

    private fun broadcastDatagram(payload: ByteArray) {
        val socket = sendSocket ?: return
        // Send to global broadcast and to every per-interface broadcast we can
        // find — Android Wi-Fi sometimes only delivers if we use the subnet
        // broadcast (e.g. 192.168.1.255).
        val targets = mutableListOf<InetAddress>()
        targets += InetAddress.getByName("255.255.255.255")
        try {
            for (nif in NetworkInterface.getNetworkInterfaces().toList()) {
                if (!nif.isUp || nif.isLoopback) continue
                for (addr in nif.interfaceAddresses) {
                    addr.broadcast?.let { targets += it }
                }
            }
        } catch (_: Exception) {}
        for (target in targets.distinctBy { it.hostAddress }) {
            try {
                socket.send(DatagramPacket(payload, payload.size, target, DISCOVERY_PORT))
            } catch (_: Exception) {}
        }
    }

    private suspend fun listenLoop() {
        val recv = recvSocket ?: return
        val buf = ByteArray(8192)
        val packet = DatagramPacket(buf, buf.size)
        while (scope.isActive) {
            try {
                withContext(Dispatchers.IO) { recv.receive(packet) }
            } catch (_: java.net.SocketTimeoutException) {
                continue
            } catch (e: Exception) {
                if (!scope.isActive) break
                Log.w(TAG, "recv error: ${e.message}")
                continue
            }
            val data = packet.data.copyOf(packet.length)
            val ip = packet.address.hostAddress ?: continue
            handle(data, ip)
        }
    }

    private fun handle(bytes: ByteArray, senderIp: String) {
        val msg = try {
            JSONObject(bytes.toString(Charsets.UTF_8))
        } catch (_: Exception) { return }
        val kind = msg.optString("type")
        val id = msg.optString("device_id")
        if (id.isEmpty() || id == deviceId) return

        val current = _peers.value.toMutableMap()
        when (kind) {
            "HELLO" -> {
                val port = msg.optInt("tcp_port", 0)
                val pub = msg.optString("pubkey")
                if (port <= 0 || pub.isEmpty()) return
                val capsArr = msg.optJSONArray("capabilities")
                val caps = buildList {
                    if (capsArr != null) {
                        for (i in 0 until capsArr.length()) add(capsArr.optString(i))
                    }
                }
                val existing = current[id]
                val peer = Peer(
                    deviceId = id,
                    name = msg.optString("name", "unknown"),
                    platform = msg.optString("platform", "?"),
                    ip = senderIp,
                    tcpPort = port,
                    pubKeyBase64 = pub,
                    lastSeenMs = System.currentTimeMillis(),
                    capabilities = caps,
                )
                if (existing == null || existing != peer.copy(lastSeenMs = existing.lastSeenMs)) {
                    current[id] = peer
                    _peers.value = current
                } else {
                    current[id] = peer.copy(lastSeenMs = peer.lastSeenMs)
                    _peers.value = current
                }
            }
            "BYE" -> {
                if (current.remove(id) != null) {
                    _peers.value = current
                }
            }
        }
    }

    private suspend fun reaperLoop() {
        while (scope.isActive) {
            delay(1000)
            val cutoff = System.currentTimeMillis() - PEER_TTL_MS
            val current = _peers.value
            val survivors = current.filterValues { it.lastSeenMs >= cutoff }
            if (survivors.size != current.size) {
                _peers.value = survivors
            }
        }
    }

    private fun detectLocalIp(): String? {
        return try {
            for (nif in NetworkInterface.getNetworkInterfaces().toList()) {
                if (!nif.isUp || nif.isLoopback) continue
                for (addr in nif.inetAddresses) {
                    if (!addr.isLoopbackAddress && addr.hostAddress?.contains(':') == false) {
                        return addr.hostAddress
                    }
                }
            }
            null
        } catch (e: Exception) {
            null
        }
    }
}
